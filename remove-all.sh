#!/usr/bin/env bash
# Removes everything the laptop side of lesson-notes put on this machine (worker + recorder).
#   ./remove-all.sh --dry-run    show what would be removed, change nothing
#   ./remove-all.sh              asks for confirmation, then removes
#   ./remove-all.sh --yes        no question
# NOT touched: this source folder (delete it yourself afterwards), the GitHub repos/PWA, `uv` and `claude` themselves,
# and the lingering setting (only undone if you ever enabled it: `loginctl disable-linger $USER`).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lesson-worker.service"
TARGETS=(
  "$UNIT"
  "$HOME/.config/lesson-notes"                  # worker.toml, recorder.toml, token files
  "$HOME/.local/share/lesson-notes"             # Whisper/diarization models AND laptop-recorded audio waiting to be processed
  "$HOME/.cache/lesson-notes"                   # intermediate transcription results
  "$HOME/.local/state/lesson-notes"             # recorder state, outbox, remote cache
  "$HOME/.local/share/applications/lesson-record-ui.desktop"
  "$DIR/worker/.venv" "$DIR/recorder/.venv"
)
mode="${1:-ask}"
echo "Will remove:"
for t in "${TARGETS[@]}"; do [ -e "$t" ] && printf '  %-75s %s\n' "$t" "$(du -sh "$t" 2>/dev/null | cut -f1)"; done
[ "$mode" = "--dry-run" ] && { echo "(dry run: nothing removed)"; exit 0; }
if [ "$mode" != "--yes" ]; then
  echo "WARNING: recordings made on this laptop that were not transcribed yet are deleted too."
  read -r -p "Type yes to continue: " a; [ "$a" = "yes" ] || { echo "aborted"; exit 1; }
fi
# stop anything running first
if [ -x "$DIR/recorder/.venv/bin/lesson-record" ]; then
  "$DIR/recorder/.venv/bin/lesson-record" ui --stop >/dev/null 2>&1 || true
  "$DIR/recorder/.venv/bin/lesson-record" status 2>/dev/null | grep -qi "recording" && echo "NOTE: a recording is in progress; it is stopped with its files below."
fi
systemctl --user disable --now lesson-worker.service 2>/dev/null || true
pkill -f "[l]esson_record" 2>/dev/null || true
for t in "${TARGETS[@]}"; do rm -rf "$t"; done
systemctl --user daemon-reload 2>/dev/null || true
echo "Removed. Left: $DIR (delete the folder when you no longer need the source) and the GitHub repos (see README)."
