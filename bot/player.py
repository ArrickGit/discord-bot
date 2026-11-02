"""Per-server playback session.

Each server gets one ``GuildPlayer`` with its own voice connection, queue,
volume, loop mode, timers and now-playing message. A single background task per
player pulls tracks off the queue and plays them one after another.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from typing import TYPE_CHECKING

import discord

from .db import GuildSettings
from .models import Track
from .queue import LoopMode, MusicQueue
from .sources.errors import SourceBlockedError, SourceError
from .ui import embeds

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5
PAUSED_TIMEOUT_MULTIPLIER = 6
# Never wait longer than this for a stream link to become usable.
MAX_AVAILABILITY_WAIT = 30.0


class PlayerError(Exception):
    """User-facing voice/playback error."""


class GuildPlayer:
    def __init__(self, bot: MusicBot, guild: discord.Guild, settings: GuildSettings) -> None:
        self.bot = bot
        self.guild = guild
        self.settings = settings
        cfg = bot.config
        self.queue = MusicQueue(cfg.maximum_queue_size)
        try:
            self.queue.loop = LoopMode(settings.loop_mode)
        except ValueError:
            self.queue.loop = LoopMode.OFF
        self.volume = settings.default_volume or cfg.default_volume
        self.autoplay = settings.autoplay
        self.voice: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.np_message: discord.Message | None = None
        self._np_view: discord.ui.View | None = None
        self.skip_votes: set[int] = set()

        self._wake = asyncio.Event()
        self._finished = asyncio.Event()
        self._end_reason: str | None = None
        self._play_error: Exception | None = None
        self._task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._alone_task: asyncio.Task[None] | None = None
        self._prefetch_task: asyncio.Task[None] | None = None
        self._np_lock = asyncio.Lock()
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._auto_paused = False
        self._failures = 0
        self._autoplay_misses = 0
        self.destroyed = False

    # ----- state ------------------------------------------------------
    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        if self.voice and self.voice.is_connected():
            return self.voice.channel  # type: ignore[return-value]
        return None

    @property
    def is_playing(self) -> bool:
        return bool(self.voice and (self.voice.is_playing() or self.voice.is_paused()))

    @property
    def is_paused(self) -> bool:
        return bool(self.voice and self.voice.is_paused())

    def listener_count(self) -> int:
        channel = self.channel
        if channel is None:
            return 0
        return sum(1 for m in channel.members if not m.bot)

    def listener_ids(self) -> list[int]:
        channel = self.channel
        return [m.id for m in channel.members if not m.bot] if channel else []

    def position(self) -> float:
        if self._started_at is None:
            return 0.0
        now = self._paused_at or time.monotonic()
        return max(0.0, now - self._started_at - self._paused_total)

    # ----- voice ------------------------------------------------------
    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        me = self.guild.me
        perms = channel.permissions_for(me)
        if not perms.connect:
            raise PlayerError(f"I don't have permission to join {channel.mention}.")
        if not perms.speak:
            raise PlayerError(f"I don't have permission to speak in {channel.mention}.")
        if channel.user_limit and len(channel.members) >= channel.user_limit and not perms.move_members:
            raise PlayerError(f"{channel.mention} is full.")
        try:
            if self.voice and self.voice.is_connected():
                if self.voice.channel.id != channel.id:
                    await self.voice.move_to(channel)
            else:
                existing = self.guild.voice_client
                if existing is not None:
                    await existing.disconnect(force=True)
                self.voice = await channel.connect(timeout=20, self_deaf=True)
        except asyncio.TimeoutError as exc:
            raise PlayerError("Connecting to the voice channel timed out. Try again.") from exc
        except discord.ClientException as exc:
            raise PlayerError(f"Couldn't join the voice channel: {exc}") from exc
        except discord.DiscordException as exc:
            log.exception("Voice connect failed in guild %s", self.guild.id)
            raise PlayerError("Couldn't connect to voice. Discord may be having trouble.") from exc
        if isinstance(channel, discord.StageChannel):
            try:
                await me.edit(suppress=False)
            except discord.HTTPException:
                pass
        self.start()
        self.check_alone()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"player-{self.guild.id}")

    # ----- queue operations -------------------------------------------
    def enqueue(self, tracks: list[Track], *, front: bool = False) -> tuple[int, int]:
        added, dropped = self.queue.add_many(tracks, front=front)
        if added:
            self._autoplay_misses = 0
            self._wake.set()
        return added, dropped

    def pause(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self._paused_at = time.monotonic()
            self._start_idle_timer(paused=True)
            return True
        return False

    def resume(self) -> bool:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self._auto_paused = False
            self._cancel(self._idle_task)
            return True
        return False

    def skip(self) -> bool:
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self._end_reason = "skipped"
            self.voice.stop()
            return True
        return False

    def stop(self) -> None:
        self.queue.stop()
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self._end_reason = "stopped"
            self.voice.stop()

    def set_volume(self, volume: int) -> None:
        self.volume = max(1, min(100, volume))
        source = self.voice.source if self.voice else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = self.volume / 100

    # ----- main loop --------------------------------------------------
    async def _run(self) -> None:
        try:
            while not self.destroyed:
                skipped = self._end_reason in ("skipped", "stopped")
                self._end_reason = None
                track = self.queue.advance(skipped=skipped)
                if track is None and self.autoplay:
                    track = await self._autoplay_pick()
                    if track is not None:
                        self.queue.current = track
                if track is None:
                    self._wake.clear()
                    if not self.queue.is_empty:
                        continue
                    await self._go_idle()
                    await self._wake.wait()
                    continue
                self._cancel(self._idle_task)
                await self._play(track)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed in guild %s", self.guild.id)
            await self._send_error("Something went wrong with playback, so I've stopped. Use `/play` to start again.")
            await self.destroy("Playback crashed.", announce=False)

    async def _play(self, track: Track) -> None:
        if not self.voice or not self.voice.is_connected():
            await self.destroy("Lost the voice connection.")
            return

        history_id: int | None = None
        try:
            for attempt in range(2):
                try:
                    await self.bot.resolver.prepare(track)
                except SourceError as exc:
                    await self._track_failed(track, exc)
                    return
                if self.destroyed or not self.voice or not self.voice.is_connected():
                    return
                if self.queue.current is not track:
                    return  # /stop or /skip happened while the track was loading
                if not await self._wait_until_available(track):
                    return
                try:
                    source = self._make_source(track)
                except discord.ClientException:
                    log.error("FFmpeg not found at %r", self.bot.config.ffmpeg_path)
                    await self._send_error("FFmpeg is missing on the bot's server, so nothing can play.")
                    self.queue.stop()
                    return

                self._finished.clear()
                self._play_error = None
                self._started_at = time.monotonic()
                self._paused_at = None
                self._paused_total = 0.0
                self.skip_votes.clear()
                try:
                    self.voice.play(source, after=self._after)
                except discord.ClientException as exc:
                    log.warning("Could not start playback in %s: %s", self.guild.id, exc)
                    source.cleanup()
                    return
                if attempt == 0:
                    self._failures = 0
                    history_id = await self._record_start(track)
                    await self.update_now_playing(new_track=True)
                    self._schedule_prefetch()

                await self._finished.wait()

                played = self.position()
                died_early = (
                    self._end_reason is None
                    and not track.is_live
                    and (track.duration or 0) > 15
                    and played < 3
                )
                if died_early and attempt == 0 and not self.destroyed:
                    # Usually an expired or rejected stream URL: refresh it once.
                    log.info("Stream for %r ended after %.1fs; retrying with a fresh URL", track.title, played)
                    track.clear_stream()
                    continue
                if died_early:
                    await self._track_failed(track, SourceError("The audio stream kept failing."))
                break
        finally:
            if history_id is not None:
                reason = self._end_reason or ("error" if self._play_error else "completed")
                await self._record_end(history_id, self.position(), reason)
            self._started_at = None

    def _after(self, error: Exception | None) -> None:
        # Runs in discord.py's audio thread.
        if error:
            log.warning("Playback error in guild %s: %s", self.guild.id, error)
        self._play_error = error
        try:
            self.bot.loop.call_soon_threadsafe(self._finished.set)
        except RuntimeError:
            pass  # event loop already closed during shutdown

    async def _wait_until_available(self, track: Track) -> bool:
        """YouTube rejects some fresh stream links (403) for a few seconds. Wait them out.

        Returns False if the track was skipped/stopped or the player was destroyed meanwhile.
        """
        wait = track.stream_available_at - time.time()
        if wait <= 0:
            return True
        await asyncio.sleep(min(wait + 0.5, MAX_AVAILABILITY_WAIT))
        return not self.destroyed and self.queue.current is track and bool(self.voice and self.voice.is_connected())

    def _make_source(self, track: Track) -> discord.AudioSource:
        before = ["-nostdin"]
        if not track.is_live:
            before += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if track.stream_headers:
            header_blob = "".join(f"{k}: {v}\r\n" for k, v in track.stream_headers.items())
            before += ["-headers", header_blob]
        audio = discord.FFmpegPCMAudio(
            track.stream_url,  # type: ignore[arg-type]
            executable=self.bot.config.ffmpeg_path,
            before_options=" ".join(shlex.quote(part) for part in before),
            options="-vn -loglevel error",
        )
        return discord.PCMVolumeTransformer(audio, volume=self.volume / 100)

    async def _track_failed(self, track: Track, exc: SourceError) -> None:
        self._failures += 1
        log.info("Skipping %r in guild %s: %s", track.title, self.guild.id, exc)
        self.queue.drop_current()
        # Platform blocks tend to affect every song, so give up sooner on those.
        limit = 2 if isinstance(exc, SourceBlockedError) else MAX_CONSECUTIVE_FAILURES
        if self._failures >= limit:
            extra = ""
            if self.autoplay:
                self.autoplay = False  # otherwise autoplay would keep feeding failing songs
                extra = " Autoplay is paused until it's turned back on."
            await self._send_error(
                f"{self._failures} songs in a row failed to play, so I've cleared the queue.{extra} "
                f"Last error: {exc}"
            )
            self.queue.stop()
            self._failures = 0
            return
        await self._send_error(f"Skipped **{embeds.escape(track.display_title)}**: {exc}")

    # ----- prefetch & autoplay ----------------------------------------
    def _schedule_prefetch(self) -> None:
        self._cancel(self._prefetch_task)
        self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        nxt = self.queue.peek()
        if nxt is None:
            return
        await asyncio.sleep(2)
        try:
            await self.bot.resolver.prepare(nxt)
        except SourceError:
            pass  # Reported properly when it's this track's turn.
        except Exception:
            log.debug("Prefetch failed", exc_info=True)

    async def _autoplay_pick(self) -> Track | None:
        if self._autoplay_misses >= 3:
            return None
        try:
            track = await self.bot.recommender.recommend(
                self.guild.id, self.listener_ids(), exclude=self._recent_keys(), via="autoplay"
            )
        except Exception:
            log.exception("Autoplay failed in guild %s", self.guild.id)
            track = None
        if track is None:
            self._autoplay_misses += 1
        return track

    def _recent_keys(self) -> set[str]:
        keys = {t.key for t in self.queue.history if t.key}
        keys |= {t.key for t in self.queue.upcoming if t.key}
        if self.queue.current and self.queue.current.key:
            keys.add(self.queue.current.key)
        return keys

    # ----- history ----------------------------------------------------
    async def _record_start(self, track: Track) -> int | None:
        try:
            return await self.bot.db.record_play_start(self.guild.id, track, track.requested_at)
        except Exception:
            log.exception("Failed to record play start")
            return None

    async def _record_end(self, history_id: int, listened: float, outcome: str) -> None:
        try:
            await self.bot.db.record_play_end(history_id, listened, outcome)
        except Exception:
            log.exception("Failed to record play end")

    # ----- idle / alone handling --------------------------------------
    async def _go_idle(self) -> None:
        self._started_at = None
        leave_at = self._start_idle_timer()
        await self.update_now_playing(idle_until=leave_at)

    def _timeout(self) -> int:
        return self.settings.inactivity_timeout or self.bot.config.inactivity_timeout_seconds

    def _start_idle_timer(self, *, paused: bool = False) -> float:
        self._cancel(self._idle_task)
        timeout = self._timeout() * (PAUSED_TIMEOUT_MULTIPLIER if paused else 1)
        self._idle_task = asyncio.create_task(self._idle_countdown(timeout, paused))
        return time.time() + timeout

    async def _idle_countdown(self, timeout: float, paused: bool) -> None:
        await asyncio.sleep(timeout)
        if paused and not self.is_paused:
            return
        if not paused and self.is_playing:
            return
        minutes = max(1, round(timeout / 60))
        await self.destroy(f"Left after {minutes} minute{'s' if minutes != 1 else ''} of inactivity.")

    def check_alone(self) -> None:
        """Called on voice-state changes: leave (and pause) when nobody is listening."""
        if self.destroyed or self.channel is None:
            return
        if self.listener_count() == 0:
            if self._alone_task is None or self._alone_task.done():
                if self.voice and self.voice.is_playing():
                    self.pause()
                    self._auto_paused = True
                self._alone_task = asyncio.create_task(self._alone_countdown())
        else:
            self._cancel(self._alone_task)
            self._alone_task = None
            if self._auto_paused:
                self.resume()
                asyncio.create_task(self.update_now_playing())

    async def _alone_countdown(self) -> None:
        await asyncio.sleep(self.bot.config.alone_timeout_seconds)
        if self.listener_count() == 0:
            await self.destroy("Everyone left the voice channel, so I left too.")

    # ----- messages ---------------------------------------------------
    async def update_now_playing(self, *, new_track: bool = False, idle_until: float | None = None) -> None:
        channel = self.text_channel
        if channel is None or self.destroyed:
            return
        from .ui.views import NowPlayingView  # local import avoids a cycle

        async with self._np_lock:
            if self.queue.current is None or idle_until is not None:
                embed, view = embeds.idle(self, idle_until), None
            else:
                embed, view = embeds.now_playing(self), NowPlayingView(self)
            if self._np_view is not None:
                self._np_view.stop()
            self._np_view = view
            msg = self.np_message
            # Re-post (and delete the old one) if other messages have buried it.
            if msg is not None and new_track and getattr(channel, "last_message_id", msg.id) != msg.id:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
                msg = self.np_message = None
            if msg is not None:
                try:
                    await msg.edit(embed=embed, view=view)
                    return
                except discord.NotFound:
                    self.np_message = None
                except discord.HTTPException as exc:
                    log.debug("Could not edit now-playing message: %s", exc)
                    return
            try:
                self.np_message = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                log.info("Missing permission to post in channel for guild %s", self.guild.id)
            except discord.HTTPException as exc:
                log.debug("Could not send now-playing message: %s", exc)

    async def _send_error(self, text: str) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(embed=embeds.error(text), delete_after=60)
        except discord.HTTPException:
            pass

    # ----- teardown ---------------------------------------------------
    @staticmethod
    def _cancel(task: asyncio.Task[None] | None) -> None:
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def destroy(self, reason: str = "Disconnected.", *, announce: bool = True) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.bot.players.forget(self.guild.id)
        for task in (self._idle_task, self._alone_task, self._prefetch_task):
            self._cancel(task)
        self._end_reason = "stopped"
        self.queue.stop()
        voice, self.voice = self.voice, None
        if voice is not None:
            try:
                voice.stop()
                await voice.disconnect(force=True)
            except Exception:
                log.debug("Error while disconnecting voice", exc_info=True)
        # Let the playback loop record the stopped track, then end it.
        self._finished.set()
        self._wake.set()
        if self._task and self._task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
        if self._np_view is not None:
            self._np_view.stop()
        if self.np_message is not None:
            try:
                await self.np_message.edit(embed=embeds.disconnected(reason), view=None)
            except discord.HTTPException:
                pass
        elif announce and self.text_channel is not None:
            try:
                await self.text_channel.send(embed=embeds.disconnected(reason), delete_after=120)
            except discord.HTTPException:
                pass
        log.info("Player for guild %s destroyed: %s", self.guild.id, reason)


class PlayerManager:
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self._players: dict[int, GuildPlayer] = {}
        self._lock = asyncio.Lock()

    def get(self, guild_id: int) -> GuildPlayer | None:
        player = self._players.get(guild_id)
        return player if player and not player.destroyed else None

    def all(self) -> list[GuildPlayer]:
        return [p for p in self._players.values() if not p.destroyed]

    def forget(self, guild_id: int) -> None:
        self._players.pop(guild_id, None)

    async def get_or_create(self, guild: discord.Guild) -> GuildPlayer:
        async with self._lock:
            player = self.get(guild.id)
            if player is None:
                settings = await self.bot.db.get_guild_settings(guild.id)
                player = GuildPlayer(self.bot, guild, settings)
                self._players[guild.id] = player
            return player

    async def destroy_all(self, reason: str) -> int:
        players = self.all()
        await asyncio.gather(*(p.destroy(reason) for p in players), return_exceptions=True)
        return len(players)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        player = self.get(member.guild.id)
        if player is None:
            return
        if self.bot.user and member.id == self.bot.user.id:
            if after.channel is None:
                # Give discord.py a moment in case this is part of a reconnect.
                await asyncio.sleep(2)
                if not (player.voice and player.voice.is_connected()):
                    await player.destroy("I was disconnected from the voice channel.")
            elif before.channel != after.channel:
                player.check_alone()
            return
        channel = player.channel
        if channel and (before.channel == channel or after.channel == channel):
            player.check_alone()
