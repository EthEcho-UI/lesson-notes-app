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
            audio=None, upload_job=True, local_dir=None, language_mode=None, context=None, images=None):
    """Simulates the PWA upload into the fake GitHub: parts first, images, job.json last. Returns the job dict.
    Without language_mode/context/images a v1 job is written; with any of them a v2 job.
    images: [{"data": bytes, "ext": "jpg", "location": "inbox"|"main"|"local", "caption": str?, "id": "i01"?}]"""
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
    v2 = language_mode is not None or context is not None or images is not None
    job = {"schema_version": 2 if v2 else 1, "job_id": job_id, "note_id": note_id, "kind": kind, "created_at": created, "source": "pwa",
           "action": action, "append": append or action == "regenerate_notes", "audio_location": location,
           "options": {"diarize": diarize, "num_speakers": num_speakers}, "parts": parts if action == "transcribe" else []}
    if v2:
        job["language_mode"] = language_mode or "mixed"
        job["context"] = context or {"subject": "Fyzika", "topic": "", "details": ""}
        job["images"] = []
        for n, im in enumerate(images or [], 1):
            iid = im.get("id") or f"i{n:02d}"
            nn = iid[1:]
            rel = f"images/{nn}.{im.get('ext', 'jpg')}"
            data = im["data"]
            ent = {"id": iid, "file": rel, "bytes": len(data), "sha256": im.get("sha256") or hashlib.sha256(data).hexdigest(),
                   "width": im.get("width", 64), "height": im.get("height", 48), "location": im.get("location", "inbox")}
            if im.get("caption"):
                ent["caption"] = im["caption"]
            job["images"].append(ent)
            if ent["location"] == "inbox":
                files[f"jobs/{note_id}/{rel}"] = data
            elif ent["location"] == "main":
                fg.ST.commit_files("main", {f"notes/{note_id}/{rel}": data}, "seed kept photo")
            else:
                local = Path(local_dir) / note_id / rel
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
    else:
        job["language"] = language
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
        self.modes = []            # language argument of every call
        self.hints = []            # hint argument of every call ("" when none was passed)
        self.hook = None           # hook(call_index, duration) may raise
        self.fail_times = 0
        self.mixed_langs = None    # in mixed mode: [lang of 1st segment, lang of 2nd] (default cs, cs)

    def transcribe(self, audio, duration, language, progress=None, hint=""):
        self.calls.append(round(duration, 2))
        self.modes.append(language)
        self.hints.append(hint)
        if self.hook:
            self.hook(len(self.calls), duration)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated whisper failure")
        if progress:
            progress(50.0)
            progress(100.0)
        half = duration / 2
        segs = [{"start": 0.0, "end": round(half, 2), "text": f"Prvni veta delky {round(duration)}."},
                {"start": round(half, 2), "end": round(duration, 2), "text": "Druha veta."}]
        if language == "mixed":
            l1, l2 = self.mixed_langs or ("cs", "cs")
            segs[0]["lang"], segs[1]["lang"] = l1, l2
        elif language in ("cs", "en"):
            for s in segs:
                s["lang"] = language
        return segs


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
def emit(obj):
    print(json.dumps({"type": "system", "subtype": "init", "tools": []}))
    print(json.dumps(obj))
if mode == "not_logged_in":      # what the real binary prints when logged out (verified): JSON with is_error
    emit({"type": "result", "is_error": True, "terminal_reason": "api_error", "result": "Not logged in · Please run /login"}); sys.exit(0)
if mode == "not_logged_in_text":
    print("Not logged in · Please run /login"); sys.exit(1)
if mode == "is_error" or (mode == "error_once" and n == 1):
    emit({"type": "result", "is_error": True, "result": "API Error: rate limit reached"}); sys.exit(1)
if mode == "hang":
    import time; time.sleep(30)
if mode == "garbage":
    print("this is not json"); sys.exit(0)
if "--json-schema" not in sys.argv:           # free-text call (doctor model check, condense step)
    emit({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.0, "modelUsage": {"claude-fake-1": {}}}); sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
meeting = "decisions" in schema["properties"]
c = {"title": "Testovaci poznamky", "description": "Shrnuti probrane latky. Druha veta popisu.",
     "outline": [{"heading": "Uvod", "points": ["bod 1", {"text": "bod 2", "children": ["podbod"]}]}],
     "key_points": [{"text": "pojem je vec", "kind": "definition", "term": "pojem"}],
     "figures": [], "open_questions": [], "uncertainties": []}
if meeting:
    c.update(decisions=["rozhodnuti A"], action_items=[{"task": "udelat B", "owner": "Petra", "due": None}])
else:
    c.update(study_materials=[{"kind": "search_terms", "title": "Hledej", "text": "newtonovy zakony"}], homework=[{"task": "domaci ukol", "source": "lesson"}])
if mode == "bad_schema":
    c.pop("outline")
if mode == "extra_fields_lesson":
    c["decisions"] = ["should be dropped"]
# key order is normative: rebuild in contract order
order = ["title", "description", "outline", "key_points", "figures", "study_materials", "homework", "decisions", "action_items", "open_questions", "uncertainties"]
c = {k: c[k] for k in order if k in c}
emit({"type": "result", "is_error": False, "result": json.dumps(c), "structured_output": c, "num_turns": 2, "total_cost_usd": 0.0,
      "modelUsage": {"claude-fake-1": {}}})
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


class FakeNotes:
    """Stand-in for the notes engine (new-style interface): records every call, returns valid v2 content."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def __call__(self, cfg, *, transcript, context, language_mode, kind, images, previous_notes, notebook_name="", usage=None):
        if self.fail:
            raise self.fail
        self.calls.append({"transcript": transcript, "context": context, "language_mode": language_mode, "kind": kind,
                           "images": [dict(i, data=Path(i["path"]).read_bytes()) for i in images], "previous_notes": previous_notes,
                           "notebook_name": notebook_name, "state": main_json(f"notes/{transcript['note_id']}/status.json")})
        if usage is not None:
            usage["images_used"] = [i["id"] for i in images]
        c = {"title": "Poznamky v2", "description": "Strucny popis probrane latky. Druha veta popisu.",
             "outline": [{"heading": "Uvod", "points": ["bod 1", {"text": "bod 2", "children": ["podbod"]}]}],
             "key_points": [{"text": "dulezite", "kind": "important"}]}
        if kind == "meeting":
            c.update(figures=[], decisions=["rozhodnuti"], action_items=[{"task": "udelat", "owner": None, "due": None}])
        else:
            c.update(figures=[], study_materials=[{"kind": "search_terms", "title": "Hledej", "text": "newtonovy zakony"}],
                     homework=[{"task": "ukol", "source": "lesson"}])
        c.update(open_questions=[], uncertainties=[])
        return c, "claude-fake-2"


def jpeg_with_exif(w=64, h=48, orientation=None, gps=True):
    """A small JPEG carrying camera make, GPS position and (optionally) an orientation tag."""
    from PIL import Image
    im = Image.new("RGB", (w, h), (30, 90, 200))
    for x in range(w):
        im.putpixel((x, 0), (255, 255, 255))          # white top row: lets tests see whether the picture was rotated
    ex = Image.Exif()
    ex[0x010F] = "SecretCameraMaker"
    if gps:
        ex[0x8825] = {1: "N", 2: (50.0, 5.0, 0.0), 3: "E", 4: (14.0, 25.0, 0.0)}
    if orientation:
        ex[0x0112] = orientation
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex, quality=90)
    return buf.getvalue()


def put_note(note_id, images=(), context=None, language_mode="mixed", notebook_id=None):
    """Simulates the PWA's note.json on main. images: [(id, ext)] listed in note.json (data is not needed)."""
    doc = {"schema_version": 2, "id": note_id, "kind": "lesson", "title": "T", "language_mode": language_mode,
           "context": context or {"subject": "Fyzika", "topic": "", "details": ""},
           "images": [{"id": i, "filename": f"{i[1:]}.{ext}", "bytes": 1, "sha256": "0" * 64, "width": 1, "height": 1,
                       "uploaded_at": "2026-10-01T00:00:00Z", "status": "uploaded"} for i, ext in images]}
    if notebook_id:
        doc["notebook_id"] = notebook_id
    fg.ST.commit_files("main", {f"notes/{note_id}/note.json": json.dumps(doc).encode()}, "pwa: note")
    return doc


def main_bytes(path):
    d = fg.ST.files("main").get(path)
    return None if d is None else fg.ST.blobs[d]
