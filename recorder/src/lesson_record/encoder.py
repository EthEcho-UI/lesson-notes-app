"""PCM -> Ogg Opus (mono 16 kHz, ~24 kbps, voip). Pages are flushed every 0.2 s, so a killed process leaves a valid file
that is at most a fraction of a second short (measured: kill -9 after 3.25 s of audio -> 3.0 s decodable)."""
import subprocess

from .common import with_parent_death


class EncoderError(Exception):
    pass


class Encoder:
    def __init__(self, path, bitrate="24k"):
        self.path = str(path)
        self.samples = 0
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
               "-c:a", "libopus", "-b:a", bitrate, "-application", "voip",
               "-flush_packets", "1", "-page_duration", "200000", "-f", "opus", self.path]
        try:
            self.proc = subprocess.Popen(with_parent_death(cmd), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError:
            raise EncoderError("ffmpeg is not installed")

    def write(self, pcm: bytes):
        try:
            self.proc.stdin.write(pcm)
        except (BrokenPipeError, ValueError, OSError) as e:
            raise EncoderError(f"encoder died: {e}")
        self.samples += len(pcm) // 2

    @property
    def pid(self):
        return self.proc.pid

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
