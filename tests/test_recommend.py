import random
import time

import pytest

from bot import recommend
from bot.db import Database, HistoryRow
from bot.recommender import Recommender
from tests.conftest import make_track

NOW = 1_800_000_000.0


def row(key, artist, outcome, listened, duration=200, started=NOW - 10 * 86_400, user=1):
    return HistoryRow(user, key, artist, started, listened, duration, outcome)


def test_classify_play():
    assert recommend.classify_play(200, 200, "completed") == "completed"
    assert recommend.classify_play(5, 200, "skipped") == "quick_skip"
    assert recommend.classify_play(10, 30, "skipped") == "partial"  # 25% of 30s = 7.5s threshold
    assert recommend.classify_play(170, 200, "skipped") == "completed"  # skipped near the end
    assert recommend.classify_play(60, 200, "skipped") == "partial"
    assert recommend.classify_play(1, 200, "error") == "partial"


def test_signals_and_scores():
    feedback = [
        (1, "yt:a", "Fav Artist", "good", NOW - 100),
        (1, "yt:b", "Fav Artist", "good", NOW - 100),
        (1, "yt:bad", "Hated", "bad", NOW - 100),
        (1, "yt:later", "Other", "not_now", NOW - 3600),
    ]
    history = [row("yt:c", "Fav Artist", "completed", 200), row("yt:skip", "Meh", "skipped", 3)]
    s = recommend.build_signals(feedback, history)

    assert recommend.score_track("yt:bad", "Hated", s, NOW) is None  # explicit dislike excluded

    liked = recommend.score_track("yt:a", "Fav Artist", s, NOW)
    same_artist = recommend.score_track("yt:new", "Fav Artist", s, NOW)
    disliked_artist = recommend.score_track("yt:new2", "Hated", s, NOW)
    later = recommend.score_track("yt:later", "Other", s, NOW)
    skipped = recommend.score_track("yt:skip", "Meh", s, NOW)
    assert liked.score > same_artist.score > 0
    assert disliked_artist.score < 0
    assert later.score < 0
    assert skipped.score < 0
    assert any("liked 2 songs by Fav Artist" in r for r in same_artist.reasons)


def test_not_now_decays():
    s = recommend.build_signals([(1, "k", "A", "not_now", NOW - 8 * 86_400)], [])
    assert recommend.score_track("k", "A", s, NOW).score == 0


def test_recent_play_penalty():
    s = recommend.build_signals([(1, "k", "A", "good", NOW)], [], recent={"k": NOW - 600})
    fresh = recommend.build_signals([(1, "k", "A", "good", NOW)], [])
    assert recommend.score_track("k", "A", s, NOW).score < recommend.score_track("k", "A", fresh, NOW).score


def test_group_veto_and_sum():
    a = recommend.Scored("k", 10, ["x"])
    b = recommend.Scored("k", 5, ["y"])
    assert recommend.combine([a, b]).score == 15
    assert recommend.combine([a, None]) is None


def test_pick_prefers_high_scores_but_varies():
    scored = [recommend.Scored(f"k{i}", float(i * 10), []) for i in range(10)]
    rng = random.Random(0)
    picks = [recommend.pick(scored, rng).track_key for _ in range(300)]
    assert "k0" not in picks  # outside the top 8
    assert picks.count("k9") > picks.count("k3")
    assert len(set(picks)) > 3
    assert recommend.pick([recommend.Scored("x", -50, [])]) is None


def test_top_artists():
    s = recommend.build_signals(
        [(1, "a", "Alpha", "good", NOW), (1, "b", "Beta", "bad", NOW)],
        [row("c", "Gamma", "completed", 200)],
    )
    assert recommend.top_artists(s) == ["Alpha", "Gamma"]


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "r.db")
    await d.connect()
    yield d
    await d.close()


async def test_recommender_end_to_end(db):
    liked = make_track(1, artist="Fav", requester_id=1)
    other_by_fav = make_track(2, artist="Fav", requester_id=2)
    hated = make_track(3, artist="Hated", requester_id=1)
    await db.set_feedback(1, liked, "good")
    await db.set_feedback(1, hated, "bad")
    for t in (other_by_fav, hated):
        hid = await db.record_play_start(99, t, None)
        await db.record_play_end(hid, 200, "completed")

    rec = Recommender(db, lastfm=None)
    picks = set()
    for _ in range(30):
        track = await rec.recommend(99, [1], requester_id=1, requester_name="me")
        assert track is not None and track.key != hated.key
        assert track.reasons and track.via == "random" and track.requester_id == 1
        picks.add(track.key)
    assert picks <= {liked.key, other_by_fav.key}

    # Excluding everything good leaves nothing to recommend.
    assert await rec.recommend(99, [1], exclude={liked.key, other_by_fav.key}) is None
    # Songs the server played in the last couple of hours are held back to avoid repeats...
    assert await rec.recommend(99, [12345]) is None
    # ...but once they're older, a brand-new user gets what's popular in the server.
    await db.conn.execute("UPDATE history SET started_at = started_at - 3 * 86400")
    await db.conn.commit()
    track = await rec.recommend(99, [12345])
    assert track is not None and any("popular" in r for r in track.reasons)


async def test_recommender_unknown_user_empty_server(db):
    assert await Recommender(db, None).recommend(1, [42]) is None
    assert await Recommender(db, None).recommend(1, []) is None


async def test_recommender_uses_lastfm_discoveries(db):
    liked = make_track(1, artist="Fav", title="Seed Song", requester_id=1)
    await db.set_feedback(1, liked, "good")

    class FakeLastFM:
        async def similar_tracks(self, artist, title, limit=10):
            assert (artist, title) == ("Fav", "Seed Song")
            return [("Fav", "Brand New Song")]

    rec = Recommender(db, FakeLastFM())
    seen_discovery = False
    for _ in range(40):
        track = await rec.recommend(1, [1], exclude={liked.key})
        if track and track.search_query:
            seen_discovery = True
            assert track.search_query == "Fav - Brand New Song"
            assert any("similar" in r for r in track.reasons)
    assert seen_discovery
