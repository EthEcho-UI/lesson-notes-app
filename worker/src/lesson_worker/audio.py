"""ffmpeg conversion to 16 kHz mono PCM and loading as float32."""
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


class AudioCorrupt(Exception):
    pass


# Uploaded files are untrusted: only plain local files and real audio containers may be opened, so a crafted
# "audio" file (HLS/concat playlist, http/tcp reference, ...) cannot make ffmpeg read other local files or call the network.
_AUDIO_DEMUXERS = "matroska,webm,ogg,mp3,mov,mp4,m4a,3gp,wav,flac,aac"


def to_wav16k(src: Path, dst: Path, timeout=3600) -> float:
    """Convert any ffmpeg-readable file to 16 kHz mono s16 WAV. Returns the duration in seconds."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    tmp = dst.with_suffix(".tmp.wav")
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-y",
                            "-protocol_whitelist", "file", "-format_whitelist", _AUDIO_DEMUXERS, "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AudioCorrupt("ffmpeg timed out converting the audio")
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise AudioCorrupt("ffmpeg could not decode the audio: " + r.stderr.decode("utf-8", "replace")[-300:].strip())
    tmp.replace(dst)
    dur = wav_duration(dst)
    if dur <= 0:
        raise AudioCorrupt("decoded audio is empty")
    return dur


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def load_f32(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
