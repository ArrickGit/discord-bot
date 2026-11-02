"""Music source resolution: one entry point for every supported platform."""

from __future__ import annotations

import logging

import aiohttp

from ..config import Config
from ..models import Platform, ResolveResult, Track
from .detect import SHORTLINK_HOSTS, Detected, Kind, Service, detect
from .errors import NoMatchError, SourceConfigError, SourceError, UnsupportedLinkError
from .matching import best_match
from .spotify import SpotifyClient
from .ytdlp import YtdlpSource

log = logging.getLogger(__name__)

__all__ = ["Resolver", "SourceError", "detect", "Detected", "Kind", "Service"]


class Resolver:
    """Turns user input into Track objects and makes tracks playable."""

    def __init__(
        self,
        config: Config,
        session: aiohttp.ClientSession,
        ytdlp: YtdlpSource,
        spotify: SpotifyClient | None,
    ) -> None:
        self._config = config
        self._session = session
        self.ytdlp = ytdlp
        self.spotify = spotify

    async def _expand_shortlink(self, detected: Detected) -> Detected:
        try:
            async with self._session.head(
                detected.url, allow_redirects=True, max_redirects=5, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                final = str(resp.url)
        except aiohttp.ClientError as exc:
            raise UnsupportedLinkError("Couldn't open that short link.") from exc
        expanded = detect(final)
        if expanded.kind in (Kind.SHORTLINK, Kind.SEARCH):
            raise UnsupportedLinkError("That short link doesn't lead to a supported track or playlist.")
        return expanded

    async def resolve(
        self, query: str, requester_id: int, requester_name: str, *, limit: int | None = None
    ) -> ResolveResult:
        detected = detect(query)
        return await self.resolve_detected(detected, requester_id, requester_name, limit=limit)

    async def resolve_detected(
        self, detected: Detected, requester_id: int, requester_name: str, *, limit: int | None = None
    ) -> ResolveResult:
        limit = limit or self._config.maximum_playlist_size
        if detected.kind is Kind.SHORTLINK:
            detected = await self._expand_shortlink(detected)

        if detected.service is Service.SEARCH:
            track = await self.ytdlp.search_track(detected.url, requester_id, requester_name)
            return ResolveResult([track])
        if detected.service is Service.SPOTIFY:
            if not self.spotify:
                raise SourceConfigError("Spotify links aren't enabled on this bot (no Spotify API credentials).")
            return await self.spotify.resolve(detected, requester_id, requester_name, limit)
        return await self.ytdlp.resolve(detected, requester_id, requester_name, limit)

    async def match(self, track: Track) -> None:
        """Match a search-based track (Spotify/Last.fm) to a YouTube video, in place."""
        if track.resolved:
            return
        assert track.search_query
        candidates = await self.ytdlp.search(track.search_query, limit=6)
        artists = [a.strip() for a in track.artist.split(",") if a.strip()]
        result = best_match(
            track.title,
            artists,
            track.expected_duration,
            candidates,
            threshold=self._config.spotify_match_threshold,
        )
        if result is None:
            raise NoMatchError(f"Couldn't find a good playable match for “{track.display_title}”.")
        cand, score = result
        log.debug("Matched %r to %r (score %.1f)", track.search_query, cand.title, score)
        track.url = cand.url
        track.source_id = cand.id
        track.platform = Platform.YOUTUBE
        track.duration = cand.duration or track.expected_duration
        track.thumbnail = track.thumbnail or cand.thumbnail
        track.search_query = None

    async def prepare(self, track: Track) -> None:
        """Make sure a track has a fresh stream URL, matching it first if needed."""
        if not track.resolved:
            await self.match(track)
        if not track.stream_is_fresh():
            await self.ytdlp.refresh_stream(track)
