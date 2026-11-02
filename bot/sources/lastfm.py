"""Optional Last.fm client used to discover similar music for recommendations."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API = "https://ws.audioscrobbler.com/2.0/"


class LastFMClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self._session = session
        self._key = api_key

    async def _call(self, method: str, **params: Any) -> dict[str, Any] | None:
        query = {"method": method, "api_key": self._key, "format": "json", "autocorrect": 1, **params}
        try:
            async with self._session.get(API, params=query, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.info("Last.fm %s returned HTTP %s", method, resp.status)
                    return None
                data = await resp.json(content_type=None)
                if "error" in data:
                    log.info("Last.fm %s error: %s", method, data.get("message"))
                    return None
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.info("Last.fm %s failed: %s", method, exc)
            return None

    async def similar_tracks(self, artist: str, title: str, limit: int = 10) -> list[tuple[str, str]]:
        data = await self._call("track.getSimilar", artist=artist, track=title, limit=limit)
        tracks = ((data or {}).get("similartracks") or {}).get("track") or []
        return [
            (t["artist"]["name"], t["name"])
            for t in tracks
            if isinstance(t, dict) and t.get("name") and (t.get("artist") or {}).get("name")
        ]

    async def similar_artists(self, artist: str, limit: int = 5) -> list[str]:
        data = await self._call("artist.getSimilar", artist=artist, limit=limit)
        artists = ((data or {}).get("similarartists") or {}).get("artist") or []
        return [a["name"] for a in artists if isinstance(a, dict) and a.get("name")]

    async def top_tracks(self, artist: str, limit: int = 5) -> list[tuple[str, str]]:
        data = await self._call("artist.getTopTracks", artist=artist, limit=limit)
        tracks = ((data or {}).get("toptracks") or {}).get("track") or []
        return [(artist, t["name"]) for t in tracks if isinstance(t, dict) and t.get("name")]
