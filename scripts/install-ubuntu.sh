#!/usr/bin/env bash
# Install the bot as a systemd service on Ubuntu/Debian.
# Usage (from the project folder):  sudo ./scripts/install-ubuntu.sh
set -euo pipefail

APP_DIR=/opt/discord-music-bot
APP_USER=musicbot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg libopus0 git curl unzip rsync >/dev/null

PY=python3
for candidate in python3.14 python3.13 python3.12; do
  if command -v "$candidate" >/dev/null; then PY=$candidate; break; fi
done
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12+ is required (found $("$PY" --version)). On Ubuntu 22.04, install python3.12 from the deadsnakes PPA." >&2
  exit 1
fi
apt-get install -y -qq "${PY}-venv" >/dev/null 2>&1 || true

echo "==> Creating service user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Copying project to $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete \
    --exclude .venv --exclude data --exclude logs --exclude .deno --exclude .ssh \
    --exclude .env --exclude config.yaml --exclude __pycache__ --exclude .pytest_cache \
    "$SRC_DIR"/ "$APP_DIR"/
  for f in .env config.yaml; do
    if [[ -f "$SRC_DIR/$f" && ! -f "$APP_DIR/$f" ]]; then cp "$SRC_DIR/$f" "$APP_DIR/$f"; fi
  done
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

echo "==> Installing deno (JavaScript runtime yt-dlp needs for YouTube)"
case "$(uname -m)" in
  x86_64) DENO_TARGET=x86_64-unknown-linux-gnu ;;
  aarch64|arm64) DENO_TARGET=aarch64-unknown-linux-gnu ;;
  *) DENO_TARGET="" ;;
esac
if [[ -n "$DENO_TARGET" && ! -x "$APP_DIR/.deno/bin/deno" ]]; then
  mkdir -p "$APP_DIR/.deno/bin"
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_TARGET}.zip" -o "$tmp/deno.zip"
  unzip -q -o "$tmp/deno.zip" -d "$APP_DIR/.deno/bin"
  rm -rf "$tmp"
elif [[ -z "$DENO_TARGET" ]]; then
  echo "    Unsupported CPU for the deno binary; YouTube may be degraded." >&2
fi

echo "==> Creating virtual environment and installing dependencies"
"$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

[[ -f "$APP_DIR/config.yaml" ]] || cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
[[ -f "$APP_DIR/.env" ]] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Installing systemd service"
cp "$APP_DIR/deploy/discord-music-bot.service" /etc/systemd/system/discord-music-bot.service
cp "$APP_DIR/deploy/discord-music-bot-deploy.service" "$APP_DIR/deploy/discord-music-bot-deploy.timer" /etc/systemd/system/
chmod +x "$APP_DIR/scripts/"*.sh
systemctl daemon-reload
systemctl enable discord-music-bot >/dev/null

# Auto-deploy from GitHub only works when the app folder is a git clone with a remote.
if sudo -u "$APP_USER" git -C "$APP_DIR" remote get-url origin >/dev/null 2>&1; then
  echo "==> Enabling auto-deploy (checks GitHub every minute)"
  systemctl enable --now discord-music-bot-deploy.timer >/dev/null
else
  echo "==> Auto-deploy not enabled: $APP_DIR is not a git clone with an 'origin' remote."
fi

cat <<EOF

Done. Next steps:
  1. sudo -u $APP_USER nano $APP_DIR/.env          # add DISCORD_TOKEN (and optional keys)
  2. sudo -u $APP_USER nano $APP_DIR/config.yaml   # set owner_id
  3. sudo systemctl start discord-music-bot
  4. journalctl -u discord-music-bot -f            # watch the logs
EOF
