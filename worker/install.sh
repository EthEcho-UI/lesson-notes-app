#!/usr/bin/env bash
# Installs the worker into this checkout (.venv) and writes the systemd USER unit. It does NOT enable or start anything.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v uv >/dev/null || { echo "uv is required (pacman -S uv)"; exit 1; }
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found (pacman -S ffmpeg)"
[ -x "$HOME/.local/bin/claude" ] || command -v claude >/dev/null || echo "WARNING: claude (Claude Code) not found; notes need it, and it must be logged in"

cd "$DIR"
uv venv --python 3.12 .venv 2>/dev/null || true
uv sync
.venv/bin/lesson-worker init-config

mkdir -p "$UNIT_DIR"
sed "s|@WORKER_DIR@|$DIR|g" systemd/lesson-worker.service > "$UNIT_DIR/lesson-worker.service"
echo "wrote $UNIT_DIR/lesson-worker.service"

cat <<EOF

Next steps (nothing was started):
  1. Put the fine-grained GitHub token (Contents: read+write on the data repo only) into ~/.config/lesson-notes/token
       install -m 600 /dev/null ~/.config/lesson-notes/token && \$EDITOR ~/.config/lesson-notes/token
  2. $DIR/.venv/bin/lesson-worker download-models     # ~1.7 GB, into ~/.local/share/lesson-notes/models
  3. claude                                            # log in once, interactively
  4. $DIR/.venv/bin/lesson-worker doctor               # everything should say [ok]
  5. systemctl --user daemon-reload
     systemctl --user enable --now lesson-worker.service
     loginctl enable-linger \$USER                      # optional: keep it running without a login session
  Logs: journalctl --user -u lesson-worker -f
EOF
