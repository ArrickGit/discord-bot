"""Buttons: now-playing controls, queue pages, ratings and confirmations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

from .. import controls
from ..permissions import ControlError
from ..queue import LoopMode
from . import embeds
from .formatting import escape, truncate

if TYPE_CHECKING:
    from ..models import Track
    from ..player import GuildPlayer

log = logging.getLogger(__name__)


async def _reply(interaction: discord.Interaction, text: str, *, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


class BaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        if isinstance(error, ControlError):
            await _reply(interaction, f"⚠️ {error}")
            return
        log.exception("View error", exc_info=error)
        await _reply(interaction, "⚠️ Something went wrong with that button.")


class NowPlayingView(BaseView):
    def __init__(self, player: GuildPlayer) -> None:
        super().__init__(timeout=None)
        self.player = player
        self.pause_button.emoji = "▶️" if player.is_paused else "⏸️"
        self.loop_button.emoji = {LoopMode.OFF: "➡️", LoopMode.QUEUE: "🔁", LoopMode.SONG: "🔂"}[player.queue.loop]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.player.destroyed or self.player.queue.current is None:
            await _reply(interaction, "That song isn't playing anymore.")
            return False
        return True

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer()
        await controls.toggle_pause(interaction, self.player)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        message, public = await controls.skip(interaction, self.player)
        await interaction.response.send_message(message, ephemeral=not public, delete_after=30 if public else None)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        message = await controls.stop(interaction, self.player)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        message = await controls.shuffle(interaction, self.player)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        message = await controls.set_loop(interaction, self.player)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Rate", emoji="⭐", style=discord.ButtonStyle.primary, row=1)
    async def rate_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        track = self.player.queue.current
        if track is None or track.key is None:
            await _reply(interaction, "This song can't be rated yet.")
            return
        await interaction.response.send_message(
            **RatingView.message_kwargs(track), view=RatingView(track, interaction.user.id), ephemeral=True
        )


class QueueView(BaseView):
    def __init__(self, player: GuildPlayer, owner_id: int, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.player = player
        self.owner_id = owner_id
        self.page = page
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> tuple[discord.Embed, int]:
        embed, pages = embeds.queue_page(self.player, self.page)
        self.page = max(0, min(self.page, pages - 1))
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= pages - 1
        return embed, pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _reply(interaction, "Run `/queue` yourself to browse the queue.")
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        embed, _ = self._sync()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self._show(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.page += 1
        await self._show(interaction)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


RATING_TEXT = {
    "good": "Saved 👍. I'll use this when choosing music for you.",
    "bad": "Saved 👎. I'll steer away from this and similar music for you.",
    "not_now": "Got it ⏭️. I won't suggest this for a while, but it's not a permanent dislike.",
}


class RatingView(BaseView):
    def __init__(self, track: Track, user_id: int) -> None:
        super().__init__(timeout=300)
        self.track = track
        self.user_id = user_id

    @staticmethod
    def message_kwargs(track: Track) -> dict[str, Any]:
        return {"content": f"How do you feel about **{escape(truncate(track.display_title, 90))}**?"}

    async def _save(self, interaction: discord.Interaction, rating: str) -> None:
        if interaction.user.id != self.user_id:
            await _reply(interaction, "Use `/rate` to rate songs yourself.")
            return
        try:
            await interaction.client.db.set_feedback(self.user_id, self.track, rating)  # type: ignore[attr-defined]
        except Exception:
            log.exception("Saving feedback failed")
            await interaction.response.edit_message(content="⚠️ Couldn't save that right now. Try again later.", view=None)
            return
        await interaction.response.edit_message(content=RATING_TEXT[rating], view=None)
        self.stop()

    @discord.ui.button(label="Good", emoji="👍", style=discord.ButtonStyle.success)
    async def good(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self._save(interaction, "good")

    @discord.ui.button(label="Bad", emoji="👎", style=discord.ButtonStyle.danger)
    async def bad(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self._save(interaction, "bad")

    @discord.ui.button(label="Not now", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def not_now(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self._save(interaction, "not_now")


class ConfirmView(BaseView):
    def __init__(self, user_id: int, confirm_label: str = "Confirm") -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.value: bool | None = None
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()
