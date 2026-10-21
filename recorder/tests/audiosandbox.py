"""A private PipeWire + WirePlumber + pipewire-pulse stack in its own runtime dir, with NO hardware devices.

Why: the real machine has a Bluetooth headset as default sink/source that someone may be using. The sandbox lets the tests
create virtual sinks, switch the default sink, unload sinks ("unplug headphones") and play tones without ever touching the real
audio server (a different socket) or its default devices. Only processes started here are ever signalled."""
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

BINARIES = ("pipewire", "wireplumber", "pipewire-pulse", "pactl", "ffmpeg", "ffprobe")


def available():
    return all(shutil.which(b) for b in BINARIES)


class Sandbox:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lrpw"))
        os.chmod(self.dir, 0o700)
        d = str(self.dir)
        self.env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER")}
        self.env.update(XDG_RUNTIME_DIR=d, PIPEWIRE_RUNTIME_DIR=d, PULSE_RUNTIME_PATH=f"{d}/pulse", PULSE_SERVER=f"unix:{d}/pulse/native",
                        XDG_STATE_HOME=f"{d}/state", XDG_CONFIG_HOME=f"{d}/cfg", XDG_CACHE_HOME=f"{d}/cache", XDG_DATA_HOME=f"{d}/data")
        self.procs = []
        self.players = []
        self.modules = []

    # -- life cycle -------------------------------------------------------------------------------
    def _spawn(self, cmd, name):
        log = open(self.dir / f"{name}.log", "wb")
        p = subprocess.Popen(cmd, env=self.env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        self.procs.append(p)
        return p

    def pactl(self, *a, check=True):
        r = subprocess.run(["pactl", *a], env=self.env, capture_output=True, text=True, timeout=10)
        if check and r.returncode != 0:
            raise RuntimeError(f"pactl {a}: {r.stderr}")
        return r.stdout.strip()

    def start(self):
        self._spawn(["pipewire"], "pipewire")
        self._wait(lambda: (self.dir / "pipewire-0").exists(), "pipewire socket")
        self._spawn(["wireplumber", "-p", "policy"], "wireplumber")       # policy only: no ALSA / Bluetooth / camera monitors
        time.sleep(0.8)
        self._spawn(["pipewire-pulse"], "pipewire-pulse")
        self._wait(lambda: subprocess.run(["pactl", "info"], env=self.env, capture_output=True).returncode == 0, "pulse server")
        self.ensure_sinks()
        self.pactl("load-module", "module-remap-source", "master=lr_mic_feed.monitor", "source_name=lr_mic")
        self.set_default_sink("lr_out_a")
        self.pactl("set-default-source", "lr_mic")
        self._wait(lambda: self.pactl("get-default-source") == "lr_mic", "default source")
        return self

    def ensure_sinks(self):
        have = self.pactl("list", "short", "sinks")
        for name in ("lr_out_a", "lr_out_b", "lr_mic_feed"):
            if name not in have:
                self.modules = [(n, m) for n, m in self.modules if n != name]
                self.modules.append((name, self.pactl("load-module", "module-null-sink", f"sink_name={name}")))

    def _wait(self, pred, what, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if pred():
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"audio sandbox: timeout waiting for {what}")

    def stop(self):
        for p in self.players:
            if p.poll() is None:
                p.terminate()
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in self.procs + self.players:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- scenario helpers -------------------------------------------------------------------------
    def set_default_sink(self, name):
        self.pactl("set-default-sink", name)
        self._wait(lambda: self.pactl("get-default-sink") == name, f"default sink {name}")

    def default_sink(self):
        return self.pactl("get-default-sink")

    def unload_sink(self, name):
        mid = dict(self.modules)[name]
        self.pactl("unload-module", mid)

    def play_tone(self, freq, seconds, sink):
        """Realtime tone into a sink (an application playing audio)."""
        p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
                              "-t", str(seconds), "-af", "volume=0.5", "-f", "pulse", "-device", sink, f"tone{freq}"],
                             env=self.env, stdin=subprocess.DEVNULL)
        self.players.append(p)
        return p


def decode(path):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768.0


def tone_power(x, freq, sr=16000):
    """Relative power of `freq` in x (Goertzel-like via a windowed single-bin DFT)."""
    if len(x) == 0:
        return 0.0
    n = np.arange(len(x))
    w = np.hanning(len(x))
    c = np.sum(x * w * np.exp(-2j * math.pi * freq * n / sr))
    return float(abs(c) / np.sum(w))


def dominant(x, candidates, sr=16000):
    """Which of the candidate frequencies is strongest in x, and its power."""
    pw = {f: tone_power(x, f, sr) for f in candidates}
    best = max(pw, key=pw.get)
    return best, pw[best], pw
