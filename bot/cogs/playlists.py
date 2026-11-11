"""/playlist …: personal saved playlists stored in the local database."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..models import Track
from ..permissions import ControlError
from ..sources.errors import SourceError
from ..ui.formatting import ACCENT, escape, fmt_duration, truncate
from ..ui.views import ConfirmView
from .common import NO_PINGS, ensure_player, requester

if TYPE_CHECKING:
    from ..client import MusicBot

log = logging.getLogger(__name__)

MAX_PLAYLISTS = 25
MAX_PLAYLIST_ITEMS = 500
MAX_IMPORT = 100

Name = app_commands.Range[str, 1, 50]


class Playlists(commands.GroupCog, group_name="playlist", group_description="Your saved playlists"):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        super().__init__()

    async def _require(self, user_id: int, name: str) -> int:
        playlist_id = await self.bot.db.find_playlist(user_id, name.strip())
        if playlist_id is None:
            raise ControlError(f"You don't have a playlist called “{escape(name)}”. See `/playlist list`.")
        return playlist_id

    async def name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.bot.db.list_playlists(interaction.user.id)
        current = current.lower()
        return [
            app_commands.Choice(name=f"{n} ({c} songs)"[:100], value=n)
            for n, c in rows
            if current in n.lower()
        ][:25]

    @app_commands.command(name="create", description="Create a new saved playlist")
    async def create(self, interaction: discord.Interaction, name: Name) -> None:
        name = " ".join(name.split())
        if len(await self.bot.db.list_playlists(interaction.user.id)) >= MAX_PLAYLISTS:
            raise ControlError(f"You can have up to {MAX_PLAYLISTS} playlists.")
        try:
            await self.bot.db.create_playlist(interaction.user.id, name)
        except ValueError as exc:
            raise ControlError(str(exc)) from None
        await interaction.response.send_message(
            f"📁 Created **{escape(name)}**. Add songs with `/playlist add`.", ephemeral=True
        )

    @app_commands.command(name="add", description="Add the current song, or a link, to a saved playlist")
    @app_commands.describe(name="Your playlist", link="Leave empty to add the song that's playing now")
    @app_commands.autocomplete(name=name_autocomplete)
    async def add(self, interaction: discord.Interaction, name: Name, link: str | None = None) -> None:
        playlist_id = await self._require(interaction.user.id, name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        space = MAX_PLAYLIST_ITEMS - await self.bot.db.playlist_size(playlist_id)
        if space <= 0:
            raise ControlError(f"That playlist is full ({MAX_PLAYLIST_ITEMS} songs).")

        if link is None:
            player = self.bot.players.get(interaction.guild.id) if interaction.guild else None
            current = player.queue.current if player else None
            if current is None or current.key is None:
                raise ControlError("Nothing is playing. Give me a link to add instead.")
            tracks = [current]
            skipped = 0
        else:
            uid, uname = requester(interaction)
            result = await self.bot.resolver.resolve(link, uid, uname, limit=min(space, MAX_IMPORT))
            tracks, skipped = await self._resolve_all(interaction, result.tracks)
            skipped += result.skipped

        added = await self.bot.db.add_to_playlist(playlist_id, tracks[:space])
        text = f"✅ Added {added} song{'s' if added != 1 else ''} to **{escape(name)}**."
        if skipped:
            text += f" {skipped} couldn't be added."
        await interaction.edit_original_response(content=text)

    async def _resolve_all(self, interaction: discord.Interaction, tracks: list[Track]) -> tuple[list[Track], int]:
        """Saved playlists store resolved tracks, so match Spotify songs now."""
        pending = [t for t in tracks if not t.resolved]
        if not pending:
            return tracks, 0
        await interaction.edit_original_response(content=f"⏳ Matching {len(pending)} songs…")

        async def match(t: Track) -> bool:
            try:
                await self.bot.resolver.match(t)
                return True
            except SourceError:
                return False

        await asyncio.gather(*(match(t) for t in pending))
        ok = [t for t in tracks if t.resolved]
        return ok, len(tracks) - len(ok)

    @app_commands.command(name="remove", description="Remove a song from a saved playlist")
    @app_commands.autocomplete(name=name_autocomplete)
    async def remove(self, interaction: discord.Interaction, name: Name, position: app_commands.Range[int, 1, 1000]) -> None:
        playlist_id = await self._require(interaction.user.id, name)
        removed = await self.bot.db.remove_from_playlist(playlist_id, position)
        if removed is None:
            raise ControlError("There's no song at that position. See `/playlist show`.")
        await interaction.response.send_message(
            f"🗑️ Removed **{escape(truncate(removed.title, 80))}** from **{escape(name)}**.", ephemeral=True
        )

    @app_commands.command(name="show", description="Show the songs in a saved playlist")
    @app_commands.autocomplete(name=name_autocomplete)
    async def show(self, interaction: discord.Interaction, name: Name, page: app_commands.Range[int, 1, 100] = 1) -> None:
        playlist_id = await self._require(interaction.user.id, name)
        tracks = await self.bot.db.playlist_tracks(playlist_id)
        per_page = 15
        pages = max(1, -(-len(tracks) // per_page))
        page = min(page, pages)
        chunk = tracks[(page - 1) * per_page : page * per_page]
        lines = [
            f"`{i}.` {escape(truncate(t.title, 60))} — {escape(truncate(t.artist, 30))} `{fmt_duration(t.duration)}`"
            for i, t in enumerate(chunk, start=(page - 1) * per_page + 1)
        ]
        embed = discord.Embed(
            title=f"📁 {name}", description="\n".join(lines) or "Empty. Add songs with `/playlist add`.", colour=ACCENT
        )
        total = sum(t.duration or 0 for t in tracks)
        embed.set_footer(text=f"Page {page}/{pages} • {len(tracks)} songs • {fmt_duration(total)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="list", description="List your saved playlists")
    async def list_playlists(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_playlists(interaction.user.id)
        text = "\n".join(f"📁 **{escape(n)}** — {c} songs" for n, c in rows) or "You have no playlists. `/playlist create` one!"
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="play", description="Queue one of your saved playlists")
    @app_commands.autocomplete(name=name_autocomplete)
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, name: Name, shuffle: bool = False) -> None:
        playlist_id = await self._require(interaction.user.id, name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        stored = await self.bot.db.playlist_tracks(playlist_id)
        if not stored:
            raise ControlError("That playlist is empty.")
        player = await ensure_player(interaction)
        uid, uname = requester(interaction)
        tracks = [s.to_track(uid, uname) for s in stored]
        if shuffle:
            random.shuffle(tracks)
        added, dropped = player.enqueue(tracks)
        text = f"✅ Queued {added} songs from **{escape(name)}**."
        if dropped:
            text += f" {dropped} didn't fit in the queue."
        await interaction.followup.send(text, ephemeral=True)
        if player.text_channel is not None:
            try:
                await player.text_channel.send(
                    f"📁 {interaction.user.mention} queued their playlist **{escape(name)}** ({added} songs).",
                    allowed_mentions=NO_PINGS,
                )
            except discord.HTTPException:
                pass

    @app_commands.command(name="delete", description="Delete one of your saved playlists")
    @app_commands.autocomplete(name=name_autocomplete)
    async def delete(self, interaction: discord.Interaction, name: Name) -> None:
        playlist_id = await self._require(interaction.user.id, name)
        view = ConfirmView(interaction.user.id, "Delete")
        await interaction.response.send_message(f"Delete **{escape(name)}**?", view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        await self.bot.db.delete_playlist(playlist_id)
        await interaction.edit_original_response(content=f"🗑️ Deleted **{escape(name)}**.", view=None)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Playlists(bot))
