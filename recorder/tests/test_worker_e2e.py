"""The hand-off really works: what the recorder writes is accepted and processed by the worker's own engine
(worker/src, its contract validation, ffmpeg/av decoding of our Opus, fake Whisper + fake `claude`, fake GitHub)."""
import json
import time

import pytest

import fake_github as fg
from conftest import run_cli
from rh import inbox_job, main_json, state

from lesson_worker import contract
from lesson_worker.ghapi import GitHub
from lesson_worker.worker import Worker
from helpers import FakeASR, FakeClock, write_fake_claude


@pytest.fixture
def worker(ctx, tmp_path):
    cfg = ctx.wcfg
    cfg.claude_path = str(write_fake_claude(tmp_path / "bin"))
    cfg.claude_retry_wait_s = 0.0
    cfg.retry_backoff_s = (0.0, 0.0, 0.0)
    cfg.cache_dir = tmp_path / "wcache"
    clock = FakeClock()
    asr = FakeASR()
    w = Worker(cfg, GitHub(cfg.repo, cfg.read_token(), cfg.api_base, sleep=clock.sleep), clock=clock, asr=asr)
    w.asr_calls = asr.calls
    return w


def record(seconds, *args, kind="lesson"):
    run_cli("start", "--kind", kind, "--notebook", "Biologie", "--title", "E2E", *args)
    time.sleep(seconds)
    return run_cli("stop").stdout


def test_worker_processes_a_recorded_lesson(ctx, gh, fake_tone, worker):
    record(4)
    nid = state(ctx.paths)["last"]["note_id"]
    assert worker.drain() == 1
    st = main_json(f"notes/{nid}/status.json")
    assert st["state"] == "done" and st["audio"]["state"] == "purged"
    tr = main_json(f"notes/{nid}/transcript.json")
    contract.check("transcript", tr)
    assert abs(worker.asr_calls[0] - 4) < 1.5                           # the worker decoded OUR opus file: right length
    notes = main_json(f"notes/{nid}/notes.json")
    contract.check("notes", notes)
    assert not (ctx.wcfg.local_audio_dir / nid).exists()                # worker deleted the laptop audio after done
    assert [p for p in fg.ST.files("inbox") if p != ".keep"] == []


def test_meeting_gets_diarization_option_and_meeting_notes(ctx, gh, fake_tone, worker):
    record(2.5, kind="meeting")
    nid = state(ctx.paths)["last"]["note_id"]
    worker.drain()
    assert main_json(f"notes/{nid}/status.json")["state"] == "done"
    assert "decisions" in main_json(f"notes/{nid}/notes.json")["content"]


def test_second_part_is_appended_to_the_same_note(ctx, gh, fake_tone, worker):
    record(3)
    nid = state(ctx.paths)["last"]["note_id"]
    run_cli("start", "--note", nid)                                      # part 2 recorded while job 1 is still queued
    time.sleep(3)
    assert "waiting" in run_cli("stop").stdout
    assert (ctx.wcfg.local_audio_dir / nid / "parts/02.opus").exists()
    assert worker.drain() == 1                                          # job 1 only
    assert (ctx.wcfg.local_audio_dir / nid / "parts/02.opus").exists()  # worker's cleanup of job 1 left part 2 alone
    assert "submitted" in run_cli("submit").stdout
    worker.invalidate()
    assert worker.drain() == 1                                          # job 2 (append)
    tr = main_json(f"notes/{nid}/transcript.json")
    contract.check("transcript", tr)
    assert [p["part_id"] for p in tr["parts"]] == ["p01", "p02"]
    assert abs(tr["parts"][1]["offset"] - tr["parts"][0]["duration"]) < 0.01
    note = main_json(f"notes/{nid}/note.json")
    assert [p["id"] for p in note["parts"]] == ["p01", "p02"] and note["source"] == "laptop"
    assert main_json(f"notes/{nid}/status.json")["state"] == "done"
    assert not (ctx.wcfg.local_audio_dir / nid).exists()


def test_recovered_recording_is_processable(ctx, gh, fake_tone, worker):
    import os
    run_cli("start", "--kind", "lesson", "--notebook", "Biologie", "--title", "Crash")
    time.sleep(4)
    os.kill(state(ctx.paths)["pid"], 9)
    time.sleep(0.4)
    run_cli("status")                                                   # auto-recovers and hands over
    nid = state(ctx.paths)["last"]["note_id"]
    assert worker.drain() == 1
    assert main_json(f"notes/{nid}/status.json")["state"] == "done"
    assert worker.asr_calls[0] > 2
