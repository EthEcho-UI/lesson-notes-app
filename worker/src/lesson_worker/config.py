"""Configuration: ~/.config/lesson-notes/worker.toml (no secrets in it; the token lives in a 0600 file)."""
import os
import socket
import stat
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def default_config_path() -> Path:
    return Path(os.environ.get("LESSON_WORKER_CONFIG") or home() / ".config/lesson-notes/worker.toml")


@dataclass
class Config:
    repo: str = "EthEcho-UI/lesson-notes-data"
    token_file: Path = field(default_factory=lambda: home() / ".config/lesson-notes/token")
    api_base: str = "https://api.github.com"
    worker_id: str = field(default_factory=lambda: socket.gethostname() or "laptop")
    data_dir: Path = field(default_factory=lambda: home() / ".local/share/lesson-notes")
    models_dir: Path = field(default_factory=lambda: home() / ".local/share/lesson-notes/models")
    cache_dir: Path = field(default_factory=lambda: home() / ".cache/lesson-notes")
    claude_path: str = ""                      # empty: ~/.local/bin/claude, then PATH
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: str = "high"                # --effort level for the notes call (low|medium|high|xhigh|max)
    claude_timeout_s: int = 1800               # thinking + figures + photos take minutes
    claude_attempts: int = 2
    claude_retry_wait_s: float = 5.0           # pause between claude attempts (x6 after a rate limit)
    poll_interval_s: float = 20.0
    heartbeat_interval_s: float = 600.0
    whisper_model: str = "large-v3-turbo"
    whisper_repo: str = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    whisper_threads: int = 8
    whisper_initial_prompt: str = ""           # static text always passed to Whisper (before the per-note hint)
    whisper_hint_mode: str = "hotwords"        # per-note context -> Whisper: hotwords (every window) | initial_prompt (first window only) | off
    whisper_hint_chars: int = 400              # max length of the per-note vocabulary hint (~130-200 tokens)
    mixed_first_pass: str = "auto"             # first decoding pass in language_mode=mixed: auto (language-id probe on 3 windows) | cs | en
    mixed_check: str = "hybrid"                # mixed strategy: hybrid (forced pass + language-id on suspicious segments) | all (language-id on every segment)
    mixed_min_logprob: float = -0.7            # hybrid: segments with avg_logprob below this get a language-id check
    mixed_switch_prob: float = 0.6             # a segment switches language only if the detector gives the other cs/en language at least this probability
    mixed_check_min_s: float = 1.0             # segments shorter than this are not checked (language-id is unreliable)
    whisper_idle_unload_s: float = 600.0
    diarize_threshold: float = 0.9
    diarize_threads: int = 4
    diarize_min_speaker_s: float = 20.0
    max_attempts: int = 3
    retry_backoff_s: tuple = (60.0, 300.0, 900.0)
    failed_audio_retention_days: int = 14      # fallback when settings/app.json is unreadable
    cache_retention_days: int = 14
    image_gc_interval_s: float = 3600.0        # how often the janitor looks for orphan photos on main

    def validate_paths(self):
        for k in ("token_file", "data_dir", "models_dir", "cache_dir"):
            setattr(self, k, Path(os.path.expanduser(str(getattr(self, k)))))

    @property
    def local_audio_dir(self) -> Path:
        """audio_location=local jobs: parts live in <local_audio_dir>/<noteId>/parts/NN.ext"""
        return self.data_dir / "local"

    @property
    def models_whisper(self) -> Path:
        return self.models_dir / "whisper"

    @property
    def models_diar(self) -> Path:
        return self.models_dir / "diarization"

    @property
    def diar_segmentation(self) -> Path:
        return self.models_diar / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"

    @property
    def diar_embedding(self) -> Path:
        return self.models_diar / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"

    def read_token(self) -> str:
        return read_token(self.token_file)

    def find_claude(self) -> str:
        import shutil
        for c in (self.claude_path, str(home() / ".local/bin/claude"), shutil.which("claude") or ""):
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return os.path.abspath(c)
        raise ConfigError("claude executable not found (set claude_path in worker.toml)")


def read_token(path: Path) -> str:
    """Token file must exist, be a regular file owned by us and not accessible by group/other (0600)."""
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ConfigError(f"token file {path} not found (create it with the fine-grained GitHub token, chmod 600)")
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"token file {path} is not a regular file")
    if st.st_mode & 0o077:
        raise ConfigError(f"token file {path} has mode {st.st_mode & 0o777:04o}; run: chmod 600 {path}")
    if st.st_uid != os.getuid():
        raise ConfigError(f"token file {path} is not owned by the current user")
    tok = path.read_text(encoding="utf-8").strip()
    if not tok:
        raise ConfigError(f"token file {path} is empty")
    return tok


def load(path=None) -> Config:
    p = Path(path) if path else default_config_path()
    cfg = Config()
    if p.exists():
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: {e}")
        known = {f.name: f for f in fields(Config)}
        for k, v in data.items():
            if k not in known:
                raise ConfigError(f"{p}: unknown setting '{k}'")
            if k == "retry_backoff_s":
                v = tuple(float(x) for x in v)
            setattr(cfg, k, v)
    cfg.validate_paths()
    return cfg


EXAMPLE = """\
# ~/.config/lesson-notes/worker.toml   (no secrets here)
repo = "EthEcho-UI/lesson-notes-data"
token_file = "~/.config/lesson-notes/token"      # fine-grained PAT, Contents read+write on that one repo; chmod 600
# claude_path = "/home/you/.local/bin/claude"     # default: ~/.local/bin/claude, then PATH
# whisper_threads = 8
# poll_interval_s = 20
# claude_model = "claude-sonnet-5-5"              # notes model
# claude_effort = "high"                          # thinking effort of the notes call
# claude_timeout_s = 1800
# whisper_hint_mode = "hotwords"                  # subject/topic/details -> Whisper vocabulary hint: hotwords | initial_prompt | off
# whisper_hint_chars = 400
"""
