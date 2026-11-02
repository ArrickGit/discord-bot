"""Helpers shared by command cogs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..models import Track
from ..permissions import Action, ControlError, require

if TYPE_CHECKING:
    from ..client import MusicBot
    from ..player import GuildPlayer

NO_PINGS = discord.AllowedMentions.none()


async def ensure_player(interaction: discord.Interaction, *, connect: bool = True) -> GuildPlayer:
    """Validate the caller and return a connected player for their server."""
    bot: MusicBot = interaction.client  # type: ignore[assignment]
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        raise ControlError("This only works inside a server.")
    is_owner = member.id in bot.owner_ids
    if bot.maintenance and not is_owner:
        raise ControlError(f"🛠️ The bot is in maintenance mode: {bot.maintenance}")
    if not bot.ffmpeg_available:
        raise ControlError("FFmpeg is missing on the bot's server, so music can't play. Tell the bot owner.")
    if member.voice is None or member.voice.channel is None:
        raise ControlError("Join a voice channel first.")

    player = await bot.players.get_or_create(guild)
    require(interaction, player if player.channel else None, Action.ADD)
    if player.channel is None and connect:
        await player.connect(member.voice.channel)
    if isinstance(interaction.channel, discord.abc.Messageable):
        player.text_channel = interaction.channel
    return player


def requester(interaction: discord.Interaction) -> tuple[int, str]:
    return interaction.user.id, interaction.user.display_name


def starts_now(player: GuildPlayer) -> bool:
    return player.queue.current is None and player.queue.is_empty


def stamp_requester(tracks: list[Track], interaction: discord.Interaction) -> list[Track]:
    uid, name = requester(interaction)
    return [t.copy_for(uid, name) for t in tracks]
