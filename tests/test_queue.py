import random

import pytest

from bot.queue import LoopMode, MusicQueue, QueueError, QueueFullError
from tests.conftest import make_track


def titles(q: MusicQueue) -> list[str]:
    return [t.title for t in q.upcoming]


def test_add_preserves_order_and_positions():
    q = MusicQueue()
    assert q.add(make_track(1)) == 1
    assert q.add(make_track(2)) == 2
    assert q.add(make_track(3), front=True) == 1
    assert titles(q) == ["Song 3", "Song 1", "Song 2"]


def test_add_many_respects_capacity():
    q = MusicQueue(max_size=3)
    added, dropped = q.add_many([make_track(i) for i in range(5)])
    assert (added, dropped) == (3, 2)
    assert titles(q) == ["Song 0", "Song 1", "Song 2"]
    with pytest.raises(QueueFullError):
        q.add(make_track(9))


def test_add_many_front_keeps_playlist_order():
    q = MusicQueue()
    q.add(make_track("x"))
    q.add_many([make_track(1), make_track(2)], front=True)
    assert titles(q) == ["Song 1", "Song 2", "Song x"]


def test_remove_and_bounds():
    q = MusicQueue()
    q.add_many([make_track(i) for i in range(3)])
    assert q.remove(2).title == "Song 1"
    assert titles(q) == ["Song 0", "Song 2"]
    for bad in (0, 3, -1):
        with pytest.raises(QueueError):
            q.remove(bad)


def test_remove_from_empty_queue_message():
    with pytest.raises(QueueError, match="empty"):
        MusicQueue().remove(1)


def test_move():
    q = MusicQueue()
    q.add_many([make_track(i) for i in range(4)])
    q.move(1, 3)
    assert titles(q) == ["Song 1", "Song 2", "Song 0", "Song 3"]
    q.move(4, 1)
    assert titles(q) == ["Song 3", "Song 1", "Song 2", "Song 0"]
    with pytest.raises(QueueError):
        q.move(1, 5)


def test_shuffle_keeps_current_and_items():
    q = MusicQueue()
    q.add_many([make_track(i) for i in range(20)])
    q.advance()
    current = q.current
    before = sorted(titles(q))
    q.shuffle(random.Random(1))
    assert q.current is current
    assert sorted(titles(q)) == before
    assert titles(q) != before  # astronomically unlikely to be identical


def test_advance_loop_off():
    q = MusicQueue()
    q.add_many([make_track(1), make_track(2)])
    assert q.advance().title == "Song 1"
    assert q.advance().title == "Song 2"
    assert q.advance() is None
    assert q.current is None
    assert [t.title for t in q.history] == ["Song 1", "Song 2"]


def test_advance_loop_song_repeats_unless_skipped():
    q = MusicQueue()
    q.add_many([make_track(1), make_track(2)])
    q.loop = LoopMode.SONG
    first = q.advance()
    first.stream_url = "https://expired"
    assert q.advance() is first
    assert first.stream_url is None  # stream refreshed on each repeat
    assert q.advance(skipped=True).title == "Song 2"


def test_advance_loop_queue_cycles():
    q = MusicQueue()
    q.add_many([make_track(1), make_track(2)])
    q.loop = LoopMode.QUEUE
    seen = [q.advance().title for _ in range(5)]
    assert seen == ["Song 1", "Song 2", "Song 1", "Song 2", "Song 1"]


def test_skip_in_queue_loop_requeues_skipped_song():
    q = MusicQueue()
    q.add_many([make_track(1), make_track(2)])
    q.loop = LoopMode.QUEUE
    q.advance()
    assert q.advance(skipped=True).title == "Song 2"
    assert titles(q) == ["Song 1"]


def test_stop_and_drop_current():
    q = MusicQueue()
    q.add_many([make_track(1), make_track(2)])
    q.loop = LoopMode.QUEUE
    q.advance()
    q.drop_current()  # a failed track must not be re-queued
    assert q.current is None
    assert titles(q) == ["Song 2"]
    q.stop()
    assert q.current is None and q.is_empty
    assert q.advance() is None


def test_clear_keeps_current():
    q = MusicQueue()
    q.add_many([make_track(i) for i in range(3)])
    q.advance()
    assert q.clear() == 2
    assert q.current.title == "Song 0"


def test_total_duration_uses_expected_for_unresolved():
    q = MusicQueue()
    q.add(make_track(1, duration=100))
    q.add(make_track(2, duration=None, expected_duration=50))
    assert q.total_duration() == 150
