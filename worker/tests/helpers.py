import hashlib
import io
import json
import math
import os
import random
import stat
import struct
import sys
import wave

import numpy as np
from pathlib import Path

import fake_github as fg
from lesson_worker.clock import Clock

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_rng = random.Random(1234)


def ulid(n=None):
    """Valid ULID; with n the 10-char time part encodes n, so ULIDs sort like n."""
    if n is None:
        head = "".join(_rng.choice(CROCKFORD) for _ in range(10))
    else:
        head = "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(9, -1, -1))
    return head + "".join(_rng.choice(CROCKFORD) for _ in range(16))


def wav_bytes(seconds, freq=440.0, sr=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        t = np.arange(n) / sr
        w.writeframes((8000 * np.sin(2 * math.pi * freq * t)).astype("<i2").tobytes())
    return buf.getvalue()


def iso(h=0, m=0, s=0, day=1):
    return f"2026-10-{day:02d}T{h:02d}:{m:02d}:{s:02d}Z"


def put_job(note_id=None, job_id=None, durations=(1.0,), kind="lesson", diarize=False, action="transcribe", append=False,
            created="2026-10-01T08:00:00Z", first_order=1, location="inbox", language="cs", with_note=True, num_speakers=None,
            audio=None, upload_job=True, local_dir=None):
    """Simulates the PWA upload into the fake GitHub: parts first, job.json last. Returns the job dict."""
    note_id, job_id = note_id or ulid(), job_id or ulid()
    parts, files = [], {}
    for i, d in enumerate(durations if action == 'transcribe' else ()):
        order = first_order + i
        data = audio[i] if audio else wav_bytes(d, 300 + 40 * order + i)
        rel = f"parts/{order:02d}.wav"
        parts.append({"id": f"p{order:02d}", "order": order, "file": rel, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "duration": d})
        if location == "inbox":
            files[f"jobs/{note_id}/{rel}"] = data
        else:
            local = Path(local_dir) / note_id / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
    job = {"schema_version": 1, "job_id": job_id, "note_id": note_id, "kind": kind, "created_at": created, "source": "pwa",
           "language": language, "action": action, "append": append or action == "regenerate_notes", "audio_location": location,
           "options": {"diarize": diarize, "num_speakers": num_speakers}, "parts": parts if action == "transcribe" else []}
    if files:
        fg.ST.commit_files("inbox", files, "parts")
    if upload_job:
        fg.ST.commit_files("inbox", {f"jobs/{note_id}/job.json": json.dumps(job, indent=2).encode()}, "job")
    return job


def main_json(path):
    d = fg.ST.files("main").get(path)
    return None if d is None else json.loads(fg.ST.blobs[d])


def inbox_files():
    f = fg.ST.files("inbox")
    return None if f is None else sorted(p for p in f if p != ".keep")


def done_order():
    """note ids in the order their `done` commits were made on main"""
    out, c = [], fg.ST.refs["main"]
    while c:
        m = fg.ST.commits[c]
        if m["message"].endswith(": done"):
            out.append(m["message"].split()[1].rstrip(":"))
        c = m["parents"][0] if m["parents"] else None
    return out[::-1]


class SimulatedCrash(BaseException):
    """Stands in for SIGKILL / power loss: not an Exception, so no cleanup or failure handling may run."""


class FakeASR:
    engine = {"name": "faster-whisper", "model": "large-v3-turbo", "version": "test"}

    def __init__(self):
        self.calls = []            # durations of audio transcribed
        self.hook = None           # hook(call_index, duration) may raise
        self.fail_times = 0

    def transcribe(self, audio, duration, language, progress=None):
        self.calls.append(round(duration, 2))
        if self.hook:
            self.hook(len(self.calls), duration)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated whisper failure")
        if progress:
            progress(50.0)
            progress(100.0)
        half = duration / 2
        return [{"start": 0.0, "end": round(half, 2), "text": f"Prvni veta delky {round(duration)}."},
                {"start": round(half, 2), "end": round(duration, 2), "text": "Druha veta."}]


class FakeClock(Clock):
    """Controllable time: sleep() advances both clocks; suspend(s) advances only the wall clock (like a real suspend)."""

    def __init__(self):
        self._mono = 1000.0
        self._wall = 1_790_000_000.0       # 2026-09-21
        self.on_sleep = None

    def mono(self):
        return self._mono

    def wall(self):
        return self._wall

    def now(self):
        import datetime as dt
        return dt.datetime.fromtimestamp(self._wall, dt.timezone.utc)

    def sleep(self, s):
        self._mono += s
        self._wall += s
        if self.on_sleep:
            self.on_sleep(self)

    def suspend(self, s):
        self._wall += s

    def advance(self, s):
        self._mono += s
        self._wall += s


FAKE_CLAUDE = r'''#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def rd(name, default=""):
    try: return open(os.path.join(here, name)).read().strip()
    except OSError: return default
mode = rd("mode", "ok")
if sys.argv[1:3] == ["auth", "status"]:
    ok = mode != "not_logged_in"
    print(json.dumps({"loggedIn": ok, "authMethod": "claude.ai", "email": "test@example.com"} if ok else {"loggedIn": False}))
    sys.exit(0 if ok else 1)
stdin = sys.stdin.read()
n = int(rd("count", "0")) + 1
open(os.path.join(here, "count"), "w").write(str(n))
open(os.path.join(here, "last_stdin.txt"), "w").write(stdin)
json.dump({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "cwd_entries": os.listdir(os.getcwd())},
          open(os.path.join(here, "last_call.json"), "w"))
if mode == "not_logged_in":      # what the real binary prints when logged out (verified): JSON with is_error
    print(json.dumps({"type": "result", "is_error": True, "terminal_reason": "api_error", "result": "Not logged in · Please run /login"})); sys.exit(0)
if mode == "not_logged_in_text":
    print("Not logged in · Please run /login"); sys.exit(1)
if mode == "is_error" or (mode == "error_once" and n == 1):
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error: rate limit reached"})); sys.exit(1)
if mode == "hang":
    import time; time.sleep(30)
if mode == "garbage":
    print("this is not json"); sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
meeting = "decisions" in schema["properties"]
content = {"title": "Testovaci poznamky", "summary": "Shrnuti probrane latky.", "key_points": ["bod 1", "bod 2"],
           "definitions": [{"term": "pojem", "definition": "vyznam"}], "todo": ["domaci ukol"], "questions_open": []}
if meeting:
    content.update(decisions=["rozhodnuti A"], action_items=[{"task": "udelat B", "owner": "Petra", "due": None}])
if mode == "bad_schema":
    content.pop("summary")
if mode == "extra_fields_lesson":
    content["decisions"] = ["should be dropped"]
print(json.dumps({"type": "result", "is_error": False, "result": json.dumps(content), "structured_output": content,
                  "num_turns": 2, "total_cost_usd": 0.0, "modelUsage": {"claude-fake-1": {}}}))
'''


def write_fake_claude(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    p = d / "claude"
    p.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    p.chmod(0o755)
    return p


def set_claude_mode(cfg, mode):
    (Path(cfg.claude_path).parent / "mode").write_text(mode)
    (Path(cfg.claude_path).parent / "count").write_text("0")


def claude_calls(cfg):
    try:
        return int((Path(cfg.claude_path).parent / "count").read_text())
    except OSError:
        return 0
