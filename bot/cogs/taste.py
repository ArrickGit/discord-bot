"""Ratings, recommendations, autoplay and per-user profile/privacy commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..permissions import Action, ControlError, require, require_setting_control
from ..ui.formatting import ACCENT, escape, fmt_duration, truncate
from ..ui.views import ConfirmView, RatingView
from .common import NO_PINGS, ensure_player, requester

if TYPE_CHECKING:
    from ..client import MusicBot
    from ..db import StoredTrack


def _stored_line(t: StoredTrack) -> str:
    title = escape(truncate(f"{t.artist} - {t.title}" if t.artist and t.artist.lower() not in t.title.lower() else t.title, 70))
    return f"[{title}]({t.origin_url or t.url})"


class Taste(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    def _current(self, interaction: discord.Interaction):  # type: ignore[no-untyped-def]
        player = self.bot.players.get(interaction.guild.id) if interaction.guild else None
        track = player.queue.current if player else None
        if track is None:
            raise ControlError("Nothing is playing right now.")
        return player, track

    @app_commands.command(name="rate", description="Rate the current song (only you can see this)")
    @app_commands.guild_only()
    async def rate(self, interaction: discord.Interaction) -> None:
        _, track = self._current(interaction)
        if track.key is None:
            raise ControlError("This song is still loading. Try again in a moment.")
        previous = await self.bot.db.get_feedback(interaction.user.id, track.key)
        kwargs = RatingView.message_kwargs(track)
        if previous:
            label = {"good": "👍 Good", "bad": "👎 Bad", "not_now": "⏭️ Not now"}[previous]
            kwargs["content"] += f"\nYou previously chose {label}. Picking again replaces it."
        await interaction.response.send_message(**kwargs, view=RatingView(track, interaction.user.id), ephemeral=True)

    @app_commands.command(name="random", description="Queue a song picked for you from your taste")
    @app_commands.guild_only()
    async def random(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await ensure_player(interaction)
        uid, name = requester(interaction)
        exclude = {t.key for t in [*player.queue.upcoming, *player.queue.history] if t.key}
        if player.queue.current and player.queue.current.key:
            exclude.add(player.queue.current.key)
        track = await self.bot.recommender.recommend(
            player.guild.id, [uid], exclude=exclude, requester_id=uid, requester_name=name, via="random"
        )
        if track is None:
            raise ControlError(
                "I don't know your taste well enough yet. Listen to some music and rate songs with `/rate` first."
            )
        player.enqueue([track])
        reason = f"\nBecause {track.reasons[0]}." if track.reasons else ""
        title = escape(truncate(track.display_title, 90))
        await interaction.followup.send(f"🎲 Queued **{title}**.{reason}", ephemeral=True)
        if player.text_channel is not None:
            try:
                await player.text_channel.send(
                    f"🎲 {interaction.user.mention} queued a recommendation: **{title}**.", allowed_mentions=NO_PINGS
                )
            except discord.HTTPException:
                pass

    @app_commands.command(name="why", description="Why the current song is playing")
    @app_commands.guild_only()
    async def why(self, interaction: discord.Interaction) -> None:
        _, track = self._current(interaction)
        title = escape(truncate(track.display_title, 90))
        if track.via == "request":
            text = f"**{title}** was requested by <@{track.requester_id}>."
        elif track.reasons:
            who = "Autoplay" if track.via == "autoplay" else f"<@{track.requester_id}> used `/random` and I"
            reasons = track.reasons
            joined = reasons[0] if len(reasons) == 1 else ", ".join(reasons[:-1]) + " and " + reasons[-1]
            text = f"{who} picked **{title}** because {joined}."
        else:
            text = f"**{title}** was picked at random from music played in this server."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="autoplay", description="Keep playing recommendations when the queue runs out")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        player = self.bot.players.get(interaction.guild.id)
        if player is not None:
            require(interaction, player, Action.CONTROL)
        else:
            require_setting_control(interaction, await self.bot.db.get_guild_settings(interaction.guild.id))
        await interaction.response.defer(ephemeral=True, thinking=True)

        member = interaction.user
        in_voice = isinstance(member, discord.Member) and member.voice is not None and member.voice.channel is not None
        if enabled and in_voice and (player is None or player.channel is None):
            # Join the caller's channel so autoplay can start straight away.
            player = await ensure_player(interaction)

        settings = await self.bot.db.update_guild_settings(interaction.guild.id, autoplay=enabled)
        if player is not None:
            player.settings = settings
            player.set_autoplay(enabled)

        if not enabled:
            if player is not None and not player.is_idle:
                await player.update_now_playing()
            await interaction.followup.send("Autoplay off.", ephemeral=True)
            return

        if player is None or player.channel is None:
            await interaction.followup.send(
                "🔁 Autoplay on. Join a voice channel and run `/autoplay true` again to start it now, "
                "or it'll take over when your queue runs out.",
                ephemeral=True,
            )
            return

        if not player.is_idle:
            await player.update_now_playing()
            await interaction.followup.send(
                "🔁 Autoplay on: when the queue ends I'll pick songs for the people listening.", ephemeral=True
            )
            return

        track = await player.autoplay_now()
        if track is None:
            await interaction.followup.send(
                "🔁 Autoplay is on, but I don't know enough about what you all like to pick a song yet. "
                "Play a few songs with `/play` (and rate them with `/rate`), then I'll take over when the queue ends.",
                ephemeral=True,
            )
            return
        reason = f"\nBecause {track.reasons[0]}." if track.reasons else ""
        title = escape(truncate(track.display_title, 90))
        await interaction.followup.send(f"🔁 Autoplay on: starting with **{title}**.{reason}", ephemeral=True)

    # ----- profile & privacy ------------------------------------------
    @app_commands.command(name="profile", description="See what the bot has learned about your taste")
    async def profile(self, interaction: discord.Interaction) -> None:
        stats = await self.bot.db.user_stats(interaction.user.id)
        _, top = await self.bot.recommender.taste_summary(interaction.user.id)
        ratings = stats["ratings"]
        embed = discord.Embed(title=f"{interaction.user.display_name}'s music profile", colour=ACCENT)
        embed.add_field(name="Songs requested", value=str(stats["plays"] or 0))
        embed.add_field(name="Finished", value=str(stats["completed"] or 0))
        embed.add_field(name="Skipped", value=str(stats["skipped"] or 0))
        embed.add_field(name="Listening time", value=fmt_duration(stats["seconds"]))
        embed.add_field(
            name="Ratings",
            value=f"👍 {ratings.get('good', 0)} · 👎 {ratings.get('bad', 0)} · ⏭️ {ratings.get('not_now', 0)}",
        )
        embed.add_field(
            name="Favourite artists",
            value="\n".join(f"{i}. {escape(a)}" for i, a in enumerate(top, 1)) or "Not enough data yet",
            inline=False,
        )
        embed.set_footer(text="Only your Discord ID and ratings/plays are stored. /reset-taste deletes them.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _rated(self, interaction: discord.Interaction, rating: str, title: str) -> None:
        tracks = await self.bot.db.rated_tracks(interaction.user.id, rating, limit=20)
        if not tracks:
            await interaction.response.send_message(f"No {title.lower()} yet. Use `/rate` while a song plays.", ephemeral=True)
            return
        embed = discord.Embed(
            title=title, description="\n".join(f"• {_stored_line(t)}" for t in tracks), colour=ACCENT
        )
        embed.set_footer(text="Most recent 20")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="likes", description="Songs you rated 👍")
    async def likes(self, interaction: discord.Interaction) -> None:
        await self._rated(interaction, "good", "Your likes")

    @app_commands.command(name="dislikes", description="Songs you rated 👎")
    async def dislikes(self, interaction: discord.Interaction) -> None:
        await self._rated(interaction, "bad", "Your dislikes")

    @app_commands.command(name="forget", description="Forget your rating and history for the current song")
    @app_commands.guild_only()
    async def forget(self, interaction: discord.Interaction) -> None:
        _, track = self._current(interaction)
        if track.key is None:
            raise ControlError("This song is still loading. Try again in a moment.")
        removed = await self.bot.db.forget_track(interaction.user.id, track.key)
        title = escape(truncate(track.display_title, 90))
        text = f"🧽 Forgot everything about **{title}** for you." if removed else f"I had nothing saved about **{title}** for you."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="reset-taste", description="Delete all your ratings and listening history")
    @app_commands.describe(include_playlists="Also delete your saved playlists")
    async def reset_taste(self, interaction: discord.Interaction, include_playlists: bool = False) -> None:
        what = "all your ratings and listening history" + (" **and your saved playlists**" if include_playlists else "")
        view = ConfirmView(interaction.user.id, "Delete")
        await interaction.response.send_message(f"Delete {what}? This can't be undone.", view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled. Nothing was deleted.", view=None)
            return
        counts = await self.bot.db.reset_user(interaction.user.id, include_playlists=include_playlists)
        summary = ", ".join(f"{n} {k}" for k, n in counts.items())
        await interaction.edit_original_response(content=f"🧽 Done. Deleted {summary}.", view=None)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Taste(bot))
