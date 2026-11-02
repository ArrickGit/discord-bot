"""Spotify Web API client (metadata only).

Spotify doesn't provide audio to third-party bots, so tracks are turned into
search-based ``Track`` objects that get matched to YouTube right before they play.

Since Spotify's February 2026 API changes, apps in Development Mode can only
read the *contents* of playlists the authorised user owns or collaborates on.
Track and album lookups still work with plain client credentials. To unlock your
own playlists, run ``python -m bot.tools.spotify_auth`` once and put the refresh
token it prints into ``.env``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..models import Platform, ResolveResult, Track
from .detect import Detected, Kind
from .errors import SourceBlockedError, SourceConfigError, SourceError, TrackUnavailableError
from .matching import search_query

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyPlaylistRestricted(SourceError):
    pass


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0

    def valid(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at - 60


class SpotifyClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        refresh_token: str | None = None,
    ) -> None:
        self._session = session
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._refresh_token = refresh_token
        self._app_token = _Token()
        self._user_token = _Token()
        self._lock = asyncio.Lock()

    @property
    def has_user_auth(self) -> bool:
        return bool(self._refresh_token)

    async def _fetch_token(self, data: dict[str, str]) -> _Token:
        async with self._session.post(
            TOKEN_URL,
            data=data,
            headers={"Authorization": f"Basic {self._basic}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                log.error("Spotify token request failed (%s): %s", resp.status, body.get("error"))
                raise SourceConfigError(
                    "Spotify rejected the bot's credentials. The bot owner needs to check the Spotify settings."
                )
            if body.get("refresh_token"):
                self._refresh_token = body["refresh_token"]
            return _Token(body["access_token"], time.time() + int(body.get("expires_in", 3600)))

    async def _token(self) -> str:
        async with self._lock:
            if self._refresh_token:
                if not self._user_token.valid():
                    self._user_token = await self._fetch_token(
                        {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
                    )
                return self._user_token.value
            if not self._app_token.valid():
                self._app_token = await self._fetch_token({"grant_type": "client_credentials"})
            return self._app_token.value

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(4):
            token = await self._token()
            try:
                async with self._session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 401 and attempt == 0:
                        self._app_token = _Token()
                        self._user_token = _Token()
                        continue
                    if resp.status == 429:
                        wait = min(int(resp.headers.get("Retry-After", "2")), 15)
                        if attempt < 3 and wait <= 15:
                            await asyncio.sleep(wait)
                            continue
                        raise SourceBlockedError("Spotify is rate-limiting the bot. Try again in a minute.")
                    if resp.status in (400, 404):
                        raise TrackUnavailableError("That Spotify item doesn't exist or isn't available.")
                    if resp.status == 403:
                        raise SpotifyPlaylistRestricted(
                            "Spotify won't let this bot read that item (it may be private or restricted)."
                        )
                    if resp.status >= 500 and attempt < 2:
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise TrackUnavailableError(f"Spotify returned an error ({resp.status}).")
            except aiohttp.ClientError as exc:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                raise TrackUnavailableError("Couldn't reach Spotify right now.") from exc
        raise TrackUnavailableError("Spotify didn't respond properly. Try again later.")

    # ----- conversion -------------------------------------------------
    @staticmethod
    def _to_track(
        item: dict[str, Any], requester_id: int, requester_name: str, album_art: str | None = None
    ) -> Track | None:
        if not item or item.get("type", "track") != "track" or item.get("is_local"):
            return None
        if item.get("is_playable") is False:
            return None
        title = item.get("name") or ""
        artists = [a.get("name", "") for a in item.get("artists") or [] if a.get("name")]
        if not title:
            return None
        images = (item.get("album") or {}).get("images") or []
        art = images[0]["url"] if images else album_art
        duration = int(item["duration_ms"] / 1000) if item.get("duration_ms") else None
        sp_url = (item.get("external_urls") or {}).get("spotify") or (
            f"https://open.spotify.com/track/{item['id']}" if item.get("id") else None
        )
        return Track(
            title=title,
            artist=", ".join(artists),
            url="",
            platform=Platform.YOUTUBE,
            thumbnail=art,
            requester_id=requester_id,
            requester_name=requester_name,
            search_query=search_query(title, artists),
            origin_url=sp_url,
            origin_platform=Platform.SPOTIFY,
            expected_duration=duration,
            duration=duration,
        )

    async def _paginate(self, first_page: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = list(first_page.get("items") or [])
        next_url = first_page.get("next")
        while next_url and len(items) < limit:
            page = await self._get(next_url)
            items.extend(page.get("items") or [])
            next_url = page.get("next")
        return items

    # ----- public API -------------------------------------------------
    async def resolve(
        self, detected: Detected, requester_id: int, requester_name: str, limit: int
    ) -> ResolveResult:
        if detected.kind is Kind.TRACK:
            item = await self._get(f"/tracks/{detected.id}")
            track = self._to_track(item, requester_id, requester_name)
            if not track:
                raise TrackUnavailableError("That Spotify track isn't playable.")
            return ResolveResult([track])

        if detected.kind is Kind.ALBUM:
            album = await self._get(f"/albums/{detected.id}")
            images = album.get("images") or []
            art = images[0]["url"] if images else None
            raw = await self._paginate(album.get("tracks") or {}, limit + 1)
            tracks = [t for t in (self._to_track(i, requester_id, requester_name, art) for i in raw) if t]
            skipped = len(raw) - len(tracks)
            name = album.get("name")
            artists = ", ".join(a["name"] for a in album.get("artists") or [] if a.get("name"))
            title = f"{name} — {artists}" if artists else name
        elif detected.kind is Kind.PLAYLIST:
            playlist = await self._get(f"/playlists/{detected.id}")
            # The Feb 2026 API renamed ``tracks`` to ``items``; accept both shapes.
            page = playlist.get("items") if isinstance(playlist.get("items"), dict) else playlist.get("tracks")
            if not isinstance(page, dict) or "items" not in page:
                hint = (
                    "Copy it into a playlist you own, or use a YouTube playlist."
                    if self.has_user_auth
                    else "The bot owner can enable their own playlists by running the Spotify login helper."
                )
                raise SpotifyPlaylistRestricted(
                    "Spotify only lets this bot read playlists owned by (or shared with) the bot owner's "
                    f"Spotify account, and Spotify's own editorial playlists aren't available. {hint}"
                )
            raw = await self._paginate(page, limit + 1)
            entries = [(e.get("item") or e.get("track")) for e in raw]
            tracks = [t for t in (self._to_track(i, requester_id, requester_name) for i in entries if i) if t]
            skipped = len(raw) - len(tracks)
            title = playlist.get("name")
        else:
            raise TrackUnavailableError("That Spotify link isn't supported.")

        if not tracks:
            raise TrackUnavailableError("That Spotify collection has no playable tracks.")
        return ResolveResult(
            tracks[:limit],
            skipped=skipped,
            collection_title=title,
            is_collection=True,
            truncated=len(tracks) > limit,
            notes=["Spotify songs are matched to YouTube when they play."],
        )
