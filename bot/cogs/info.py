"""/help, /ping, /about."""

from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from .. import __version__
from ..ui.formatting import ACCENT, fmt_duration

if TYPE_CHECKING:
    from ..client import MusicBot

HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "🎵 Playing music",
        [
            ("/play <link or search>", "Play or queue a YouTube, SoundCloud or Spotify link, or search YouTube. "
             "Add `next: True` to put it at the front."),
            ("/join · /leave", "Join your voice channel / disconnect and clear the queue"),
            ("/pause · /resume · /skip · /stop", "Playback controls (also available as buttons)"),
            ("/volume [1-100]", "Show or change this server's volume"),
            ("/loop off|song|queue · /shuffle", "Repeat and shuffle"),
            ("/nowplaying", "Show the player message here"),
        ],
    ),
    (
        "📜 Queue",
        [
            ("/queue [page]", "Show upcoming songs"),
            ("/remove <position> · /move <from> <to> · /clear", "Edit the queue"),
        ],
    ),
    (
        "⭐ Your taste",
        [
            ("/rate", "Rate the current song (only you see it)"),
            ("/random", "Queue a song picked for you"),
            ("/why", "Why the current song was picked"),
            ("/autoplay on|off", "Keep playing recommendations when the queue runs out"),
            ("/profile · /likes · /dislikes", "See what the bot has learned about you"),
            ("/forget · /reset-taste", "Delete what the bot knows about a song, or everything"),
        ],
    ),
    (
        "💾 Saved playlists",
        [
            ("/playlist create|add|remove|show|list|play|delete", "Your own playlists, usable in any server"),
        ],
    ),
    (
        "⚙️ Server settings (Manage Server)",
        [("/settings show|control|dj-role|default-volume|timeout|vote-skip", "Who can control music, and defaults")],
    ),
]


class Info(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List commands with examples")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Music bot help",
            description=(
                "Examples:\n"
                "`/play https://youtu.be/dQw4w9WgXcQ`\n"
                "`/play https://open.spotify.com/album/…`\n"
                "`/play lofi hip hop`"
            ),
            colour=ACCENT,
        )
        for name, rows in HELP_SECTIONS:
            embed.add_field(name=name, value="\n".join(f"**{c}** — {d}" for c, d in rows), inline=False)
        embed.set_footer(text="You need to be in the same voice channel as the bot to control it.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Check that the bot is responding")
    async def ping(self, interaction: discord.Interaction) -> None:
        start = time.perf_counter()
        await interaction.response.send_message("🏓 Pong…", ephemeral=True)
        rtt = (time.perf_counter() - start) * 1000
        await interaction.edit_original_response(
            content=f"🏓 Pong! Gateway {self.bot.latency * 1000:.0f} ms · API round-trip {rtt:.0f} ms"
        )

    @app_commands.command(name="about", description="Version and basic bot information")
    async def about(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="About", colour=ACCENT)
        embed.add_field(name="Version", value=__version__)
        embed.add_field(name="Uptime", value=fmt_duration(time.time() - self.bot.started_at))
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Playing in", value=f"{len(self.bot.players.all())} servers")
        sources = ["YouTube", "SoundCloud"] + (["Spotify"] if self.bot.spotify else [])
        embed.add_field(name="Sources", value=", ".join(sources))
        embed.add_field(
            name="Built with",
            value=f"Python {platform.python_version()} · discord.py {discord.__version__} · "
            f"yt-dlp {yt_dlp.version.__version__}",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Info(bot))
