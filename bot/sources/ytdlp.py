"""yt-dlp wrapper for YouTube and SoundCloud.

yt-dlp is synchronous and can block for seconds; every call runs in a worker
thread so the Discord gateway heartbeat is never starved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from ..config import YtdlpConfig
from ..models import Platform, ResolveResult, Track
from .detect import Detected, Kind, Service
from .errors import SourceError, TrackUnavailableError, friendly_ytdlp_error
from .matching import Candidate

log = logging.getLogger(__name__)

EXTRACT_TIMEOUT = 60
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}


class _YtdlpLogger:
    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        # Warnings often explain playback failures (e.g. a failed JS challenge), so keep them visible.
        log.warning("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _platform_for(info: dict[str, Any]) -> Platform:
    key = (info.get("ie_key") or info.get("extractor_key") or info.get("extractor") or "").lower()
    if "soundcloud" in key:
        return Platform.SOUNDCLOUD
    url = info.get("webpage_url") or info.get("url") or ""
    return Platform.SOUNDCLOUD if "soundcloud.com" in url else Platform.YOUTUBE


def _slug_title(url: str) -> tuple[str, str]:
    """Placeholder title/artist from a SoundCloud URL like /artist-name/track-name."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    artist = parts[0].replace("-", " ").title() if parts else "SoundCloud"
    title = parts[1].replace("-", " ").title() if len(parts) > 1 else url
    return title, artist


def _stream_expiry(url: str) -> float:
    """Most stream URLs carry an ``expire``/``expires`` epoch timestamp."""
    try:
        query = parse_qs(urlsplit(url).query)
        for key in ("expire", "expires", "Expires"):
            if key in query:
                return float(query[key][0])
    except (ValueError, IndexError):
        pass
    return time.time() + 15 * 60


class YtdlpSource:
    def __init__(self, cfg: YtdlpConfig, max_duration: int) -> None:
        self._cfg = cfg
        self._max_duration = max_duration
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent_extractions)

    def _options(self, **extra: Any) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "extractor_retries": 2,
            "logger": _YtdlpLogger(),
            "js_runtimes": {name: {} for name in self._cfg.js_runtimes},
            "cachedir": False,
        }
        if self._cfg.force_ipv4:
            opts["source_address"] = "0.0.0.0"
        if self._cfg.cookies_file:
            opts["cookiefile"] = self._cfg.cookies_file
        opts.update(extra)
        return opts

    def _extract_sync(self, url: str, opts: dict[str, Any]) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise TrackUnavailableError("Nothing playable was found at that link.")
            info = ydl.sanitize_info(info)
            if info.get("entries") is not None:
                info["entries"] = list(info["entries"])
            return info

    async def _extract(self, url: str, **opts: Any) -> dict[str, Any]:
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._extract_sync, url, self._options(**opts)),
                    timeout=EXTRACT_TIMEOUT,
                )
            except SourceError:
                raise
            except asyncio.TimeoutError as exc:
                raise TrackUnavailableError("The music platform took too long to respond.") from exc
            except yt_dlp.utils.DownloadError as exc:
                log.info("yt-dlp failed for %s: %s", url, exc)
                raise friendly_ytdlp_error(str(exc)) from exc
            except Exception as exc:  # yt-dlp can raise almost anything on site changes
                log.exception("Unexpected yt-dlp failure for %s", url)
                raise TrackUnavailableError("That content couldn't be loaded.") from exc

    # ----- conversion -------------------------------------------------
    def _track_from_info(self, info: dict[str, Any], requester_id: int, requester_name: str) -> Track:
        platform = _platform_for(info)
        url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
        title = info.get("track") or info.get("title")
        artist = info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or ""
        needs_metadata = False
        if not title:
            title, slug_artist = _slug_title(url)
            artist = artist or slug_artist
            needs_metadata = True
        if isinstance(artist, list):
            artist = ", ".join(artist)
        artist = str(artist).removesuffix(" - Topic")
        duration = info.get("duration")
        return Track(
            title=str(title),
            artist=artist,
            url=url,
            platform=platform,
            source_id=str(info.get("id")) if info.get("id") else None,
            duration=int(duration) if duration else None,
            thumbnail=_best_thumbnail(info),
            requester_id=requester_id,
            requester_name=requester_name,
            needs_metadata=needs_metadata,
            is_live=bool(info.get("is_live")),
        )

    def _check_duration(self, track: Track) -> None:
        if self._max_duration and track.duration and track.duration > self._max_duration:
            raise TrackUnavailableError(
                f"That track is longer than the {self._max_duration // 60}-minute limit."
            )

    # ----- public API -------------------------------------------------
    async def resolve(
        self, detected: Detected, requester_id: int, requester_name: str, limit: int
    ) -> ResolveResult:
        if detected.kind is Kind.TRACK:
            info = await self._extract(detected.url, format="bestaudio/best")
            if info.get("_type") == "playlist":
                raise TrackUnavailableError("That link points to a playlist, not a single track.")
            track = self._track_from_info(info, requester_id, requester_name)
            self._check_duration(track)
            self._cache_stream(track, info)
            return ResolveResult([track])

        # Playlists / sets / albums: flat extraction is one request instead of one per song.
        info = await self._extract(
            detected.url, noplaylist=False, extract_flat="in_playlist", playlistend=limit + 50
        )
        entries = [e for e in (info.get("entries") or []) if e]
        tracks: list[Track] = []
        skipped = 0
        for entry in entries:
            title = (entry.get("title") or "").strip().lower()
            if title in _UNAVAILABLE_TITLES or entry.get("availability") in ("private", "needs_auth", "subscriber_only"):
                skipped += 1
                continue
            if not (entry.get("url") or entry.get("webpage_url")):
                skipped += 1
                continue
            entry.setdefault("webpage_url", entry.get("url"))
            if "ie_key" not in entry and detected.service is Service.SOUNDCLOUD:
                entry["ie_key"] = "Soundcloud"
            track = self._track_from_info(entry, requester_id, requester_name)
            if self._max_duration and track.duration and track.duration > self._max_duration:
                skipped += 1
                continue
            tracks.append(track)
        truncated = len(tracks) > limit
        if not tracks:
            raise TrackUnavailableError("That playlist has no playable tracks.")
        return ResolveResult(
            tracks[:limit],
            skipped=skipped,
            collection_title=info.get("title"),
            is_collection=True,
            truncated=truncated,
        )

    async def search(self, query: str, limit: int = 5) -> list[Candidate]:
        info = await self._extract(f"ytsearch{limit}:{query}", noplaylist=False, extract_flat="in_playlist")
        results = []
        for e in info.get("entries") or []:
            if not e or not e.get("id") or e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            results.append(
                Candidate(
                    title=e.get("title") or "",
                    channel=e.get("channel") or e.get("uploader") or "",
                    duration=int(e["duration"]) if e.get("duration") else None,
                    url=e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    id=str(e["id"]),
                    thumbnail=_best_thumbnail(e),
                )
            )
        return results

    async def search_track(self, query: str, requester_id: int, requester_name: str) -> Track:
        """Plain-text /play: take the top usable search result."""
        candidates = await self.search(query, limit=5)
        for cand in candidates:
            if self._max_duration and cand.duration and cand.duration > self._max_duration:
                continue
            return Track(
                title=cand.title,
                artist=cand.channel.removesuffix(" - Topic"),
                url=cand.url,
                platform=Platform.YOUTUBE,
                source_id=cand.id,
                duration=cand.duration,
                thumbnail=cand.thumbnail,
                requester_id=requester_id,
                requester_name=requester_name,
            )
        raise TrackUnavailableError(f"No results found for “{query[:80]}”.")

    def _cache_stream(self, track: Track, info: dict[str, Any]) -> None:
        url = info.get("url")
        if not url or info.get("_type") == "playlist":
            return
        track.stream_url = url
        track.stream_headers = dict(info.get("http_headers") or {})
        track.stream_expires_at = _stream_expiry(url)
        track.is_live = bool(info.get("is_live"))

    async def refresh_stream(self, track: Track) -> None:
        """Fetch a fresh playable stream URL (and fill placeholder metadata)."""
        info = await self._extract(track.url, format="bestaudio/best")
        if not info.get("url"):
            raise TrackUnavailableError("No playable audio stream was found.")
        if track.needs_metadata or not track.duration:
            fresh = self._track_from_info(info, track.requester_id, track.requester_name)
            track.title, track.artist = fresh.title, fresh.artist or track.artist
            track.duration = fresh.duration or track.duration
            track.thumbnail = track.thumbnail or fresh.thumbnail
            track.source_id = fresh.source_id or track.source_id
            track.needs_metadata = False
        self._check_duration(track)
        self._cache_stream(track, info)
