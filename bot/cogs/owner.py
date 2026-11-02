"""/owner …: commands only the configured bot owner can run."""

from __future__ import annotations

import logging
import resource
import shutil
import sys
import time
from typing import TYPE_CHECKING

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from ..permissions import ControlError
from ..ui.formatting import ACCENT, fmt_duration
from ..ui.views import ConfirmView

if TYPE_CHECKING:
    from ..client import MusicBot

log = logging.getLogger(__name__)


@app_commands.default_permissions(administrator=True)
class Owner(commands.GroupCog, group_name="owner", group_description="Bot owner commands"):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        # Server-side check: Discord's permission hiding is cosmetic, this is the real gate.
        if interaction.user.id not in self.bot.owner_ids:
            log.warning("Non-owner %s tried an owner command", interaction.user.id)
            raise ControlError("Only the bot owner can use this command.")
        return True

    @app_commands.command(name="status", description="Health and resource usage")
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
        players = bot.players.all()
        embed = discord.Embed(title="Bot status", colour=ACCENT)
        embed.add_field(name="Uptime", value=fmt_duration(time.time() - bot.started_at))
        embed.add_field(name="Latency", value=f"{bot.latency * 1000:.0f} ms")
        embed.add_field(name="Peak memory", value=f"{rss_mb:.0f} MB")
        embed.add_field(name="Servers", value=str(len(bot.guilds)))
        embed.add_field(name="Active players", value=str(len(players)))
        embed.add_field(name="Queued songs", value=str(sum(len(p.queue) for p in players)))
        embed.add_field(name="Database", value="✅ OK" if await bot.db.healthcheck() else "❌ Error")
        embed.add_field(name="FFmpeg", value="✅ Found" if bot.ffmpeg_available else "❌ Missing")
        spotify = "Off"
        if bot.spotify:
            spotify = "On (your playlists too)" if bot.spotify.has_user_auth else "On (tracks & albums)"
        embed.add_field(name="Spotify", value=spotify)
        embed.add_field(name="Last.fm", value="On" if bot.recommender.lastfm else "Off")
        embed.add_field(name="yt-dlp", value=yt_dlp.version.__version__)
        js = [r for r in bot.config.ytdlp.js_runtimes if shutil.which(r)]
        embed.add_field(name="JS runtime", value=", ".join(js) if js else "⚠️ None found")
        embed.add_field(name="Maintenance", value=bot.maintenance or "Off", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="shutdown", description="Disconnect everywhere and stop the bot")
    async def shutdown(self, interaction: discord.Interaction) -> None:
        view = ConfirmView(interaction.user.id, "Shut down")
        await interaction.response.send_message("Shut the bot down?", view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        await interaction.edit_original_response(content="👋 Shutting down…", view=None)
        log.warning("Shutdown requested by owner %s", interaction.user.id)
        await self.bot.close()

    @app_commands.command(name="leaveall", description="Disconnect from every voice channel")
    async def leaveall(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.bot.players.destroy_all("The bot owner disconnected all players.")
        await interaction.followup.send(f"Disconnected from {count} voice channel(s).", ephemeral=True)

    @app_commands.command(name="reload", description="Reload command modules without restarting")
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        failed = []
        for ext in list(self.bot.extensions):
            try:
                await self.bot.reload_extension(ext)
            except commands.ExtensionError as exc:
                log.exception("Reloading %s failed", ext)
                failed.append(f"{ext}: {exc.__class__.__name__}")
        text = "✅ Reloaded all modules." if not failed else "⚠️ Some modules failed:\n" + "\n".join(failed)
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="sync", description="Re-register slash commands with Discord")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="configured (dev server if set, else global)", value="auto"),
            app_commands.Choice(name="this server only", value="here"),
            app_commands.Choice(name="global", value="global"),
        ]
    )
    async def sync(self, interaction: discord.Interaction, scope: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = scope.value if scope else "auto"
        guild = interaction.guild if value == "here" else None
        count = await self.bot.sync_commands(guild=guild, force_global=value == "global")
        await interaction.followup.send(f"✅ Synced {count} commands.", ephemeral=True)

    @app_commands.command(name="maintenance", description="Block new music requests (owner still allowed)")
    @app_commands.describe(enabled="On or off", message="Shown to users who try to play music")
    async def maintenance(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        message: app_commands.Range[str, 1, 200] = "Back soon!",
    ) -> None:
        self.bot.maintenance = message if enabled else None
        await self.bot.db.set_state("maintenance", self.bot.maintenance)
        await self.bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening if not enabled else discord.ActivityType.playing,
                name="/play" if not enabled else "🛠️ maintenance",
            )
        )
        await interaction.response.send_message(
            f"🛠️ Maintenance mode {'on' if enabled else 'off'}.", ephemeral=True
        )

    @app_commands.command(name="backup", description="Back up the database now")
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        path = await self.bot.run_backup()
        await interaction.followup.send(f"💾 Backup written to `{path}`.", ephemeral=True)

    @app_commands.command(name="servers", description="List servers the bot is in")
    async def servers(self, interaction: discord.Interaction) -> None:
        lines = []
        for g in sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)[:25]:
            playing = "🎶 " if self.bot.players.get(g.id) else ""
            lines.append(f"{playing}**{discord.utils.escape_markdown(g.name)}** · {g.member_count or '?'} members · `{g.id}`")
        more = len(self.bot.guilds) - len(lines)
        text = "\n".join(lines) + (f"\n…and {more} more" if more > 0 else "")
        await interaction.response.send_message(text or "Not in any servers.", ephemeral=True)

    @app_commands.command(name="leave-server", description="Make the bot leave a server")
    async def leave_server(self, interaction: discord.Interaction, server_id: str) -> None:
        guild = self.bot.get_guild(int(server_id)) if server_id.isdigit() else None
        if guild is None:
            raise ControlError("I'm not in a server with that ID.")
        view = ConfirmView(interaction.user.id, "Leave")
        await interaction.response.send_message(
            f"Leave **{discord.utils.escape_markdown(guild.name)}**?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        await guild.leave()
        await interaction.edit_original_response(content=f"Left {discord.utils.escape_markdown(guild.name)}.", view=None)


async def setup(bot: MusicBot) -> None:
    guild_id = bot.config.command_sync_guild_id
    if guild_id:
        # Keep owner commands out of every other server's command list.
        await bot.add_cog(Owner(bot), guilds=[discord.Object(id=guild_id)])
    else:
        await bot.add_cog(Owner(bot))
