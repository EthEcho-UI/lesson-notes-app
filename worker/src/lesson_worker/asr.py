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


_EN = frozenset("the and of to in is that it for with as was on are this be at by from or an have not but what all were when we there can your which their if do will each about how up out them then she many some so these would other into has more her two like him see time could no make than first been its who now people my made over did down only way find use may water long little very after words called just where most know".split())
_CS = frozenset("a i o u v s k z se na je to že si by ale jako pro od do po při tak jak co ten ta že nebo jsou byl byla bylo být má mají tady když jsem jsme jsi není ještě už také jen než které který která kde proto potom protože tedy tohle toho".split())


def text_language_guess(text):
    """Cheap hint (not a decision) which of cs/en a decoded text looks like: Czech diacritics or Czech function words
    -> cs, mostly English function words and no diacritics -> en, otherwise None. Used only to decide where a
    (costly) audio language-id check is worthwhile."""
    words = [w.strip(".,;:!?()\"'").lower() for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return None
    if any(c in "ěščřžýáíéúůďťňó" for c in text.lower()):
        return "cs"
    en = sum(w in _EN for w in words)
    cs = sum(w in _CS for w in words)
    if en >= 2 and en > cs:
        return "en"
    return "cs" if cs > en else None


def mixed_key(cfg):
    """Everything that changes the result of a mixed-mode run (part of the ASR cache key)."""
    return [cfg.mixed_first_pass, cfg.mixed_check, cfg.mixed_min_logprob, cfg.mixed_switch_prob, cfg.mixed_check_min_s, 3]


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
    def _langid(self, audio, seg):
        a = audio[max(0, int((seg["start"] - 0.1) * SR)): int((seg["end"] + 0.1) * SR)]
        return self._detect(a)

    def _detect(self, a):
        lang, p, allp = self._model.detect_language(a)
        d = dict(allp)
        return lang, p, d.get("cs", 0.0), d.get("en", 0.0)

    def _probe_first_language(self, audio):
        """cs or en for the first pass: language-id on up to 3 speech windows spread over the recording (~2.5 s each)."""
        from faster_whisper.vad import get_speech_timestamps
        try:
            chunks = get_speech_timestamps(audio)
        except Exception:
            chunks = []
        if not chunks:
            return "cs"
        picks = {chunks[min(len(chunks) - 1, int(len(chunks) * q))]["start"] for q in (0.15, 0.5, 0.85)}
        pc = pe = 0.0
        for st in sorted(picks):
            _top, _p, c, e = self._detect(audio[st: st + 30 * SR])
            pc, pe = pc + c, pe + e
        first = "en" if pe > pc else "cs"
        log.info("mixed: first-pass language %s (probe cs=%.2f en=%.2f over %d windows)", first, pc / len(picks), pe / len(picks), len(picks))
        return first

    def _mixed(self, audio, duration, progress, hint):
        cfg = self.cfg
        first = cfg.mixed_first_pass if cfg.mixed_first_pass in ("cs", "en") else self._probe_first_language(audio)
        other = "en" if first == "cs" else "cs"
        segs = self._decode(audio, first, hint, progress=progress, duration=duration, lo=0.0, hi=85.0)
        n = len(segs)
        labels = []                                   # per segment: [lang | None (= resolve later), lang_detected | None]
        for k, sg in enumerate(segs):
            lang, det = first, None
            if sg["end"] - sg["start"] >= cfg.mixed_check_min_s and self._suspicious(sg, other):
                top, p, pcs, pen = self._langid(audio, sg)
                if top == other and p >= cfg.mixed_switch_prob:
                    lang = other
                elif top not in (first, other):
                    lang, det = None, top             # third language (sk, pl, ru ...): resolved with the dominant cs/en language
            labels.append([lang, det])
            if progress:
                progress(85.0 + 15.0 * (k + 1) / max(n, 1))
        tot = {"cs": 0.0, "en": 0.0}                  # dominant cs/en language by speech time (tie -> cs)
        for sg, (lang, _d) in zip(segs, labels):
            if lang:
                tot[lang] += sg["end"] - sg["start"]
        dominant = "en" if tot["en"] > tot["cs"] else "cs"
        out = []
        for sg, (lang, det) in zip(segs, labels):
            lang = lang or dominant
            text = sg["text"]
            if lang != first:
                text = self._redo(audio, sg, lang, hint) or text
            d = {"start": sg["start"], "end": sg["end"], "text": text, "lang": lang}
            if det and det != lang:
                d["lang_detected"] = det
            out.append(d)
        return out

    def _suspicious(self, sg, other):
        cfg = self.cfg
        return (cfg.mixed_check == "all" or sg["lp"] < cfg.mixed_min_logprob or sg["cr"] > 2.4
                or text_language_guess(sg["text"]) == other)

    def _redo(self, audio, seg, lang, hint):
        a = audio[max(0, int((seg["start"] - 0.15) * SR)): int((seg["end"] + 0.15) * SR)]
        got = self._decode(a, lang, hint, vad=False)
        return " ".join(g["text"] for g in got).strip()

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
