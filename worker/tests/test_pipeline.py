"""Core pipeline against the fake GitHub: single job, multi-part, append, regenerate, ordering, meetings."""
import json
from pathlib import Path

import fake_github as fg
from helpers import done_order, inbox_files, main_json, put_job, ulid, claude_calls, set_claude_mode
from lesson_worker import contract


def test_single_lesson_end_to_end(worker, asr, cfg, gh):
    job = put_job(durations=(2.0,), created="2026-10-01T08:00:00Z")
    note = job["note_id"]
    old_inbox_tip = gh.refs["inbox"]
    assert worker.drain() == 1

    st = main_json(f"notes/{note}/status.json")
    assert st["state"] == "done" and st["job_id"] == job["job_id"] and st["completed_job_ids"] == [job["job_id"]]
    assert st["audio"] == {"state": "purged", "purge_after": None} and st["attempt"] == 1
    tr = main_json(f"notes/{note}/transcript.json")
    assert contract.problems("transcript", tr) == []
    assert tr["parts"][0]["offset"] == 0 and tr["merged"]["text"].startswith("[Část 1]\nPrvni veta")
    assert tr["parts"][0]["duration"] == 2.0 and tr["language"] == "cs"
    nt = main_json(f"notes/{note}/notes.json")
    assert contract.problems("notes", nt) == []
    assert nt["content"]["title"] == "Testovaci poznamky" and nt["based_on_parts"] == ["p01"] and nt["model"] == "claude-fake-1"
    # inbox was emptied and recreated as a fresh orphan (no old audio reachable from the ref)
    assert inbox_files() == [] and gh.refs["inbox"] != old_inbox_tip
    assert gh.commits[gh.refs["inbox"]]["parents"] == []
    hb = main_json("worker/heartbeat.json")
    assert contract.problems("worker", hb) == [] and hb["state"] == "idle" and hb["worker_id"] == "test-laptop"
    # no audio on main, ever
    assert all(not p.endswith((".wav", ".opus")) for p in gh.files("main"))
    # commit messages never contain titles (generic text only)
    assert all("Testovaci" not in c["message"] for c in gh.commits.values())


def test_status_progresses_through_states(worker, asr, gh):
    job = put_job(durations=(2.0, 1.0), diarize=False)
    seen = []

    def hook(i, d):
        seen.append(main_json(f"notes/{job['note_id']}/status.json"))
    asr.hook = hook
    worker.drain()
    assert [s["state"] for s in seen] == ["transcribing", "transcribing"]
    assert seen[0]["progress"]["part_index"] == 1 and seen[0]["progress"]["part_total"] == 2 and seen[0]["attempt"] == 1
    assert seen[1]["progress"]["part_index"] == 2 and seen[1]["progress"]["part_id"] == "p02"
    assert seen[1]["parts"][0]["state"] == "transcribed"
    for s in seen:
        assert contract.problems("status", s) == []


def test_multi_part_merge_and_append(worker, asr, cfg, gh):
    note = ulid()
    j1 = put_job(note_id=note, durations=(3.0, 2.0), created="2026-10-01T08:00:00Z")
    worker.drain()
    tr = main_json(f"notes/{note}/transcript.json")
    assert [p["part_id"] for p in tr["parts"]] == ["p01", "p02"]
    assert [p["offset"] for p in tr["parts"]] == [0, 3.0] and tr["merged"]["duration"] == 5.0
    assert "[Část 1]" in tr["merged"]["text"] and "\n[Část 2]\n" in tr["merged"]["text"]
    assert tr["merged"]["text"].index("[Část 1]") < tr["merged"]["text"].index("[Část 2]")
    first_calls = list(asr.calls)

    # add a part later (append): only the new part is transcribed, old segments are kept
    j2 = put_job(note_id=note, durations=(4.0,), append=True, first_order=3, created="2026-10-02T08:00:00Z")
    worker.drain()
    assert asr.calls == first_calls + [4.0]
    tr2 = main_json(f"notes/{note}/transcript.json")
    assert [p["part_id"] for p in tr2["parts"]] == ["p01", "p02", "p03"]
    assert [p["offset"] for p in tr2["parts"]] == [0, 3.0, 5.0] and tr2["merged"]["duration"] == 9.0
    assert tr2["parts"][0]["segments"] == tr["parts"][0]["segments"] and tr2["parts"][0]["job_id"] == j1["job_id"]
    assert tr2["parts"][2]["job_id"] == j2["job_id"]
    nt = main_json(f"notes/{note}/notes.json")
    assert nt["based_on_parts"] == ["p01", "p02", "p03"] and nt["job_id"] == j2["job_id"]
    assert "[Část 3]" in Path(cfg.claude_path).parent.joinpath("last_stdin.txt").read_text()
    st = main_json(f"notes/{note}/status.json")
    assert st["completed_job_ids"] == [j1["job_id"], j2["job_id"]]
    assert inbox_files() == []


def test_regenerate_notes_only(worker, asr, cfg, gh):
    note = ulid()
    put_job(note_id=note, durations=(2.0,), created="2026-10-01T08:00:00Z")
    worker.drain()
    n_asr, n_claude = len(asr.calls), claude_calls(cfg)
    tr_before = main_json(f"notes/{note}/transcript.json")
    rj = put_job(note_id=note, action="regenerate_notes", created="2026-10-02T08:00:00Z")
    assert rj["parts"] == []
    worker.drain()
    assert len(asr.calls) == n_asr                       # no transcription
    assert claude_calls(cfg) == n_claude + 1             # notes re-run
    nt = main_json(f"notes/{note}/notes.json")
    assert nt["job_id"] == rj["job_id"]
    tr_after = main_json(f"notes/{note}/transcript.json")
    assert tr_after["parts"] == tr_before["parts"] and tr_after["merged"] == tr_before["merged"]
    st = main_json(f"notes/{note}/status.json")
    assert st["state"] == "done" and st["action"] == "regenerate_notes" and len(st["completed_job_ids"]) == 2
    assert inbox_files() == []


def test_regenerate_without_transcript_fails_clearly(worker, gh):
    j = put_job(action="regenerate_notes")
    worker.drain()
    st = main_json(f"notes/{j['note_id']}/status.json")
    assert st["state"] == "failed" and st["error"]["code"] == "no_transcript" and st["error"]["retryable"] is False


def test_many_queued_jobs_oldest_first(worker, asr, gh):
    # upload order deliberately differs from created_at order; two jobs share created_at (ULID tie-break)
    specs = [(5, "2026-10-01T10:00:00Z"), (1, "2026-10-01T08:00:00Z"), (4, "2026-10-01T09:30:00Z"),
             (2, "2026-10-01T08:30:00Z"), (3, "2026-10-01T09:00:00Z"), (6, "2026-10-01T10:00:00Z")]
    jobs = {n: put_job(note_id=ulid(100 + n), job_id=ulid(200 + n), durations=(1.0 + n / 10,), created=c) for n, c in specs}
    queued_seen = {}

    def hook(i, d):
        if i == 1:     # while the first job runs, every other job already has a queued status with its position
            for n, j in jobs.items():
                st = main_json(f"notes/{j['note_id']}/status.json")
                queued_seen[n] = (st["state"], st.get("queue_position"))
    asr.hook = hook
    assert worker.drain() == 6
    assert done_order() == [jobs[n]["note_id"] for n in (1, 2, 3, 4, 5, 6)]
    assert queued_seen[1][0] == "transcribing"
    assert [queued_seen[n] for n in (2, 3, 4, 5, 6)] == [("queued", p) for p in (2, 3, 4, 5, 6)] or \
        [queued_seen[n][0] for n in (2, 3, 4, 5, 6)] == ["queued"] * 5
    assert inbox_files() == []
    # branch only recreated once the last one finished: earlier cleanups were plain commits
    commits_msgs = [c["message"] for c in gh.commits.values()]
    assert commits_msgs.count("remove processed audio") == 5


def test_job_without_job_json_is_ignored_until_complete(worker, asr, gh):
    j = put_job(durations=(1.0,), upload_job=False)       # parts uploaded, job.json not yet (still uploading)
    assert worker.drain() == 0 and asr.calls == []
    gh.commit_files("inbox", {f"jobs/{j['note_id']}/job.json": json.dumps(j).encode()}, "job")
    assert worker.drain() == 1


def test_meeting_variant_has_decisions_and_actions(worker, cfg, gh):
    j = put_job(kind="meeting", durations=(2.0,))
    worker.drain()
    nt = main_json(f"notes/{j['note_id']}/notes.json")
    assert nt["kind"] == "meeting" and contract.problems("notes", nt) == []
    assert nt["content"]["decisions"] == ["rozhodnuti A"] and nt["content"]["action_items"][0]["owner"] == "Petra"
    assert "kind: meeting" in Path(cfg.claude_path).parent.joinpath("last_stdin.txt").read_text()


def test_lesson_never_gets_meeting_fields(worker, cfg, gh):
    set_claude_mode(cfg, "extra_fields_lesson")
    j = put_job(kind="lesson", durations=(1.0,))
    worker.drain()
    nt = main_json(f"notes/{j['note_id']}/notes.json")
    assert "decisions" not in nt["content"] and contract.problems("notes", nt) == []


def test_diarization_labels_speakers(make_worker, asr, gh, cfg):
    cfg.models_diar.mkdir(parents=True, exist_ok=True)
    cfg.diar_embedding.write_bytes(b"x")
    cfg.diar_segmentation.parent.mkdir(parents=True, exist_ok=True)
    cfg.diar_segmentation.write_bytes(b"x")
    seen = {}

    def fake_diar(c, audio, num_speakers):
        seen["num_speakers"] = num_speakers
        return [{"start": 0.0, "end": 1.0, "speaker": "SPK_7"}, {"start": 1.0, "end": 4.0, "speaker": "SPK_2"}]
    w = make_worker(diarizer=fake_diar)
    j = put_job(kind="meeting", diarize=True, durations=(4.0,), num_speakers=2)
    states = []
    asr.hook = lambda i, d: None
    w.drain()
    tr = main_json(f"notes/{j['note_id']}/transcript.json")
    assert seen["num_speakers"] == 2
    assert [s["speaker"] for s in tr["parts"][0]["segments"]] == ["S1", "S2"]       # first appearance order
    assert tr["diarization"]["enabled"] is True and tr["diarization"]["speaker_scope"] == "per_part"
    assert tr["merged"]["text"] == "[Část 1]\n[S1] Prvni veta delky 4. [S2] Druha veta."
    assert contract.problems("transcript", tr) == []
    st = main_json(f"notes/{j['note_id']}/status.json")
    assert st["state"] == "done" and st["parts"][0]["state"] == "diarized"


def test_diarization_failure_is_best_effort(make_worker, gh, cfg):
    cfg.diar_embedding.parent.mkdir(parents=True, exist_ok=True)
    cfg.diar_embedding.write_bytes(b"x")
    cfg.diar_segmentation.parent.mkdir(parents=True, exist_ok=True)
    cfg.diar_segmentation.write_bytes(b"x")

    def boom(*a):
        raise RuntimeError("onnx exploded")
    w = make_worker(diarizer=boom)
    j = put_job(kind="meeting", diarize=True, durations=(2.0,))
    w.drain()
    tr = main_json(f"notes/{j['note_id']}/transcript.json")
    assert main_json(f"notes/{j['note_id']}/status.json")["state"] == "done"
    assert tr["diarization"]["enabled"] is False and "speaker" not in tr["parts"][0]["segments"][0]


def test_diarization_missing_models_is_best_effort(worker, gh):
    j = put_job(kind="meeting", diarize=True, durations=(2.0,))
    worker.drain()
    assert main_json(f"notes/{j['note_id']}/status.json")["state"] == "done"
    assert main_json(f"notes/{j['note_id']}/transcript.json")["diarization"]["enabled"] is False


def test_local_audio_job(make_worker, cfg, asr, gh):
    j = put_job(location="local", local_dir=cfg.local_audio_dir, durations=(2.0,), kind="meeting")
    w = make_worker()
    w.drain()
    st = main_json(f"notes/{j['note_id']}/status.json")
    assert st["state"] == "done" and st["audio"]["state"] == "purged"
    assert not (cfg.local_audio_dir / j["note_id"]).exists()      # local audio deleted after done
    assert inbox_files() == []


def test_local_cleanup_keeps_parts_of_a_later_job(make_worker, cfg, asr, gh):
    """The laptop recorder may store the next part of a note while the previous job is still being processed."""
    j = put_job(location="local", local_dir=cfg.local_audio_dir, durations=(2.0,))
    later = cfg.local_audio_dir / j["note_id"] / "parts" / "02.opus"
    later.write_bytes(b"audio of a later job")
    make_worker().drain()
    assert main_json(f"notes/{j['note_id']}/status.json")["state"] == "done"
    assert not (cfg.local_audio_dir / j["note_id"] / "parts" / "01.wav").exists()
    assert later.read_bytes() == b"audio of a later job"
