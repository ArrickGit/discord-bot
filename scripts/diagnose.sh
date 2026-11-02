#!/usr/bin/env bash
# Check why YouTube playback fails, using the bot's own settings.
# Usage: sudo /opt/discord-music-bot/scripts/diagnose.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=musicbot
run=()
if [[ $EUID -eq 0 ]] && id "$APP_USER" >/dev/null 2>&1; then run=(runuser -u "$APP_USER" --); fi

cd "$APP_DIR"
echo "Deployed commit: $("${run[@]}" git -C "$APP_DIR" log -1 --format='%h %s' 2>/dev/null || echo unknown)"
exec "${run[@]}" env \
  PATH="$APP_DIR/.deno/bin:/usr/local/bin:/usr/bin:/bin" \
  DENO_DIR="$APP_DIR/data/deno-cache" \
  HOME="$APP_DIR" \
  "$APP_DIR/.venv/bin/python" -m bot.tools.diagnose
