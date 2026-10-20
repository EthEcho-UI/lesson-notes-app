"""Local worker cache (never synced, never committed): verified audio, WAV, ASR and diarization results,
keyed by the part's sha256 so a retry with a NEW job_id (same audio) reuses finished work."""
import json
import os
import shutil
import time
from pathlib import Path


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root) / "parts"

    def part_dir(self, sha256: str) -> Path:
        d = self.root / sha256
        d.mkdir(parents=True, exist_ok=True)
        return d

    def read_json(self, sha256, name):
        p = self.root / sha256 / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_json(self, sha256, name, obj):
        d = self.part_dir(sha256)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(d / name)

    def drop(self, sha256):
        shutil.rmtree(self.root / sha256, ignore_errors=True)

    def purge_older_than(self, days, now=None):
        now = now or time.time()
        n = 0
        if not self.root.exists():
            return 0
        for d in self.root.iterdir():
            try:
                if now - d.stat().st_mtime > days * 86400:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                pass
        return n


class InstanceLock:
    """One worker per machine (flock on a file in the cache dir)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def acquire(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
