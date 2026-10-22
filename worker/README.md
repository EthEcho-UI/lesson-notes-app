# lesson-worker (laptop worker)

Background service on the Linux laptop. It watches the private data repo (`EthEcho-UI/lesson-notes-data`) for new audio on the
orphan `inbox` branch, transcribes it with Whisper (Czech), optionally labels speakers, asks Claude Code (headless) for notes,
writes transcript + notes + status to `main`, then deletes the audio. Everything it reads/writes follows the data contract
(`output/contract/CONTRACT.md`, schemas bundled in `src/lesson_worker/schemas/`; every file is validated before it is written).

```
PWA --uploads parts + job.json--> inbox branch --poll 20 s--> worker --ffmpeg--> faster-whisper large-v3-turbo int8 (cs)
      --(diarization: sherpa-onnx CAM++, thr 0.9, per part)--> merge parts --> `claude -p` notes --> ONE commit on main
      (transcript.json + notes.json + status.json=done) --> delete the job's audio from inbox (recreate orphan when empty)
```

## Requirements
Linux, Python 3.12 (uv manages it), `uv`, `ffmpeg`, Claude Code (`claude`, logged in once), ~3 GB RAM while transcribing, ~1.7 GB disk for models.

## Install
```bash
cd output/lesson-notes-app/worker
./install.sh          # uv venv + deps, writes ~/.config/lesson-notes/worker.toml (if absent) and the systemd USER unit. Starts nothing.
install -m 600 /dev/null ~/.config/lesson-notes/token      # then paste the token into it (see below)
.venv/bin/lesson-worker download-models                     # Whisper (~1.6 GB) + diarization models (~35 MB)
claude                                                      # log in interactively once
.venv/bin/lesson-worker doctor                              # every required line must say [ok]
systemctl --user daemon-reload
systemctl --user enable --now lesson-worker.service         # <- you do this; the install script never does
loginctl enable-linger "$USER"                              # optional: run without an open login session
journalctl --user -u lesson-worker -f                       # logs
```
**Token**: GitHub > Settings > Developer settings > Fine-grained tokens; resource owner `EthEcho-UI`, only repository `lesson-notes-data`,
permission *Contents: Read and write* (Metadata read is added automatically), expiry <= 1 year. Use a token separate from the PWA's.
The file must be mode 600 (the worker refuses to start otherwise: `chmod 600 ~/.config/lesson-notes/token`). The token is never logged and never in a repo.

## Config: `~/.config/lesson-notes/worker.toml` (all optional)
| key | default | meaning |
|---|---|---|
| `repo` | `EthEcho-UI/lesson-notes-data` | data repo |
| `token_file` | `~/.config/lesson-notes/token` | 0600 file |
| `whisper_threads` | 8 | CPU threads for Whisper (benchmark used 12; lower = nicer to other jobs) |
| `whisper_initial_prompt` | empty | static text always given to Whisper |
| `whisper_hint_mode` / `whisper_hint_chars` | `hotwords` / 400 | per-note context (subject, topic, details) -> short vocabulary hint for Whisper: `hotwords` (applied to every 30 s window) \| `initial_prompt` (first window only) \| `off` |
| `mixed_gap_ms`, `mixed_max_unit_s`, `mixed_min_unit_s` | 500, 25, 1.0 | `language_mode=mixed`: utterance splitting, see "Languages" |
| `image_gc_interval_s` | 3600 | how often orphan photos on main are removed |
| `diarize_threshold` | 0.9 | speaker clustering threshold (higher = fewer speakers) |
| `claude_path` | `~/.local/bin/claude`, then PATH | |
| `claude_model` / `claude_effort` | `claude-sonnet-5-5` / `high` | notes model id and thinking effort (`doctor` makes one tiny call to prove the id works) |
| `claude_timeout_s` / `claude_attempts` | 1800 / 2 | per call (thinking, photos) / bounded retries inside one try |
| `poll_interval_s` | 20 | inbox poll (1 request, ETag/304) |
| `max_attempts`, `retry_backoff_s` | 3, `[60, 300, 900]` | automatic retries per job |
| `models_dir`, `cache_dir`, `data_dir` | `~/.local/share/lesson-notes/models`, `~/.cache/lesson-notes`, `~/.local/share/lesson-notes` | |

Settings that come from the repo (`settings/app.json`): `failed_audio_retention_days` (14).

## Commands
`lesson-worker run` daemon loop | `once` process everything runnable and exit | `status` heartbeat + queue from GitHub |
`doctor` checks ffmpeg, token file (mode 600), GitHub API (push permission), Whisper model, diarization models (optional), `claude auth status`, cache dir; exit 1 if a required check fails |
`download-models` | `init-config`. `-v` for debug logs, `--config PATH` for another config.

## Languages, context, photos (contract v2)
- **Languages**: `language_mode` of the job: `cs`/`en` force that language; `mixed` = the audio is split at pauses (>= 500 ms) into utterances, each utterance is language-identified (Whisper language-id) and decoded in its language; utterances detected as a third language (sk, ru, ...) and too-short ones get the dominant cs/en language (first guess kept in `lang_detected`). Costs about 1.6-2x the time of a forced run. Every segment has `lang`. Measurements and limits (an English phrase inside one Czech sentence without a pause is not separated): `output/b7-REPORT.md`.
- **Context -> Whisper**: subject/topic/details become a <= 400 char hint passed as `hotwords`; part of the ASR cache key. Notes get the full context.
- **Photos**: `job.images[]` are fetched (inbox / main / laptop cache) and sha256-verified up front (`sha_mismatch`); in state `reading_images` Pillow validates them and removes EXIF/GPS/XMP/comments (lossless byte surgery; if an EXIF rotation exists the pixels are rotated first); the scrubbed copies go to the notes engine and, in the ONE completion commit, to `notes/<id>/images/NN.ext` (only photos still listed in note.json; orphans are deleted; `status.images_kept`). The inbox copies leave with the job. The worker never edits note.json.
- **Old data**: v1 jobs are processed (v2 outputs); v1 transcripts get `lang: cs` when a part is appended; v1 notes are passed to the engine as `previous_notes`.

## Behaviour you can rely on
- **Offline laptop / catch-up**: jobs never expire in the queue. On start and on resume from suspend the worker drops all caches and does a full scan,
  then processes oldest `created_at` first (ULID tie-break), strictly one at a time. Suspend is detected by comparing wall clock vs monotonic clock (noticed within 5 s).
- **Safe to kill any time** (SIGKILL, power loss): intermediate results live only in `~/.cache/lesson-notes/parts/<sha256>/` (reused on restart and on a user "retry");
  `main` only changes via status updates and ONE completion commit. `done` is the commit point; after it a restart only finishes the cleanup. SIGTERM finishes the current unit then exits (status back to `queued`, attempt not burned).
- **Retries**: transient errors (whisper crash, claude timeout/error) -> status `queued` again, back-off 1/5/15 min, max 3 attempts, then `failed` (retryable:true). `sha_mismatch`, `part_missing`, `claude_not_logged_in` fail at once (retryable:true, the user re-uploads / logs in and presses retry in the PWA = new job_id, same parts, nothing re-transcribed).
  `audio_corrupt`, `no_speech` are `retryable:false`. A crash loop (attempt > 3) ends as `attempts_exhausted`.
- **Failed audio** stays on `inbox` for 14 days from `failed_at` (`status.audio.purge_after`), then the janitor deletes it (`audio.state = purged`).
- **Inbox cleanup**: only the finished job's files are removed; if nothing else is left on `inbox` the branch is deleted and recreated as a fresh orphan (so the audio is no longer reachable from any ref), otherwise one commit removes just those files. A newer `job.json` for the same note is never touched.
- **Notes**: `claude -p` runs from an empty temp dir, absolute path, `HOME`+`PATH`+`LANG`+`TERM` only, `--tools ""`, `--setting-sources project`, `--strict-mcp-config`, no session persistence, JSON schema output, timeout, `is_error` check. The transcript is passed between `<prepis>` tags and declared to be data. If Claude is not logged in the job fails with `claude_not_logged_in` and a clear message.
- **Diarization** is best-effort: if models/onnxruntime are missing or it errors, the transcript is written without speaker labels (logged), the job still succeeds. Labels are `S1`,`S2`.. per part (first appearance order), clusters < 20 s of speech are merged into the nearest one (only for parts > 2 min).
- **Meetings** (`kind: meeting`) get `decisions[]` and `action_items[{task,owner,due}]`; lessons never do.
- **Laptop-recorded audio** (`audio_location: local`): the job.json is on inbox, the parts are read from `~/.local/share/lesson-notes/local/<noteId>/parts/NN.ext` and deleted after `done`. (Written by the laptop recorder, `../recorder`.)
- One daemon per machine (flock in the cache dir); a second `run` exits with code 2.

## Uninstall
```bash
./uninstall.sh            # disables+removes the user unit, keeps config/token/models/cache
./uninstall.sh --purge    # also deletes ~/.config/lesson-notes, ~/.local/share/lesson-notes, ~/.cache/lesson-notes
```
Then delete this folder (the venv lives in `worker/.venv`).

## Tests
```bash
cd worker && uv sync
.venv/bin/python -m pytest tests -q                      # fake GitHub + stub `claude`; real Whisper/diarization e2e runs if the model/clips exist
LESSON_REAL_CLAUDE=1 .venv/bin/python -m pytest tests/test_real.py -q -s -k claude   # one real `claude -p` call (~0.05 USD)
```
`tests/fake_github.py` is a copy of the PWA's fake GitHub API (do not edit the PWA copy). The real-Whisper test reads public FLEURS clips from
`output/spikes/b0a-whisper-bench/data/eval` (override with `LESSON_TEST_SPIKES`) and reuses the cached HF model; no audio is committed to the repo.
Tests never touch the real data repo.
