"""The standard Track object every source produces."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum


class Platform(StrEnum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    SPOTIFY = "spotify"
    LASTFM = "lastfm"


PLATFORM_LABELS = {
    Platform.YOUTUBE: "YouTube",
    Platform.SOUNDCLOUD: "SoundCloud",
    Platform.SPOTIFY: "Spotify",
    Platform.LASTFM: "Last.fm",
}


@dataclass
class Track:
    title: str
    artist: str
    url: str
    """Link to the playable page (YouTube/SoundCloud). Empty until a search-based track is matched."""
    platform: Platform
    """Where the audio actually comes from once resolved."""
    source_id: str | None = None
    duration: int | None = None
    thumbnail: str | None = None
    requester_id: int = 0
    requester_name: str = ""

    # Search-based tracks (Spotify, Last.fm) start unresolved: they carry a query and
    # are matched to a YouTube video only when they are about to play.
    search_query: str | None = None
    origin_url: str | None = None
    origin_platform: Platform | None = None
    expected_duration: int | None = None
    needs_metadata: bool = False
    """True when only placeholder metadata is known (e.g. flat SoundCloud sets)."""

    # Temporary stream data. Never persisted: stream URLs expire.
    stream_url: str | None = field(default=None, repr=False)
    stream_headers: dict[str, str] = field(default_factory=dict, repr=False)
    stream_expires_at: float = 0.0
    # Some YouTube links return 403 until this epoch time (yt-dlp's ``available_at``).
    stream_available_at: float = 0.0
    is_live: bool = False

    # Why this track was chosen (recommendations / autoplay), shown by /why.
    reasons: list[str] = field(default_factory=list)
    via: str = "request"
    requested_at: float = field(default_factory=time.time)

    @property
    def resolved(self) -> bool:
        return bool(self.url) and self.search_query is None

    @property
    def key(self) -> str | None:
        """Stable identity for the database, e.g. ``youtube:dQw4w9WgXcQ``."""
        if not self.resolved or not self.source_id:
            return None
        return f"{self.platform.value}:{self.source_id}"

    @property
    def display_url(self) -> str:
        return self.origin_url or self.url

    @property
    def display_title(self) -> str:
        if self.artist and self.artist.lower() not in self.title.lower():
            return f"{self.artist} - {self.title}"
        return self.title

    def stream_is_fresh(self, margin: float = 120.0) -> bool:
        return bool(self.stream_url) and self.stream_expires_at - margin > time.time()

    def clear_stream(self) -> None:
        self.stream_url = None
        self.stream_headers = {}
        self.stream_expires_at = 0.0
        self.stream_available_at = 0.0

    def copy_for(self, requester_id: int, requester_name: str) -> Track:
        """A fresh copy for re-queueing (e.g. queue loop or saved playlists)."""
        return replace(
            self,
            requester_id=requester_id,
            requester_name=requester_name,
            stream_url=None,
            stream_headers={},
            stream_expires_at=0.0,
            stream_available_at=0.0,
            reasons=list(self.reasons),
            requested_at=time.time(),
        )


@dataclass
class ResolveResult:
    tracks: list[Track]
    skipped: int = 0
    collection_title: str | None = None
    is_collection: bool = False
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
