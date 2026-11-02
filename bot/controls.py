"""Playback actions shared by slash commands and now-playing buttons.

Each function performs the permission check, does the action and returns the
message to show the user.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import discord

from .permissions import Action, ControlError, require
from .queue import LoopMode
from .ui.formatting import escape, truncate

if TYPE_CHECKING:
    from .player import GuildPlayer

LOOP_CYCLE = {LoopMode.OFF: LoopMode.QUEUE, LoopMode.QUEUE: LoopMode.SONG, LoopMode.SONG: LoopMode.OFF}
LOOP_TEXT = {
    LoopMode.OFF: "Loop is off.",
    LoopMode.SONG: "🔂 Looping the current song.",
    LoopMode.QUEUE: "🔁 Looping the whole queue.",
}


def get_player(interaction: discord.Interaction) -> GuildPlayer:
    if interaction.guild is None:
        raise ControlError("This only works inside a server.")
    player = interaction.client.players.get(interaction.guild.id)  # type: ignore[attr-defined]
    if player is None or player.channel is None:
        raise ControlError("I'm not in a voice channel. Use `/play` to start.")
    return player


def _title(player: GuildPlayer) -> str:
    current = player.queue.current
    return escape(truncate(current.display_title, 80)) if current else "the song"


async def toggle_pause(interaction: discord.Interaction, player: GuildPlayer) -> str:
    require(interaction, player, Action.CONTROL)
    if player.is_paused:
        player.resume()
        msg = "▶️ Resumed."
    elif player.pause():
        msg = "⏸️ Paused."
    else:
        raise ControlError("Nothing is playing.")
    await player.update_now_playing()
    return msg


async def pause(interaction: discord.Interaction, player: GuildPlayer) -> str:
    require(interaction, player, Action.CONTROL)
    if player.is_paused:
        raise ControlError("Already paused. Use `/resume`.")
    if not player.pause():
        raise ControlError("Nothing is playing.")
    await player.update_now_playing()
    return "⏸️ Paused."


async def resume(interaction: discord.Interaction, player: GuildPlayer) -> str:
    require(interaction, player, Action.CONTROL)
    if not player.resume():
        raise ControlError("Playback isn't paused.")
    await player.update_now_playing()
    return "▶️ Resumed."


async def skip(interaction: discord.Interaction, player: GuildPlayer) -> tuple[str, bool]:
    """Returns ``(message, public)``. Vote-skip progress is announced publicly."""
    if player.queue.current is None or not player.is_playing:
        raise ControlError("Nothing is playing.")
    title = _title(player)
    try:
        require(interaction, player, Action.SKIP)
    except ControlError:
        if not player.settings.vote_skip:
            raise
        # Same-channel requirement still applies to voting.
        require(interaction, player, Action.ADD)
        player.skip_votes.add(interaction.user.id)
        listeners = set(player.listener_ids())
        votes = len(player.skip_votes & listeners)
        needed = max(1, math.ceil(len(listeners) * player.settings.vote_skip_ratio))
        if votes < needed:
            return f"🗳️ {interaction.user.mention} voted to skip **{title}** ({votes}/{needed}).", True
        player.skip()
        return f"⏭️ Vote passed. Skipped **{title}**.", True
    player.skip()
    return f"⏭️ Skipped **{title}**.", False


async def stop(interaction: discord.Interaction, player: GuildPlayer) -> str:
    require(interaction, player, Action.CONTROL)
    player.stop()
    return "⏹️ Stopped playback and cleared the queue."


async def shuffle(interaction: discord.Interaction, player: GuildPlayer) -> str:
    require(interaction, player, Action.CONTROL)
    if len(player.queue) < 2:
        raise ControlError("There need to be at least two upcoming songs to shuffle.")
    player.queue.shuffle()
    await player.update_now_playing()
    return f"🔀 Shuffled {len(player.queue)} upcoming songs."


async def set_loop(interaction: discord.Interaction, player: GuildPlayer, mode: LoopMode | None = None) -> str:
    require(interaction, player, Action.CONTROL)
    mode = mode or LOOP_CYCLE[player.queue.loop]
    player.queue.loop = mode
    await interaction.client.db.update_guild_settings(player.guild.id, loop_mode=mode.value)  # type: ignore[attr-defined]
    player.settings.loop_mode = mode.value
    await player.update_now_playing()
    return LOOP_TEXT[mode]
