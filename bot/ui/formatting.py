"""Text helpers shared by embeds and command replies."""

from __future__ import annotations

import discord

from ..models import PLATFORM_LABELS, Track

ACCENT = discord.Colour.from_rgb(88, 101, 242)
ERROR = discord.Colour.from_rgb(237, 66, 69)
SUCCESS = discord.Colour.from_rgb(87, 242, 135)


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "?:??"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def track_length(track: Track) -> str:
    if track.is_live:
        return "LIVE"
    return fmt_duration(track.duration or track.expected_duration)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def escape(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text or ""))


def track_link(track: Track, limit: int = 80) -> str:
    title = escape(truncate(track.display_title, limit)).replace("]", "\\]").replace("[", "\\[")
    url = track.display_url
    return f"[{title}]({url})" if url else title


def source_label(track: Track) -> str:
    if track.origin_platform and track.origin_platform != track.platform:
        origin = PLATFORM_LABELS.get(track.origin_platform, str(track.origin_platform))
        if track.resolved:
            return f"{origin} → matched on {PLATFORM_LABELS[track.platform]}"
        return f"{origin} (matched when played)"
    return PLATFORM_LABELS.get(track.platform, str(track.platform))


def progress_bar(position: float, duration: int | None, width: int = 16) -> str:
    if not duration:
        return ""
    filled = min(width, int(width * position / duration))
    return "▬" * filled + "🔘" + "▬" * (width - filled)
