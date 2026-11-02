"""Simple, explainable recommendation scoring.

Signals come from explicit ratings and listening history. Every score change
carries a human-readable reason so /why can explain a pick.
"""

from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .db import HistoryRow

# Tunable weights.
LIKED_TRACK = 50.0
LIKED_ARTIST_EACH, LIKED_ARTIST_CAP = 12.0, 36.0
DISLIKED_ARTIST_EACH, DISLIKED_ARTIST_CAP = -15.0, -45.0
COMPLETED_TRACK_EACH, COMPLETED_TRACK_CAP = 3.0, 15.0
COMPLETED_ARTIST_EACH, COMPLETED_ARTIST_CAP = 1.0, 10.0
QUICK_SKIP_TRACK_EACH, QUICK_SKIP_TRACK_CAP = -8.0, -32.0
QUICK_SKIP_ARTIST_EACH, QUICK_SKIP_ARTIST_CAP = -2.0, -10.0
NOT_NOW_PENALTY, NOT_NOW_DAYS = -30.0, 7.0
RECENT_HOUR_PENALTY, RECENT_DAY_PENALTY = -40.0, -15.0
DISCOVERY_BONUS = 10.0

QUICK_SKIP_SECONDS = 30.0
QUICK_SKIP_FRACTION = 0.25
COMPLETED_FRACTION = 0.8


def classify_play(listened: float | None, duration: int | None, outcome: str | None) -> str:
    """Return ``completed``, ``quick_skip`` or ``partial`` for one play."""
    listened = listened or 0.0
    if outcome == "completed":
        return "completed"
    if outcome == "skipped":
        threshold = QUICK_SKIP_SECONDS
        if duration:
            threshold = min(QUICK_SKIP_SECONDS, duration * QUICK_SKIP_FRACTION)
        if listened < threshold:
            return "quick_skip"
        if duration and listened >= duration * COMPLETED_FRACTION:
            return "completed"
    return "partial"


def normalize_artist(name: str) -> str:
    return " ".join((name or "").lower().removesuffix(" - topic").split())


@dataclass
class Signals:
    liked_tracks: set[str] = field(default_factory=set)
    disliked_tracks: set[str] = field(default_factory=set)
    not_now: dict[str, float] = field(default_factory=dict)
    liked_artists: Counter[str] = field(default_factory=Counter)
    disliked_artists: Counter[str] = field(default_factory=Counter)
    completed_tracks: Counter[str] = field(default_factory=Counter)
    completed_artists: Counter[str] = field(default_factory=Counter)
    skipped_tracks: Counter[str] = field(default_factory=Counter)
    skipped_artists: Counter[str] = field(default_factory=Counter)
    recent: dict[str, float] = field(default_factory=dict)
    artist_names: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.liked_tracks or self.completed_tracks or self.liked_artists)


def build_signals(
    feedback: Iterable[tuple[int, str, str, str, float]],
    history: Iterable[HistoryRow],
    recent: dict[str, float] | None = None,
) -> Signals:
    s = Signals(recent=dict(recent or {}))
    for _user, key, artist, rating, updated in feedback:
        a = normalize_artist(artist)
        if a:
            s.artist_names.setdefault(a, artist)
        if rating == "good":
            s.liked_tracks.add(key)
            if a:
                s.liked_artists[a] += 1
        elif rating == "bad":
            s.disliked_tracks.add(key)
            if a:
                s.disliked_artists[a] += 1
        elif rating == "not_now":
            s.not_now[key] = max(updated, s.not_now.get(key, 0))
    for row in history:
        a = normalize_artist(row.artist)
        if a:
            s.artist_names.setdefault(a, row.artist)
        kind = classify_play(row.listened_seconds, row.duration, row.outcome)
        if kind == "completed":
            s.completed_tracks[row.track_key] += 1
            if a:
                s.completed_artists[a] += 1
        elif kind == "quick_skip":
            s.skipped_tracks[row.track_key] += 1
            if a:
                s.skipped_artists[a] += 1
        s.recent[row.track_key] = max(row.started_at, s.recent.get(row.track_key, 0))
    return s


@dataclass
class Scored:
    track_key: str
    score: float
    reasons: list[str]


def _capped(count: int, each: float, cap: float) -> float:
    value = count * each
    return max(value, cap) if each < 0 else min(value, cap)


def score_track(
    track_key: str,
    artist: str,
    s: Signals,
    now: float | None = None,
    *,
    discovery_reason: str | None = None,
) -> Scored | None:
    """Score one candidate. ``None`` means never recommend it."""
    if track_key in s.disliked_tracks:
        return None
    now = now or time.time()
    a = normalize_artist(artist)
    shown_artist = s.artist_names.get(a, artist)
    score = 0.0
    reasons: list[str] = []

    if track_key in s.liked_tracks:
        score += LIKED_TRACK
        reasons.append("you rated this song 👍")
    if a and s.liked_artists[a]:
        score += _capped(s.liked_artists[a], LIKED_ARTIST_EACH, LIKED_ARTIST_CAP)
        n = s.liked_artists[a]
        reasons.append(f"you liked {n} song{'s' if n != 1 else ''} by {shown_artist}")
    if a and s.disliked_artists[a]:
        score += _capped(s.disliked_artists[a], DISLIKED_ARTIST_EACH, DISLIKED_ARTIST_CAP)
    if s.completed_tracks[track_key]:
        score += _capped(s.completed_tracks[track_key], COMPLETED_TRACK_EACH, COMPLETED_TRACK_CAP)
        reasons.append("you usually listen to this one all the way through")
    if a and s.completed_artists[a] >= 2:
        score += _capped(s.completed_artists[a], COMPLETED_ARTIST_EACH, COMPLETED_ARTIST_CAP)
        reasons.append(f"you often finish songs by {shown_artist}")
    if s.skipped_tracks[track_key]:
        score += _capped(s.skipped_tracks[track_key], QUICK_SKIP_TRACK_EACH, QUICK_SKIP_TRACK_CAP)
    if a and s.skipped_artists[a]:
        score += _capped(s.skipped_artists[a], QUICK_SKIP_ARTIST_EACH, QUICK_SKIP_ARTIST_CAP)

    if track_key in s.not_now:
        age_days = (now - s.not_now[track_key]) / 86_400
        if age_days < NOT_NOW_DAYS:
            score += NOT_NOW_PENALTY * (1 - age_days / NOT_NOW_DAYS)

    last = s.recent.get(track_key)
    if last is not None:
        if now - last < 3600 * 2:
            score += RECENT_HOUR_PENALTY
        elif now - last < 86_400:
            score += RECENT_DAY_PENALTY

    if discovery_reason:
        score += DISCOVERY_BONUS
        reasons.append(discovery_reason)
    return Scored(track_key, score, reasons)


def combine(per_user: list[Scored | None]) -> Scored | None:
    """Group scoring: anyone's hard dislike vetoes, otherwise scores are summed."""
    if any(s is None for s in per_user) or not per_user:
        return None
    total = sum(s.score for s in per_user if s)
    reasons = list(dict.fromkeys(r for s in per_user if s for r in s.reasons))
    return Scored(per_user[0].track_key, total, reasons)  # type: ignore[union-attr]


def pick(scored: list[Scored], rng: random.Random | None = None, top_n: int = 8) -> Scored | None:
    """Weighted random choice among the best candidates, keeping some variety."""
    rng = rng or random.Random()
    pool = sorted((s for s in scored if s.score > -20), key=lambda s: s.score, reverse=True)[:top_n]
    if not pool:
        return None
    floor = min(s.score for s in pool)
    weights = [s.score - floor + 5 for s in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def top_artists(s: Signals, n: int = 5) -> list[str]:
    scores: defaultdict[str, float] = defaultdict(float)
    for a, c in s.liked_artists.items():
        scores[a] += c * 3
    for a, c in s.completed_artists.items():
        scores[a] += c
    for a, c in s.disliked_artists.items():
        scores[a] -= c * 3
    ranked = sorted((a for a, v in scores.items() if v > 0), key=lambda a: scores[a], reverse=True)
    return [s.artist_names.get(a, a) for a in ranked[:n]]
