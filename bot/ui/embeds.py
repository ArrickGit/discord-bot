"""Embed builders."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import discord

from ..queue import LoopMode
from .formatting import (
    ACCENT,
    escape,
    fmt_duration,
    progress_bar,
    source_label,
    track_length,
    track_link,
    truncate,
)

if TYPE_CHECKING:
    from ..player import GuildPlayer

LOOP_LABELS = {LoopMode.OFF: "Off", LoopMode.SONG: "🔂 Song", LoopMode.QUEUE: "🔁 Queue"}
QUEUE_PAGE_SIZE = 10


def now_playing(player: GuildPlayer) -> discord.Embed:
    track = player.queue.current
    if track is None:
        return discord.Embed(title="Nothing playing", colour=ACCENT)
    state = "⏸️ Paused" if player.is_paused else "🎶 Now playing"
    embed = discord.Embed(title=state, description=f"**{track_link(track, 120)}**", colour=ACCENT)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    position = player.position()
    if track.is_live:
        embed.add_field(name="Length", value="🔴 LIVE")
    else:
        bar = progress_bar(position, track.duration)
        timing = f"{fmt_duration(position)} / {track_length(track)}"
        embed.add_field(name="Progress", value=f"{bar}\n{timing}" if bar else timing, inline=False)
    embed.add_field(name="Requested by", value=f"<@{track.requester_id}>" if track.requester_id else "Autoplay")
    embed.add_field(name="Source", value=source_label(track))
    embed.add_field(name="Volume", value=f"{player.volume}%")
    nxt = player.queue.peek()
    footer = [f"Loop: {LOOP_LABELS[player.queue.loop]}", f"{len(player.queue)} in queue"]
    if player.autoplay:
        footer.append("Autoplay on")
    if nxt:
        embed.add_field(name="Up next", value=track_link(nxt, 60), inline=False)
    embed.set_footer(text=" • ".join(footer))
    return embed


def idle(player: GuildPlayer, leave_at: float | None) -> discord.Embed:
    text = "The queue is empty. Add more with `/play`."
    if leave_at:
        text += f"\nI'll leave the voice channel <t:{int(leave_at)}:R> if nothing is added."
    return discord.Embed(title="Queue finished", description=text, colour=ACCENT)


def disconnected(reason: str) -> discord.Embed:
    return discord.Embed(title="Disconnected", description=reason, colour=ACCENT)


def queue_page(player: GuildPlayer, page: int) -> tuple[discord.Embed, int]:
    upcoming = player.queue.upcoming
    pages = max(1, math.ceil(len(upcoming) / QUEUE_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    embed = discord.Embed(title="Queue", colour=ACCENT)
    current = player.queue.current
    if current:
        embed.add_field(
            name="Now playing",
            value=f"{track_link(current, 70)} `{track_length(current)}` — <@{current.requester_id}>"
            if current.requester_id
            else f"{track_link(current, 70)} `{track_length(current)}`",
            inline=False,
        )
    start = page * QUEUE_PAGE_SIZE
    lines = []
    for i, track in enumerate(upcoming[start : start + QUEUE_PAGE_SIZE], start=start + 1):
        lines.append(f"`{i}.` {track_link(track, 55)} `{track_length(track)}`")
    embed.add_field(
        name="Up next",
        value=truncate("\n".join(lines), 1024) if lines else "Nothing queued. Add songs with `/play`.",
        inline=False,
    )
    total = player.queue.total_duration()
    embed.set_footer(
        text=f"Page {page + 1}/{pages} • {len(upcoming)} songs • {fmt_duration(total)} total • "
        f"Loop: {LOOP_LABELS[player.queue.loop]}"
    )
    return embed, pages


def error(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", colour=discord.Colour.from_rgb(237, 66, 69))


def added(title: str, detail: str) -> discord.Embed:
    return discord.Embed(description=f"**{escape(title)}**\n{detail}", colour=ACCENT)
