#!/usr/bin/env bash
# Removes the systemd user unit. Keeps config, token, models and cache unless you pass --purge.
set -euo pipefail
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/lesson-worker.service"
systemctl --user disable --now lesson-worker.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload 2>/dev/null || true
echo "removed $UNIT"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.local/share/lesson-notes" "$HOME/.cache/lesson-notes" "$HOME/.config/lesson-notes"
  echo "removed config, token, models and cache"
else
  echo "kept ~/.config/lesson-notes, ~/.local/share/lesson-notes (models), ~/.cache/lesson-notes; use --purge to delete them"
fi
echo "The project folder itself (venv) is untouched: delete it manually if you want."
