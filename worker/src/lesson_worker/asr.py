"""faster-whisper large-v3-turbo int8 on CPU (benchmark b0a: RTF ~0.33, WER 11-14 % on Czech FLEURS).

Language modes (contract E): `cs`/`en` force the language. `mixed` = a Czech-first pass (forced, best Czech quality), then a
language-id check on suspicious segments only; segments the detector assigns to English are re-decoded as English, segments
assigned to another language (sk, pl, ...) are re-decoded with the dominant cs/en language. Measurements: output/b7-REPORT.md."""
import logging
import threading
import time

log = logging.getLogger("lesson_worker.asr")


def model_path(cfg, local_only=True):
    """Local snapshot dir of the Whisper model (HF cache layout under models_dir/whisper)."""
    from huggingface_hub import snapshot_download
    return snapshot_download(cfg.whisper_repo, cache_dir=str(cfg.models_whisper), local_files_only=local_only,
                             allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"])


SR = 16000


def build_hint(cfg, ctx):
    """Short vocabulary hint for Whisper from the note context (subject, topic, details), at most whisper_hint_chars."""
    if cfg.whisper_hint_mode == "off" or not ctx:
        return ""
    bits = [" ".join(str(ctx.get(k) or "").split()).rstrip(".") for k in ("subject", "topic", "details")]
    text = ". ".join(b for b in bits if b)
    if len(text) > cfg.whisper_hint_chars:
        text = text[:cfg.whisper_hint_chars].rsplit(" ", 1)[0]
    return text


def mixed_key(cfg):
    """Everything that changes the result of a mixed-mode run (part of the ASR cache key)."""
    return [cfg.mixed_gap_ms, cfg.mixed_max_unit_s, cfg.mixed_min_unit_s, 4]


class Transcriber:
    """Lazily loads the model; unloads it after `idle_unload_s` without work (frees ~3 GB)."""

    def __init__(self, cfg, mono=time.monotonic):
        self.cfg = cfg
        self._model = None
        self._last_used = 0.0
        self._mono = mono
        self._lock = threading.Lock()

    def _get(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            path = model_path(self.cfg, local_only=True)
            t = time.time()
            self._model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=self.cfg.whisper_threads)
            log.info("Whisper model loaded in %.1fs", time.time() - t)
        return self._model

    def release_if_idle(self):
        if self._model is not None and self._mono() - self._last_used > self.cfg.whisper_idle_unload_s:
            self._model = None
            log.info("Whisper model unloaded (idle)")

    # ---- decoding ---------------------------------------------------------------------------------
    def _opts(self, hint):
        kw = dict(beam_size=5, condition_on_previous_text=False)
        prompt = self.cfg.whisper_initial_prompt
        if hint and self.cfg.whisper_hint_mode == "initial_prompt":
            prompt = (prompt + " " + hint).strip()
        elif hint and self.cfg.whisper_hint_mode == "hotwords":
            kw["hotwords"] = hint
        if prompt:
            kw["initial_prompt"] = prompt
        return kw

    def _decode(self, audio, language, hint, vad=True, progress=None, duration=0.0, lo=0.0, hi=100.0):
        """Run Whisper; returns [{start,end,text,lp,cr,ns}] (times in seconds of `audio`)."""
        segs, _info = self._model.transcribe(audio, language=language, vad_filter=vad, **self._opts(hint))
        out = []
        for s in segs:
            t = s.text.strip()
            if t:
                out.append({"start": round(max(s.start, 0.0), 2), "end": round(max(s.end, s.start), 2), "text": t,
                            "lp": s.avg_logprob, "cr": s.compression_ratio, "ns": s.no_speech_prob})
            if progress and duration > 0:
                progress(min(hi, lo + (hi - lo) * s.end / duration))
        return out

    def transcribe(self, audio, duration, language="cs", progress=None, hint=""):
        """audio: float32 16 kHz mono array. language: cs | en | mixed (or another ISO code, forced).
        Returns [{start,end,text,lang[,lang_detected]}] with times in seconds. progress(percent) after every segment."""
        with self._lock:
            self._get()
            if language == "mixed":
                out = self._mixed(audio, duration, progress, hint)
            else:
                out = [{"start": s["start"], "end": s["end"], "text": s["text"], "lang": language}
                       for s in self._decode(audio, language, hint, progress=progress, duration=duration)]
            self._last_used = self._mono()
            return out

    # ---- mixed ------------------------------------------------------------------------------------
    def _detect(self, a):
        lang, p, allp = self._model.detect_language(a)
        d = dict(allp)
        return lang, p, d.get("cs", 0.0), d.get("en", 0.0)

    def _units(self, audio):
        """Utterances: speech chunks separated by >= mixed_gap_ms of silence (silero VAD), at most mixed_max_unit_s long.
        [(start_sample, end_sample)]"""
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        cfg = self.cfg
        opts = VadOptions(min_silence_duration_ms=int(cfg.mixed_gap_ms), max_speech_duration_s=float(cfg.mixed_max_unit_s), speech_pad_ms=150)
        return [(c["start"], c["end"]) for c in get_speech_timestamps(audio, opts)]

    def _mixed(self, audio, duration, progress, hint):
        """Per-utterance language: split at pauses, language-id each utterance (cs / en / other), decode each in its
        language. Utterances detected as a third language, and ones too short to judge (< mixed_min_unit_s), get the
        dominant cs/en language (by speech time, tie -> cs; a too-short one follows the previous utterance)."""
        cfg = self.cfg
        units = self._units(audio)
        total = max(sum(e - s for s, e in units), 1)
        det, done = [], 0
        for s0, e0 in units:                                  # phase 1: language-id (0..30 %)
            if (e0 - s0) / SR >= cfg.mixed_min_unit_s:
                top, _p, pcs, pen = self._detect(audio[s0:e0])
                det.append(top)
            else:
                det.append(None)
            done += e0 - s0
            if progress:
                progress(30.0 * done / total)
        tot = {"cs": 0.0, "en": 0.0}
        for (s0, e0), d in zip(units, det):
            if d in tot:
                tot[d] += e0 - s0
        dominant = "en" if tot["en"] > tot["cs"] else "cs"
        out, prev, done = [], dominant, 0
        for (s0, e0), d in zip(units, det):                   # phase 2: decode in the final language (30..100 %)
            lang = d if d in ("cs", "en") else (prev if d is None else dominant)
            prev = lang
            pad0 = max(0, s0 - int(0.15 * SR))
            for g in self._decode(audio[pad0:e0 + int(0.15 * SR)], lang, hint):
                seg = {"start": round(pad0 / SR + g["start"], 2), "end": round(pad0 / SR + g["end"], 2), "text": g["text"], "lang": lang}
                if d not in (None, "cs", "en"):
                    seg["lang_detected"] = d
                out.append(seg)
            done += e0 - s0
            if progress:
                progress(30.0 + 70.0 * done / total)
        return out

    @property
    def engine(self):
        try:
            from importlib.metadata import version
            v = version("faster-whisper")
        except Exception:
            v = None
        d = {"name": "faster-whisper", "model": self.cfg.whisper_model}
        if v:
            d["version"] = v
        return d
