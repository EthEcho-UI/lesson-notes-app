"""The recording daemon: `python -m lesson_record.recorder <active/rec_dir>`.

Started detached (own session, SIGHUP ignored) by `lesson-record start` / the UI, so closing a terminal or the UI never stops it.
It owns the lock file, reads PCM from a source, meters it, encodes it to Ogg Opus part files and publishes its state.
Signals: SIGTERM/SIGINT = stop and finalize, SIGUSR1 = pause, SIGUSR2 = resume."""
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import audio, finalize as fin
from .audio import SourceEnded, SourceError
from .common import BYTES_PER_SEC, Paths, load_recorder_config, load_worker_config, now_iso, read_json, write_json
from .encoder import Encoder, EncoderError
from .remote import Remote
from .submit import process_outbox

log = logging.getLogger("lesson_record.daemon")
LIVE_INTERVAL = 0.2
STATE_INTERVAL = 1.0
STALL_S = 5.0
MAX_FAILS = 6


def acquire_lock(paths):
    paths.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)       # not inheritable: ffmpeg children do not keep it alive
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def lock_is_held(paths) -> bool:
    if not paths.lock_file.exists():
        return False
    fd = os.open(paths.lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


class Daemon:
    def __init__(self, rec_dir, paths, wcfg, rcfg, meta):
        self.rec_dir, self.paths, self.wcfg, self.rcfg, self.meta = Path(rec_dir), paths, wcfg, rcfg, meta
        self.stop_req = self.pause_req = False
        self.paused = False
        self.state = "starting"
        self.samples = 0                     # recorded (excludes pauses)
        self.part_index = 0
        self.part_samples = 0
        self.part_limit = int(rcfg.part_minutes * 60 * 16000)
        self.encoder = None
        self.source = None
        self.warnings = {}
        self.events = []
        self.restarts = 0
        self.last_data = time.monotonic()
        self.last_loud = time.monotonic()
        self.got_audio = False
        self.t_start = time.time()
        self.level = (-120.0, -120.0)
        self.error = None
        self._last_state = self._last_live = 0.0

    # ---- publishing -------------------------------------------------------------------------------------
    def event(self, msg):
        log.info(msg)
        self.events = (self.events + [{"t": now_iso(), "msg": msg}])[-12:]

    def publish(self, force=False, extra=None):
        now = time.monotonic()
        if force or now - self._last_state >= STATE_INTERVAL:
            self._last_state = now
            m = self.meta
            st = {"state": self.state, "rec_id": m["rec_id"], "pid": os.getpid(), "dir": str(self.rec_dir), "mode": m["mode"], "kind": m["kind"],
                  "title": m["title"], "note_id": m["note_id"], "new_note": m["new_note"], "started_at": m["started_at"],
                  "elapsed": round(self.samples / 16000, 1), "parts": self.part_index, "warnings": sorted(self.warnings.values()),
                  "events": self.events, "capture": getattr(self.source, "description", {}), "capture_restarts": self.restarts,
                  "updated_at": now_iso(), "last": (read_json(self.paths.state_file, {}) or {}).get("last")}
            if extra:
                st.update(extra)
            write_json(self.paths.state_file, st)
        if force or now - self._last_live >= LIVE_INTERVAL:
            self._last_live = now
            try:
                write_json(self.paths.live_file, {"state": self.state, "rec_id": self.meta["rec_id"], "rms_db": round(self.level[0], 1),
                                                  "peak_db": round(self.level[1], 1), "elapsed": round(self.samples / 16000, 1),
                                                  "t": time.time()})
            except OSError:
                pass

    # ---- encoder / parts --------------------------------------------------------------------------------
    def open_part(self):
        self.part_index += 1
        self.part_samples = 0
        self.encoder = Encoder(self.rec_dir / f"part_{self.part_index:03d}.opus", self.rcfg.bitrate)

    def close_part(self):
        if self.encoder is not None:
            self.encoder.close()
            self.encoder = None

    def feed(self, pcm):
        for attempt in (0, 1):
            try:
                if self.encoder is None:
                    self.open_part()
                self.encoder.write(pcm)
                break
            except EncoderError as e:
                if attempt:
                    raise
                self.event(f"encoder failed ({e}); starting a new part file")
                self.close_part()
        n = len(pcm) // 2
        self.samples += n
        self.part_samples += n
        if self.part_samples >= self.part_limit:
            self.event(f"part {self.part_index} reached {self.rcfg.part_minutes:g} min; starting the next part")
            self.close_part()

    # ---- capture restarts ---------------------------------------------------------------------------------
    def restart_source(self, why):
        self.event(f"restarting capture: {why}")
        self.restarts += 1
        self.source.stop()
        self.source.start()
        self.last_data = time.monotonic()

    # ---- main loop ---------------------------------------------------------------------------------------
    def run(self):
        os.chdir(self.rec_dir)
        self.source = audio.make_source(self.meta["mode"], self.rcfg, self.rec_dir / "capture.log")
        self.publish(force=True)
        try:
            self.source.start()
        except SourceError as e:
            self.error = ("capture_failed", str(e))
            return self.finish()
        fails = 0
        while not self.stop_req:
            if self.pause_req != self.paused:
                self.toggle_pause()
            if self.paused:
                time.sleep(0.1)
                self.publish()
                continue
            try:
                chunk = self.source.read(0.25)
            except SourceEnded as e:
                fails = fails + 1 if not self.got_audio or time.monotonic() - self.last_data < 10 else 1
                tail = getattr(self.source, "stderr_tail", lambda: "")()
                self.event(f"capture ended: {e}" + (f" [{tail[-160:]}]" if tail else ""))
                if fails >= MAX_FAILS:
                    self.error = ("capture_failed", f"the audio capture keeps failing: {e} {tail[-200:]}")
                    break
                time.sleep(min(1.0 * fails, 3.0))
                try:
                    self.restart_source("capture process died")
                except SourceError as e2:
                    self.event(f"cannot restart capture: {e2}")
                continue
            if chunk:
                fails = 0
                self.last_data = time.monotonic()
                if not self.got_audio:
                    self.got_audio = True
                    self.state = "recording"
                    self.publish(force=True)
                try:
                    self.feed(chunk)
                except EncoderError as e:
                    self.error = ("encoder_failed", f"cannot write audio to disk: {e}")
                    break
                self.level = audio.levels(chunk)
                if self.level[1] > self.rcfg.silent_db:
                    self.last_loud = time.monotonic()
                    self.warnings.pop("silent", None)
                elif time.monotonic() - self.last_loud > self.rcfg.silent_warn_s:
                    self.warnings["silent"] = (f"No sound for {int(time.monotonic() - self.last_loud)} s: "
                                               f"is the microphone muted or the wrong input selected?")
            elif time.monotonic() - self.last_data > STALL_S:
                try:
                    self.restart_source("no audio for 5 s")
                except SourceError as e:
                    self.event(f"cannot restart capture: {e}")
            reason = self.source.check_change()
            if reason:
                try:
                    self.restart_source(reason)
                except SourceError as e:
                    self.event(f"cannot follow device change: {e}")
            self.publish()
        return self.finish()

    def toggle_pause(self):
        self.paused = self.pause_req
        if self.paused:
            self.source.stop()                         # mic is released while paused
            self.state = "paused"
            self.event("paused")
        else:
            try:
                self.source.start()
            except SourceError as e:
                self.event(f"cannot resume capture: {e}")
                self.paused = True
                self.pause_req = True
                return
            self.last_data = self.last_loud = time.monotonic()
            self.state = "recording"
            self.event("resumed")
        self.publish(force=True)

    # ---- end ---------------------------------------------------------------------------------------------
    def finish(self):
        self.state = "stopping"
        self.publish(force=True)
        try:
            self.source.close()
        except Exception:
            log.exception("closing source")
        self.close_part()
        last = {"ok": False, "rec_id": self.meta["rec_id"], "note_id": self.meta["note_id"], "finished_at": now_iso()}
        try:
            remote = Remote(self.wcfg, self.paths, timeout=8, retries=1)
            rn = None if self.meta["new_note"] else remote.find_note(self.meta["note_id"], refresh=False)
            entry = fin.finalize(self.paths, self.rec_dir, self.meta, rn)
            last.update(ok=True, parts=[{k: p[k] for k in ("id", "bytes", "duration", "sha256")} for p in entry["parts"]],
                        seconds=round(sum(p["duration"] for p in entry["parts"]), 1), message="saved")
            if self.error:
                last["warning"] = self.error[1]
            try:
                res = process_outbox(remote, self.paths, only=entry["rec_id"])
                if res:
                    last["submit"] = {"status": res[0][1], "message": res[0][2]}
                    if res[0][1] == "offline":
                        last["message"] = "saved on the laptop; will be announced to the worker when online"
            except Exception as e:                    # never lose the recording because of the network side
                log.exception("submit")
                last["submit"] = {"status": "offline", "message": str(e)}
        except fin.NoAudio as e:
            last.update(ok=False, code=e.code, message=e.message)
        except Exception as e:
            log.exception("finalize")
            last.update(ok=False, code="finalize_failed", message=f"could not finalize ({e}); run: lesson-record recover")
            write_json(self.paths.state_file, {"state": "orphaned", "rec_id": self.meta["rec_id"], "dir": str(self.rec_dir), "last": last,
                                               "updated_at": now_iso()})
            return 1
        if self.error and not last["ok"]:
            last["message"] = f"{self.error[1]} ({last['message']})"
            last["code"] = self.error[0]
        write_json(self.paths.state_file, {"state": "idle", "updated_at": now_iso(), "last": last})
        try:
            self.paths.live_file.unlink()
        except OSError:
            pass
        return 0 if last["ok"] else 1


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    rec_dir = Path(argv[0])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    wcfg = load_worker_config()
    paths = Paths(wcfg)
    paths.ensure()
    meta = read_json(rec_dir / "meta.json")
    if not meta:
        print("meta.json missing", file=sys.stderr)
        return 2
    fd = acquire_lock(paths)
    if fd is None:
        print("another recording is running", file=sys.stderr)
        return 3
    d = Daemon(rec_dir, paths, wcfg, load_recorder_config(), meta)

    def on_term(*_):
        d.stop_req = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGUSR1, lambda *_: setattr(d, "pause_req", True))
    signal.signal(signal.SIGUSR2, lambda *_: setattr(d, "pause_req", False))
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
