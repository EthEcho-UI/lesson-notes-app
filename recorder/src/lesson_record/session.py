"""Control side: start / stop / pause / resume / status / recover. Used by the CLI and the UI.
The recording itself lives in a separate detached process (recorder.py); this module only talks to it via the lock file,
the state file and signals."""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import finalize as fin
from . import photos as photomod
from .common import LANGUAGE_MODES, Paths, now_iso, read_json, ulid, write_json
from .recorder import acquire_lock, lock_is_held
from .remote import Remote, RemoteError
from .submit import process_outbox

ACTIVE_STATES = ("starting", "recording", "paused", "stopping", "orphaned")


class SessionError(Exception):
    pass


def read_state(paths):
    return read_json(paths.state_file, {"state": "idle"}) or {"state": "idle"}


def is_orphan(paths, st=None):
    st = st or read_state(paths)
    return st.get("state") in ACTIVE_STATES and not lock_is_held(paths)


def _pid(paths):
    pid = read_state(paths).get("pid")
    return pid if isinstance(pid, int) and lock_is_held(paths) else None


# ---------------------------------------------------------------------------------------------------
def resolve_inputs(remote, rcfg, existing, notebook_name, *, language_mode=None, topic=None, details=None):
    """Language mode + context of a recording -> (language_mode, context, overrides).
    New note: given values or defaults (subject = notebook name), overrides = {}. Existing note: its own values; `overrides` holds only
    what was given AND differs, and is applied on top of the live note.json at hand-off."""
    if language_mode is not None and language_mode not in LANGUAGE_MODES:
        raise SessionError(f"language mode must be one of: {', '.join(LANGUAGE_MODES)}")
    topic = None if topic is None else topic.strip()
    details = None if details is None else details.strip()
    if topic is not None and len(topic) > 300:
        raise SessionError("topic is longer than 300 characters")
    if details is not None and len(details) > 2000:
        raise SessionError("details are longer than 2000 characters")
    base_ctx = (existing or {}).get("context") or {"subject": notebook_name[:200] or "?", "topic": "", "details": ""}
    base_mode = (existing or {}).get("language_mode") or remote.default_language_mode(rcfg)
    mode = language_mode or base_mode
    ctx = {"subject": base_ctx["subject"], "topic": base_ctx["topic"] if topic is None else topic,
           "details": base_ctx["details"] if details is None else details}
    overrides = {}
    if existing is not None:
        if mode != base_mode:
            overrides["language_mode"] = mode
        for k in ("topic", "details"):
            if ctx[k] != base_ctx[k]:
                overrides[k] = ctx[k]
    return mode, ctx, overrides


def start(wcfg, rcfg, paths, *, kind="lesson", mode=None, notebook=None, title=None, note=None, language=None, diarize=None,
          language_mode=None, topic=None, details=None, images=(), wait=12.0, remote=None):
    paths.ensure()
    wait = float(os.environ.get("LESSON_RECORD_START_TIMEOUT", wait))
    if kind not in ("lesson", "meeting"):
        raise SessionError("kind must be lesson or meeting")
    if mode not in (None, "mic", "meeting"):
        raise SessionError("mode must be mic or meeting")
    if language in LANGUAGE_MODES and not language_mode:              # old `--language cs|en` spelling
        language_mode = language
    prepared = []
    for i, src in enumerate(images or (), 1):                          # fail before anything starts
        try:
            prepared.append((src, *photomod.prepare_file(src)))
        except photomod.PhotoError as e:
            raise SessionError(str(e))
    orphan = recover(wcfg, rcfg, paths, submit=False) if is_orphan(paths) else []
    if lock_is_held(paths):
        st = read_state(paths)
        raise SessionError(f"already recording ('{st.get('title')}', {st.get('elapsed', 0):.0f} s); stop it first")
    if not os.environ.get("LESSON_RECORD_FAKE_SOURCE"):
        for tool in ("ffmpeg", "ffprobe", "pactl"):
            if not shutil.which(tool):
                raise SessionError(f"{tool} is not installed (pacman -S ffmpeg libpulse)")
    free = shutil.disk_usage(paths.data).free
    if free < 300 * 1024 * 1024:
        raise SessionError(f"only {free // 2**20} MiB free on the disk of {paths.data}; free some space first")
    remote = remote or Remote(wcfg, paths)
    n = None
    if note:
        n = remote.find_note(note)
        if n is None:                                  # recorded here, not announced yet (offline)
            e = next((e for e in fin.outbox_entries(paths) if e["note"]["id"] == note), None)
            if e:
                n = {**e["note"], "notebook_id": e["note"]["notebook_id"], "parts": e["parts"], "images": e.get("images", [])}
        if n is None:
            raise SessionError(f"unknown note {note}; see: lesson-record list-notes")
        kind, notebook_id, title = n["kind"], n["notebook_id"], n["title"]
        nb = next((x for x in remote.cached().get("notebooks", []) if x["id"] == notebook_id), None)
        notebook_name = nb["name"] if nb else "?"
    else:
        if not notebook:
            default_nb = (remote.cached().get("settings") or {}).get("default_notebook_id")
            if not default_nb:
                raise SessionError("--notebook is required for a new note")
            notebook = default_nb
        try:
            nbk = remote.resolve_notebook(notebook)
            notebook_id, notebook_name = nbk["id"], nbk["name"]
        except RemoteError as e:
            raise SessionError(str(e))
        title = (title or "").strip() or f"{'Lesson' if kind == 'lesson' else 'Meeting'} {time.strftime('%Y-%m-%d %H:%M')}"
    lmode, context, overrides = resolve_inputs(remote, rcfg, n, notebook_name, language_mode=language_mode, topic=topic, details=details)
    have_images = [i for i in ((n or {}).get("images") or [])]
    if len(have_images) + len(prepared) > photomod.MAX_IMAGES:
        raise SessionError(f"a note can have at most {photomod.MAX_IMAGES} photos ({len(have_images)} already attached)")
    if diarize is None:
        diarize = remote.diarize_default(kind)
    mode = mode or ("meeting" if kind == "meeting" else "mic")
    rec_id = ulid()
    rec_dir = paths.active / rec_id
    rec_dir.mkdir(parents=True)
    meta = {"rec_id": rec_id, "job_id": ulid(), "note_id": note or ulid(), "new_note": not note, "kind": kind, "mode": mode,
            "notebook_id": notebook_id, "title": title[:200], "language_mode": lmode, "context": context, "overrides": overrides,
            "diarize": bool(diarize), "started_at": now_iso(), "photos": []}
    if prepared:
        (rec_dir / "photos").mkdir()
        for k, (src, data, w, h) in enumerate(prepared, 1):
            (rec_dir / "photos" / f"{k:02d}.jpg").write_bytes(data)
            meta["photos"].append({"src": f"photos/{k:02d}.jpg", "width": w, "height": h})
    write_json(rec_dir / "meta.json", meta)
    logf = open(rec_dir / "daemon.log", "ab")
    cmd = [sys.executable, "-m", "lesson_record.recorder", str(rec_dir)]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True, close_fds=True)
    logf.close()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        st = read_state(paths)
        if st.get("rec_id") == rec_id and st.get("state") in ("recording", "paused"):
            st["recovered_orphans"] = orphan
            return st
        if proc.poll() is not None:
            last = (read_state(paths).get("last") or {})
            msg = last.get("message") if last.get("rec_id") == rec_id else None
            if not msg:
                tail = (rec_dir / "daemon.log").read_text(errors="replace")[-300:] if (rec_dir / "daemon.log").exists() else ""
                msg = f"the recorder exited with code {proc.returncode}: {tail.strip()}"
            raise SessionError(f"could not start recording: {msg}")
        time.sleep(0.1)
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    cap = ""
    try:
        cap = (rec_dir / "capture.log").read_text(errors="replace")[-300:]
    except OSError:
        pass
    raise SessionError("no audio arrived within %d s; recording cancelled. %s" % (wait, cap.strip()))


def _wait_for(paths, pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = read_state(paths)
        if pred(st):
            return st
        time.sleep(0.1)
    return None


def stop(wcfg, rcfg, paths, wait=90.0):
    st = read_state(paths)
    if is_orphan(paths, st):
        return {"orphans": recover(wcfg, rcfg, paths)}
    pid = _pid(paths)
    if pid is None:
        raise SessionError("not recording")
    os.kill(pid, signal.SIGTERM)
    done = _wait_for(paths, lambda s: s.get("state") not in ACTIVE_STATES and not lock_is_held(paths), wait)
    if done is None:
        raise SessionError("the recorder is still finishing (uploading?); it keeps running in the background, check: lesson-record status")
    return done.get("last") or {}


def pause(paths):
    pid = _pid(paths)
    if pid is None:
        raise SessionError("not recording")
    os.kill(pid, signal.SIGUSR1)
    if _wait_for(paths, lambda s: s.get("state") == "paused", 5) is None:
        raise SessionError("the recorder did not pause")
    return read_state(paths)


def resume(paths):
    pid = _pid(paths)
    if pid is None:
        raise SessionError("not recording")
    os.kill(pid, signal.SIGUSR2)
    if _wait_for(paths, lambda s: s.get("state") == "recording", 8) is None:
        raise SessionError("the recorder did not resume")
    return read_state(paths)


# ---------------------------------------------------------------------------------------------------
def _kill_leftovers(rec_dir):
    """ffmpeg children of a killed daemon that are still writing into rec_dir."""
    me = os.getpid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == me:
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if str(rec_dir) in cmd and "ffmpeg" in cmd:
            try:
                os.kill(int(p.name), signal.SIGINT)
                time.sleep(1.0)
                os.kill(int(p.name), signal.SIGKILL)
            except ProcessLookupError:
                pass


def recover(wcfg, rcfg, paths, submit=True):
    """Turn recordings that were not finalized (crash, kill -9, power loss) into parts. Returns a list of result dicts."""
    paths.ensure()
    fd = acquire_lock(paths)
    if fd is None:
        return []                                         # a live recorder holds the lock: nothing is orphaned
    results = []
    try:
        remote = Remote(wcfg, paths, timeout=8, retries=1)
        for rec_dir in sorted(d for d in paths.active.iterdir() if d.is_dir()):
            meta = read_json(rec_dir / "meta.json")
            if not meta:
                continue
            _kill_leftovers(rec_dir)
            try:
                rn = None if meta["new_note"] else remote.find_note(meta["note_id"], refresh=False)
                entry = fin.finalize(paths, rec_dir, meta, rn)
                r = {"ok": True, "rec_id": meta["rec_id"], "note_id": meta["note_id"], "recovered": True,
                     "parts": [{k: p[k] for k in ("id", "bytes", "duration", "sha256")} for p in entry["parts"]],
                     "seconds": round(sum(p["duration"] for p in entry["parts"]), 1), "message": "recovered an interrupted recording",
                     "finished_at": now_iso()}
            except fin.NoAudio as e:
                r = {"ok": False, "rec_id": meta["rec_id"], "code": e.code, "message": e.message, "recovered": True, "finished_at": now_iso()}
            results.append(r)
        write_json(paths.state_file, {"state": "idle", "updated_at": now_iso(), "last": results[-1] if results else (read_state(paths).get("last"))})
    finally:
        os.close(fd)
    if submit and results:
        try:
            process_outbox(Remote(wcfg, paths, timeout=8, retries=1), paths)
        except Exception:
            pass
    return results


def status(wcfg, rcfg, paths, auto_recover=True):
    """Current state + pending outbox; detects and (by default) recovers an orphaned recording."""
    paths.ensure()
    recovered = []
    if auto_recover and is_orphan(paths):
        recovered = recover(wcfg, rcfg, paths)
    st = read_state(paths)
    st["alive"] = lock_is_held(paths)
    st["orphan"] = is_orphan(paths, st)
    st["recovered"] = recovered
    st["outbox"] = [{"rec_id": e["rec_id"], "state": e["state"], "note_id": e["note"]["id"], "title": e["note"]["title"],
                     "seconds": round(sum(p["duration"] for p in e["parts"]), 1), "last_error": e.get("last_error")}
                    for e in fin.outbox_entries(paths) if e["state"] != "submitted"]
    return st
