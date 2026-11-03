import sqlite3

import pytest

from bot.db import Database
from tests.conftest import make_track


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


async def test_migration_sets_version(db):
    async with db.conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == 2
    assert await db.healthcheck()


async def test_reconnect_does_not_remigrate(tmp_path):
    d = Database(tmp_path / "x.db")
    await d.connect()
    await d.close()
    await d.connect()  # would fail with "table exists" if migrations re-ran
    await d.close()


async def test_guild_settings_roundtrip(db):
    s = await db.get_guild_settings(42)
    assert s.control_mode == "everyone" and s.default_volume is None
    s = await db.update_guild_settings(42, default_volume=30, control_mode="dj", vote_skip=True)
    assert (s.default_volume, s.control_mode, s.vote_skip) == (30, "dj", True)
    s = await db.update_guild_settings(42, dj_role_id=None)
    assert s.default_volume == 30


async def test_guild_settings_rejects_unknown_columns(db):
    with pytest.raises(ValueError):
        await db.update_guild_settings(1, **{"default_volume = 1; DROP TABLE tracks; --": 1})
    with pytest.raises(ValueError):
        await db.update_guild_settings(1, control_mode="anarchy")


async def test_parameterised_queries_handle_hostile_text(db):
    t = make_track(1, title="'); DROP TABLE tracks; --", artist="Robert'); --")
    await db.upsert_track(t)
    stored = await db.get_track(t.key)
    assert stored.title == t.title and stored.artist == t.artist


async def test_unresolved_tracks_are_not_stored(db):
    t = make_track(1)
    t.search_query = "artist - song"
    assert await db.upsert_track(t) is None


async def test_history_and_stats(db):
    t = make_track(1)
    hid = await db.record_play_start(7, t, requested_at=None)
    await db.record_play_end(hid, 190.0, "completed")
    hid = await db.record_play_start(7, make_track(2), requested_at=None)
    await db.record_play_end(hid, 5.0, "skipped")
    stats = await db.user_stats(111)
    assert (stats["plays"], stats["completed"], stats["skipped"]) == (2, 1, 1)
    assert stats["top_artists"] == [("Artist", 1)]
    rows = await db.user_history([111])
    assert {r.outcome for r in rows} == {"completed", "skipped"}
    assert await db.guild_popular_tracks(7) == ["youtube:vid1"]


async def test_feedback_updates_instead_of_duplicating(db):
    t = make_track(1)
    await db.set_feedback(5, t, "good")
    await db.set_feedback(5, t, "bad")
    await db.set_feedback(6, t, "good")  # another user's rating is independent
    assert await db.get_feedback(5, t.key) == "bad"
    assert await db.get_feedback(6, t.key) == "good"
    rows = await db.user_feedback([5])
    assert len(rows) == 1
    with pytest.raises(ValueError):
        await db.set_feedback(5, t, "meh")


async def test_forget_and_reset(db):
    t = make_track(1, requester_id=5)
    await db.set_feedback(5, t, "good")
    hid = await db.record_play_start(1, t, None)
    await db.record_play_end(hid, 100, "completed")
    assert await db.forget_track(5, t.key) == 2
    assert await db.get_feedback(5, t.key) is None

    await db.set_feedback(5, t, "good")
    pid = await db.create_playlist(5, "Mine")
    counts = await db.reset_user(5)
    assert counts == {"ratings": 1, "history": 0}
    assert await db.find_playlist(5, "mine") == pid  # playlists kept unless asked
    counts = await db.reset_user(5, include_playlists=True)
    assert counts["playlists"] == 1


async def test_playlists(db):
    pid = await db.create_playlist(9, "Road Trip")
    with pytest.raises(ValueError):
        await db.create_playlist(9, "road trip")  # names are case-insensitive per user
    await db.create_playlist(10, "Road Trip")  # other users can reuse names
    assert await db.find_playlist(9, "ROAD TRIP") == pid
    await db.add_to_playlist(pid, [make_track(i) for i in range(4)])
    assert [t.title for t in await db.playlist_tracks(pid)] == ["Song 0", "Song 1", "Song 2", "Song 3"]
    removed = await db.remove_from_playlist(pid, 2)
    assert removed.title == "Song 1"
    assert [t.title for t in await db.playlist_tracks(pid)] == ["Song 0", "Song 2", "Song 3"]
    await db.add_to_playlist(pid, [make_track(9)])
    assert [t.title for t in await db.playlist_tracks(pid)][-1] == "Song 9"
    assert await db.remove_from_playlist(pid, 99) is None
    assert await db.list_playlists(9) == [("Road Trip", 4)]
    await db.delete_playlist(pid)
    assert await db.list_playlists(9) == []


async def test_backup(db, tmp_path):
    await db.upsert_track(make_track(1))
    dest = await db.backup(tmp_path / "backups" / "b.db")
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    await db.backup(dest)  # overwriting an existing backup works


async def test_state(db):
    assert await db.get_state("maintenance") is None
    await db.set_state("maintenance", "upgrading")
    assert await db.get_state("maintenance") == "upgrading"
    await db.set_state("maintenance", None)
    assert await db.get_state("maintenance") is None


async def test_saved_sessions_roundtrip_and_are_taken_once(db):
    await db.save_session(1, 10, 20, {"current": None, "upcoming": [{"title": "x"}]})
    await db.save_session(1, 11, None, {"upcoming": []})  # same server: replaced
    await db.save_session(2, 30, 40, {"upcoming": []})
    sessions = {s.guild_id: s for s in await db.take_sessions(max_age=60)}
    assert set(sessions) == {1, 2}
    assert (sessions[1].voice_channel_id, sessions[1].text_channel_id) == (11, None)
    assert sessions[1].data == {"upcoming": []}
    assert await db.take_sessions(max_age=60) == []  # taken: a second start won't restore again


async def test_old_saved_sessions_are_dropped(db):
    await db.save_session(1, 10, None, {"upcoming": []})
    await db.conn.execute("UPDATE saved_sessions SET saved_at = saved_at - 7200")
    await db.conn.commit()
    assert await db.take_sessions(max_age=3600) == []
    await db.save_session(1, 10, None, {"upcoming": []})
    await db.delete_session(1)
    assert await db.take_sessions(max_age=3600) == []
