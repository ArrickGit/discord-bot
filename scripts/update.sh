#!/usr/bin/env bash
# Update dependencies (most importantly yt-dlp) and restart the service.
# Run this whenever YouTube playback starts failing.
# Usage: sudo /opt/discord-music-bot/scripts/update.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=musicbot
run() { if [[ $EUID -eq 0 ]] && id "$APP_USER" >/dev/null 2>&1; then sudo -u "$APP_USER" "$@"; else "$@"; fi; }

if [[ -d "$APP_DIR/.git" ]]; then
  echo "==> Pulling latest code"
  run git -C "$APP_DIR" pull --ff-only
fi

echo "==> Updating Python packages"
run "$APP_DIR/.venv/bin/pip" install -q --upgrade -r "$APP_DIR/requirements.txt"
run "$APP_DIR/.venv/bin/python" -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"

if [[ -x "$APP_DIR/.deno/bin/deno" ]]; then
  echo "==> Updating deno"
  run "$APP_DIR/.deno/bin/deno" upgrade -q || echo "    (deno upgrade skipped)"
fi

if systemctl list-unit-files discord-music-bot.service >/dev/null 2>&1; then
  echo "==> Restarting service"
  systemctl restart discord-music-bot
  systemctl --no-pager --lines=5 status discord-music-bot || true
fi
