"""Contract v2 plumbing against the fake GitHub: languages, context -> Whisper/notes, photos end-to-end, regenerate, v1 compatibility."""
import hashlib
import io
import json

import pytest
from PIL import Image

import fake_github as fg
from helpers import (FakeNotes, inbox_files, jpeg_with_exif, main_bytes, main_json, put_job, put_note, ulid)
from lesson_worker import asr as asrmod, contract
from lesson_worker.images import has_metadata


@pytest.fixture
def notes():
    return FakeNotes()


@pytest.fixture
def w(make_worker, notes):
    return make_worker(notes=notes)


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- languages
def test_mixed_job_writes_lang_per_segment_and_detected_languages(w, asr, notes):
    asr.mixed_langs = ("cs", "en")
    j = put_job(durations=(4.0,), language_mode="mixed")
    w.drain()
    tr = main_json(f"notes/{j['note_id']}/transcript.json")
    assert tr["schema_version"] == 2 and tr["language_mode"] == "mixed" and contract.problems("transcript", tr) == []
    assert [s["lang"] for s in tr["parts"][0]["segments"]] == ["cs", "en"]
    assert tr["parts"][0]["detected_languages"] == [{"lang": "cs", "share": 0.5}, {"lang": "en", "share": 0.5}]
    assert tr["language"] == "cs"                                     # tie -> cs
    assert asr.modes == ["mixed"] and notes.calls[0]["language_mode"] == "mixed"
    assert "en" not in tr["merged"]["text"].split("]")[0]             # merged.text carries no language tags


def test_en_job_forces_english_and_dominant_language_is_en(w, asr):
    j = put_job(durations=(2.0,), language_mode="en")
    w.drain()
    tr = main_json(f"notes/{j['note_id']}/transcript.json")
    assert asr.modes == ["en"] and tr["language"] == "en" and tr["language_mode"] == "en"
    assert {s["lang"] for s in tr["parts"][0]["segments"]} == {"en"} and contract.problems("transcript", tr) == []
    nt = main_json(f"notes/{j['note_id']}/notes.json")
    assert nt["language"] == "cs" and nt["schema_version"] == 2           # notes are Czech whatever the audio language


def test_segment_redecoded_after_other_language_keeps_first_guess(w, asr):
    """an ASR result with lang_detected (e.g. sk -> re-run as cs) is stored as is and the raw detector share survives"""
    orig = asr.transcribe

    def tr(audio, duration, language, progress=None, hint=""):
        segs = orig(audio, duration, language, progress, hint)
        segs[0].update(lang="cs", lang_detected="sk")
        segs[1].update(lang="cs")
        return segs
    asr.transcribe = tr
    j = put_job(durations=(4.0,), language_mode="mixed")
    w.drain()
    t = main_json(f"notes/{j['note_id']}/transcript.json")
    assert t["parts"][0]["segments"][0]["lang_detected"] == "sk"
    assert {d["lang"]: d["share"] for d in t["parts"][0]["detected_languages"]} == {"sk": 0.5, "cs": 0.5}
    assert t["language"] == "cs" and contract.problems("transcript", t) == []


# ---------------------------------------------------------------- context
def test_context_reaches_whisper_hint_and_notes_generator(w, asr, notes):
    ctx = {"subject": "Fyzika 2.B", "topic": "Newtonovy pohybove zakony", "details": "setrvacnost, sila, zrychleni; ucitel pan Novak"}
    put_note(ulid(1), context=ctx)
    j = put_job(note_id=ulid(1), durations=(2.0,), language_mode="cs", context=ctx)
    w.drain()
    assert len(asr.hints) == 1 and all(x in asr.hints[0] for x in ("Fyzika 2.B", "Newtonovy pohybove zakony", "setrvacnost"))
    assert len(asr.hints[0]) <= w.cfg.whisper_hint_chars
    assert notes.calls[0]["context"] == ctx and notes.calls[0]["kind"] == "lesson"


def test_hint_is_bounded_and_can_be_switched_off(cfg):
    long_ctx = {"subject": "S", "topic": "T", "details": "slovo " * 500}
    h = asrmod.build_hint(cfg, long_ctx)
    assert 0 < len(h) <= cfg.whisper_hint_chars and not h.endswith(" ")
    cfg.whisper_hint_mode = "off"
    assert asrmod.build_hint(cfg, long_ctx) == ""
    cfg.whisper_hint_mode = "hotwords"
    assert asrmod.build_hint(cfg, {"subject": "Fyzika", "topic": "", "details": ""}) == "Fyzika"


def test_changed_context_invalidates_asr_cache_but_same_context_reuses_it(make_worker, asr, notes, gh):
    """a failed job keeps its transcription cache for the retry (new job id, same audio): reused only if the Whisper hint is unchanged"""
    from helpers import wav_bytes
    from lesson_worker.notes import NotesError
    audio = wav_bytes(1.0)
    ctx_a = {"subject": "A", "topic": "", "details": ""}
    notes.fail = NotesError("claude_not_logged_in", "log in", True)
    w = make_worker(notes=notes)
    nid = ulid(2)
    put_job(note_id=nid, durations=(1.0,), context=ctx_a, language_mode="cs", audio=[audio], created="2026-10-01T08:00:00Z")
    w.drain()
    assert len(asr.calls) == 1
    notes.fail = None
    put_job(note_id=nid, durations=(1.0,), context=ctx_a, language_mode="cs", audio=[audio], created="2026-10-02T08:00:00Z")
    w.drain()
    assert len(asr.calls) == 1 and main_json(f"notes/{nid}/status.json")["state"] == "done"      # cached
    notes.fail = NotesError("claude_not_logged_in", "log in", True)
    nid2 = ulid(3)
    put_job(note_id=nid2, durations=(1.0,), context=ctx_a, language_mode="cs", audio=[audio], created="2026-10-03T08:00:00Z")
    w.drain()
    notes.fail = None
    n = len(asr.calls)
    put_job(note_id=nid2, durations=(1.0,), context={"subject": "B", "topic": "x", "details": ""}, language_mode="cs", audio=[audio], created="2026-10-04T08:00:00Z")
    w.drain()
    assert len(asr.calls) == n + 1 and "B. x" in asr.hints[-1]                                   # new hint -> transcribed again


# ---------------------------------------------------------------- photos
def test_photos_end_to_end_stored_on_main_scrubbed_removed_from_inbox(w, asr, notes):
    nid = ulid(10)
    raw = jpeg_with_exif()
    png = io.BytesIO()
    Image.new("RGB", (20, 20), (0, 200, 0)).save(png, "PNG")
    assert has_metadata(raw, "jpg") and b"SecretCameraMaker" in raw
    put_note(nid, images=[("i01", "jpg"), ("i02", "png")])
    j = put_job(note_id=nid, durations=(2.0,), language_mode="mixed", images=[
        {"data": raw, "ext": "jpg", "caption": "tabule"}, {"data": png.getvalue(), "ext": "png"}])
    seen = []
    asr.hook = None
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "done" and st["schema_version"] == 2 and st["images_kept"] == ["i01", "i02"]
    assert contract.problems("status", st) == []
    # stored on main, metadata-free, still a valid picture of the same size
    stored = main_bytes(f"notes/{nid}/images/01.jpg")
    assert stored and b"SecretCameraMaker" not in stored and not has_metadata(stored, "jpg")
    assert Image.open(io.BytesIO(stored)).size == (64, 48)
    assert main_bytes(f"notes/{nid}/images/02.png") is not None
    # removed from the inbox with the job
    assert inbox_files() == []
    # the notes generator saw the scrubbed photos with captions; status went through reading_images
    call = notes.calls[0]
    assert [i["id"] for i in call["images"]] == ["i01", "i02"] and call["images"][0]["caption"] == "tabule"
    assert b"SecretCameraMaker" not in call["images"][0]["data"] and call["images"][0]["data"] == stored
    assert call["state"]["state"] == "summarizing"
    nt = main_json(f"notes/{nid}/notes.json")
    assert nt["based_on_images"] == ["i01", "i02"] and contract.problems("notes", nt) == []


def test_status_reading_images_written_only_with_photos(w, notes, gh):
    nid = ulid(11)
    states = []
    real = w._write_status
    w._write_status = lambda st, *a, **k: (states.append(st["state"]), real(st, *a, **k))[1]
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": jpeg_with_exif(), "ext": "jpg"}])
    w.drain()
    assert states.index("reading_images") < states.index("summarizing")
    states.clear()
    put_job(durations=(1.0,), language_mode="cs")
    w.drain()
    assert "reading_images" not in states


def test_exif_orientation_is_applied_before_the_tag_is_dropped(w, notes):
    nid = ulid(12)
    raw = jpeg_with_exif(w=60, h=40, orientation=6)                 # needs a 90 degree turn
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": raw, "ext": "jpg"}])
    w.drain()
    stored = main_bytes(f"notes/{nid}/images/01.jpg")
    im = Image.open(io.BytesIO(stored))
    assert im.size == (40, 60) and not im.getexif() and not has_metadata(stored, "jpg")


def test_photo_sha_mismatch_fails_retryable(w, gh):
    nid = ulid(13)
    put_note(nid, images=[("i01", "jpg")])
    j = put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": jpeg_with_exif(), "ext": "jpg", "sha256": "a" * 64}])
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "failed" and st["error"]["code"] == "sha_mismatch" and st["error"]["retryable"] is True
    assert main_bytes(f"notes/{nid}/images/01.jpg") is None


def test_broken_photo_fails_non_retryable_with_stage_read_images(w):
    nid = ulid(14)
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": b"this is not a jpeg at all", "ext": "jpg"}])
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "failed" and st["error"]["code"] == "image_invalid" and st["error"]["stage"] == "read_images"
    assert st["error"]["retryable"] is False and contract.problems("status", st) == []


def test_photo_deleted_from_note_json_during_the_run_is_not_kept(w, asr, notes):
    nid = ulid(15)
    put_note(nid, images=[("i01", "jpg"), ("i02", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[
        {"data": jpeg_with_exif(), "ext": "jpg"}, {"data": jpeg_with_exif(w=30, h=30), "ext": "jpg"}])
    asr.hook = lambda i, d: put_note(nid, images=[("i01", "jpg")])           # the user deletes i02 while the worker transcribes
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["images_kept"] == ["i01"] and main_bytes(f"notes/{nid}/images/02.jpg") is None
    assert main_bytes(f"notes/{nid}/images/01.jpg") is not None and inbox_files() == []


def test_orphans_are_deleted_in_the_completion_commit_and_by_the_janitor(w, cfg, gh, notes):
    nid = ulid(16)
    put_note(nid, images=[("i01", "jpg")])
    gh.commit_files("main", {f"notes/{nid}/images/01.jpg": b"x" * 10, f"notes/{nid}/images/05.jpg": b"old orphan"}, "seed")
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[
        {"data": jpeg_with_exif(), "ext": "jpg", "id": "i01", "location": "inbox"}])
    w.drain()
    assert main_bytes(f"notes/{nid}/images/05.jpg") is None and main_bytes(f"notes/{nid}/images/01.jpg") != b"x" * 10
    # janitor: another note that is never regenerated
    other = ulid(17)
    put_note(other, images=[])
    gh.commit_files("main", {f"notes/{other}/images/03.png": b"orphan"}, "seed")
    w.gc_images(force=True)
    assert main_bytes(f"notes/{other}/images/03.png") is None
    # without a readable note.json nothing is deleted
    lonely = ulid(18)
    gh.commit_files("main", {f"notes/{lonely}/images/01.jpg": b"keep me"}, "seed")
    w.gc_images(force=True)
    assert main_bytes(f"notes/{lonely}/images/01.jpg") == b"keep me"


# ---------------------------------------------------------------- regenerate
def test_regenerate_with_changed_context_and_photos(w, asr, notes, gh):
    nid = ulid(20)
    ctx1 = {"subject": "Fyzika", "topic": "Newton", "details": ""}
    raw1 = jpeg_with_exif()
    put_note(nid, images=[("i01", "jpg")], context=ctx1)
    put_job(note_id=nid, durations=(2.0,), language_mode="cs", context=ctx1, images=[{"data": raw1, "ext": "jpg"}], created="2026-10-01T08:00:00Z")
    w.drain()
    tr1 = main_bytes(f"notes/{nid}/transcript.json")
    asr_calls = len(asr.calls)
    kept1 = main_bytes(f"notes/{nid}/images/01.jpg")
    # user: changes context, deletes i01, adds i02 (new) and i03... here: keeps i01 (location main), adds i02 on inbox
    ctx2 = {"subject": "Fyzika", "topic": "Newton a Hooke", "details": "pruziny"}
    put_note(nid, images=[("i01", "jpg"), ("i02", "png")], context=ctx2)
    png = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 0, 0)).save(png, "PNG")
    kept = {"data": raw1, "ext": "jpg", "id": "i01", "location": "main"}
    kept["sha256"], kept["width"], kept["height"] = sha(raw1), 64, 48
    put_job(note_id=nid, action="regenerate_notes", kind="lesson", language_mode="cs", context=ctx2, created="2026-10-02T08:00:00Z",
            images=[kept, {"data": png.getvalue(), "ext": "png", "id": "i02", "location": "inbox"}])
    fg.ST.commit_files("main", {f"notes/{nid}/images/01.jpg": kept1}, "restore the worker-scrubbed copy")   # job sha256 is the ORIGINAL's
    w.drain()
    assert len(asr.calls) == asr_calls                                  # no audio work
    c = notes.calls[-1]
    assert c["context"] == ctx2 and [i["id"] for i in c["images"]] == ["i01", "i02"]
    assert c["previous_notes"]["content"]["title"] == "Poznamky v2"        # previous notes handed over
    assert main_bytes(f"notes/{nid}/transcript.json") == tr1              # v2 transcript is not rewritten
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "done" and st["images_kept"] == ["i01", "i02"] and len(st["completed_job_ids"]) == 2
    assert main_bytes(f"notes/{nid}/images/02.png") is not None and main_bytes(f"notes/{nid}/images/01.jpg") == kept1
    assert inbox_files() == []
    assert main_json(f"notes/{nid}/notes.json")["based_on_images"] == ["i01", "i02"]


def test_regenerate_main_photo_missing_fails_cleanly(w, gh):
    nid = ulid(21)
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", created="2026-10-01T08:00:00Z")
    w.drain()
    raw = jpeg_with_exif()
    gone = {"data": raw, "ext": "jpg", "id": "i01", "location": "main"}
    gh_files = fg.ST.files("main")
    assert f"notes/{nid}/images/01.jpg" not in gh_files
    put_job(note_id=nid, action="regenerate_notes", kind="lesson", language_mode="cs", created="2026-10-02T08:00:00Z", images=[gone])
    # put_job seeded the main copy; remove it to simulate a lost file
    fg.ST.commit_files("main", {f"notes/{nid}/images/01.jpg": None}, "lose it")
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "failed" and st["error"]["code"] == "image_missing" and st["error"]["retryable"] is False


# ---------------------------------------------------------------- v1 compatibility
def test_v1_job_is_still_processed_and_writes_v2_files(w, asr, notes):
    j = put_job(durations=(2.0,), language="cs")                       # no language_mode/context/images: schema v1 job
    assert j["schema_version"] == 1
    w.drain()
    st = main_json(f"notes/{j['note_id']}/status.json")
    assert st["state"] == "done" and asr.modes == ["cs"]
    tr = main_json(f"notes/{j['note_id']}/transcript.json")
    assert tr["schema_version"] == 2 and tr["language_mode"] == "cs" and contract.problems("transcript", tr) == []
    assert notes.calls[0]["language_mode"] == "cs" and "images" not in j


def test_v1_job_with_english_language_forces_english(w, asr):
    put_job(durations=(1.0,), language="en")
    w.drain()
    assert asr.modes == ["en"]


def test_v1_context_falls_back_to_notebook_name(w, notes, gh):
    nid, nb = ulid(30), ulid(31)
    gh.commit_files("main", {f"notebooks/{nb}.json": json.dumps({"schema_version": 1, "id": nb, "name": "Biologie"}).encode(),
                             f"notes/{nid}/note.json": json.dumps({"schema_version": 1, "id": nid, "notebook_id": nb}).encode()}, "pwa")
    put_job(note_id=nid, durations=(1.0,))
    w.drain()
    assert notes.calls[0]["context"]["subject"] == "Biologie" and notes.calls[0]["notebook_name"] == "Biologie"


def test_append_to_a_v1_transcript_upgrades_old_parts(w, asr, notes, gh):
    """A note transcribed by the old worker (v1 transcript + v1 notes) gets another part: old segments become lang=cs."""
    nid = ulid(32)
    old = json.load(open(__import__("pathlib").Path(__file__).parents[3] / "contract/examples/lesson-2part/transcript.json"))
    old["note_id"] = nid
    old_notes = json.load(open(__import__("pathlib").Path(__file__).parents[3] / "contract/examples/lesson-2part/notes.json"))
    old_notes["note_id"] = nid
    gh.commit_files("main", {f"notes/{nid}/transcript.json": json.dumps(old).encode(), f"notes/{nid}/notes.json": json.dumps(old_notes).encode()}, "old worker")
    n_old = len(old["parts"])
    put_job(note_id=nid, durations=(2.0,), append=True, first_order=n_old + 1, language_mode="cs", created="2026-10-05T08:00:00Z")
    w.drain()
    tr = main_json(f"notes/{nid}/transcript.json")
    assert tr["schema_version"] == 2 and contract.problems("transcript", tr) == []
    assert all(s["lang"] == "cs" for p in tr["parts"] for s in p["segments"]) and len(tr["parts"]) == n_old + 1
    assert notes.calls[0]["previous_notes"]["schema_version"] == 1                # v1 notes handed to the engine as they are
    assert main_json(f"notes/{nid}/notes.json")["schema_version"] == 2


def test_regenerate_on_v1_transcript_upgrades_it(w, asr, notes, gh):
    nid = ulid(33)
    old = json.load(open(__import__("pathlib").Path(__file__).parents[3] / "contract/examples/lesson-2part/transcript.json"))
    old["note_id"] = nid
    gh.commit_files("main", {f"notes/{nid}/transcript.json": json.dumps(old).encode()}, "old worker")
    put_job(note_id=nid, action="regenerate_notes", kind="lesson", language_mode="cs", created="2026-10-05T08:00:00Z")
    w.drain()
    assert asr.calls == [] and main_json(f"notes/{nid}/transcript.json")["schema_version"] == 2
    assert main_json(f"notes/{nid}/status.json")["state"] == "done"


# ---------------------------------------------------------------- crash/resume with photos
def test_crash_after_reading_images_resumes_and_commits_photos(make_worker, asr, notes, gh):
    from helpers import SimulatedCrash
    nid = ulid(40)
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": jpeg_with_exif(), "ext": "jpg"}])

    def crashing(cfg, **kw):
        raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        make_worker(notes=crashing).drain()
    assert main_json(f"notes/{nid}/status.json")["state"] == "summarizing"
    assert main_bytes(f"notes/{nid}/images/01.jpg") is None          # `done` is the commit point: nothing leaked before it
    asr.calls.clear()
    make_worker(notes=notes).drain()
    assert asr.calls == [] and main_json(f"notes/{nid}/status.json")["images_kept"] == ["i01"]
    assert main_bytes(f"notes/{nid}/images/01.jpg") is not None and inbox_files() == []


def test_notes_error_stage_and_photos_stay_on_inbox_for_retry(w, notes, gh):
    from lesson_worker.notes import NotesError
    nid = ulid(41)
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(1.0,), language_mode="cs", images=[{"data": jpeg_with_exif(), "ext": "jpg"}])
    notes.fail = NotesError("claude_not_logged_in", "log in", True)
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "failed" and st["error"]["stage"] == "summarize"
    assert any(p.endswith("images/01.jpg") for p in inbox_files())            # kept for the retry


def test_local_photos_of_a_laptop_recording_are_kept_on_main(w, asr, notes, tmp_path):
    """b9 finding: photos with location=local were shown to the notes engine but not copied to notes/<id>/images/."""
    nid = ulid(11)
    raw = jpeg_with_exif()
    put_note(nid, images=[("i01", "jpg")])
    put_job(note_id=nid, durations=(2.0,), language_mode="cs", location="local", local_dir=w.cfg.local_audio_dir,
            images=[{"data": raw, "ext": "jpg", "location": "local", "id": "i01"}])
    w.drain()
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "done" and st["images_kept"] == ["i01"]
    stored = main_bytes(f"notes/{nid}/images/01.jpg")
    assert stored and b"SecretCameraMaker" not in stored
