"""Identify and validate what the user passed to /play.

Links are parsed and checked against an allow-list of hosts before anything is
handed to yt-dlp, and canonical URLs are rebuilt from the extracted IDs so
tracking parameters and odd URL tricks never reach the extractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from .errors import UnsupportedLinkError

MAX_QUERY_LENGTH = 300


class Kind(StrEnum):
    TRACK = "track"
    PLAYLIST = "playlist"
    ALBUM = "album"
    SEARCH = "search"
    SHORTLINK = "shortlink"


class Service(StrEnum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    SPOTIFY = "spotify"
    SEARCH = "search"


@dataclass(frozen=True)
class Detected:
    service: Service
    kind: Kind
    url: str
    """Canonical URL (or the search text for searches)."""
    id: str | None = None

    @property
    def is_collection(self) -> bool:
        return self.kind in (Kind.PLAYLIST, Kind.ALBUM)


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_LIST = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_SC_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_SPOTIFY_URI = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SC_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
_SC_RESERVED = {
    "discover", "stream", "search", "upload", "you", "charts", "pages", "terms-of-use",
    "settings", "notifications", "messages", "people", "tags", "stations", "mobile", "jobs",
}
_SC_USER_SUBPAGES = {"likes", "tracks", "albums", "sets", "reposts", "popular-tracks", "followers", "following", "comments"}
SHORTLINK_HOSTS = {"spotify.link", "on.soundcloud.com", "spotify.app.link"}


def _yt_video(video_id: str) -> Detected:
    return Detected(Service.YOUTUBE, Kind.TRACK, f"https://www.youtube.com/watch?v={video_id}", video_id)


def _yt_playlist(list_id: str) -> Detected:
    return Detected(Service.YOUTUBE, Kind.PLAYLIST, f"https://www.youtube.com/playlist?list={list_id}", list_id)


def _detect_youtube(host: str, path: str, query: dict[str, list[str]]) -> Detected:
    parts = [p for p in path.split("/") if p]
    if host == "youtu.be":
        if parts and _YT_ID.match(parts[0]):
            return _yt_video(parts[0])
        raise UnsupportedLinkError("That youtu.be link doesn't contain a valid video ID.")

    video = (query.get("v") or [None])[0]
    playlist = (query.get("list") or [None])[0]
    if parts and parts[0] == "playlist":
        if playlist and _YT_LIST.match(playlist):
            return _yt_playlist(playlist)
        raise UnsupportedLinkError("That YouTube playlist link is missing its playlist ID.")
    if parts and parts[0] == "watch":
        if video and _YT_ID.match(video):
            return _yt_video(video)
        raise UnsupportedLinkError("That YouTube link doesn't contain a valid video ID.")
    if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v") and _YT_ID.match(parts[1]):
        return _yt_video(parts[1])
    if parts and parts[0] == "browse" and len(parts) >= 2 and parts[1].startswith("MPREb"):
        # YouTube Music album page; yt-dlp resolves it to its playlist.
        return Detected(Service.YOUTUBE, Kind.ALBUM, f"https://music.youtube.com/browse/{parts[1]}", parts[1])
    raise UnsupportedLinkError(
        "Only YouTube video, Shorts, YouTube Music and playlist links are supported (not channels or searches)."
    )


def _detect_soundcloud(path: str) -> Detected:
    parts = [p for p in path.split("/") if p]
    if not parts or not all(_SC_SEGMENT.match(p) for p in parts) or parts[0] in _SC_RESERVED:
        raise UnsupportedLinkError("That SoundCloud link isn't a track or playlist.")
    url = "https://soundcloud.com/" + "/".join(parts)
    if len(parts) >= 3 and parts[1] == "sets":
        return Detected(Service.SOUNDCLOUD, Kind.PLAYLIST, url, f"{parts[0]}/sets/{parts[2]}")
    if len(parts) in (2, 3) and parts[1] not in _SC_USER_SUBPAGES:
        # A third segment is a private share token (``/user/track/s-AbCdE``).
        if len(parts) == 3 and not parts[2].startswith("s-"):
            raise UnsupportedLinkError("That SoundCloud link isn't a track or playlist.")
        return Detected(Service.SOUNDCLOUD, Kind.TRACK, url, f"{parts[0]}/{parts[1]}")
    raise UnsupportedLinkError("Link to a SoundCloud track or set, not a profile page.")


def _detect_spotify(path: str) -> Detected:
    parts = [p for p in path.split("/") if p]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist") and _SPOTIFY_ID.match(parts[1]):
        kind = {"track": Kind.TRACK, "album": Kind.ALBUM, "playlist": Kind.PLAYLIST}[parts[0]]
        return Detected(Service.SPOTIFY, kind, f"https://open.spotify.com/{parts[0]}/{parts[1]}", parts[1])
    if parts and parts[0] in ("artist", "episode", "show", "user", "genre"):
        raise UnsupportedLinkError("Only Spotify track, album and playlist links are supported.")
    raise UnsupportedLinkError("That Spotify link isn't a track, album or playlist.")


def detect(text: str) -> Detected:
    """Classify user input. Plain text becomes a YouTube search."""
    text = (text or "").strip().strip("<>")
    if not text:
        raise UnsupportedLinkError("Give me a link or something to search for.")
    if len(text) > 2048:
        raise UnsupportedLinkError("That link is far too long.")

    uri = _SPOTIFY_URI.match(text)
    if uri:
        return _detect_spotify(f"/{uri.group(1)}/{uri.group(2)}")

    looks_like_url = re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I) or re.match(
        r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be|soundcloud\.com|open\.spotify\.com)/", text, re.I
    )
    if not looks_like_url:
        if len(text) > MAX_QUERY_LENGTH:
            raise UnsupportedLinkError(f"Search text must be under {MAX_QUERY_LENGTH} characters.")
        return Detected(Service.SEARCH, Kind.SEARCH, text)

    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UnsupportedLinkError("That link is malformed.") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsupportedLinkError("Only http(s) links are supported.")
    if parts.username or parts.password or port not in (None, 80, 443):
        raise UnsupportedLinkError("That link is malformed.")

    host = (parts.hostname or "").lower().rstrip(".")
    query = parse_qs(parts.query)
    if host in _YT_HOSTS or host == "youtu.be":
        return _detect_youtube(host, parts.path, query)
    if host in _SC_HOSTS:
        return _detect_soundcloud(parts.path)
    if host == "open.spotify.com":
        return _detect_spotify(parts.path)
    if host in SHORTLINK_HOSTS:
        service = Service.SPOTIFY if "spotify" in host else Service.SOUNDCLOUD
        clean = f"https://{host}{parts.path}"
        return Detected(service, Kind.SHORTLINK, clean)
    raise UnsupportedLinkError("Only YouTube, SoundCloud and Spotify links are supported.")
