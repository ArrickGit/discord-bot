"""User-facing errors raised while resolving or streaming music."""

from __future__ import annotations


class SourceError(Exception):
    """Base error. ``str(error)`` is safe to show to Discord users."""


class UnsupportedLinkError(SourceError):
    pass


class TrackUnavailableError(SourceError):
    pass


class NoMatchError(SourceError):
    pass


class SourceConfigError(SourceError):
    pass


class SourceBlockedError(SourceError):
    """The platform is refusing requests (bot checks, rate limits)."""


def friendly_ytdlp_error(message: str) -> SourceError:
    """Translate a raw yt-dlp error message into something readable."""
    m = message.lower()
    if "private video" in m or "this track is private" in m or "private playlist" in m:
        return TrackUnavailableError("That video or track is private.")
    if "confirm your age" in m or "age-restricted" in m or "age restricted" in m or "inappropriate for some users" in m:
        return TrackUnavailableError("That video is age-restricted, so it can't be played.")
    if "not available in your country" in m or "geo restrict" in m or "geo-restrict" in m or "blocked it in your country" in m:
        return TrackUnavailableError("That content is region-restricted and isn't available here.")
    if "not a bot" in m or "sign in to confirm" in m:
        return SourceBlockedError(
            "YouTube is asking this server to prove it isn't a bot. Try again later, or ask the bot owner "
            "to update yt-dlp / configure cookies."
        )
    if "http error 429" in m or "too many requests" in m:
        return SourceBlockedError("The music platform is rate-limiting the bot. Try again in a few minutes.")
    if "members-only" in m or "join this channel" in m or "requires payment" in m or "premium" in m:
        return TrackUnavailableError("That content is members-only or paid, so it can't be played.")
    if "live event will begin" in m or "premieres in" in m:
        return TrackUnavailableError("That stream or premiere hasn't started yet.")
    if (
        "video unavailable" in m
        or "video is unavailable" in m
        or "has been removed" in m
        or "does not exist" in m
        or "http error 404" in m
        or "no longer available" in m
        or "unable to download json metadata" in m
    ):
        return TrackUnavailableError("That content is unavailable (it may have been deleted).")
    if "unsupported url" in m:
        return UnsupportedLinkError("That link isn't supported.")
    if "requested format is not available" in m:
        return TrackUnavailableError("No playable audio format was found for that link.")
    return TrackUnavailableError("That content couldn't be loaded.")
