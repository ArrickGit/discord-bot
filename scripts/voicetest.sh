#!/usr/bin/env bash
# Play a test beep and a few seconds of a song into your voice channel, and report
# what the voice connection does. Join a voice channel first.
# Usage: sudo /opt/discord-music-bot/scripts/voicetest.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=musicbot
SERVICE=discord-music-bot
run=()
if [[ $EUID -eq 0 ]] && id "$APP_USER" >/dev/null 2>&1; then run=(runuser -u "$APP_USER" --); fi

# The test logs in as the bot, so pause the real bot meanwhile and always bring it back.
was_active=false
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  was_active=true
  echo "Pausing the bot for the test..."
  systemctl stop "$SERVICE"
fi
restore() { if $was_active; then echo "Starting the bot again..."; systemctl start "$SERVICE"; fi; }
trap restore EXIT

cd "$APP_DIR"
"${run[@]}" env \
  PATH="$APP_DIR/.deno/bin:/usr/local/bin:/usr/bin:/bin" \
  DENO_DIR="$APP_DIR/data/deno-cache" \
  HOME="$APP_DIR" \
  "$APP_DIR/.venv/bin/python" -m bot.tools.voicetest
