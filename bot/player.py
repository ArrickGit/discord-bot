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
from typing import TYPE_CHECKING, Any

import discord

from .db import GuildSettings, SavedSession
from .models import Track
from .queue import LoopMode, MusicQueue
from .sources.errors import SourceBlockedError, SourceError
from .ui import embeds
from .ui.formatting import fmt_duration

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5
PAUSED_TIMEOUT_MULTIPLIER = 6
# Never wait longer than this for a stream link to become usable.
MAX_AVAILABILITY_WAIT = 30.0
# After the queue changes, wait this long before getting the next song ready, so a
# burst of edits (several /move or /remove) only triggers one lookup.
PREFETCH_DELAY = 1.0
# Pick the autoplay song this long before the current one ends: late enough that
# ratings made during the song still count, early enough to get it ready in time.
AUTOPLAY_LOOKAHEAD = 45.0
# The queue is saved (debounced) after every change so a restart can pick it up.
SAVE_DELAY = 0.5
# While playing, also save the position this often, so a crash loses at most this much.
AUTOSAVE_INTERVAL = 30.0
# Only pick a saved queue back up if the bot was down for less than this.
RESTORE_MAX_AGE = 30 * 60


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
        self._prefetch_target: Track | None = None  # the song the prefetch task is for
        self._prefetch_loading: Track | None = None  # set while it's actually looking that song up
        self._autoplay_task: asyncio.Task[None] | None = None
        self._autoplay_next: Track | None = None  # autoplay's pick for after the current song
        self._save_task: asyncio.Task[None] | None = None
        self._autosave_task: asyncio.Task[None] | None = None
        # After a restart: start this track this many seconds in (and maybe paused).
        self._resume_from: tuple[Track, float] | None = None
        self._resume_paused = False
        self._start_offset = 0.0  # seconds skipped at the start of the current track
        self._np_lock = asyncio.Lock()
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._auto_paused = False
        self._failures = 0
        self._autoplay_misses = 0
        self.destroyed = False
        # Set last: the hook uses the attributes above.
        self.queue.on_change = self._queue_changed

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

    @property
    def _tag(self) -> str:
        """``[Server name]`` prefix for log lines."""
        return f"[{getattr(self.guild, 'name', None) or self.guild.id}]"

    def position(self) -> float:
        """Seconds into the current track (including any part skipped when resuming)."""
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
        log.info("%s Joined voice channel \"%s\"", self._tag, channel.name)
        self.start()
        self.check_alone()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"player-{self.guild.id}")
        if self._autosave_task is None or self._autosave_task.done():
            self._autosave_task = asyncio.create_task(self._autosave_loop())

    # ----- queue operations -------------------------------------------
    def enqueue(self, tracks: list[Track], *, front: bool = False) -> tuple[int, int]:
        added, dropped = self.queue.add_many(tracks, front=front)
        if added:
            self._autoplay_misses = 0
            self._wake.set()
            first = tracks[0]
            where = "to play next" if front else f"at position {len(self.queue) if added == 1 else len(self.queue) - added + 1}"
            what = f'"{first.display_title}"' if added == 1 else f'{added} songs, starting with "{first.display_title}"'
            extra = f"; {dropped} didn't fit" if dropped else ""
            log.info("%s Queued %s %s (by %s)%s", self._tag, what, where, first.requester_id or "autoplay", extra)
        return added, dropped

    def pause(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self._paused_at = time.monotonic()
            self._start_idle_timer(paused=True)
            log.info("%s Paused at %s", self._tag, fmt_duration(self.position()))
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
            log.info("%s Resumed at %s", self._tag, fmt_duration(self.position()))
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
                    track, self._autoplay_next = self._autoplay_next, None
                    if track is None:
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
        started = False
        offset = 0.0
        if self._resume_from is not None and self._resume_from[0] is track:
            offset = 0.0 if track.is_live else max(0.0, self._resume_from[1])
        self._resume_from = None
        try:
            for attempt in range(2):
                await self._await_prefetch(track)
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
                self._start_offset = offset
                try:
                    source = self._make_source(track)
                except discord.ClientException:
                    log.error("FFmpeg not found at %r", self.bot.config.ffmpeg_path)
                    await self._send_error("FFmpeg is missing on the bot's server, so nothing can play.")
                    self.queue.stop()
                    return

                self._finished.clear()
                self._play_error = None
                self._started_at = time.monotonic() - offset
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
                    started = True
                    self._failures = 0
                    self._log_now_playing(track, offset)
                    if self._resume_paused:
                        self._resume_paused = False
                        self.pause()
                    history_id = await self._record_start(track)
                    await self.update_now_playing(new_track=True)
                    self._schedule_prefetch()
                    self._schedule_autoplay_pick(track)

                await self._finished.wait()

                played = self.position() - offset  # time actually played this attempt
                died_early = (
                    self._end_reason is None
                    and not track.is_live
                    and (track.duration or 0) > 15
                    and played < 3
                )
                if died_early and attempt == 0 and not self.destroyed:
                    # Usually an expired or rejected stream URL: refresh it once.
                    log.info(
                        "%s Stream for \"%s\" ended after %.1fs; retrying with a fresh link",
                        self._tag, track.display_title, played,
                    )
                    track.clear_stream()
                    continue
                if died_early:
                    await self._track_failed(track, SourceError("The audio stream kept failing."))
                break
        finally:
            reason = self._end_reason or ("error" if self._play_error else "completed")
            if started:
                log.info(
                    "%s Finished \"%s\" after %s (%s)",
                    self._tag, track.display_title, fmt_duration(self.position()), reason,
                )
            if history_id is not None:
                await self._record_end(history_id, self.position(), reason)
            self._started_at = None
            self._start_offset = 0.0

    def _log_now_playing(self, track: Track, offset: float) -> None:
        details = [fmt_duration(track.duration) if track.duration else ("live" if track.is_live else "length unknown")]
        details.append("autoplay pick" if track.via == "autoplay" else f"requested by {track.requester_id}")
        if offset >= 1:
            details.append(f"resumed from {fmt_duration(offset)}")
        if self.queue.loop is not LoopMode.OFF:
            details.append(f"loop: {self.queue.loop.value}")
        details.append(f"{len(self.queue)} more queued")
        log.info("%s Now playing \"%s\" (%s)", self._tag, track.display_title, ", ".join(details))

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
        if self._start_offset > 0:
            before += ["-ss", f"{self._start_offset:.1f}"]  # resume part-way through after a restart
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
        log.info("%s Skipping \"%s\": %s", self._tag, track.display_title, exc)
        self.queue.drop_current()
        # Platform blocks tend to affect every song, so give up sooner on those.
        limit = 2 if isinstance(exc, SourceBlockedError) else MAX_CONSECUTIVE_FAILURES
        if self._failures >= limit:
            extra = ""
            if self.autoplay:
                self.set_autoplay(False)  # otherwise autoplay would keep feeding failing songs
                extra = " Autoplay is paused until it's turned back on."
            await self._send_error(
                f"{self._failures} songs in a row failed to play, so I've cleared the queue.{extra} "
                f"Last error: {exc}"
            )
            self.queue.stop()
            self._failures = 0
            return
        await self._send_error(f"Skipped **{embeds.escape(track.display_title)}**: {exc}")

    # ----- saving the queue across restarts ----------------------------
    def _queue_changed(self) -> None:
        self._schedule_prefetch()
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self.destroyed:
            return
        self._cancel(self._save_task)
        self._save_task = asyncio.create_task(self._save_soon())

    async def _save_soon(self) -> None:
        await asyncio.sleep(SAVE_DELAY)  # one save for a burst of changes
        await self.save_session()

    async def _autosave_loop(self) -> None:
        while not self.destroyed:
            await asyncio.sleep(AUTOSAVE_INTERVAL)
            if self.is_playing and not self.is_paused:
                await self.save_session()

    def snapshot(self) -> dict[str, Any] | None:
        """Everything needed to pick the music back up after a restart, or None if nothing's queued."""
        current = self.queue.current
        upcoming = self.queue.upcoming
        if current is None and not upcoming:
            return None
        position = self.position() if current is not None and self._started_at is not None else 0.0
        return {
            "current": current.to_dict() if current is not None else None,
            "position": round(position, 1),
            "upcoming": [t.to_dict() for t in upcoming],
            "loop": self.queue.loop.value,
            "volume": self.volume,
            "autoplay": self.autoplay,
            # Paused by someone (not because everyone left): stay paused after the restart.
            "paused": self.is_paused and not self._auto_paused,
        }

    async def save_session(self) -> None:
        """Save the queue so a restart can pick it up; forget it once nothing's left to play."""
        if self.destroyed:
            return
        data = self.snapshot()
        channel = self.channel
        try:
            if data is None:
                await self.bot.db.delete_session(self.guild.id)
            elif channel is not None:
                await self.bot.db.save_session(self.guild.id, channel.id, getattr(self.text_channel, "id", None), data)
        except Exception:
            log.debug("Could not save the queue for guild %s", self.guild.id, exc_info=True)

    def load_snapshot(self, data: dict[str, Any]) -> tuple[Track | None, int]:
        """Queue up a saved session. Returns ``(current track, songs queued after it)``."""
        current = Track.from_dict(data["current"]) if data.get("current") else None
        upcoming = [Track.from_dict(t) for t in data.get("upcoming") or []]
        try:
            self.queue.loop = LoopMode(data.get("loop") or LoopMode.OFF.value)
        except ValueError:
            pass
        if isinstance(data.get("volume"), int):
            self.set_volume(data["volume"])
        self.autoplay = bool(data.get("autoplay", self.autoplay))
        if current is not None:
            self._resume_from = (current, float(data.get("position") or 0.0))
            self._resume_paused = bool(data.get("paused"))
        self.enqueue(([current] if current is not None else []) + upcoming)
        return current, len(upcoming)

    # ----- prefetch & autoplay ----------------------------------------
    def _next_up(self) -> Track | None:
        """The song that plays after the current one, if it's known yet.

        Loops need nothing extra: a repeating song or queue keeps its stream link.
        """
        return self.queue.peek() or (self._autoplay_next if self.autoplay else None)

    def _schedule_prefetch(self) -> None:
        """Get the next song ready in the background, so it starts without a gap.

        Runs whenever the queue changes and when a song starts.
        """
        if self.destroyed:
            return
        nxt = self._next_up()
        if nxt is not None and nxt is self._prefetch_target and self._prefetch_task is not None:
            return  # already getting this song ready (or done)
        running = self._prefetch_task is not None and not self._prefetch_task.done()
        if running and self._prefetch_target is self.queue.current:
            # Its turn just came: _play is about to wait for this lookup, so let it finish.
            # The next song gets prefetched when this one starts playing.
            return
        self._cancel(self._prefetch_task)
        self._prefetch_target = nxt
        self._prefetch_task = asyncio.create_task(self._prefetch(nxt)) if nxt is not None else None

    async def _prefetch(self, track: Track) -> None:
        await asyncio.sleep(PREFETCH_DELAY)  # let a burst of queue edits settle
        self._prefetch_loading = track
        try:
            await self.bot.resolver.prepare(track)
            log.info("%s Got \"%s\" ready to play next", self._tag, track.display_title)
        except SourceError:
            pass  # Reported properly when it's this track's turn.
        except Exception:
            log.debug("Prefetch failed", exc_info=True)
        finally:
            if self._prefetch_loading is track:
                self._prefetch_loading = None

    async def _await_prefetch(self, track: Track) -> None:
        """If the prefetch is already looking this song up, wait for it instead of doing it twice."""
        task = self._prefetch_task
        if task is None or task.done() or self._prefetch_target is not track:
            return
        if self._prefetch_loading is not track:
            self._cancel(task)  # still in its settle delay: just load the song now
            return
        await asyncio.wait({task})  # doesn't cancel the prefetch if we're cancelled

    def set_autoplay(self, enabled: bool) -> None:
        self.autoplay = enabled
        self._autoplay_misses = 0
        current = self.queue.current
        if enabled and current is not None and self.is_playing:
            self._schedule_autoplay_pick(current)
        elif not enabled:
            self._cancel(self._autoplay_task)
            self._autoplay_next = None

    def _schedule_autoplay_pick(self, track: Track) -> None:
        """Pick autoplay's next song shortly before ``track`` ends, so it can be got ready in time."""
        self._cancel(self._autoplay_task)
        self._autoplay_next = None
        if self.autoplay and track.duration and not track.is_live:
            self._autoplay_task = asyncio.create_task(self._autoplay_pick_ahead(track))

    async def _autoplay_pick_ahead(self, track: Track) -> None:
        # Pausing stretches the wait, because position() stops while paused.
        while True:
            if self.destroyed or self.queue.current is not track:
                return
            remaining = (track.duration or 0) - self.position()
            if remaining <= AUTOPLAY_LOOKAHEAD:
                break
            await asyncio.sleep(min(remaining - AUTOPLAY_LOOKAHEAD, 5.0))
        if not self.autoplay or self.queue.loop is not LoopMode.OFF or not self.queue.is_empty:
            return  # something else plays next
        pick = await self._autoplay_pick(count_miss=False)  # the end-of-song pick counts misses
        if pick is None or self.destroyed or self.queue.current is not track or not self.queue.is_empty:
            return
        self._autoplay_next = pick
        self._schedule_prefetch()

    async def _autoplay_pick(self, *, count_miss: bool = True) -> Track | None:
        if self._autoplay_misses >= 3:
            return None
        try:
            track = await self.bot.recommender.recommend(
                self.guild.id, self.listener_ids(), exclude=self._recent_keys(), via="autoplay"
            )
        except Exception:
            log.exception("Autoplay failed in guild %s", self.guild.id)
            track = None
        if track is None and count_miss:
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
        if self.queue.history:  # not on a fresh join, before anything has played
            log.info(
                "%s Queue finished; leaving in %s unless something is added",
                self._tag, fmt_duration(max(0, leave_at - time.time())),
            )
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

    async def destroy(self, reason: str = "Disconnected.", *, announce: bool = True, keep_saved: bool = False) -> None:
        """Stop and leave. ``keep_saved`` keeps the saved queue (used when the bot restarts)."""
        if self.destroyed:
            return
        self.destroyed = True
        self.bot.players.forget(self.guild.id)
        for task in (
            self._idle_task,
            self._alone_task,
            self._prefetch_task,
            self._autoplay_task,
            self._save_task,
            self._autosave_task,
        ):
            self._cancel(task)
        if not keep_saved:
            try:
                await self.bot.db.delete_session(self.guild.id)
            except Exception:
                log.debug("Could not clear the saved queue for guild %s", self.guild.id, exc_info=True)
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
        log.info("%s Left voice: %s", self._tag, reason)


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

    async def destroy_all(self, reason: str, *, keep_saved: bool = False) -> int:
        players = self.all()
        await asyncio.gather(*(p.destroy(reason, keep_saved=keep_saved) for p in players), return_exceptions=True)
        return len(players)

    async def save_all(self) -> None:
        await asyncio.gather(*(p.save_session() for p in self.all()), return_exceptions=True)

    async def restore_saved(self) -> int:
        """After a restart, pick up each server's music where it left off. Returns how many."""
        try:
            sessions = await self.bot.db.take_sessions(RESTORE_MAX_AGE)
        except Exception:
            log.exception("Couldn't load saved queues")
            return 0
        restored = 0
        for session in sessions:
            try:
                restored += await self._restore(session)
            except Exception:
                log.exception("Couldn't restore the queue for guild %s", session.guild_id)
        return restored

    async def _restore(self, session: SavedSession) -> bool:
        guild = self.bot.get_guild(session.guild_id)
        channel = guild.get_channel(session.voice_channel_id) if guild else None
        if guild is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return False
        if not any(not m.bot for m in channel.members):
            log.info("Not restoring the queue for guild %s: nobody is in the voice channel", guild.id)
            return False
        player = await self.get_or_create(guild)
        text = guild.get_channel(session.text_channel_id) if session.text_channel_id else None
        if isinstance(text, discord.abc.Messageable):
            player.text_channel = text
        await player.connect(channel)
        current, more = player.load_snapshot(session.data)
        log.info("Restored the queue for guild %s after a restart", guild.id)
        if player.text_channel is not None and (current is not None or more):
            what = f"**{embeds.escape(current.display_title)}**" if current else "the queue"
            at = session.data.get("position") or 0
            where = f" from {fmt_duration(at)}" if current and at >= 1 else ""
            tail = f", with {more} more queued" if current and more else ""
            try:
                await player.text_channel.send(
                    f"\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} I restarted. "
                    f"Picking up where we left off: {what}{where}{tail}.",
                    allowed_mentions=discord.AllowedMentions.none(),
                    delete_after=120,
                )
            except discord.HTTPException:
                pass
        return True

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
