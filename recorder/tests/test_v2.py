"""Recorder v2: language mode, context, photos (start/submit, note-edit, local UI) - hand-off validated with the contract validator;
the worker (stub notes engine, no claude) consumes what we write."""
import base64
import hashlib
import io
import json
import time

import pytest
from PIL import Image

import fake_github as fg
from conftest import NOTEBOOK_ID, jb, run_cli
from rh import inbox_job, main_json, state

from lesson_record import photos
from lesson_worker import contract


def make_img(path, size=(640, 480), fmt="JPEG", exif=False, mode="RGB", color=(30, 90, 200)):
    im = Image.new(mode, size, color)
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "SecretCam"
        ex[0x0112] = 6
        ex[0x8825] = {1: "N", 2: (50.0, 5.0, 1.0)}
        kw["exif"] = ex
    im.save(path, fmt, **kw)
    return path


def record(seconds, *args, kind="lesson"):
    run_cli("start", "--kind", kind, "--notebook", "Biologie", "--title", "V2", *args)
    time.sleep(seconds)
    return run_cli("stop").stdout


# ------------------------------------------------------------------------------------------------- photos
def test_photo_is_resized_exif_free_and_small(tmp_path):
    p = make_img(tmp_path / "big.jpg", (4000, 3000), exif=True)
    data, w, h = photos.prepare_file(p)
    assert max(w, h) == 1600 and len(data) <= photos.MAX_BYTES
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG" and not im.getexif() and (im.width, im.height) == (w, h)
    assert (w, h) == (1200, 1600)                      # orientation 6 applied (landscape 4000x3000 becomes portrait)
    assert b"SecretCam" not in data and b"Exif" not in data


def test_png_with_alpha_and_noise_get_under_the_limit(tmp_path):
    import os
    p = tmp_path / "a.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 0)).save(p)
    assert photos.prepare_file(p)[0][:2] == b"\xff\xd8"
    noise = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))
    buf = io.BytesIO()
    noise.save(buf, "PNG")
    data, w, h = photos.prepare_bytes(buf.getvalue())
    assert len(data) <= photos.MAX_BYTES and max(w, h) <= 1600


def test_garbage_and_missing_files_are_rejected(tmp_path):
    (tmp_path / "x.jpg").write_bytes(b"not an image")
    for f in (tmp_path / "x.jpg", tmp_path / "nope.jpg"):
        with pytest.raises(photos.PhotoError):
            photos.prepare_file(f)


# ------------------------------------------------------------------------------------------------- start / stop
def test_new_note_carries_language_mode_context_and_photos(ctx, gh, fake_tone, tmp_path):
    a, b = make_img(tmp_path / "a.jpg", exif=True), make_img(tmp_path / "b.png", fmt="PNG")
    out = record(2.5, "--language-mode", "en", "--topic", "Mitoza", "--details", "profase, metafaze", "--image", str(a), "--image", str(b))
    assert "submitted" in out
    nid = state(ctx.paths)["last"]["note_id"]
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["schema_version"] == 2 and "language" not in note and note["source"] == "laptop" and note["language_mode"] == "en"
    assert note["context"] == {"subject": "Biologie 2.A", "topic": "Mitoza", "details": "profase, metafaze"}
    assert [i["id"] for i in note["images"]] == ["i01", "i02"] and all(i["status"] == "uploaded" for i in note["images"])
    assert note["inputs_updated_at"]
    job = inbox_job(nid)
    contract.check("job", job)
    assert job["schema_version"] == 2 and job["language_mode"] == "en" and job["context"] == note["context"] and "language" not in job
    assert job["audio_location"] == "local" and job["action"] == "transcribe"
    assert [(i["id"], i["file"], i["location"]) for i in job["images"]] == [("i01", "images/01.jpg", "local"), ("i02", "images/02.jpg", "local")]
    for im, ni in zip(job["images"], note["images"]):
        f = ctx.wcfg.local_audio_dir / nid / im["file"]
        raw = f.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == im["sha256"] == ni["sha256"] and len(raw) == im["bytes"] <= 614400
        assert b"SecretCam" not in raw
    assert all(not p.startswith(f"jobs/{nid}/images") for p in fg.ST.files("inbox"))      # laptop photos stay local until the worker takes them


def test_defaults_are_mixed_and_subject_is_the_notebook(ctx, gh, fake_tone):
    record(2.2)
    nid = state(ctx.paths)["last"]["note_id"]
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["language_mode"] == "mixed" and note["context"] == {"subject": "Biologie 2.A", "topic": "", "details": ""} and note["images"] == []
    job = inbox_job(nid)
    assert job["images"] == [] and job["language_mode"] == "mixed"


def test_settings_default_language_mode_is_honoured(ctx, gh, fake_tone):
    s = json.loads(json.dumps(fg.ST.files("main") and json.loads(fg.ST.blobs[fg.ST.files("main")["settings/app.json"]])))
    s["default_language_mode"] = "cs"
    fg.ST.commit_files("main", {"settings/app.json": jb(s)}, "settings")
    record(2.2)
    assert main_json(f"notes/{state(ctx.paths)['last']['note_id']}/note.json")["language_mode"] == "cs"


def test_bad_inputs_are_refused_before_recording(ctx, gh, fake_tone, tmp_path):
    for args in (["--topic", "x" * 301], ["--details", "x" * 2001], ["--image", str(tmp_path / "missing.jpg")]):
        r = run_cli("start", "--notebook", "Biologie", *args, check=False)
        assert r.returncode == 1 and "error" in r.stderr and state(ctx.paths)["state"] == "idle"
    imgs = []
    for i in range(13):
        imgs += ["--image", str(make_img(tmp_path / f"{i}.jpg", (50, 50)))]
    r = run_cli("start", "--notebook", "Biologie", *imgs, check=False)
    assert r.returncode == 1 and "at most 12" in r.stderr or "12" in r.stderr
    assert run_cli("start", "--notebook", "Biologie", "--language-mode", "de", check=False).returncode == 2


def test_second_part_of_a_v1_note_upgrades_it_and_keeps_context(ctx, gh, fake_tone, tmp_path):
    nid = "01K8G0M1N2P3Q4R5S6T7V8W9XY"
    v1 = {"schema_version": 1, "id": nid, "kind": "lesson", "title": "Stara", "notebook_id": NOTEBOOK_ID, "created_at": "2026-10-20T10:20:00Z",
          "updated_at": "2026-10-20T10:35:00Z", "source": "pwa", "language": "cs",
          "parts": [{"id": "p01", "order": 1, "filename": "01.opus", "bytes": 3780000, "sha256": "0" * 64, "duration": 60.0,
                     "uploaded_at": "2026-10-20T10:35:00Z", "status": "uploaded"}]}
    fg.ST.commit_files("main", {f"notes/{nid}/note.json": jb(v1)}, "v1 note")
    run_cli("start", "--note", nid, "--topic", "Doplneni", "--image", str(make_img(tmp_path / "a.jpg", (300, 200))))
    time.sleep(2.2)
    run_cli("stop")
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["schema_version"] == 2 and "language" not in note and [p["id"] for p in note["parts"]] == ["p01", "p02"]
    assert note["language_mode"] == "mixed" and note["context"] == {"subject": "Biologie 2.A", "topic": "Doplneni", "details": ""}
    assert [i["id"] for i in note["images"]] == ["i01"]
    job = inbox_job(nid)
    contract.check("job", job)
    assert job["append"] is True and [p["id"] for p in job["parts"]] == ["p02"] and job["images"][0]["location"] == "local"


def test_existing_v2_note_keeps_its_context_unless_overridden(ctx, gh, fake_tone, tmp_path):
    record(2.2, "--topic", "Puvodni", "--details", "d1", "--image", str(make_img(tmp_path / "a.jpg", (300, 200))))
    nid = state(ctx.paths)["last"]["note_id"]
    jid = inbox_job(nid)["job_id"]
    fg.ST.commit_files("main", {f"notes/{nid}/status.json": json.dumps({"schema_version": 1, "note_id": nid, "job_id": jid, "state": "done",
                                                                       "completed_job_ids": [jid]}).encode()}, "done")
    run_cli("start", "--note", nid, "--details", "d2", "--image", str(make_img(tmp_path / "b.jpg", (300, 200))))
    time.sleep(2.2)
    run_cli("stop")
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["context"]["topic"] == "Puvodni" and note["context"]["details"] == "d2"
    assert [i["id"] for i in note["images"]] == ["i01", "i02"]
    job = inbox_job(nid)
    contract.check("job", job)
    assert [(i["id"], i["location"]) for i in job["images"]] == [("i01", "main"), ("i02", "local")]      # complete set


# ------------------------------------------------------------------------------------------------- note-edit
def seed_done_note(extra=None, images=()):
    nid = "01K8G0M1N2P3Q4R5S6T7V8W9XY"
    note = {"schema_version": 2, "id": nid, "kind": "lesson", "title": "Hotova", "notebook_id": NOTEBOOK_ID, "created_at": "2026-10-20T10:20:00Z",
            "updated_at": "2026-10-20T10:35:00Z", "source": "pwa", "language_mode": "mixed",
            "context": {"subject": "Biologie 2.A", "topic": "", "details": ""}, "images": list(images),
            "parts": [{"id": "p01", "order": 1, "filename": "01.opus", "bytes": 3780000, "sha256": "0" * 64, "duration": 60.0,
                       "uploaded_at": "2026-10-20T10:35:00Z", "status": "uploaded"}]}
    st = {"schema_version": 1, "note_id": nid, "job_id": "01K8G0M1N2P3Q4R5S6T7V8W9XZ", "state": "done", **(extra or {})}
    fg.ST.commit_files("main", {f"notes/{nid}/note.json": jb(note), f"notes/{nid}/status.json": jb(st)}, "seed")
    return nid


def test_note_edit_changes_context_adds_and_removes_photos_and_regenerates(ctx, gh, tmp_path):
    kept = {"id": "i01", "filename": "01.jpg", "bytes": 1000, "sha256": "1" * 64, "width": 100, "height": 100, "uploaded_at": "2026-10-20T10:36:00Z", "status": "uploaded"}
    gone = {**kept, "id": "i02", "filename": "02.jpg", "sha256": "2" * 64}
    nid = seed_done_note(images=[kept, gone])
    a = make_img(tmp_path / "a.jpg", (800, 600), exif=True)
    out = run_cli("note-edit", nid, "--topic", "Nove tema", "--language-mode", "cs", "--image", str(a), "--remove-image", "i02").stdout
    assert "regeneration queued" in out
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["context"]["topic"] == "Nove tema" and note["language_mode"] == "cs" and note["inputs_updated_at"] > "2026-10-21"
    assert [(i["id"], i["status"]) for i in note["images"]] == [("i01", "uploaded"), ("i03", "uploaded")]
    job = inbox_job(nid)
    contract.check("job", job)
    assert job["action"] == "regenerate_notes" and job["append"] is True and job["parts"] == [] and job["audio_location"] == "inbox"
    assert [(i["id"], i["location"]) for i in job["images"]] == [("i01", "main"), ("i03", "inbox")]
    inbox = fg.ST.files("inbox")
    blob = fg.ST.blobs[inbox[f"jobs/{nid}/images/03.jpg"]]
    assert hashlib.sha256(blob).hexdigest() == job["images"][1]["sha256"] == note["images"][1]["sha256"] and b"SecretCam" not in blob
    assert job["context"] == note["context"]


def test_note_edit_refuses_unfinished_or_busy_notes_and_empty_edits(ctx, gh, tmp_path):
    nid = seed_done_note({"state": "transcribing"})
    r = run_cli("note-edit", nid, "--topic", "x", check=False)
    assert r.returncode == 1 and "not been processed" in r.stderr
    nid = seed_done_note()
    assert "nothing to change" in run_cli("note-edit", nid, check=False).stderr
    assert "no such photo" in run_cli("note-edit", nid, "--remove-image", "i09", check=False).stderr
    run_cli("note-edit", nid, "--topic", "a")
    r = run_cli("note-edit", nid, "--topic", "b", check=False)                      # first regenerate job still on the inbox
    assert r.returncode == 1 and "still waiting" in r.stderr
    assert main_json(f"notes/{nid}/note.json")["context"]["topic"] == "a"


# ------------------------------------------------------------------------------------------------- worker consumes it
def test_worker_processes_recorded_note_with_photos(ctx, gh, fake_tone, tmp_path):
    import sys
    from lesson_worker.ghapi import GitHub
    from lesson_worker.worker import Worker
    from helpers import FakeASR, FakeClock, FakeNotes
    cfg = ctx.wcfg
    cfg.retry_backoff_s = (0.0, 0.0, 0.0)
    cfg.cache_dir = tmp_path / "wcache"
    clock, notes = FakeClock(), FakeNotes()
    w = Worker(cfg, GitHub(cfg.repo, cfg.read_token(), cfg.api_base, sleep=clock.sleep), clock=clock, asr=FakeASR(), notes=notes)
    record(3, "--language-mode", "cs", "--topic", "Fotosynteza", "--image", str(make_img(tmp_path / "a.jpg", (500, 400))))
    nid = state(ctx.paths)["last"]["note_id"]
    assert w.drain() == 1
    assert main_json(f"notes/{nid}/status.json")["state"] == "done"
    call = notes.calls[0]
    assert call["language_mode"] == "cs" and call["context"]["topic"] == "Fotosynteza" and [i["id"] for i in call["images"]] == ["i01"]
    contract.check("notes", main_json(f"notes/{nid}/notes.json"))
    assert main_json(f"notes/{nid}/notes.json")["based_on_images"] == ["i01"]


# ------------------------------------------------------------------------------------------------- UI
def test_ui_photo_upload_and_start_with_context(ui_app, ctx, fake_tone, tmp_path):
    from test_ui import Client
    c = Client(ui_app)
    raw = make_img(tmp_path / "p.jpg", (2000, 1500), exif=True).read_bytes()
    code, r = c.json("POST", "/api/photo", {"name": "p.jpg", "data": base64.b64encode(raw).decode()})
    assert code == 200 and r["photo"]["bytes"] <= 614400
    code, bad = c.json("POST", "/api/photo", {"name": "x", "data": base64.b64encode(b"junk").decode()})
    assert code == 400
    code, bad = c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": NOTEBOOK_ID, "photo_ids": ["nope"]})
    assert code == 400
    code, bad = c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": NOTEBOOK_ID, "language_mode": "de"})
    assert code == 409
    code, st = c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": NOTEBOOK_ID, "title": "UI", "language_mode": "en", "topic": "T",
                                             "details": "D", "photo_ids": [r["photo"]["id"]]})
    assert code == 200, st
    time.sleep(2)
    code, last = c.json("POST", "/api/stop", {})
    nid = last["last"]["note_id"]
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    assert note["language_mode"] == "en" and note["context"]["topic"] == "T" and len(note["images"]) == 1
    contract.check("job", inbox_job(nid))
    assert not list((ctx.paths.state / "ui-photos").glob("*"))                # staging files are removed
