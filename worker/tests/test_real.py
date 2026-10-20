"""Slow tests with REAL components: Whisper large-v3-turbo + sherpa-onnx diarization on public FLEURS Czech clips,
and ONE real `claude -p` call (costs ~0.05 USD; opt-in with LESSON_REAL_CLAUDE=1)."""
import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest

import fake_github as fg
from helpers import main_json, put_job, inbox_files
from lesson_worker import contract
from lesson_worker.asr import Transcriber
from lesson_worker.config import Config
from lesson_worker.ghapi import GitHub
from lesson_worker.notes import generate_notes
from lesson_worker.worker import Worker

SPIKES = Path(os.environ.get("LESSON_TEST_SPIKES", Path(__file__).resolve().parents[3] / "spikes"))
EVAL = SPIKES / "b0a-whisper-bench/data/eval"
HF = Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
DIAR = SPIKES / "b0b-diarization-bench/models"


def wer(ref, hyp):
    norm = lambda t: re.sub(r"[^\w\s]", "", t.lower()).split()
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[-1] / max(len(r), 1)


@pytest.mark.real_whisper
def test_end_to_end_with_real_whisper_and_diarization(cfg, clock, gh):
    if not EVAL.is_dir() or not (HF / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo").is_dir():
        pytest.skip("FLEURS clips or the cached Whisper model are not available on this machine")
    refs = {e["file"]: e for e in json.loads((EVAL / "eval.json").read_text())}
    ok = [n for n in refs if 9 <= refs[n]["dur"] <= 16 and 1.5 <= len(refs[n]["ref"].split()) / refs[n]["dur"] <= 3.5]   # reference text matches audio
    names = sorted(ok)[:2]                                                      # two ~10-15 s clips
    assert len(names) == 2
    cfg.models_whisper.parent.mkdir(parents=True, exist_ok=True)
    cfg.models_whisper.symlink_to(HF, target_is_directory=True)                 # reuse the already-downloaded model
    cfg.models_diar.mkdir(parents=True)
    if DIAR.is_dir():
        (cfg.models_diar / cfg.diar_embedding.name).symlink_to(DIAR / cfg.diar_embedding.name)
        (cfg.models_diar / "sherpa-onnx-pyannote-segmentation-3-0").symlink_to(DIAR / "sherpa-onnx-pyannote-segmentation-3-0", target_is_directory=True)
    audio = [(EVAL / n).read_bytes() for n in names]
    job = put_job(kind="meeting", diarize=True, durations=tuple(refs[n]["dur"] for n in names), audio=audio)
    asr = Transcriber(cfg)
    w = Worker(cfg, GitHub(cfg.repo, cfg.read_token(), cfg.api_base), clock=clock, asr=asr)
    t = time.time()
    assert w.drain() == 1
    took = time.time() - t
    note = job["note_id"]
    st = main_json(f"notes/{note}/status.json")
    assert st["state"] == "done", st.get("error")
    tr = main_json(f"notes/{note}/transcript.json")
    assert contract.problems("transcript", tr) == []
    assert tr["engine"]["name"] == "faster-whisper" and tr["engine"]["model"] == "large-v3-turbo"
    scores = []
    for part, n in zip(tr["parts"], names):
        hyp = " ".join(s["text"] for s in part["segments"])
        scores.append(wer(refs[n]["ref"], hyp))
        print("REF:", refs[n]["ref"], "\nHYP:", hyp)
        assert abs(part["duration"] - refs[n]["dur"]) < 0.5
    total = wer(" ".join(refs[n]["ref"] for n in names), " ".join(s["text"] for p in tr["parts"] for s in p["segments"]))
    print(f"\nreal e2e: {sum(refs[n]['dur'] for n in names):.1f}s audio in {took:.1f}s wall; WER per clip {scores}, combined {total:.3f}; diarization enabled={tr['diarization']['enabled']}")
    assert total < 0.30          # benchmark b0a: 11-14 % on FLEURS; two short clips are noisy
    if DIAR.is_dir():
        assert tr["diarization"]["enabled"] is True
        assert all(re.fullmatch(r"S\d+", s["speaker"]) for p in tr["parts"] for s in p["segments"] if "speaker" in s)
    assert inbox_files() == []
    assert main_json(f"notes/{note}/notes.json")["content"]["decisions"]


@pytest.mark.real_claude
@pytest.mark.skipif(os.environ.get("LESSON_REAL_CLAUDE") != "1", reason="set LESSON_REAL_CLAUDE=1 to run the real `claude -p` smoke test (~0.05 USD)")
def test_real_claude_smoke(tmp_path):
    cfg = Config(cache_dir=tmp_path / "cache")           # real claude: ~/.local/bin/claude, real login, real HOME
    cfg.validate_paths()
    text = ("[Část 1]\nDobrý den, dnes si povíme o fotosyntéze. Fotosyntéza je proces, při kterém rostliny přeměňují světelnou "
            "energii na chemickou. Probíhá v chloroplastech a potřebuje oxid uhličitý, vodu a světlo. Na příští hodinu si "
            "připravte referát o Calvinově cyklu. V pátek píšeme krátký test z druhé kapitoly. Kdo neví, proč je list zelený?")
    t = time.time()
    content, model = generate_notes(cfg, "lesson", text)
    print(f"\nreal claude: {time.time() - t:.1f}s model={model}\n{json.dumps(content, ensure_ascii=False, indent=1)[:900]}")
    notes_doc = {"schema_version": 1, "note_id": "01K8F4A0B1C2D3E4F5G6H7J8K9", "kind": "lesson", "job_id": "01K8F4A9M3N5P7Q9R1S3T5V7W9",
                 "generated_at": "2026-10-21T10:00:00Z", "model": model, "based_on_parts": ["p01"], "content": content}
    assert contract.problems("notes", notes_doc) == []
    assert "fotosyn" in json.dumps(content, ensure_ascii=False).lower()
    assert any("referát" in t.lower() or "test" in t.lower() for t in content["todo"])
