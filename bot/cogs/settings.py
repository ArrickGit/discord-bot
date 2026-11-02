"""/settings: per-server configuration (requires Manage Server)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..db import GuildSettings
from ..permissions import ControlError
from ..ui.formatting import ACCENT

if TYPE_CHECKING:
    from ..client import MusicBot

CONTROL_DESCRIPTIONS = {
    "everyone": "Everyone in the bot's voice channel",
    "dj": "Members with the DJ role (and moderators)",
    "mods": "Moderators only (Manage Server / Move Members)",
}


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Settings(commands.GroupCog, group_name="settings", group_description="Server music settings"):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            raise ControlError("This only works inside a server.")
        if member.guild_permissions.manage_guild or member.id in self.bot.owner_ids:
            return True
        raise ControlError("You need the Manage Server permission to change music settings.")

    async def _save(self, interaction: discord.Interaction, **values: object) -> GuildSettings:
        assert interaction.guild is not None
        settings = await self.bot.db.update_guild_settings(interaction.guild.id, **values)
        player = self.bot.players.get(interaction.guild.id)
        if player is not None:
            player.settings = settings
        return settings

    def _summary(self, s: GuildSettings) -> discord.Embed:
        cfg = self.bot.config
        embed = discord.Embed(title="Music settings", colour=ACCENT)
        embed.add_field(name="Who can control music", value=CONTROL_DESCRIPTIONS[s.control_mode], inline=False)
        embed.add_field(name="DJ role", value=f"<@&{s.dj_role_id}>" if s.dj_role_id else "Not set")
        embed.add_field(name="Default volume", value=f"{s.default_volume or cfg.default_volume}%")
        timeout = s.inactivity_timeout or cfg.inactivity_timeout_seconds
        embed.add_field(name="Leave when idle after", value=f"{timeout // 60} min {timeout % 60} s")
        embed.add_field(
            name="Vote skip",
            value=f"On ({int(s.vote_skip_ratio * 100)}% of listeners)" if s.vote_skip else "Off",
        )
        embed.add_field(name="Autoplay", value="On" if s.autoplay else "Off")
        embed.add_field(name="Loop", value=s.loop_mode)
        embed.set_footer(text="Users alone with the bot, moderators and the bot owner can always control it.")
        return embed

    @app_commands.command(name="show", description="Show this server's music settings")
    async def show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        settings = await self.bot.db.get_guild_settings(interaction.guild.id)
        await interaction.response.send_message(embed=self._summary(settings), ephemeral=True)

    @app_commands.command(name="control", description="Choose who can skip, stop and change the queue")
    @app_commands.choices(
        mode=[app_commands.Choice(name=desc, value=key) for key, desc in CONTROL_DESCRIPTIONS.items()]
    )
    async def control(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        settings = await self._save(interaction, control_mode=mode.value)
        note = ""
        if mode.value == "dj" and not settings.dj_role_id:
            note = "\nSet a DJ role with `/settings dj-role`."
        await interaction.response.send_message(f"✅ Music control: {mode.name}.{note}", ephemeral=True)

    @app_commands.command(name="dj-role", description="Set or clear the DJ role")
    @app_commands.describe(role="Leave empty to clear the DJ role")
    async def dj_role(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        await self._save(interaction, dj_role_id=role.id if role else None)
        text = f"✅ DJ role set to {role.mention}." if role else "✅ DJ role cleared."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="default-volume", description="Volume the bot starts at in this server")
    async def default_volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]) -> None:
        await self._save(interaction, default_volume=level)
        await interaction.response.send_message(
            f"✅ Default volume set to {level}%. It applies the next time I join.", ephemeral=True
        )

    @app_commands.command(name="timeout", description="Minutes to wait with an empty queue before leaving")
    async def timeout(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 120]) -> None:
        await self._save(interaction, inactivity_timeout=minutes * 60)
        await interaction.response.send_message(f"✅ I'll leave after {minutes} idle minutes.", ephemeral=True)

    @app_commands.command(name="vote-skip", description="Let listeners vote to skip when they can't skip directly")
    @app_commands.describe(enabled="Turn vote skipping on or off", percent="Share of listeners needed (default 50)")
    async def vote_skip(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        percent: app_commands.Range[int, 10, 100] = 50,
    ) -> None:
        await self._save(interaction, vote_skip=enabled, vote_skip_ratio=percent / 100)
        text = f"✅ Vote skip on: {percent}% of listeners needed." if enabled else "✅ Vote skip off."
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Settings(bot))
