"""Build eval input 6: REAL faster-whisper output on public Czech speech (FLEURS clips from spike b0a).
Concatenates N clips into one recording and transcribes it with the small model (noisier than large-v3-turbo,
on purpose: realistic ASR errors). Usage: python tools/real_asr.py"""
import json, subprocess, sys, tempfile, wave
from pathlib import Path
from faster_whisper import WhisperModel
DATA = Path(__file__).resolve().parents[3].parent / "spikes/b0a-whisper-bench/data/eval"
N = 24
clips = sorted(DATA.glob("*.wav"))[:N]
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "all.wav"
    with wave.open(str(out), "wb") as w:
        for i, c in enumerate(clips):
            with wave.open(str(c)) as r:
                if i == 0:
                    w.setparams(r.getparams())
                w.writeframes(r.readframes(r.getnframes()))
                w.writeframes(b"\0\0" * int(r.getframerate() * 0.6))
    m = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)
    segs, info = m.transcribe(str(out), language="cs", beam_size=1, vad_filter=True, condition_on_previous_text=False)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(), "lang": "cs"} for s in segs]
text = "[Část 1]\n" + " ".join(s["text"] for s in segs)
inp = {"name": "06_real_asr_cs", "kind": "lesson", "language_mode": "cs", "notebook": "Společenské vědy",
       "context": {"subject": "Společenské vědy", "topic": "Různá témata (veřejné čtené věty z FLEURS)", "details": "Nahrávka je sada nesouvisejících čtených vět, ne souvislá hodina."},
       "transcript": text, "photos": [], "real_asr": True, "duration_s": round(info.duration, 1)}
Path(__file__).resolve().parents[1].joinpath("inputs/06_real_asr_cs.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1))
print(len(text.split()), "words", info.duration, "s")
