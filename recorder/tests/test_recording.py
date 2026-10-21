"""Recording lifecycle with a fake audio source (a generated tone) and the fake GitHub: start/stop, pause, parts, crash recovery."""
import hashlib
import json
import os
import signal
import time

import pytest

import fake_github as fg
from conftest import NOTEBOOK_ID, run_cli, wait_until
from rh import alive, children, daemon_pids, inbox_job, main_json, probe, state

from lesson_worker import contract


def last(paths):
    return state(paths).get("last") or {}


def record(seconds, *start_args, kind="lesson", title="Mitoza"):
    run_cli("start", "--kind", kind, "--notebook", "Biologie", "--title", title, *start_args)
    time.sleep(seconds)


def test_start_stop_creates_valid_note_job_and_part(ctx, gh, fake_tone):
    t0 = time.time()
    record(4.5)
    st = state(ctx.paths)
    assert st["state"] == "recording" and 3 <= st["elapsed"] <= 5
    out = run_cli("stop").stdout
    assert "saved" in out and "submitted" in out
    note_id = last(ctx.paths)["note_id"]
    # --- audio file where the worker reads it
    f = ctx.wcfg.local_audio_dir / note_id / "parts" / "01.opus"
    info = probe(f)
    assert info["codec"] == "opus" and info["channels"] == 1
    assert abs(info["duration"] - 4.5) < 1.5 and info["duration"] <= time.time() - t0
    assert f.stat().st_size < 4.5 * 50000 / 8                               # a pure tone costs more than speech (35 kbps measured); real speech ~24
    # --- note.json (main) validates and matches the file
    note = main_json(f"notes/{note_id}/note.json")
    contract.check("note", note)
    p = note["parts"][0]
    assert note["source"] == "laptop" and note["title"] == "Mitoza" and note["notebook_id"] == NOTEBOOK_ID and note["kind"] == "lesson"
    assert p["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest() and p["bytes"] == f.stat().st_size and p["status"] == "uploaded"
    assert abs(p["duration"] - info["duration"]) < 0.1 and p["filename"] == "01.opus"
    # --- job.json (inbox): local audio, diarize default for lessons = false
    job = inbox_job(note_id)
    contract.check("job", job)
    assert job["audio_location"] == "local" and job["source"] == "laptop" and job["action"] == "transcribe"
    assert job["append"] is False and job["options"]["diarize"] is False and job["kind"] == "lesson" and job["language"] == "cs"
    assert job["parts"] == [{"id": "p01", "order": 1, "file": "parts/01.opus", "bytes": p["bytes"], "sha256": p["sha256"], "duration": p["duration"]}]
    assert not any(ctx.paths.active.iterdir())                               # recording dir cleaned up
    assert state(ctx.paths)["state"] == "idle"
    # commit messages carry no title
    assert all("Mitoza" not in c["message"] for c in fg.ST.commits.values())


def test_meeting_defaults(ctx, gh, fake_tone):
    record(2.5, kind="meeting", title="Porada")
    assert state(ctx.paths)["mode"] == "meeting"
    run_cli("stop")
    job = inbox_job(last(ctx.paths)["note_id"])
    assert job["kind"] == "meeting" and job["options"]["diarize"] is True


def test_diarize_override(ctx, gh, fake_tone):
    record(2.2, "--diarize")
    run_cli("stop")
    assert inbox_job(last(ctx.paths)["note_id"])["options"]["diarize"] is True


def test_only_one_recording_at_a_time(ctx, gh, fake_tone):
    record(1.5)
    r = run_cli("start", "--notebook", "Biologie", check=False)
    assert r.returncode == 1 and "already recording" in r.stderr
    run_cli("stop")


def test_unknown_notebook(ctx, gh, fake_tone):
    r = run_cli("start", "--notebook", "Chemie", check=False)
    assert r.returncode == 1 and "unknown notebook" in r.stderr and "Biologie 2.A" in r.stderr
    assert state(ctx.paths)["state"] == "idle"


def test_pause_resume_excludes_the_pause(ctx, gh, fake_tone):
    record(3)
    run_cli("pause")
    assert state(ctx.paths)["state"] == "paused"
    e1 = state(ctx.paths)["elapsed"]
    time.sleep(3)
    assert state(ctx.paths)["elapsed"] <= e1 + 0.5
    run_cli("resume")
    time.sleep(3)
    run_cli("stop")
    d = probe(next((ctx.wcfg.local_audio_dir).glob("*/parts/01.opus")))["duration"]
    assert 5.0 <= d <= 8.0, d                                                 # ~6-7 s of audio (incl. CLI latency), not 9.5+ (pause excluded)


def test_closing_the_terminal_does_not_stop_recording(ctx, gh, fake_tone):
    record(1.5)
    (pid,) = daemon_pids(ctx.wcfg.data_dir.parent.parent) or [state(ctx.paths)["pid"]]
    os.kill(pid, signal.SIGHUP)                                                # what a closing terminal sends
    time.sleep(1.0)
    assert alive(pid) and state(ctx.paths)["state"] == "recording"
    assert os.getsid(pid) != os.getsid(os.getpid())                            # own session: no controlling terminal
    run_cli("stop")


def test_part_rotation_for_long_recordings(ctx, gh, fake_tone, home):
    (home / ".config/lesson-notes/recorder.toml").write_text("part_minutes = 0.05\n")      # 3 s parts
    record(8)
    run_cli("stop")
    nid = last(ctx.paths)["note_id"]
    job = inbox_job(nid)
    assert [p["order"] for p in job["parts"]] == [1, 2, 3]
    note = main_json(f"notes/{nid}/note.json")
    contract.check("note", note)
    for p in job["parts"]:
        assert probe(ctx.wcfg.local_audio_dir / nid / p["file"])["codec"] == "opus"
    assert abs(sum(p["duration"] for p in job["parts"]) - 8) < 2.0


def test_capture_process_dying_is_restarted(ctx, gh, monkeypatch):
    monkeypatch.setenv("LESSON_RECORD_FAKE_SOURCE", "flaky")
    record(6)
    st = state(ctx.paths)
    assert st["capture_restarts"] >= 1 and any("capture ended" in e["msg"] for e in st["events"])
    run_cli("stop")
    assert last(ctx.paths)["ok"] and last(ctx.paths)["seconds"] > 3.5


# --------------------------------------------------------------------------- failure modes
def test_no_audio_is_a_failure_not_an_empty_part(ctx, gh, monkeypatch):
    monkeypatch.setenv("LESSON_RECORD_FAKE_SOURCE", "none")
    monkeypatch.setenv("LESSON_RECORD_START_TIMEOUT", "3")
    r = run_cli("start", "--notebook", "Biologie", check=False)
    assert r.returncode == 1 and ("no audio" in r.stderr.lower())
    wait_until(lambda: state(ctx.paths).get("state") == "idle" and not any(ctx.paths.active.iterdir()), 15)
    assert last(ctx.paths)["ok"] is False and last(ctx.paths)["code"] == "no_audio"
    assert not list(ctx.paths.outbox.glob("*.json")) and not any((ctx.wcfg.local_audio_dir).glob("**/*.opus"))
    assert not [p for p in (fg.ST.files("inbox") or {}) if p.endswith("job.json")]


def test_silent_input_warns_then_fails_instead_of_saving_silence(ctx, gh, monkeypatch, home):
    (home / ".config/lesson-notes/recorder.toml").write_text("silent_warn_s = 2.0\n")
    monkeypatch.setenv("LESSON_RECORD_FAKE_SOURCE", "silence")
    record(4.5)
    st = state(ctx.paths)
    assert any("No sound" in w for w in st["warnings"])
    assert "WARNING" in run_cli("status").stdout
    r = run_cli("stop", check=False)
    assert r.returncode == 1 and "silence" in r.stderr
    assert last(ctx.paths)["code"] == "silent"
    assert not list(ctx.paths.outbox.glob("*.json")) and any(ctx.paths.failed.iterdir())      # kept, not deleted


# --------------------------------------------------------------------------- crash safety
@pytest.mark.parametrize("how", ["daemon", "everything"])
def test_kill_9_mid_recording_is_recovered(ctx, gh, fake_tone, how):
    t0 = time.time()
    record(5)
    st = state(ctx.paths)
    pid = st["pid"]
    kids = children(pid)
    assert kids, "expected ffmpeg encoder child"
    elapsed_at_kill = time.time() - t0
    if how == "everything":                      # power loss: nothing gets to shut down cleanly
        for k in kids:
            os.kill(k, 9)
    os.kill(pid, 9)
    time.sleep(0.5)
    assert not alive(pid)
    # recorder.json still claims "recording": status must notice and recover automatically
    out = run_cli("status").stdout
    assert "recovered" in out
    nid = last(ctx.paths)["note_id"]
    f = ctx.wcfg.local_audio_dir / nid / "parts" / "01.opus"
    info = probe(f)
    assert info["codec"] == "opus"
    assert abs(info["duration"] - (elapsed_at_kill - 1.0)) < 2.0 or abs(info["duration"] - elapsed_at_kill) < 2.0, (info, elapsed_at_kill)
    assert info["duration"] > 2.0
    contract.check("job", inbox_job(nid))                                       # recovered part was handed over too
    contract.check("note", main_json(f"notes/{nid}/note.json"))
    assert not any(ctx.paths.active.iterdir()) and state(ctx.paths)["state"] == "idle"
    assert not [p for p in daemon_pids(ctx.wcfg.data_dir.parent.parent)]          # no stray recorder
    # children died with the daemon (no process keeps the microphone)
    time.sleep(1.0)
    assert not any(alive(k) for k in kids)


def test_explicit_recover_command_and_start_recovers_first(ctx, gh, fake_tone):
    record(3.5)
    os.kill(state(ctx.paths)["pid"], 9)
    time.sleep(0.5)
    r = run_cli("start", "--notebook", "Biologie", "--title", "Druha")
    assert "recovered an interrupted earlier recording" in r.stdout
    run_cli("stop")
    assert len(list((ctx.wcfg.local_audio_dir).glob("*/parts/01.opus"))) == 2


def test_recover_when_state_file_is_lost(ctx, gh, fake_tone):
    record(3.5)
    os.kill(state(ctx.paths)["pid"], 9)
    ctx.paths.state_file.unlink()
    time.sleep(0.3)
    assert "nothing" not in run_cli("recover").stdout
    assert len(list((ctx.wcfg.local_audio_dir).glob("*/parts/01.opus"))) == 1


def test_crash_during_finalize_is_resumed(ctx, gh, fake_tone, monkeypatch):
    """Segments measured and the outbox entry written, but the move never happened: a second finalize completes it."""
    from lesson_record import finalize as fin
    from lesson_record.common import read_json
    record(3)
    st = state(ctx.paths)
    os.kill(st["pid"], 9)
    time.sleep(0.4)
    rec_dir = next(ctx.paths.active.iterdir())
    meta = read_json(rec_dir / "meta.json")
    real_move = fin.shutil.move
    monkeypatch.setattr(fin.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        fin.finalize(ctx.paths, rec_dir, meta)
    entry = read_json(ctx.paths.outbox / f"{meta['rec_id']}.json")
    assert entry["state"] == "finalizing" and rec_dir.exists()
    monkeypatch.setattr(fin.shutil, "move", real_move)
    run_cli("recover")
    assert (ctx.wcfg.local_audio_dir / meta["note_id"] / "parts/01.opus").exists()
    assert read_json(ctx.paths.outbox / f"{meta['rec_id']}.json")["state"] == "submitted"


# --------------------------------------------------------------------------- multi-part notes / hand-off
def test_add_part_to_existing_note_waits_for_previous_job(ctx, gh, fake_tone):
    record(2.5)
    run_cli("stop")
    nid = last(ctx.paths)["note_id"]
    # second part while the worker has not even looked at the first job
    r = run_cli("start", "--note", nid)
    assert "adding a part" in r.stdout
    time.sleep(2.5)
    out = run_cli("stop").stdout
    assert "waiting" in out
    pending = [e for e in json.loads(run_cli("status", "--json").stdout)["outbox"]]
    assert len(pending) == 1 and "still waiting" in pending[0]["last_error"]
    note = main_json(f"notes/{nid}/note.json")                                 # note.json already has p02 (audio is safe locally)
    assert [p["id"] for p in note["parts"]] == ["p01", "p02"]
    assert inbox_job(nid)["parts"][0]["id"] == "p01"                           # the first job was NOT replaced
    assert (ctx.wcfg.local_audio_dir / nid / "parts/02.opus").exists()
    # the worker finishes job 1 (status done + job dir removed) -> the next submit goes through
    jid = inbox_job(nid)["job_id"]
    st = {"schema_version": 1, "note_id": nid, "job_id": jid, "state": "done", "completed_job_ids": [jid]}
    fg.ST.commit_files("main", {f"notes/{nid}/status.json": json.dumps(st).encode()}, "done")
    out = run_cli("submit").stdout
    assert "submitted" in out
    job = inbox_job(nid)
    contract.check("job", job)
    assert job["append"] is True and [p["id"] for p in job["parts"]] == ["p02"] and job["job_id"] != jid


def test_failed_previous_job_is_carried_along(ctx, gh, fake_tone):
    record(2.3)
    run_cli("stop")
    nid = last(ctx.paths)["note_id"]
    jid = inbox_job(nid)["job_id"]
    st = {"schema_version": 1, "note_id": nid, "job_id": jid, "state": "failed", "completed_job_ids": []}
    fg.ST.commit_files("main", {f"notes/{nid}/status.json": json.dumps(st).encode()}, "failed")
    run_cli("start", "--note", nid)
    time.sleep(2.3)
    assert "submitted" in run_cli("stop").stdout
    job = inbox_job(nid)
    contract.check("job", job)
    assert [p["id"] for p in job["parts"]] == ["p01", "p02"] and job["append"] is False


def test_offline_keeps_audio_and_submit_later(ctx, gh, fake_tone, home):
    run_cli("list-notes")                                                      # primes the notebook cache while online
    toml = home / ".config/lesson-notes/worker.toml"
    good = toml.read_text()
    toml.write_text(good.replace("127.0.0.1:", "127.0.0.1:1"))                  # nothing listens there
    record(2.5)
    out = run_cli("stop").stdout
    assert "offline" in out
    nid = last(ctx.paths)["note_id"]
    assert (ctx.wcfg.local_audio_dir / nid / "parts/01.opus").exists()
    assert main_json(f"notes/{nid}/note.json") is None and inbox_job(nid) is None
    assert "waiting for hand-off" in run_cli("status").stdout
    toml.write_text(good)
    assert "submitted" in run_cli("submit").stdout
    contract.check("job", inbox_job(nid))
    assert run_cli("submit").stdout.strip() == "nothing waiting"


def test_missing_inbox_branch_is_created(ctx, gh, fake_tone):
    fg.ST.refs.pop("inbox")
    record(2.2)
    run_cli("stop")
    assert inbox_job(last(ctx.paths)["note_id"]) is not None


def test_list_notes(ctx, gh, fake_tone):
    record(2.2, title="Bunka")
    run_cli("stop")
    out = run_cli("list-notes").stdout
    assert "Bunka" in out and "Biologie 2.A" in out and "lesson" in out
