"""Who may control music. Pure decision logic plus a Discord adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from .db import GuildSettings
    from .player import GuildPlayer


class ControlError(app_commands.CheckFailure):
    """A permission/state problem whose message is shown to the user ephemerally."""


class Action(StrEnum):
    ADD = "add"          # /play, /playlist play, /random
    CONTROL = "control"  # pause, volume, shuffle, loop, remove, move, clear, stop, leave
    SKIP = "skip"        # skip (requester may skip their own song; vote-skip may apply)


@dataclass(frozen=True)
class ControlContext:
    is_owner: bool
    is_moderator: bool
    has_dj_role: bool
    user_channel_id: int | None
    bot_channel_id: int | None
    listeners: int
    """Non-bot members in the bot's voice channel."""
    is_requester: bool = False


def check_control(ctx: ControlContext, mode: str, action: Action) -> None:
    """Raise :class:`ControlError` if the action isn't allowed."""
    if ctx.is_owner:
        return
    if ctx.user_channel_id is None:
        raise ControlError("Join a voice channel first.")
    if ctx.bot_channel_id is not None and ctx.user_channel_id != ctx.bot_channel_id:
        raise ControlError(f"You need to be in <#{ctx.bot_channel_id}> with me to do that.")
    if action is Action.ADD or mode == "everyone":
        return
    alone = ctx.bot_channel_id is not None and ctx.listeners <= 1
    if alone or ctx.is_moderator:
        return
    if action is Action.SKIP and ctx.is_requester:
        return
    if mode == "dj" and ctx.has_dj_role:
        return
    if mode == "dj":
        raise ControlError("Only DJs and moderators can do that in this server.")
    raise ControlError("Only moderators can control the music in this server.")


def is_moderator(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.move_members


def context_for(interaction: discord.Interaction, player: GuildPlayer | None) -> ControlContext:
    member = interaction.user
    if not isinstance(member, discord.Member):
        raise ControlError("This only works inside a server.")
    bot = interaction.client
    settings = player.settings if player else None
    voice_channel = member.voice.channel if member.voice else None
    bot_channel = player.channel if player else None
    dj_role_id = settings.dj_role_id if settings else None
    current = player.queue.current if player else None
    return ControlContext(
        is_owner=member.id in bot.owner_ids,  # type: ignore[attr-defined]
        is_moderator=is_moderator(member),
        has_dj_role=bool(dj_role_id and member.get_role(dj_role_id)),
        user_channel_id=voice_channel.id if voice_channel else None,
        bot_channel_id=bot_channel.id if bot_channel else None,
        listeners=player.listener_count() if player else 0,
        is_requester=bool(current and current.requester_id == member.id),
    )


def require(interaction: discord.Interaction, player: GuildPlayer | None, action: Action) -> None:
    mode = player.settings.control_mode if player else "everyone"
    check_control(context_for(interaction, player), mode, action)


def require_setting_control(interaction: discord.Interaction, settings: GuildSettings) -> None:
    """For control actions while the bot isn't connected (no voice-channel requirement)."""
    member = interaction.user
    if not isinstance(member, discord.Member):
        raise ControlError("This only works inside a server.")
    if settings.control_mode == "everyone" or member.id in interaction.client.owner_ids or is_moderator(member):  # type: ignore[attr-defined]
        return
    if settings.control_mode == "dj" and settings.dj_role_id and member.get_role(settings.dj_role_id):
        return
    raise ControlError("Only DJs and moderators can change that in this server.")
