"""Gathers candidates and turns recommendation scores into playable tracks."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from . import recommend
from .db import Database
from .models import Platform, Track
from .recommend import Scored, Signals
from .sources.lastfm import LastFMClient
from .sources.matching import normalize

log = logging.getLogger(__name__)

LASTFM_CACHE_SECONDS = 24 * 3600
HISTORY_WINDOW_DAYS = 180


@dataclass
class _Discovery:
    artist: str
    title: str
    reason: str


class Recommender:
    def __init__(self, db: Database, lastfm: LastFMClient | None) -> None:
        self.db = db
        self.lastfm = lastfm
        self._lastfm_cache: dict[tuple[str, str], tuple[float, list[tuple[str, str]]]] = {}
        self._rng = random.Random()

    async def signals(self, user_ids: list[int], guild_id: int | None = None) -> dict[int, Signals]:
        since = time.time() - HISTORY_WINDOW_DAYS * 86_400
        feedback = await self.db.user_feedback(user_ids)
        history = await self.db.user_history(user_ids, since=since)
        recent_guild = await self.db.recent_guild_tracks(guild_id, time.time() - 86_400) if guild_id else {}
        result = {}
        for uid in user_ids:
            result[uid] = recommend.build_signals(
                [f for f in feedback if f[0] == uid], [h for h in history if h.user_id == uid], recent_guild
            )
        return result

    async def _similar(self, artist: str, title: str) -> list[tuple[str, str]]:
        if not self.lastfm:
            return []
        key = (artist.lower(), title.lower())
        cached = self._lastfm_cache.get(key)
        if cached and time.time() - cached[0] < LASTFM_CACHE_SECONDS:
            return cached[1]
        similar = await self.lastfm.similar_tracks(artist, title, limit=10)
        self._lastfm_cache[key] = (time.time(), similar)
        return similar

    async def _discoveries(self, signals: dict[int, Signals]) -> list[_Discovery]:
        """Last.fm similar tracks seeded from liked songs."""
        if not self.lastfm:
            return []
        seeds: list[str] = []
        for s in signals.values():
            seeds.extend(sorted(s.liked_tracks)[:20])
            seeds.extend(k for k, _ in s.completed_tracks.most_common(5))
        seeds = list(dict.fromkeys(seeds))
        self._rng.shuffle(seeds)
        stored = await self.db.get_tracks(seeds[:3])
        found: list[_Discovery] = []
        for track in stored.values():
            for artist, title in await self._similar(track.artist, track.title):
                found.append(_Discovery(artist, title, f"it's similar to “{track.title}”, which you enjoyed"))
        return found

    async def recommend(
        self,
        guild_id: int,
        user_ids: list[int],
        *,
        exclude: set[str] | None = None,
        requester_id: int = 0,
        requester_name: str = "",
        via: str = "random",
    ) -> Track | None:
        exclude = exclude or set()
        if not user_ids:
            return None
        signals = await self.signals(user_ids, guild_id)

        # Candidate pool: liked + finished songs, other songs by favourite artists,
        # and what's popular in this server.
        keys: set[str] = set()
        artists: list[str] = []
        for s in signals.values():
            keys |= s.liked_tracks
            keys |= set(s.completed_tracks)
            artists.extend(recommend.top_artists(s, 8))
        keys |= set(await self.db.tracks_by_artists(artists))
        popular = await self.db.guild_popular_tracks(guild_id)
        keys |= set(popular)
        keys -= exclude
        stored = await self.db.get_tracks(keys)
        popular_set = set(popular)

        now = time.time()
        scored: list[Scored] = []
        for key, track in stored.items():
            per_user = [recommend.score_track(key, track.artist, signals[u], now) for u in user_ids]
            combined = recommend.combine(per_user)
            if combined is None:
                continue
            if not combined.reasons and key in popular_set:
                combined.reasons.append("it's popular with listeners in this server")
            if combined.reasons:
                scored.append(combined)

        # Discoveries from Last.fm get scored by artist affinity only.
        discoveries: dict[str, _Discovery] = {}
        known_titles = {(normalize(t.artist), normalize(t.title)) for t in stored.values()}
        for d in await self._discoveries(signals):
            if (normalize(d.artist), normalize(d.title)) in known_titles:
                continue
            pseudo = f"search:{normalize(d.artist)}:{normalize(d.title)}"
            per_user = [recommend.score_track(pseudo, d.artist, signals[u], now, discovery_reason=d.reason) for u in user_ids]
            combined = recommend.combine(per_user)
            if combined is not None:
                discoveries[pseudo] = d
                scored.append(combined)

        choice = recommend.pick(scored, self._rng, shuffle=via == "autoplay")
        if choice is None:
            return None
        if choice.track_key in discoveries:
            d = discoveries[choice.track_key]
            track = Track(
                title=d.title,
                artist=d.artist,
                url="",
                platform=Platform.YOUTUBE,
                requester_id=requester_id,
                requester_name=requester_name,
                search_query=f"{d.artist} - {d.title}",
                origin_platform=Platform.LASTFM,
            )
        else:
            track = stored[choice.track_key].to_track(requester_id, requester_name)
        track.reasons = choice.reasons[:4]
        track.via = via
        return track

    async def taste_summary(self, user_id: int) -> tuple[Signals, list[str]]:
        signals = (await self.signals([user_id]))[user_id]
        return signals, recommend.top_artists(signals, 5)
