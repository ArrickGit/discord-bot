# Discord Music Bot

A self-hosted, multi-server Discord music bot. It plays YouTube, SoundCloud and Spotify links (or plain-text searches), gives every server its own queue, and learns what each person likes so `/random` and autoplay can pick songs for them.

## Features

- **Sources:** YouTube videos, Shorts, YouTube Music and playlists; SoundCloud tracks and sets; Spotify tracks, albums and playlists (each matched to YouTube when it plays); plain-text search.
- **Per-server sessions:** each server has its own voice connection, queue, volume, loop mode, settings and timers.
- **Controls:** slash commands, plus buttons on a single now-playing message that updates itself.
- **Leaves on its own:** after an idle timeout. When everyone leaves the channel it pauses, then disconnects.
- **Permissions:** you must be in the bot's voice channel to control it. Servers can restrict control to a DJ role or to moderators, and can turn on vote-skip.
- **Taste system:** `/rate` (👍 / 👎 / ⏭️ not now), `/random`, `/why`, autoplay, `/profile`, `/likes`, `/dislikes`, `/forget`, `/reset-taste`.
- **Saved playlists:** personal playlists that work in any server.
- **Owner tools:** status, maintenance mode, reload, command sync, backups, leave all, shut down.
- **Storage:** SQLite with automatic daily backups. Only Discord IDs are stored, never usernames.

## Quick start (local)

Requirements: Python 3.12+, FFmpeg, libopus, and **deno** (yt-dlp needs a JavaScript runtime for YouTube).

```bash
# macOS
brew install python@3.12 ffmpeg opus deno
# Ubuntu: see "Deploying on Ubuntu" below

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env              # add DISCORD_TOKEN
cp config.example.yaml config.yaml  # set owner_id (and command_sync_guild_id while testing)
.venv/bin/python -m bot
```

### Creating the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Under **Bot**, click **Reset Token** and copy the token into `.env` as `DISCORD_TOKEN`. No privileged intents are needed.
3. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes.
   - Bot permissions: View Channels, Send Messages, Embed Links, Read Message History, Connect, Speak.
4. Open the generated URL to invite the bot.
5. For instant command registration while testing, put your test server's ID in `command_sync_guild_id`. Set it back to `null` for normal multi-server use; global commands can take up to an hour to appear the first time.

### Spotify (optional)

Spotify gives metadata only, so each song is matched to a YouTube video right before it plays.

1. Create an app at <https://developer.spotify.com/dashboard>. Since February 2026, the account that owns the app needs **Spotify Premium**.
2. Put `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`. Track and album links now work.
3. **Playlists:** Spotify now only returns the contents of playlists that the logged-in account owns or collaborates on. Spotify's editorial playlists (Discover Weekly, Today's Top Hits, …) can't be read at all. To unlock your own playlists:
   - Add `http://127.0.0.1:8888/callback` as a Redirect URI in your Spotify app settings.
   - Run `.venv/bin/python -m bot.tools.spotify_auth` on a computer with a browser.
   - Copy the printed `SPOTIFY_REFRESH_TOKEN` line into `.env`.

   Links to other people's playlists get a clear explanation instead of failing silently.

### Last.fm (optional)

Add `LASTFM_API_KEY` to `.env` and `/random` and autoplay can suggest songs you haven't heard yet: tracks similar to the ones you liked. Without it, they pick from music already played through the bot.

## Commands

| Command | What it does |
|---|---|
| `/play <link or search> [next]` | Play or queue; `next: True` puts it at the front |
| `/join` `/leave` | Join your channel / disconnect and clear the queue |
| `/pause` `/resume` `/skip` `/stop` | Playback controls |
| `/volume [1-100]` `/loop off\|song\|queue` `/shuffle` | Volume, repeat and shuffle |
| `/nowplaying` `/queue [page]` | Show the player here / show the queue (with page buttons) |
| `/remove <pos>` `/move <from> <to>` `/clear` | Edit the queue (anyone can remove their own songs) |
| `/rate` `/random` `/why` `/autoplay on\|off` | Taste and recommendations |
| `/profile` `/likes` `/dislikes` `/forget` `/reset-taste` | See or delete what the bot has learned about you |
| `/playlist create\|add\|remove\|show\|list\|play\|delete` | Personal saved playlists |
| `/settings show\|control\|dj-role\|default-volume\|timeout\|vote-skip` | Server settings (requires Manage Server) |
| `/owner status\|maintenance\|reload\|sync\|backup\|servers\|leave-server\|leaveall\|shutdown` | Bot owner only |
| `/help` `/ping` `/about` | Info |

### Who can control playback

- **Anyone** in the bot's voice channel can add songs.
- **Everyone** mode (the default): anyone in the channel can control playback.
- **`dj`** mode: only the DJ role and moderators.
- **`mods`** mode: only moderators (Manage Server or Move Members).

In every mode, these people always keep control:
- the bot owner;
- anyone alone with the bot;
- whoever requested the current song, for skipping it.

With `/settings vote-skip`, other listeners can vote to skip.

## How recommendations work

Scores are simple and explainable. `/why` shows the reasons behind a pick.

| Signal | Effect |
|---|---|
| You liked the song | strong boost |
| You liked other songs by the artist | moderate boost |
| You usually finish it / the artist's songs | small boost |
| You skipped it in the first 30 s (or first 25% of the song) | small penalty |
| You rated it 👎 | never suggested |
| You rated the artist's songs 👎 | penalty |
| You rated it ⏭️ not now | temporary penalty that fades over 7 days |
| It played in this server recently | penalty, to avoid repeats |

The bot then picks randomly among the top-scoring songs, weighted by score, so results vary.

Autoplay scores songs for everyone in the voice channel together. Anyone's 👎 vetoes a song.

## Deploying on Ubuntu

```bash
git clone <your repo> discord-music-bot && cd discord-music-bot   # or copy the folder over
sudo ./scripts/install-ubuntu.sh
sudo -u musicbot nano /opt/discord-music-bot/.env
sudo -u musicbot nano /opt/discord-music-bot/config.yaml
sudo systemctl start discord-music-bot
journalctl -u discord-music-bot -f
```

The install script:
- installs FFmpeg, libopus and deno;
- creates a locked-down `musicbot` system user and a virtual environment in `/opt/discord-music-bot`;
- installs a hardened systemd service that starts on boot and restarts on crashes.

`/owner shutdown` stops the service; it doesn't restart.

**When YouTube playback starts failing, run `sudo /opt/discord-music-bot/scripts/update.sh` first.** It updates yt-dlp (and deno) and restarts the bot, which fixes most YouTube breakages.

### Auto-deploy from GitHub

If you install from a `git clone`, the install script also enables a timer. Every minute, the timer checks GitHub for new commits on the server's branch. When it finds some, it:
1. installs any changed dependencies;
2. updates the code to match GitHub;
3. restarts the bot.

So deploying is just `git push`.

- **Watch deploys:** `journalctl -u discord-music-bot-deploy -f`
- **Deploy right now:** `sudo systemctl start discord-music-bot-deploy`
- **Turn it off:** `sudo systemctl disable --now discord-music-bot-deploy.timer`

Notes:
- The server copy is deploy-only. Edits to tracked files in `/opt/discord-music-bot` are overwritten. `.env`, `config.yaml`, `data/` and `logs/` are never touched.
- A restart (a deploy, a crash or a reboot) pauses the music for a few seconds. The bot saves each server's queue, then rejoins and picks up the current song where it stopped. It only does this if people are still in the voice channel and the bot was down for less than 30 minutes.
- If the bot was stopped on purpose, it stays stopped.
- **Private repo:** give the `musicbot` user a read-only deploy key:
  ```bash
  sudo -u musicbot mkdir -p /opt/discord-music-bot/.ssh
  sudo -u musicbot ssh-keygen -t ed25519 -N "" -f /opt/discord-music-bot/.ssh/id_ed25519
  sudo cat /opt/discord-music-bot/.ssh/id_ed25519.pub   # add under GitHub repo → Settings → Deploy keys
  sudo -u musicbot ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # trust GitHub's host key
  sudo -u musicbot git -C /opt/discord-music-bot remote set-url origin git@github.com:<you>/<repo>.git
  ```

Docker also works: `docker compose up -d --build`, after creating `.env` and `config.yaml`. The `data/` and `logs/` folders must be writable by UID 1000.

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests cover:
- the queue, link detection and validation, and Spotify match scoring;
- config validation and log redaction;
- every database operation, the permission rules and the recommendation engine;
- the real playback loop, driven by a fake voice connection: skip, stop, loops, failures, retries, autoplay, idle and alone timeouts.

Live voice playback still needs a manual test in a Discord server.

Layout:

```
bot/
  client.py        startup, shutdown, command sync, error handling, backups
  player.py        per-server playback session + manager
  queue.py         queue logic (pure)
  permissions.py   who may control music (pure rules + Discord adapter)
  controls.py      actions shared by slash commands and buttons
  db.py            SQLite storage
  recommend.py     scoring (pure)      recommender.py  candidate gathering
  sources/         detect.py (link validation), ytdlp.py, spotify.py, lastfm.py, matching.py
  cogs/            slash commands
  ui/              embeds, buttons, formatting
  tools/           spotify_auth.py
```

## Notes and limits

- Streaming YouTube audio through a bot is against YouTube's Terms of Service. That's why large public music bots were shut down. Keep this bot private to servers you run.
- Age-restricted, private, members-only and region-locked content is skipped with an explanation.
- If YouTube keeps blocking the server, `ytdlp.cookies_file` can point to a cookies file. Only ever use a throwaway account for it.
- Volume uses Discord's PCM volume transformer, which costs some CPU per active server. That's fine for a handful of servers.

