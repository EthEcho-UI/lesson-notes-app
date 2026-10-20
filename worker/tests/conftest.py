import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fake_github as fg                      # noqa: E402  (copy of the PWA's fake GitHub API, plain-http use here)
from helpers import FakeASR, FakeClock, write_fake_claude   # noqa: E402

from lesson_worker.config import Config        # noqa: E402
from lesson_worker.ghapi import GitHub         # noqa: E402
from lesson_worker.worker import Worker        # noqa: E402


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
    fg.ST.commit_files("inbox", {".keep": b"inbox"}, "init", parent_ok=False)
    fg.TOKEN_OVERRIDE = None
    return fg.ST


@pytest.fixture
def cfg(tmp_path, server, gh):
    tok = tmp_path / "token"
    tok.write_text(fg.TOKEN + "\n")
    tok.chmod(0o600)
    claude = write_fake_claude(tmp_path / "bin")
    c = Config(repo=fg.REPO, token_file=tok, api_base=server, worker_id="test-laptop",
               data_dir=tmp_path / "data", models_dir=tmp_path / "models", cache_dir=tmp_path / "cache",
               claude_path=str(claude), claude_timeout_s=60, claude_retry_wait_s=0.0, poll_interval_s=20, retry_backoff_s=(0.0, 0.0, 0.0))
    c.validate_paths()
    return c


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def asr():
    return FakeASR()


@pytest.fixture
def api(cfg, clock):
    return GitHub(cfg.repo, cfg.read_token(), cfg.api_base, sleep=clock.sleep)


@pytest.fixture
def make_worker(cfg, api, clock, asr):
    def _make(**kw):
        kw.setdefault("asr", asr)
        return Worker(cfg, GitHub(cfg.repo, cfg.read_token(), cfg.api_base, sleep=clock.sleep), clock=clock, **kw)
    return _make


@pytest.fixture
def worker(make_worker):
    return make_worker()
