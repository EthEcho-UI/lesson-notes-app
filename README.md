# lesson-notes-app
PWA + laptop worker for recording lessons/meetings, transcribing and summarizing them (code only, no data).

- `web/` the PWA (static, no build step, vanilla ES modules). Live: https://ethecho-ui.github.io/lesson-notes-app/web/
- `worker/` laptop worker (separate block).
- `tools/stamp-precache.py` regenerates `web/precache.js` from `git ls-files web` (run, then `git add`, before every commit; `--check` verifies).
- `tests/` Playwright + fake GitHub API: `uv venv tests/.venv && uv pip install --python tests/.venv/bin/python playwright jsonschema pillow && tests/.venv/bin/python -m playwright install chromium && tests/.venv/bin/python tests/run_tests.py`

The PWA talks to a private data repo with a fine-grained token (Contents: read/write on that one repo) that the user pastes once per device; it is stored only in that device's IndexedDB. No tokens, audio or transcripts belong in this repo.
