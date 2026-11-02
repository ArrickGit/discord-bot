"""Score search results when matching a Spotify / Last.fm song to a playable video.

Pure functions only, so the scoring can be unit-tested without network access.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Words that indicate a different version of a song. If the candidate has one but
# the target title doesn't, the candidate is probably wrong.
VERSION_WORDS = (
    "live", "cover", "remix", "karaoke", "instrumental", "sped up", "speed up", "slowed",
    "nightcore", "reverb", "8d", "acoustic", "reaction", "1 hour", "10 hours", "loop",
    "tutorial", "lesson", "piano version", "guitar cover", "drum cover", "bass boosted", "mashup",
    "extended mix", "demo", "performance", "concert", "fan made", "fanmade",
)
_NOISE = re.compile(
    r"\((?:official|lyric|lyrics|audio|video|music video|hd|hq|4k|visuali[sz]er)[^)]*\)"
    r"|\[(?:official|lyric|lyrics|audio|video|music video|hd|hq|4k|visuali[sz]er)[^\]]*\]",
    re.I,
)
_FEAT = re.compile(r"[\(\[]?\b(?:feat|ft|featuring)\.?\s[^\)\]\-]*[\)\]]?", re.I)
_NON_WORD = re.compile(r"[^\w\s]")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NOISE.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = text.replace("&", " and ")
    text = _NON_WORD.sub(" ", text.lower())
    return " ".join(text.split())


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", haystack) is not None


@dataclass(frozen=True)
class Candidate:
    title: str
    channel: str
    duration: int | None
    url: str
    id: str
    thumbnail: str | None = None


def score_candidate(
    title: str, artists: list[str], duration: int | None, cand: Candidate
) -> float:
    cand_title = normalize(cand.title)
    cand_channel = normalize(cand.channel.removesuffix(" - Topic"))
    target_tokens = _tokens(title)
    score = 0.0

    # Title overlap (up to 40).
    if target_tokens:
        overlap = len(target_tokens & set(cand_title.split())) / len(target_tokens)
        score += 40 * overlap
        if overlap < 0.5:
            score -= 25

    # Artist presence (up to 35). "Artist - Topic" channels are YouTube Music's
    # auto-generated official audio uploads, the best possible match.
    norm_artists = [normalize(a) for a in artists if normalize(a)]
    if norm_artists:
        primary = norm_artists[0]
        if primary == cand_channel:
            score += 25
            if cand.channel.endswith(" - Topic"):
                score += 10
        elif any(_contains_phrase(cand_title, a) or _contains_phrase(cand_channel, a) for a in norm_artists):
            score += 18
        else:
            score -= 10

    # Duration (the strongest signal when available).
    if duration and cand.duration:
        diff = abs(duration - cand.duration)
        if diff <= 3:
            score += 30
        elif diff <= 7:
            score += 20
        elif diff <= 15:
            score += 5
        elif diff <= 30:
            score -= 10
        else:
            score -= 45

    # Different-version penalty.
    target_norm = normalize(title)
    for word in VERSION_WORDS:
        if _contains_phrase(cand_title, word) and not _contains_phrase(target_norm, word):
            score -= 35
            break

    return score


def best_match(
    title: str,
    artists: list[str],
    duration: int | None,
    candidates: list[Candidate],
    threshold: float = 45.0,
) -> tuple[Candidate, float] | None:
    scored = [(c, score_candidate(title, artists, duration, c)) for c in candidates]
    scored = [s for s in scored if s[1] >= threshold]
    if not scored:
        return None
    return max(scored, key=lambda s: s[1])


def search_query(title: str, artists: list[str]) -> str:
    artist = artists[0] if artists else ""
    return f"{artist} - {title}".strip(" -")
