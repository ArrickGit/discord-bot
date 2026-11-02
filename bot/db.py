"""SQLite storage via aiosqlite.

All queries are parameterised. Only Discord IDs (never usernames) are stored
about people.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import Platform, Track

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE guild_settings (
        guild_id            INTEGER PRIMARY KEY,
        default_volume      INTEGER,
        dj_role_id          INTEGER,
        control_mode        TEXT    NOT NULL DEFAULT 'everyone',
        inactivity_timeout  INTEGER,
        loop_mode           TEXT    NOT NULL DEFAULT 'off',
        autoplay            INTEGER NOT NULL DEFAULT 0,
        vote_skip           INTEGER NOT NULL DEFAULT 0,
        vote_skip_ratio     REAL    NOT NULL DEFAULT 0.5
    );

    CREATE TABLE tracks (
        track_key    TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        artist       TEXT NOT NULL DEFAULT '',
        url          TEXT NOT NULL,
        platform     TEXT NOT NULL,
        duration     INTEGER,
        artwork_url  TEXT,
        origin_url   TEXT,
        first_seen   REAL NOT NULL,
        last_seen    REAL NOT NULL
    );
    CREATE INDEX idx_tracks_artist ON tracks(artist COLLATE NOCASE);

    CREATE TABLE history (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id          INTEGER NOT NULL,
        user_id           INTEGER NOT NULL,
        track_key         TEXT    NOT NULL REFERENCES tracks(track_key),
        requested_at      REAL,
        started_at        REAL    NOT NULL,
        ended_at          REAL,
        listened_seconds  REAL,
        outcome           TEXT,
        via               TEXT    NOT NULL DEFAULT 'request'
    );
    CREATE INDEX idx_history_user ON history(user_id, started_at);
    CREATE INDEX idx_history_guild ON history(guild_id, started_at);
    CREATE INDEX idx_history_track ON history(track_key);

    CREATE TABLE feedback (
        user_id     INTEGER NOT NULL,
        track_key   TEXT    NOT NULL REFERENCES tracks(track_key),
        rating      TEXT    NOT NULL CHECK (rating IN ('good', 'bad', 'not_now')),
        updated_at  REAL    NOT NULL,
        PRIMARY KEY (user_id, track_key)
    );

    CREATE TABLE user_playlists (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id    INTEGER NOT NULL,
        name        TEXT    NOT NULL COLLATE NOCASE,
        created_at  REAL    NOT NULL,
        UNIQUE (owner_id, name)
    );

    CREATE TABLE user_playlist_items (
        playlist_id  INTEGER NOT NULL REFERENCES user_playlists(id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        track_key    TEXT    NOT NULL REFERENCES tracks(track_key),
        added_at     REAL    NOT NULL,
        PRIMARY KEY (playlist_id, position)
    );

    CREATE TABLE bot_state (
        key    TEXT PRIMARY KEY,
        value  TEXT
    );
    """,
}

CONTROL_MODES = ("everyone", "dj", "mods")
RATINGS = ("good", "bad", "not_now")


@dataclass
class GuildSettings:
    guild_id: int
    default_volume: int | None = None
    dj_role_id: int | None = None
    control_mode: str = "everyone"
    inactivity_timeout: int | None = None
    loop_mode: str = "off"
    autoplay: bool = False
    vote_skip: bool = False
    vote_skip_ratio: float = 0.5


_SETTING_COLUMNS = {f.name for f in fields(GuildSettings)} - {"guild_id"}


@dataclass
class StoredTrack:
    track_key: str
    title: str
    artist: str
    url: str
    platform: str
    duration: int | None
    artwork_url: str | None
    origin_url: str | None

    def to_track(self, requester_id: int = 0, requester_name: str = "") -> Track:
        try:
            platform = Platform(self.platform)
        except ValueError:
            platform = Platform.YOUTUBE
        return Track(
            title=self.title,
            artist=self.artist,
            url=self.url,
            platform=platform,
            source_id=self.track_key.split(":", 1)[1] if ":" in self.track_key else None,
            duration=self.duration,
            thumbnail=self.artwork_url,
            requester_id=requester_id,
            requester_name=requester_name,
            origin_url=self.origin_url,
        )


@dataclass
class HistoryRow:
    user_id: int
    track_key: str
    artist: str
    started_at: float
    listened_seconds: float | None
    duration: int | None
    outcome: str | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        for target in range(version + 1, SCHEMA_VERSION + 1):
            log.info("Applying database migration %s", target)
            await self.conn.executescript(f"BEGIN;\n{_MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;")

    async def healthcheck(self) -> bool:
        try:
            async with self.conn.execute("SELECT 1") as cur:
                return (await cur.fetchone())[0] == 1
        except Exception:
            return False

    async def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        await self.conn.execute("VACUUM INTO ?", (str(destination),))
        return destination

    # ----- bot state --------------------------------------------------
    async def get_state(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            await self.conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
        else:
            await self.conn.execute(
                "INSERT INTO bot_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await self.conn.commit()

    # ----- guild settings ---------------------------------------------
    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        async with self.conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return GuildSettings(guild_id=guild_id)
        data = dict(row)
        data["autoplay"] = bool(data["autoplay"])
        data["vote_skip"] = bool(data["vote_skip"])
        return GuildSettings(**data)

    async def update_guild_settings(self, guild_id: int, **values: Any) -> GuildSettings:
        unknown = set(values) - _SETTING_COLUMNS
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        if "control_mode" in values and values["control_mode"] not in CONTROL_MODES:
            raise ValueError("Invalid control mode")
        await self.conn.execute("INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,))
        if values:
            # Column names come from the dataclass allow-list above, never from users.
            assignments = ", ".join(f"{col} = ?" for col in values)
            params = [int(v) if isinstance(v, bool) else v for v in values.values()]
            await self.conn.execute(
                f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?", (*params, guild_id)
            )
        await self.conn.commit()
        return await self.get_guild_settings(guild_id)

    # ----- tracks -----------------------------------------------------
    async def upsert_track(self, track: Track, *, commit: bool = True) -> str | None:
        key = track.key
        if key is None:
            return None
        now = time.time()
        await self.conn.execute(
            """
            INSERT INTO tracks(track_key, title, artist, url, platform, duration, artwork_url, origin_url,
                               first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_key) DO UPDATE SET
                title = excluded.title,
                artist = CASE WHEN excluded.artist != '' THEN excluded.artist ELSE tracks.artist END,
                url = excluded.url,
                duration = COALESCE(excluded.duration, tracks.duration),
                artwork_url = COALESCE(excluded.artwork_url, tracks.artwork_url),
                origin_url = COALESCE(excluded.origin_url, tracks.origin_url),
                last_seen = excluded.last_seen
            """,
            (
                key, track.title, track.artist, track.url, track.platform.value, track.duration,
                track.thumbnail, track.origin_url, now, now,
            ),
        )
        if commit:
            await self.conn.commit()
        return key

    async def get_track(self, track_key: str) -> StoredTrack | None:
        async with self.conn.execute(
            "SELECT track_key, title, artist, url, platform, duration, artwork_url, origin_url "
            "FROM tracks WHERE track_key = ?",
            (track_key,),
        ) as cur:
            row = await cur.fetchone()
        return StoredTrack(**dict(row)) if row else None

    async def get_tracks(self, keys: Iterable[str]) -> dict[str, StoredTrack]:
        keys = list(dict.fromkeys(keys))
        result: dict[str, StoredTrack] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ",".join("?" * len(chunk))
            async with self.conn.execute(
                "SELECT track_key, title, artist, url, platform, duration, artwork_url, origin_url "
                f"FROM tracks WHERE track_key IN ({marks})",
                chunk,
            ) as cur:
                for row in await cur.fetchall():
                    result[row["track_key"]] = StoredTrack(**dict(row))
        return result

    # ----- history ----------------------------------------------------
    async def record_play_start(self, guild_id: int, track: Track, requested_at: float | None) -> int | None:
        key = await self.upsert_track(track, commit=False)
        if key is None:
            return None
        cur = await self.conn.execute(
            "INSERT INTO history(guild_id, user_id, track_key, requested_at, started_at, via) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, track.requester_id, key, requested_at, time.time(), track.via),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def record_play_end(self, history_id: int, listened_seconds: float, outcome: str) -> None:
        await self.conn.execute(
            "UPDATE history SET ended_at = ?, listened_seconds = ?, outcome = ? WHERE id = ?",
            (time.time(), listened_seconds, outcome, history_id),
        )
        await self.conn.commit()

    async def user_history(self, user_ids: Iterable[int], since: float = 0, limit: int = 2000) -> list[HistoryRow]:
        ids = list(user_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        async with self.conn.execute(
            f"""
            SELECT h.user_id, h.track_key, t.artist, h.started_at, h.listened_seconds, t.duration, h.outcome
            FROM history h JOIN tracks t ON t.track_key = h.track_key
            WHERE h.user_id IN ({marks}) AND h.started_at >= ?
            ORDER BY h.started_at DESC LIMIT ?
            """,
            (*ids, since, limit),
        ) as cur:
            return [HistoryRow(**dict(r)) for r in await cur.fetchall()]

    async def recent_guild_tracks(self, guild_id: int, since: float) -> dict[str, float]:
        async with self.conn.execute(
            "SELECT track_key, MAX(started_at) AS last FROM history WHERE guild_id = ? AND started_at >= ? "
            "GROUP BY track_key",
            (guild_id, since),
        ) as cur:
            return {r["track_key"]: r["last"] for r in await cur.fetchall()}

    async def guild_popular_tracks(self, guild_id: int, limit: int = 200) -> list[str]:
        async with self.conn.execute(
            "SELECT track_key FROM history WHERE guild_id = ? AND outcome = 'completed' "
            "GROUP BY track_key ORDER BY COUNT(*) DESC, MAX(started_at) DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [r["track_key"] for r in await cur.fetchall()]

    async def tracks_by_artists(self, artists: Iterable[str], limit: int = 300) -> list[str]:
        names = [a for a in dict.fromkeys(a.strip() for a in artists) if a]
        if not names:
            return []
        marks = ",".join("?" * len(names))
        async with self.conn.execute(
            f"SELECT track_key FROM tracks WHERE artist COLLATE NOCASE IN ({marks}) "
            "ORDER BY last_seen DESC LIMIT ?",
            (*names, limit),
        ) as cur:
            return [r["track_key"] for r in await cur.fetchall()]

    async def user_stats(self, user_id: int) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT COUNT(*) AS plays, "
            "SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN outcome = 'skipped' THEN 1 ELSE 0 END) AS skipped, "
            "COALESCE(SUM(listened_seconds), 0) AS seconds "
            "FROM history WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = dict(await cur.fetchone())
        async with self.conn.execute(
            "SELECT t.artist, COUNT(*) AS n FROM history h JOIN tracks t ON t.track_key = h.track_key "
            "WHERE h.user_id = ? AND h.outcome = 'completed' AND t.artist != '' "
            "GROUP BY t.artist COLLATE NOCASE ORDER BY n DESC LIMIT 5",
            (user_id,),
        ) as cur:
            row["top_artists"] = [(r["artist"], r["n"]) for r in await cur.fetchall()]
        async with self.conn.execute(
            "SELECT rating, COUNT(*) AS n FROM feedback WHERE user_id = ? GROUP BY rating", (user_id,)
        ) as cur:
            row["ratings"] = {r["rating"]: r["n"] for r in await cur.fetchall()}
        return row

    # ----- feedback ---------------------------------------------------
    async def set_feedback(self, user_id: int, track: Track, rating: str) -> None:
        if rating not in RATINGS:
            raise ValueError("Invalid rating")
        key = await self.upsert_track(track, commit=False)
        if key is None:
            raise ValueError("Track has no stable identity yet")
        await self.conn.execute(
            "INSERT INTO feedback(user_id, track_key, rating, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, track_key) DO UPDATE SET rating = excluded.rating, updated_at = excluded.updated_at",
            (user_id, key, rating, time.time()),
        )
        await self.conn.commit()

    async def get_feedback(self, user_id: int, track_key: str) -> str | None:
        async with self.conn.execute(
            "SELECT rating FROM feedback WHERE user_id = ? AND track_key = ?", (user_id, track_key)
        ) as cur:
            row = await cur.fetchone()
        return row["rating"] if row else None

    async def user_feedback(self, user_ids: Iterable[int]) -> list[tuple[int, str, str, str, float]]:
        """Rows of ``(user_id, track_key, artist, rating, updated_at)``."""
        ids = list(user_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        async with self.conn.execute(
            f"SELECT f.user_id, f.track_key, t.artist, f.rating, f.updated_at "
            f"FROM feedback f JOIN tracks t ON t.track_key = f.track_key WHERE f.user_id IN ({marks})",
            ids,
        ) as cur:
            return [tuple(r) for r in await cur.fetchall()]  # type: ignore[misc]

    async def rated_tracks(self, user_id: int, rating: str, limit: int = 25, offset: int = 0) -> list[StoredTrack]:
        async with self.conn.execute(
            "SELECT t.track_key, t.title, t.artist, t.url, t.platform, t.duration, t.artwork_url, t.origin_url "
            "FROM feedback f JOIN tracks t ON t.track_key = f.track_key "
            "WHERE f.user_id = ? AND f.rating = ? ORDER BY f.updated_at DESC LIMIT ? OFFSET ?",
            (user_id, rating, limit, offset),
        ) as cur:
            return [StoredTrack(**dict(r)) for r in await cur.fetchall()]

    async def forget_track(self, user_id: int, track_key: str) -> int:
        c1 = await self.conn.execute("DELETE FROM feedback WHERE user_id = ? AND track_key = ?", (user_id, track_key))
        c2 = await self.conn.execute("DELETE FROM history WHERE user_id = ? AND track_key = ?", (user_id, track_key))
        await self.conn.commit()
        return c1.rowcount + c2.rowcount

    async def reset_user(self, user_id: int, *, include_playlists: bool = False) -> dict[str, int]:
        counts = {}
        cur = await self.conn.execute("DELETE FROM feedback WHERE user_id = ?", (user_id,))
        counts["ratings"] = cur.rowcount
        cur = await self.conn.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        counts["history"] = cur.rowcount
        if include_playlists:
            cur = await self.conn.execute("DELETE FROM user_playlists WHERE owner_id = ?", (user_id,))
            counts["playlists"] = cur.rowcount
        await self.conn.commit()
        return counts

    # ----- saved playlists --------------------------------------------
    async def create_playlist(self, owner_id: int, name: str) -> int:
        try:
            cur = await self.conn.execute(
                "INSERT INTO user_playlists(owner_id, name, created_at) VALUES (?, ?, ?)",
                (owner_id, name, time.time()),
            )
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"You already have a playlist called “{name}”.") from exc
        await self.conn.commit()
        return int(cur.lastrowid)

    async def find_playlist(self, owner_id: int, name: str) -> int | None:
        async with self.conn.execute(
            "SELECT id FROM user_playlists WHERE owner_id = ? AND name = ?", (owner_id, name)
        ) as cur:
            row = await cur.fetchone()
        return row["id"] if row else None

    async def list_playlists(self, owner_id: int) -> list[tuple[str, int]]:
        async with self.conn.execute(
            "SELECT p.name, COUNT(i.track_key) AS n FROM user_playlists p "
            "LEFT JOIN user_playlist_items i ON i.playlist_id = p.id "
            "WHERE p.owner_id = ? GROUP BY p.id ORDER BY p.name",
            (owner_id,),
        ) as cur:
            return [(r["name"], r["n"]) for r in await cur.fetchall()]

    async def delete_playlist(self, playlist_id: int) -> None:
        await self.conn.execute("DELETE FROM user_playlists WHERE id = ?", (playlist_id,))
        await self.conn.commit()

    async def playlist_size(self, playlist_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM user_playlist_items WHERE playlist_id = ?", (playlist_id,)
        ) as cur:
            return (await cur.fetchone())[0]

    async def add_to_playlist(self, playlist_id: int, tracks: list[Track]) -> int:
        async with self.conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM user_playlist_items WHERE playlist_id = ?", (playlist_id,)
        ) as cur:
            position = (await cur.fetchone())[0]
        added = 0
        now = time.time()
        for track in tracks:
            key = await self.upsert_track(track, commit=False)
            if key is None:
                continue
            position += 1
            await self.conn.execute(
                "INSERT INTO user_playlist_items(playlist_id, position, track_key, added_at) VALUES (?, ?, ?, ?)",
                (playlist_id, position, key, now),
            )
            added += 1
        await self.conn.commit()
        return added

    async def playlist_tracks(self, playlist_id: int) -> list[StoredTrack]:
        async with self.conn.execute(
            "SELECT t.track_key, t.title, t.artist, t.url, t.platform, t.duration, t.artwork_url, t.origin_url "
            "FROM user_playlist_items i JOIN tracks t ON t.track_key = i.track_key "
            "WHERE i.playlist_id = ? ORDER BY i.position",
            (playlist_id,),
        ) as cur:
            return [StoredTrack(**dict(r)) for r in await cur.fetchall()]

    async def remove_from_playlist(self, playlist_id: int, index: int) -> StoredTrack | None:
        """Remove the ``index``-th (1-based) song and renumber the rest."""
        async with self.conn.execute(
            "SELECT position, track_key FROM user_playlist_items WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not 1 <= index <= len(rows):
            return None
        target = rows[index - 1]
        stored = await self.get_track(target["track_key"])
        await self.conn.execute(
            "DELETE FROM user_playlist_items WHERE playlist_id = ? AND position = ?", (playlist_id, target["position"])
        )
        # Renumber to keep positions contiguous (two passes avoid PK collisions).
        await self.conn.execute(
            "UPDATE user_playlist_items SET position = -position WHERE playlist_id = ? AND position > ?",
            (playlist_id, target["position"]),
        )
        await self.conn.execute(
            "UPDATE user_playlist_items SET position = -position - 1 WHERE playlist_id = ? AND position < 0",
            (playlist_id,),
        )
        await self.conn.commit()
        return stored
