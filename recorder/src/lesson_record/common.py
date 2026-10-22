"""Paths, small config, ULIDs, atomic JSON files. Everything resolves from $HOME so tests can isolate it."""
import json
import os
import secrets
import time
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from lesson_worker.config import Config as WorkerConfig, ConfigError, home, load as load_worker_config  # noqa: F401

RATE = 16000                 # capture/encode sample rate (mono, s16le)
BYTES_PER_SEC = RATE * 2
LANGUAGE_MODES = ("cs", "en", "mixed")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def with_parent_death(cmd):
    """Children of the daemon die with it (so a kill -9 leaves no capture process holding the microphone)."""
    import shutil
    return (["setpriv", "--pdeathsig", "term"] + cmd) if shutil.which("setpriv") else cmd


def ulid() -> str:
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- locations ---------------------------------------------------------------------------------
class Paths:
    def __init__(self, wcfg: WorkerConfig):
        self.state = home() / ".local/state/lesson-notes"
        self.data = wcfg.data_dir
        self.local_audio = wcfg.local_audio_dir                   # <note>/parts/NN.opus (what the worker reads)
        self.active = self.data / "recorder" / "active"            # one dir per recording in progress
        self.failed = self.data / "recorder" / "failed"            # recordings that produced no usable audio (kept, never deleted)
        self.outbox = self.state / "outbox"                        # finalized recordings waiting to be announced to GitHub
        self.state_file = self.state / "recorder.json"
        self.lock_file = self.state / "recorder.lock"
        self.cache_file = self.state / "remote-cache.json"
        self.ui_file = self.state / "ui.json"
        run = os.environ.get("XDG_RUNTIME_DIR")
        self.live_file = (Path(run) / "lesson-record-live.json") if run and os.access(run, os.W_OK) else self.state / "live.json"

    def ensure(self):
        for d in (self.state, self.active, self.failed, self.outbox, self.local_audio):
            d.mkdir(parents=True, exist_ok=True)
        for d in (self.state, self.data / "recorder", self.local_audio):     # recordings of other people: owner only
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


@dataclass
class RecorderConfig:
    """~/.config/lesson-notes/recorder.toml (all optional)."""
    part_minutes: float = 75.0        # a recording longer than this is split into several parts (contract: <= ~20 MiB per part)
    bitrate: str = "24k"
    mic_source: str = ""              # pulse source name; empty = follow the default source
    silent_warn_s: float = 10.0
    silent_db: float = -60.0          # peak below this for silent_warn_s -> warning
    language: str = "cs"              # v1 leftover, accepted and ignored (notes are always Czech; the audio language is language_mode)
    language_mode: str = "mixed"      # cs | en | mixed: default for new notes when neither the command line nor settings/app.json say


def load_recorder_config() -> RecorderConfig:
    p = Path(os.environ.get("LESSON_RECORD_CONFIG") or home() / ".config/lesson-notes/recorder.toml")
    cfg = RecorderConfig()
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        known = {f.name for f in fields(RecorderConfig)}
        for k, v in data.items():
            if k not in known:
                raise ConfigError(f"{p}: unknown setting '{k}'")
            setattr(cfg, k, v)
    if cfg.language_mode not in LANGUAGE_MODES:
        raise ConfigError(f"{p}: language_mode must be one of {', '.join(LANGUAGE_MODES)}")
    return cfg


# ---- JSON files --------------------------------------------------------------------------------
def write_json(path: Path, obj, mode=0o600):
    """Atomic: write temp file in the same dir, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
