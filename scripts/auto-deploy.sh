#!/usr/bin/env bash
# Pull new commits from GitHub and restart the bot if anything changed.
# Run every minute by discord-music-bot-deploy.timer (see deploy/).
# Manual run: sudo /opt/discord-music-bot/scripts/auto-deploy.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=musicbot
SERVICE=discord-music-bot

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (normally via the systemd timer)." >&2
  exit 1
fi

# Git runs as the service user so file ownership stays correct and its SSH deploy key is used.
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"; }
git_app() { as_app git -C "$APP_DIR" "$@"; }

BRANCH="${DEPLOY_BRANCH:-$(git_app rev-parse --abbrev-ref HEAD)}"

git_app fetch --quiet origin "$BRANCH"
LOCAL="$(git_app rev-parse HEAD)"
REMOTE="$(git_app rev-parse "origin/$BRANCH")"

if [[ "$LOCAL" == "$REMOTE" ]]; then
  exit 0
fi

echo "==> New commits on $BRANCH: ${LOCAL:0:7} -> ${REMOTE:0:7}"
git_app log --oneline "$LOCAL..$REMOTE" || true
CHANGED="$(git_app diff --name-only "$LOCAL" "$REMOTE")"

# Install new dependencies BEFORE switching code. If pip fails, nothing changes
# and the next run tries again.
if grep -qx 'requirements.txt' <<<"$CHANGED"; then
  echo "==> requirements.txt changed; installing dependencies"
  tmp="$(mktemp)"
  git_app show "origin/$BRANCH:requirements.txt" >"$tmp"
  chmod 644 "$tmp"
  as_app "$APP_DIR/.venv/bin/pip" install -q -r "$tmp"
  rm -f "$tmp"
fi

# The server copy is deploy-only: tracked files always match GitHub.
# Untracked files (.env, config.yaml, data/, logs/, .venv/) are never touched.
git_app reset --hard --quiet "origin/$BRANCH"

# Pick up changes to the systemd unit files themselves.
if grep -q '^deploy/.*\.\(service\|timer\)$' <<<"$CHANGED"; then
  echo "==> systemd units changed; reinstalling"
  cp "$APP_DIR"/deploy/*.service "$APP_DIR"/deploy/*.timer /etc/systemd/system/
  systemctl daemon-reload
fi

# try-restart only restarts if the bot is running, so an intentional
# `/owner shutdown` or `systemctl stop` is respected.
echo "==> Restarting $SERVICE"
systemctl try-restart "$SERVICE"
echo "==> Deployed ${REMOTE:0:7}"
