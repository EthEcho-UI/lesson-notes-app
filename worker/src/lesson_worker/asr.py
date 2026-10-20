"""faster-whisper large-v3-turbo int8 on CPU (benchmark b0a: RTF ~0.33, WER 11-14 % on Czech FLEURS)."""
import logging
import threading
import time

log = logging.getLogger("lesson_worker.asr")


def model_path(cfg, local_only=True):
    """Local snapshot dir of the Whisper model (HF cache layout under models_dir/whisper)."""
    from huggingface_hub import snapshot_download
    return snapshot_download(cfg.whisper_repo, cache_dir=str(cfg.models_whisper), local_files_only=local_only,
                             allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"])


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

    def transcribe(self, audio, duration, language="cs", progress=None):
        """audio: float32 16 kHz mono array. Returns [{start,end,text}] with times in seconds.
        progress(percent) is called after every segment."""
        with self._lock:
            m = self._get()
            kw = dict(language=language, beam_size=5, vad_filter=True, condition_on_previous_text=False)
            if self.cfg.whisper_initial_prompt:
                kw["initial_prompt"] = self.cfg.whisper_initial_prompt
            segs, _info = m.transcribe(audio, **kw)
            out = []
            for s in segs:
                t = s.text.strip()
                if t:
                    out.append({"start": round(max(s.start, 0.0), 2), "end": round(max(s.end, s.start), 2), "text": t})
                if progress and duration > 0:
                    progress(min(100.0, 100.0 * s.end / duration))
            self._last_used = self._mono()
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
