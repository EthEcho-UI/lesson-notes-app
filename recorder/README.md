# lesson-record (laptop recorder)

Records a lesson or a meeting **directly on this laptop** (microphone, or microphone + system audio for online meetings) and hands the
audio to the lesson-notes worker (`../worker`), which transcribes it and writes the notes. Several recordings can be parts of one note.

```
lesson-record start ──detached recorder process──> ~/.local/share/lesson-notes/recorder/active/<id>/part_001.opus ...   (written continuously)
lesson-record stop  ──finalize──> ~/.local/share/lesson-notes/local/<noteId>/parts/NN.opus        (where the worker reads local audio)
                    ──hand-off──> GitHub data repo:  notes/<noteId>/note.json (main)  +  jobs/<noteId>/job.json (inbox, audio_location=local)
```
The audio never leaves the laptop. Only the small `note.json` / `job.json` go to GitHub (contract: `output/contract/CONTRACT.md`, sections 3 and 5).

> **Consent:** recording other people (meeting mode records the other side of a call) may require their consent - that is your responsibility.

## Install
Needs Linux with PipeWire (pipewire-pulse), `ffmpeg` (with libopus), `pactl` (libpulse), Python 3.12 via `uv`, and the worker configured
(`../worker/README.md`: `~/.config/lesson-notes/worker.toml` + the 0600 token file; the recorder reuses both, the token needs Contents read+write).
```bash
cd output/lesson-notes-app/recorder
uv sync                 # installs lesson-record and the worker package (shared GitHub client, config, contract validation)
.venv/bin/lesson-record status
```
Optional `~/.config/lesson-notes/recorder.toml`: `part_minutes = 75` (a longer recording is split into several parts), `bitrate = "24k"`,
`mic_source = ""` (pulse source name; empty = follow the default input), `silent_warn_s = 10`, `silent_db = -60`, `language = "cs"`.

## Use
```bash
lesson-record start --kind lesson  --notebook "Biologie 2.A" --title "Mitosis"     # mic only (default mode for lessons)
lesson-record start --kind meeting --notebook "Work" --title "Weekly"              # mic + system audio (default mode for meetings)
lesson-record start --mode meeting --kind lesson ...                               # override the capture mode
lesson-record start --note <noteId>                                                # record ANOTHER PART of an existing note
lesson-record pause | resume | stop | status | list-notes | recover | submit
lesson-record ui                                                                   # local web UI
```
* `start` returns as soon as audio flows; the recording runs as its **own background process (own session, SIGHUP ignored)** - closing the
  terminal, the UI or the browser does not stop it. Only `stop` (or `kill`) ends it. One recording at a time (flock on `~/.local/state/lesson-notes/recorder.lock`).
* Defaults per kind come from the contract: lesson -> diarize false, meeting -> true (override with `--diarize/--no-diarize`; the repo's
  `settings/app.json` `diarize_default` is honoured when present). Notebook, language and the notes list come from the data repo and are cached
  (`~/.local/state/lesson-notes/remote-cache.json`), so `start` works offline.
* `pause` releases the microphone; the paused time is not in the file. `list-notes` shows recent notes and their worker state.
* State lives in `~/.local/state/lesson-notes/` (`recorder.json`, `outbox/`, `remote-cache.json`; mode 0700).

### Crash safety
Audio is encoded continuously (Ogg Opus pages flushed every 0.2 s), so a crash, `kill -9` or power loss loses about a second. The next
`lesson-record status` / `start` / `recover` (and the UI) notices a recording whose recorder is gone (the lock is free but the state says "recording"),
turns the segments into parts and hands them over like a normal stop. Recordings with no audio at all or only digital silence are **not**
turned into an empty part: a "no audio captured" error is reported (silent ones are kept under `recorder/failed/`).
### Meeting mode and changing devices
Meeting mode mixes the default input with the **monitor of the default output** (what the call plays). The recorder follows `pactl subscribe`
events (plus a 2 s poll): when the default sink or source changes (headphones, HDMI, Bluetooth on/off, a sink disappearing) the capture is restarted on the
new devices (a gap of well under a second, counted in `capture_restarts`). If the default input is only a monitor (no mic) meeting mode records system audio only.
A silent input (nothing above -60 dB for 10 s) raises a warning in `status` and the UI.
### Hand-off, multi-part notes, offline
The announcement (`note.json` then `job.json`) is retried by every later `status`/`submit`/UI run, so recording with the laptop offline loses nothing.
A new part of a note whose previous job is still queued/running waits in the outbox (the worker holds one job per note), then goes out as `append: true`;
if the previous job failed, its parts are carried along in the new job.

## Local UI
`lesson-record ui` starts a server on **127.0.0.1 and a free port**, prints a link with a per-launch random token and opens it. Big Start / Pause / Stop,
live level meter, elapsed time, notebook / title / kind pickers, "add to existing note", recent notes with their worker state, and the worker heartbeat
(read from the data repo, same data as `lesson-worker status`). `lesson-record ui --stop` closes it; it also closes itself after 30 idle minutes (never while recording).
Security (any web page can reach 127.0.0.1): every request and the websocket need the token (HttpOnly SameSite=Strict cookie, set when you open the printed link),
an exact `Host` (127.0.0.1:port / localhost:port), a matching `Origin` (required for POST and websocket), `Sec-Fetch-Site` same-origin, and POST bodies are JSON only.
Strict CSP (`default-src 'none'; script-src 'self'; style-src 'self'`), no external assets, no inline script/style, text rendered with `textContent` only.

Desktop launcher (user level, nothing system-wide; not installed by default):
```bash
mkdir -p ~/.local/share/applications
sed "s|@RECORDER@|$PWD|g" lesson-record-ui.desktop > ~/.local/share/applications/lesson-record-ui.desktop      # run inside recorder/
# remove again: rm ~/.local/share/applications/lesson-record-ui.desktop
```

## Tests
```bash
uv sync && .venv/bin/python -m pytest tests -q
```
Needs the worker clone next to it (reuses its fake GitHub + fake Whisper/claude). Real capture tests run against a **private PipeWire/WirePlumber/pipewire-pulse
instance in a temp dir with no hardware** (`tests/audiosandbox.py`; skipped if the binaries are missing): they never touch your real audio server, default
devices or volume, and only signal processes they started. The UI walkthrough with screenshots (`docs/*.png`) needs Playwright from `../tests/.venv`.
