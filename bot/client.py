"""The bot: startup, shutdown, command sync, error handling and backups."""

from __future__ import annotations

import asyncio
import ctypes.util
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from . import __version__
from .config import Config, Secrets
from .db import Database
from .permissions import ControlError
from .player import PlayerError, PlayerManager
from .queue import QueueError
from .recommender import Recommender
from .sources import Resolver
from .sources.errors import SourceError
from .sources.lastfm import LastFMClient
from .sources.spotify import SpotifyClient
from .sources.ytdlp import YtdlpSource

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bot.cogs.music",
    "bot.cogs.info",
    "bot.cogs.settings",
    "bot.cogs.taste",
    "bot.cogs.playlists",
    "bot.cogs.owner",
)

_OPUS_FALLBACKS = (
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
    "libopus.so.0",
    "libopus.so",
)

USER_ERRORS = (ControlError, SourceError, PlayerError, QueueError)


def load_opus(preferred: str | None) -> bool:
    if discord.opus.is_loaded():
        return True
    candidates = [preferred, ctypes.util.find_library("opus"), *_OPUS_FALLBACKS]
    for name in candidates:
        if not name:
            continue
        try:
            discord.opus.load_opus(name)
            log.debug("Loaded opus from %s", name)
            return True
        except OSError:
            continue
    return False


class MusicBot(commands.Bot):
    def __init__(self, config: Config, secrets: Secrets) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        # Message *events* (not content) let the bot notice when its now-playing
        # message has been buried so it can re-post it at the bottom.
        intents.guild_messages = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            owner_ids=set(config.owner_ids),
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
            max_messages=200,
        )
        self.config = config
        self.secrets = secrets
        self.started_at = time.time()
        self.maintenance: str | None = None
        self.players = PlayerManager(self)
        self.ffmpeg_available = False
        self.spotify: SpotifyClient | None = None
        self._backup_task: asyncio.Task[None] | None = None
        self._closing = False
        self._restored = False

    # ----- lifecycle --------------------------------------------------
    async def setup_hook(self) -> None:
        cfg = self.config
        if not load_opus(cfg.opus_library):
            log.error("Couldn't load the opus library; voice playback will fail. Install libopus (see README).")
        self.ffmpeg_available = shutil.which(cfg.ffmpeg_path) is not None
        if not self.ffmpeg_available:
            log.error("FFmpeg not found at %r. Install it or set ffmpeg_path in config.yaml.", cfg.ffmpeg_path)
        if not any(shutil.which(r) for r in cfg.ytdlp.js_runtimes):
            log.warning(
                "No JavaScript runtime (%s) found. YouTube may fail or be degraded; install deno (see README).",
                ", ".join(cfg.ytdlp.js_runtimes),
            )

        self.http_session = aiohttp.ClientSession(headers={"User-Agent": f"discord-music-bot/{__version__}"})
        self.db = Database(cfg.database_path)
        await self.db.connect()
        self.maintenance = await self.db.get_state("maintenance")

        ytdlp = YtdlpSource(cfg.ytdlp, cfg.maximum_track_duration_seconds)
        if self.secrets.spotify_enabled:
            self.spotify = SpotifyClient(
                self.http_session,
                self.secrets.spotify_client_id,  # type: ignore[arg-type]
                self.secrets.spotify_client_secret,  # type: ignore[arg-type]
                self.secrets.spotify_refresh_token,
            )
        lastfm = LastFMClient(self.http_session, self.secrets.lastfm_api_key) if self.secrets.lastfm_api_key else None
        self.resolver = Resolver(cfg, self.http_session, ytdlp, self.spotify)
        self.recommender = Recommender(self.db, lastfm)

        self.tree.on_error = self.on_app_command_error  # type: ignore[assignment]
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self.sync_commands()
        if cfg.backup_interval_hours:
            self._backup_task = asyncio.create_task(self._backup_loop())

    async def sync_commands(self, *, guild: discord.abc.Snowflake | None = None, force_global: bool = False) -> int:
        """Register slash commands. A dev server gets them instantly; global takes up to an hour."""
        dev_id = self.config.command_sync_guild_id
        if guild is None and dev_id and not force_global:
            guild = discord.Object(id=dev_id)
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to server %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take a while to appear)", len(synced))
        return len(synced)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))
        if self.maintenance:
            await self.change_presence(activity=discord.Game("🛠️ maintenance"))
        if not self._restored:  # on_ready can fire again after a reconnect
            self._restored = True
            count = await self.players.restore_saved()
            if count:
                log.info("Picked up %d queue(s) from before the restart", count)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        await self.players.on_voice_state_update(member, before, after)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        if player:
            await player.destroy("Removed from server.", announce=False)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("Shutting down")
        try:
            # Save each queue (with the exact position) so the next start can pick it up.
            await asyncio.wait_for(self.players.save_all(), timeout=5)
        except Exception:
            log.debug("Error while saving queues", exc_info=True)
        try:
            await asyncio.wait_for(
                self.players.destroy_all("The bot is restarting or shutting down.", keep_saved=True), timeout=10
            )
        except Exception:
            log.debug("Error while stopping players", exc_info=True)
        if self._backup_task:
            self._backup_task.cancel()
        await super().close()
        if hasattr(self, "db"):
            await self.db.close()
        if hasattr(self, "http_session"):
            await self.http_session.close()

    # ----- errors -----------------------------------------------------
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, USER_ERRORS):
            message = str(original)
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You don't have permission to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = f"I'm missing permissions: {', '.join(error.missing_permissions)}."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use this command here."
        elif isinstance(original, discord.Forbidden):
            message = "Discord says I don't have permission to do that here."
        else:
            command = interaction.command.qualified_name if interaction.command else "?"
            log.error("Error in /%s (guild %s)", command, interaction.guild_id, exc_info=original)
            message = "Something went wrong running that command. The error has been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {message}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {message}", ephemeral=True)
        except discord.HTTPException:
            pass

    # ----- backups ----------------------------------------------------
    async def run_backup(self) -> Path:
        cfg = self.config
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = await self.db.backup(cfg.backup_directory / f"bot-{stamp}.db")
        backups = sorted(cfg.backup_directory.glob("bot-*.db"))
        for old in backups[: -cfg.backups_to_keep]:
            old.unlink(missing_ok=True)
        log.info("Database backed up to %s", path)
        return path

    async def _backup_loop(self) -> None:
        interval = self.config.backup_interval_hours * 3600
        while True:
            try:
                backups = sorted(self.config.backup_directory.glob("bot-*.db"))
                newest = backups[-1].stat().st_mtime if backups else 0
                if time.time() - newest >= interval:
                    await self.run_backup()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Automatic backup failed")
            await asyncio.sleep(3600)
