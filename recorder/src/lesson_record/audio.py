"""Audio sources. A source yields raw mono 16 kHz s16le PCM; the daemon encodes it.

PulseSource  : ffmpeg -f pulse (works with PipeWire's pulse server). mic only, or mic + system audio (default sink's monitor)
               mixed. It FOLLOWS the default sink/source: when they change (headphones, HDMI, Bluetooth) the capture is
               restarted on the new devices (gap < ~1 s), see check_change().
FakeSource   : generated tone / silence, for tests (LESSON_RECORD_FAKE_SOURCE).
"""
import logging
import math
import os
import select
import signal
import subprocess
import threading
import time

import numpy as np

from .common import RATE, with_parent_death

log = logging.getLogger("lesson_record.audio")
CHUNK = RATE * 2 // 4          # 250 ms


class SourceError(Exception):
    """Cannot start capturing (no mic, pulse unreachable, ...). Message is user-facing."""


class SourceEnded(Exception):
    """The capture process ended / produced EOF."""


def pactl(*args, timeout=5):
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SourceError(f"pactl {' '.join(args)} failed: {r.stderr.strip() or r.returncode}")
    return r.stdout.strip()


def levels(pcm: bytes):
    """(rms_dbfs, peak_dbfs) of s16le PCM; -120 for digital silence."""
    if len(pcm) < 2:
        return -120.0, -120.0
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a * a)))
    f = lambda v: -120.0 if v <= 1e-6 else max(-120.0, 20 * math.log10(v))
    return f(rms), f(peak)


class Source:
    description = {}

    def start(self): ...
    def read(self, timeout: float):
        """bytes, or None on timeout; raises SourceEnded."""
    def stop(self): ...
    def check_change(self):
        """A reason string if the capture should be restarted on other devices, else None."""
        return None


# ---------------------------------------------------------------------------------------------------
class PulseSource(Source):
    def __init__(self, mode: str, mic_override: str = "", log_path=None, poll_s: float = 2.0):
        assert mode in ("mic", "meeting")
        self.mode, self.mic_override, self.log_path, self.poll_s = mode, mic_override, log_path, poll_s
        self.proc = None
        self.mic = self.monitor = None
        self.description = {}
        self._dirty = threading.Event()
        self._sub = None
        self._sub_thread = None
        self._last_poll = 0.0

    # -- device resolution ------------------------------------------------------------------------
    def resolve(self):
        if self.mic_override:
            known = {line.split("\t")[1] for line in pactl("list", "short", "sources").splitlines() if "\t" in line}
            if self.mic_override not in known:      # pulse silently falls back to the default source for unknown names
                raise SourceError(f"microphone '{self.mic_override}' not found (see: pactl list short sources)")
        mic = self.mic_override or pactl("get-default-source")
        if not mic or mic.startswith("@") or (mic.endswith(".monitor") and not self.mic_override):
            mic = None                       # default source is a monitor (or nothing): there is no real microphone
        monitor = None
        if self.mode == "meeting":
            sink = pactl("get-default-sink")
            if not sink or sink.startswith("@"):
                raise SourceError("meeting mode: no default audio output (sink) found")
            monitor = sink + ".monitor"
        if self.mode == "mic" and mic is None:
            raise SourceError("no microphone: the default input is missing or is a monitor of an output")
        if self.mode == "meeting" and mic is None:
            log.warning("no microphone found; recording system audio only")
        return mic, monitor

    def build_cmd(self, mic, monitor):
        c = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
        n = 0
        for dev in (mic, monitor):
            if dev:
                c += ["-thread_queue_size", "1024", "-f", "pulse", "-i", dev]
                n += 1
        mono = "aresample={r},aformat=channel_layouts=mono".format(r=RATE)
        if n == 2:
            # both sides to mono 16 kHz, summed (normalize=0 keeps speech level), limiter against clipping
            c += ["-filter_complex",
                  f"[0:a]{mono}[m];[1:a]{mono}[s];[m][s]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[o]",
                  "-map", "[o]"]
        else:
            c += ["-af", mono]
        return c + ["-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"]

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self):
        self.mic, self.monitor = self.resolve()
        cmd = self.build_cmd(self.mic, self.monitor)
        err = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(with_parent_death(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, bufsize=0)
        except FileNotFoundError:
            raise SourceError("ffmpeg is not installed (pacman -S ffmpeg)")
        finally:
            if self.log_path:
                err.close()
        self.description = {"mic": self.mic, "monitor": self.monitor}
        self._dirty.clear()
        self._last_poll = time.monotonic()
        self._start_subscribe()

    def _start_subscribe(self):
        if self._sub and self._sub.poll() is None:
            return
        try:
            self._sub = subprocess.Popen(with_parent_death(["pactl", "subscribe"]), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self._sub = None
            return

        def pump(proc):
            for line in proc.stdout:
                # "Event 'change' on server #n" is what a default-sink/source change looks like; sinks coming and going too
                if " on server" in line or " on sink " in line or " on source " in line:
                    self._dirty.set()
        self._sub_thread = threading.Thread(target=pump, args=(self._sub,), daemon=True)
        self._sub_thread.start()

    def read(self, timeout):
        if self.proc is None:
            raise SourceEnded("not started")
        fd = self.proc.stdout.fileno()
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            if self.proc.poll() is not None:
                raise SourceEnded(f"capture process exited with {self.proc.returncode}")
            return None
        data = os.read(fd, CHUNK)
        if not data:
            self.proc.wait(timeout=2)
            raise SourceEnded(f"capture process exited with {self.proc.returncode}")
        return data

    def stop(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if p is not None and p.stdout:
            p.stdout.close()

    def close(self):
        self.stop()
        s, self._sub = self._sub, None
        if s is not None:
            s.terminate()
            try:
                s.wait(timeout=2)
            except subprocess.TimeoutExpired:
                s.kill()

    def check_change(self):
        now = time.monotonic()
        if not self._dirty.is_set() and now - self._last_poll < self.poll_s:
            return None
        self._dirty.clear()
        self._last_poll = now
        try:
            mic, monitor = self.resolve()
        except SourceError:
            return None
        if monitor != self.monitor or (not self.mic_override and mic != self.mic):
            return f"default device changed: mic {self.mic} -> {mic}, monitor {self.monitor} -> {monitor}"
        return None

    def stderr_tail(self, n=400):
        try:
            return open(self.log_path, "rb").read()[-n:].decode("utf-8", "replace").strip()
        except (OSError, TypeError):
            return ""


# ---------------------------------------------------------------------------------------------------
class FakeSource(Source):
    """spec: 'tone[:Hz]' | 'silence' | 'none' (EOF immediately) | 'flaky' (dies once after ~1 s). Realtime paced unless speed>1."""

    def __init__(self, spec="tone:440", speed=1.0):
        kind, _, arg = spec.partition(":")
        self.kind, self.freq, self.speed = kind, float(arg or 440), speed
        self.t0 = None
        self.sent = 0
        self.description = {"mic": f"fake:{spec}", "monitor": None}
        self.generation = 0
        self._died = False

    def start(self):
        self.t0 = time.monotonic()
        self.sent = 0
        self.generation += 1

    def read(self, timeout):
        if self.kind == "none":
            raise SourceEnded("fake source: no audio")
        if self.kind == "flaky" and not self._died and self.sent >= RATE * 2:
            self._died = True
            raise SourceEnded("fake source: died")
        due = self.t0 + self.sent / (RATE * 2) / self.speed + 0.25 / self.speed
        wait = due - time.monotonic()
        if wait > timeout:
            time.sleep(timeout)
            return None
        if wait > 0:
            time.sleep(wait)
        n = CHUNK // 2
        if self.kind in ("silence",):
            pcm = np.zeros(n, dtype="<i2")
        else:
            t = (self.sent // 2 + np.arange(n)) / RATE
            pcm = (9000 * np.sin(2 * math.pi * self.freq * t)).astype("<i2")
        self.sent += CHUNK
        return pcm.tobytes()

    def stop(self):
        pass

    def close(self):
        pass


def make_source(mode, rcfg, log_path=None):
    spec = os.environ.get("LESSON_RECORD_FAKE_SOURCE")
    if spec:
        return FakeSource(spec, float(os.environ.get("LESSON_RECORD_FAKE_SPEED", "1")))
    return PulseSource(mode, os.environ.get("LESSON_RECORD_MIC") or rcfg.mic_source, log_path)
