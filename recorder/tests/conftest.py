import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

WORKER_TESTS = Path(__file__).resolve().parents[2] / "worker" / "tests"      # reuse the worker's fake GitHub + helpers
sys.path.insert(0, str(WORKER_TESTS))
import fake_github as fg                                                      # noqa: E402

NOTEBOOK_ID = "01K8F3Z6N7Q2W9XJ4M5R8TBVCA"
NOTEBOOK = {"schema_version": 1, "id": NOTEBOOK_ID, "name": "Biologie 2.A", "created_at": "2026-09-01T07:30:00Z",
            "updated_at": "2026-09-01T07:30:00Z", "archived": False, "color": "#2e7d32", "sort": 10}
SETTINGS = {"schema_version": 1, "updated_at": "2026-10-20T12:30:00Z", "default_language": "cs", "failed_audio_retention_days": 14,
            "part_target_bytes": 20971520, "inbox_warn_bytes": 524288000, "inbox_block_bytes": 943718400,
            "diarize_default": {"lesson": False, "meeting": True}}


def jb(d):
    return (json.dumps(d, indent=2) + "\n").encode()


@pytest.fixture(scope="session")
def server():
    srv = fg.QuietServer(("127.0.0.1", 0), fg.Api)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def gh(server):
    fg.ST.reset()
    fg.ST.commit_files("main", {f"notebooks/{NOTEBOOK_ID}.json": jb(NOTEBOOK), "settings/app.json": jb(SETTINGS)}, "seed")
    fg.ST.commit_files("inbox", {".keep": b"inbox"}, "init", parent_ok=False)
    fg.TOKEN_OVERRIDE = None
    return fg.ST


@pytest.fixture
def home(tmp_path, monkeypatch, server, gh):
    """Isolated HOME with a worker.toml pointing at the fake GitHub. Subprocesses inherit this environment."""
    h = tmp_path / "home"
    (h / ".config/lesson-notes").mkdir(parents=True)
    tok = h / ".config/lesson-notes/token"
    tok.write_text(fg.TOKEN + "\n")
    tok.chmod(0o600)
    (h / ".config/lesson-notes/worker.toml").write_text(
        f'repo = "{fg.REPO}"\napi_base = "{server}"\ntoken_file = "{tok}"\n')
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.delenv("LESSON_RECORD_MIC", raising=False)
    monkeypatch.delenv("LESSON_WORKER_CONFIG", raising=False)
    monkeypatch.delenv("LESSON_RECORD_CONFIG", raising=False)
    yield h
    # make sure no recorder survives a failing test
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                cmd = (p / "cmdline").read_bytes()
            except OSError:
                continue
            if b"lesson_record.recorder" in cmd and str(h).encode() in (p / "environ").read_bytes() if (p / "environ").exists() else False:
                try:
                    os.kill(int(p.name), 9)
                except OSError:
                    pass


@pytest.fixture
def fake_tone(monkeypatch):
    monkeypatch.setenv("LESSON_RECORD_FAKE_SOURCE", "tone:440")


class Ctx:
    """Handy access to config objects of the isolated HOME."""

    def __init__(self):
        from lesson_record.common import Paths, load_recorder_config, load_worker_config
        self.wcfg = load_worker_config()
        self.rcfg = load_recorder_config()
        self.paths = Paths(self.wcfg)
        self.paths.ensure()

    def args(self):
        return self.wcfg, self.rcfg, self.paths


@pytest.fixture
def ctx(home):
    return Ctx()


def run_cli(*args, check=True, timeout=120):
    r = subprocess.run([sys.executable, "-m", "lesson_record.cli", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"lesson-record {' '.join(args)} -> {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
    return r


def wait_until(pred, timeout=10, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timeout waiting for condition")


@pytest.fixture(scope="session")
def pw():
    import audiosandbox
    if not audiosandbox.available():
        pytest.skip("pipewire/wireplumber/pipewire-pulse/pactl/ffmpeg not installed")
    sb = audiosandbox.Sandbox()
    try:
        sb.start()
    except Exception:
        sb.stop()
        raise
    yield sb
    sb.stop()


@pytest.fixture
def pwenv(pw, monkeypatch, home):
    """Point the recorder (and its ffmpeg/pactl children) at the private audio server; restore sinks afterwards."""
    for k in ("XDG_RUNTIME_DIR", "PIPEWIRE_RUNTIME_DIR", "PULSE_RUNTIME_PATH", "PULSE_SERVER"):
        monkeypatch.setenv(k, pw.env[k])
    monkeypatch.delenv("LESSON_RECORD_FAKE_SOURCE", raising=False)
    pw.ensure_sinks()
    pw.set_default_sink("lr_out_a")
    yield pw
    for p in pw.players:
        if p.poll() is None:
            p.terminate()
