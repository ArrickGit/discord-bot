"""Connection, playback and queue commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import controls
from ..permissions import Action, ControlError, require
from ..queue import LoopMode, QueueError
from ..sources import detect
from ..ui import embeds
from ..ui.formatting import escape, fmt_duration, truncate
from ..ui.views import QueueView
from .common import NO_PINGS, ensure_player, requester, starts_now

if TYPE_CHECKING:
    from ..client import MusicBot

log = logging.getLogger(__name__)


class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    # ----- connection -------------------------------------------------
    @app_commands.command(name="join", description="Join your voice channel")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await ensure_player(interaction)
        await interaction.followup.send(f"👋 Joined {player.channel.mention}.", ephemeral=True)  # type: ignore[union-attr]

    @app_commands.command(name="leave", description="Disconnect and clear this server's queue")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        player = controls.get_player(interaction)
        require(interaction, player, Action.CONTROL)
        await interaction.response.send_message("👋 Leaving the voice channel.", ephemeral=True)
        await player.destroy(f"{interaction.user.mention} disconnected me.", announce=True)

    @app_commands.command(name="play", description="Play or queue a YouTube, SoundCloud or Spotify link, or search YouTube")
    @app_commands.describe(
        query="A link, or words to search YouTube for",
        next="Put it at the front of the queue instead of the end",
    )
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 2048], next: bool = False) -> None:
        detected = detect(query)  # fast validation before anything else
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await ensure_player(interaction)
        if next:
            try:
                require(interaction, player, Action.CONTROL)
            except ControlError:
                raise ControlError("You can't put songs at the front of the queue here. Leave `next` off.") from None
        if player.queue.space_left == 0:
            raise QueueError(f"The queue is full ({player.queue.max_size} songs).")

        if detected.is_collection:
            await interaction.edit_original_response(content="⏳ Processing playlist…")

        uid, name = requester(interaction)
        limit = min(self.bot.config.maximum_playlist_size, player.queue.space_left)
        result = await self.bot.resolver.resolve_detected(detected, uid, name, limit=limit)
        if player.destroyed:
            raise ControlError("I left the voice channel while that was loading.")

        immediate = starts_now(player)
        shuffled = player.shuffle_for_autoplay(result.tracks)
        added, dropped = player.enqueue(result.tracks, front=next)
        if not added:
            raise QueueError("The queue is full.")

        if result.is_collection:
            parts = [f"✅ Added **{added}** songs from **{escape(truncate(result.collection_title or 'playlist', 80))}**."]
            skipped = result.skipped
            if skipped:
                parts.append(f"{skipped} unavailable song{'s were' if skipped != 1 else ' was'} skipped.")
            if result.truncated or dropped:
                parts.append(f"Stopped at the limit of {limit} songs.")
            parts.extend(result.notes)
            if shuffled:
                parts.append("🔀 Shuffled because autoplay is on.")
            await interaction.edit_original_response(content=" ".join(parts))
            await self._announce(
                player,
                f"🎶 {interaction.user.mention} added **{added}** songs from "
                f"**{escape(truncate(result.collection_title or 'a playlist', 80))}**.",
            )
            return

        track = result.tracks[0]
        title = escape(truncate(track.display_title, 90))
        if immediate:
            await interaction.edit_original_response(content=f"▶️ Starting **{title}**.")
            return
        position = 1 if next else len(player.queue)
        wait = (player.queue.current.duration or 0) - player.position() if player.queue.current else 0
        wait += sum(t.duration or t.expected_duration or 0 for t in player.queue.upcoming[: position - 1])
        await interaction.edit_original_response(
            content=f"✅ Queued **{title}** at position {position} (plays in about {fmt_duration(max(0, wait))})."
        )
        await self._announce(player, f"🎵 {interaction.user.mention} queued **{title}**.")

    async def _announce(self, player, text: str) -> None:  # type: ignore[no-untyped-def]
        if player.text_channel is None:
            return
        try:
            await player.text_channel.send(text, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass

    # ----- playback ---------------------------------------------------
    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        message = await controls.pause(interaction, controls.get_player(interaction))
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        message = await controls.resume(interaction, controls.get_player(interaction))
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        message, public = await controls.skip(interaction, controls.get_player(interaction))
        await interaction.response.send_message(message, ephemeral=not public, allowed_mentions=NO_PINGS)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        message = await controls.stop(interaction, controls.get_player(interaction))
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="volume", description="Show or change this server's playback volume")
    @app_commands.describe(level="Volume from 1 to 100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100] | None = None) -> None:
        player = controls.get_player(interaction)
        if level is None:
            await interaction.response.send_message(f"🔊 Volume is {player.volume}%.", ephemeral=True)
            return
        require(interaction, player, Action.CONTROL)
        player.set_volume(level)
        await interaction.response.send_message(f"🔊 Volume set to {player.volume}%.", ephemeral=True)
        await player.update_now_playing()

    @app_commands.command(name="shuffle", description="Shuffle the upcoming songs")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        message = await controls.shuffle(interaction, controls.get_player(interaction))
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="loop", description="Repeat nothing, the current song, or the whole queue")
    @app_commands.guild_only()
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="song", value="song"),
            app_commands.Choice(name="queue", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        message = await controls.set_loop(interaction, controls.get_player(interaction), LoopMode(mode.value))
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the current song (moves the player message here)")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = controls.get_player(interaction)
        if player.queue.current is None:
            raise ControlError("Nothing is playing.")
        if isinstance(interaction.channel, discord.abc.Messageable):
            if player.text_channel is None or getattr(player.text_channel, "id", None) != interaction.channel.id:
                old = player.np_message
                player.np_message = None
                if old is not None:
                    try:
                        await old.delete()
                    except discord.HTTPException:
                        pass
                player.text_channel = interaction.channel
        await interaction.response.send_message("🎶 Here's what's playing:", ephemeral=True, delete_after=5)
        await player.update_now_playing(new_track=True)

    # ----- queue ------------------------------------------------------
    @app_commands.command(name="queue", description="Show the current and upcoming songs")
    @app_commands.describe(page="Page number")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1) -> None:
        player = controls.get_player(interaction)
        view = QueueView(player, interaction.user.id, page - 1)
        embed, pages = embeds.queue_page(player, view.page)
        await interaction.response.send_message(embed=embed, view=view if pages > 1 else discord.utils.MISSING)
        view.message = await interaction.original_response()

    @app_commands.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Position in the queue (see /queue)")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 10_000]) -> None:
        player = controls.get_player(interaction)
        upcoming = player.queue.upcoming
        own = 1 <= position <= len(upcoming) and upcoming[position - 1].requester_id == interaction.user.id
        if own:
            require(interaction, player, Action.ADD)  # anyone can remove their own song
        else:
            require(interaction, player, Action.CONTROL)
        track = player.queue.remove(position)
        await interaction.response.send_message(
            f"🗑️ Removed **{escape(truncate(track.display_title, 90))}**.", ephemeral=True
        )
        await player.update_now_playing()

    @app_commands.command(name="move", description="Move a song to another queue position")
    @app_commands.describe(from_position="Current position", to_position="New position")
    @app_commands.rename(from_position="from", to_position="to")
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        from_position: app_commands.Range[int, 1, 10_000],
        to_position: app_commands.Range[int, 1, 10_000],
    ) -> None:
        player = controls.get_player(interaction)
        require(interaction, player, Action.CONTROL)
        track = player.queue.move(from_position, to_position)
        await interaction.response.send_message(
            f"↕️ Moved **{escape(truncate(track.display_title, 90))}** to position {to_position}.", ephemeral=True
        )
        await player.update_now_playing()

    @app_commands.command(name="clear", description="Remove all upcoming songs (the current song keeps playing)")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        player = controls.get_player(interaction)
        require(interaction, player, Action.CONTROL)
        count = player.queue.clear()
        await interaction.response.send_message(f"🧹 Cleared {count} upcoming songs.", ephemeral=True)
        await player.update_now_playing()


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Music(bot))
