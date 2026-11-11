"""Drive the real GuildPlayer loop with a fake voice connection."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import time

import pytest

from bot.config import parse_config
from bot.db import Database, GuildSettings
from bot import player as player_module
from bot.player import GuildPlayer, PlayerManager
from bot.queue import LoopMode
from bot.sources.errors import TrackUnavailableError
from tests.conftest import make_track

_ids = itertools.count(1000)


class FakeMember:
    def __init__(self, bot=False):
        self.bot = bot
        self.id = next(_ids)


class FakeChannel:
    def __init__(self, listeners=1):
        self.id = 1
        self.members = [FakeMember() for _ in range(listeners)] + [FakeMember(bot=True)]
        self.mention = "<#1>"


class FakeSource:
    def __init__(self, track, volume):
        self.title = track.title
        self.volume = volume

    def cleanup(self):
        pass


class FakeVoice:
    def __init__(self, channel):
        self.channel = channel
        self.connected = True
        self.played: list[str] = []
        self.source = None
        self._after = None
        self._playing = self._paused = False

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._playing and self._paused

    def play(self, source, after):
        self.source, self._after, self._playing, self._paused = source, after, True, False
        self.played.append(source.title)

    def finish(self, error=None):
        """Simulate the audio thread reaching the end of the stream."""
        after, self._after = self._after, None
        self._playing = self._paused = False
        if after:
            after(error)

    def stop(self):
        if self._playing:
            self.finish()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    async def disconnect(self, force=False):
        self.connected = False


class FakeMessage:
    _ids = itertools.count(1)

    def __init__(self, channel, embed):
        self.id = next(self._ids)
        self.channel = channel
        self.embed = embed

    async def edit(self, embed=None, view=None, **_):
        self.embed = embed

    async def delete(self):
        pass


class FakeText:
    def __init__(self):
        self.sent: list = []
        self.last_message_id = None

    async def send(self, content=None, *, embed=None, view=None, delete_after=None, allowed_mentions=None):
        msg = FakeMessage(self, embed)
        self.sent.append(content or (embed.description if embed else None) or (embed.title if embed else ""))
        self.last_message_id = msg.id
        return msg


class FakeResolver:
    """Like the real resolver: a track that already has a stream link isn't looked up again."""

    def __init__(self):
        self.fail: set[str] = set()
        self.prepared: list[str] = []  # one entry per actual lookup
        self.available_in = 0.0  # seconds until the stream link "works"
        self.delay = 0.0  # how long a lookup takes

    async def prepare(self, track):
        if track.stream_url and track.title not in self.fail:
            return
        self.prepared.append(track.title)
        if self.delay:
            await asyncio.sleep(self.delay)
        if track.title in self.fail:
            raise TrackUnavailableError("That video is private.")
        track.stream_url = "https://stream.example/" + track.title
        track.stream_available_at = time.time() + self.available_in if self.available_in else 0.0


class FakeRecommender:
    def __init__(self):
        self.next = None
        self.calls = 0

    async def recommend(self, *a, **k):
        self.calls += 1
        t, self.next = self.next, None
        return t


class FakeGuild:
    id = 77
    me = None
    voice_client = None


class FakeBot:
    def __init__(self, db, **cfg_overrides):
        self.config = dataclasses.replace(parse_config({"owner_id": 123456789012345678}), **cfg_overrides)
        self.db = db
        self.resolver = FakeResolver()
        self.recommender = FakeRecommender()
        self.players = PlayerManager(self)
        self.loop = asyncio.get_running_loop()


async def until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(GuildPlayer, "_make_source", lambda self, t: FakeSource(t, self.volume / 100))
    db = Database(tmp_path / "p.db")
    await db.connect()
    bot = FakeBot(db)
    player = GuildPlayer(bot, FakeGuild(), GuildSettings(guild_id=77))
    bot.players._players[77] = player
    player.voice = FakeVoice(FakeChannel(listeners=2))
    player.text_channel = FakeText()
    player.start()
    yield bot, player, player.voice
    await player.destroy("test over", announce=False)
    await db.close()


async def outcomes(db):
    async with db.conn.execute("SELECT track_key, outcome FROM history ORDER BY id") as cur:
        return [tuple(r) for r in await cur.fetchall()]


def short(n, **kw):
    # Short tracks (<= 15s) skip the "died early, retry" heuristic.
    return make_track(n, duration=10, **kw)


async def test_plays_in_order_and_records_history(setup):
    bot, player, voice = setup
    player.enqueue([short(1), short(2)])
    await until(lambda: voice.played == ["Song 1"])
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    voice.finish()
    await until(lambda: player.queue.current is None)
    await asyncio.sleep(0.05)
    assert await outcomes(bot.db) == [("youtube:vid1", "completed"), ("youtube:vid2", "completed")]
    assert any("Queue finished" in (m or "") or "queue is empty" in (m or "") for m in player.text_channel.sent)


async def test_skip_and_stop(setup):
    bot, player, voice = setup
    player.enqueue([short(1), short(2), short(3)])
    await until(lambda: voice.played == ["Song 1"])
    assert player.skip()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    player.stop()
    await until(lambda: player.queue.current is None)
    await asyncio.sleep(0.05)
    assert voice.played == ["Song 1", "Song 2"]  # Song 3 was cleared by stop
    assert [o for _, o in await outcomes(bot.db)] == ["skipped", "stopped"]


async def test_song_loop_repeats_until_skipped(setup):
    _, player, voice = setup
    player.queue.loop = LoopMode.SONG
    player.enqueue([short(1), short(2)])
    await until(lambda: len(voice.played) == 1)
    voice.finish()
    await until(lambda: len(voice.played) == 2)
    voice.finish()
    await until(lambda: len(voice.played) == 3)
    assert voice.played == ["Song 1"] * 3
    player.skip()
    await until(lambda: len(voice.played) == 4)
    assert voice.played[-1] == "Song 2"


async def test_failed_track_is_skipped_with_message(setup):
    bot, player, voice = setup
    bot.resolver.fail.add("Song 1")
    player.enqueue([short(1), short(2)])
    await until(lambda: voice.played == ["Song 2"])
    assert any("Skipped" in (m or "") and "private" in (m or "") for m in player.text_channel.sent)


async def test_failed_track_not_requeued_in_queue_loop(setup):
    bot, player, voice = setup
    bot.resolver.fail.add("Song 1")
    player.queue.loop = LoopMode.QUEUE
    player.enqueue([short(1), short(2)])
    await until(lambda: voice.played == ["Song 2"])
    voice.finish()
    await until(lambda: len(voice.played) == 2)
    assert voice.played == ["Song 2", "Song 2"]


async def test_stream_that_dies_immediately_is_retried_once(setup):
    bot, player, voice = setup
    player.enqueue([make_track(1, duration=200), short(2)])
    await until(lambda: len(voice.played) == 1)
    voice.finish()  # ends after ~0s of a 200s song: refresh stream and retry
    await until(lambda: len(voice.played) == 2)
    assert voice.played == ["Song 1", "Song 1"]
    voice.finish()  # dies again: give up and move on
    await until(lambda: len(voice.played) == 3)
    assert voice.played[-1] == "Song 2"
    assert bot.resolver.prepared.count("Song 1") == 2


async def test_waits_until_stream_link_is_available(setup):
    bot, player, voice = setup
    bot.resolver.available_in = 0.3
    player.enqueue([short(1)])
    await asyncio.sleep(0.15)
    assert voice.played == []  # still inside YouTube's "not yet" window
    await until(lambda: voice.played == ["Song 1"])


async def test_stop_during_availability_wait_does_not_play(setup):
    bot, player, voice = setup
    bot.resolver.available_in = 0.3
    player.enqueue([short(1), short(2)])
    await until(lambda: bot.resolver.prepared == ["Song 1"])
    player.stop()
    await asyncio.sleep(0.6)
    assert voice.played == []


@pytest.fixture
def fast_prefetch(monkeypatch):
    monkeypatch.setattr(player_module, "PREFETCH_DELAY", 0.01)


async def test_song_queued_during_playback_is_ready_before_its_turn(setup, fast_prefetch):
    bot, player, voice = setup
    player.enqueue([short(1)])
    await until(lambda: voice.played == ["Song 1"])
    player.enqueue([short(2)])
    await until(lambda: "Song 2" in bot.resolver.prepared)
    assert voice.played == ["Song 1"]  # got ready while Song 1 was still playing
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    assert bot.resolver.prepared.count("Song 2") == 1  # not looked up again at its turn


async def test_moving_a_song_to_the_front_gets_it_ready(setup, fast_prefetch):
    bot, player, voice = setup
    player.enqueue([short(1), short(2), short(3)])
    await until(lambda: voice.played == ["Song 1"] and "Song 2" in bot.resolver.prepared)
    player.queue.move(2, 1)  # Song 3 is next now
    await until(lambda: "Song 3" in bot.resolver.prepared)


async def test_song_end_waits_for_lookup_already_in_progress(setup, fast_prefetch):
    bot, player, voice = setup
    player.enqueue([short(1)])
    await until(lambda: voice.played == ["Song 1"])
    bot.resolver.delay = 0.3
    player.enqueue([short(2)])
    await until(lambda: "Song 2" in bot.resolver.prepared)  # prefetch lookup has started
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    assert bot.resolver.prepared.count("Song 2") == 1


async def test_looping_song_is_not_looked_up_again(setup):
    bot, player, voice = setup
    player.queue.loop = LoopMode.SONG
    player.enqueue([short(1)])
    await until(lambda: len(voice.played) == 1)
    voice.finish()
    await until(lambda: len(voice.played) == 2)
    assert bot.resolver.prepared == ["Song 1"]


async def test_autoplay_pick_is_made_and_readied_before_song_ends(setup, fast_prefetch):
    bot, player, voice = setup
    player.autoplay = True
    rec = short("auto")
    rec.via = "autoplay"
    bot.recommender.next = rec
    player.enqueue([short(1)])  # 10s song: inside the lookahead window, so it picks right away
    await until(lambda: "Song auto" in bot.resolver.prepared)
    assert voice.played == ["Song 1"]
    assert bot.recommender.calls == 1
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song auto"])


async def test_autoplay_early_pick_is_dropped_if_user_queues_a_song(setup, fast_prefetch):
    bot, player, voice = setup
    player.autoplay = True
    bot.recommender.next = short("auto")
    player.enqueue([short(1)])
    await until(lambda: player._autoplay_next is not None)
    player.enqueue([short(2)])
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    assert player._autoplay_next is None  # a new song started, so the old pick was dropped


@pytest.fixture
def fast_save(monkeypatch):
    monkeypatch.setattr(player_module, "SAVE_DELAY", 0.01)


async def saved(db):
    import json

    async with db.conn.execute("SELECT data FROM saved_sessions WHERE guild_id = 77") as cur:
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None


def saved_titles(data):
    current = data["current"]["title"] if data["current"] else None
    return current, [t["title"] for t in data["upcoming"]]


async def test_queue_is_saved_and_songs_drop_off_as_they_play(setup, fast_save):
    bot, player, voice = setup
    player.enqueue([short(1), short(2)])
    await until(lambda: voice.played == ["Song 1"])
    await asyncio.sleep(0.1)
    assert saved_titles(await saved(bot.db)) == ("Song 1", ["Song 2"])
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song 2"])
    await asyncio.sleep(0.1)
    assert saved_titles(await saved(bot.db)) == ("Song 2", [])
    voice.finish()
    await until(lambda: player.queue.current is None)
    await asyncio.sleep(0.1)
    assert await saved(bot.db) is None  # nothing left to play: forgotten


async def test_leaving_forgets_the_queue_but_a_restart_keeps_it(setup, fast_save):
    bot, player, voice = setup
    player.enqueue([short(1), short(2)])
    await until(lambda: voice.played == ["Song 1"])
    await player.save_session()
    await player.destroy("bye", keep_saved=True)  # what a restart does
    assert saved_titles(await saved(bot.db)) == ("Song 1", ["Song 2"])
    await bot.db.save_session(77, 1, None, {"current": None, "upcoming": []})
    other = GuildPlayer(bot, FakeGuild(), player.settings)
    await other.destroy("everyone left")  # an ordinary leave
    assert await saved(bot.db) is None


async def test_restored_queue_resumes_where_it_left_off(setup):
    bot, player, voice = setup
    snapshot = {
        "current": make_track(1, duration=300).to_dict(),
        "position": 42.0,
        "upcoming": [short(2).to_dict()],
        "loop": "queue",
        "volume": 30,
        "autoplay": False,
        "paused": False,
    }
    current, more = player.load_snapshot(snapshot)
    assert (current.title, more) == ("Song 1", 1)
    await until(lambda: voice.played == ["Song 1"])
    assert player._start_offset == 42.0  # FFmpeg is told to seek here
    assert 42.0 <= player.position() < 44
    assert player.volume == 30 and player.queue.loop is LoopMode.QUEUE
    voice.finish()  # ends ~0s after resuming a 300s song: judged on time played, so retried
    await until(lambda: len(voice.played) == 2)
    assert voice.played[-1] == "Song 1"


async def test_restored_paused_queue_stays_paused(setup):
    _, player, voice = setup
    player.load_snapshot({"current": short(1).to_dict(), "position": 3.0, "upcoming": [], "paused": True})
    await until(lambda: voice.played == ["Song 1"])
    await until(lambda: voice.is_paused())


async def test_too_many_failures_clears_queue(setup):
    bot, player, voice = setup
    tracks = [short(i) for i in range(8)]
    bot.resolver.fail |= {t.title for t in tracks[:5]}
    player.enqueue(tracks)
    await until(lambda: player.queue.current is None and player.queue.is_empty)
    assert voice.played == []
    assert any("in a row failed" in (m or "") for m in player.text_channel.sent)


async def test_volume_applies_to_live_source(setup, monkeypatch):
    _, player, voice = setup
    import discord

    monkeypatch.setattr(discord, "PCMVolumeTransformer", FakeSource)
    player.enqueue([short(1)])
    await until(lambda: voice.source is not None)
    player.set_volume(80)
    assert voice.source.volume == pytest.approx(0.8)
    player.set_volume(500)
    assert player.volume == 100


async def test_autoplay_picks_when_queue_empty(setup):
    bot, player, voice = setup
    player.autoplay = True
    rec = short("auto")
    rec.via = "autoplay"
    bot.recommender.next = rec
    player.enqueue([short(1)])
    await until(lambda: voice.played == ["Song 1"])
    voice.finish()
    await until(lambda: voice.played == ["Song 1", "Song auto"])


async def test_autoplay_now_starts_playing_when_idle(setup):
    bot, player, voice = setup
    player.autoplay = True
    rec = short("auto")
    rec.via = "autoplay"
    bot.recommender.next = rec
    assert player.is_idle
    assert await player.autoplay_now() is rec
    await until(lambda: voice.played == ["Song auto"])


async def test_autoplay_now_reports_nothing_to_pick(setup):
    bot, player, voice = setup
    player.autoplay = True
    assert await player.autoplay_now() is None
    await asyncio.sleep(0.05)
    assert voice.played == []
    assert player.is_idle


async def test_autoplay_now_does_not_queue_if_something_started(setup):
    bot, player, voice = setup
    player.enqueue([short(1)])
    await until(lambda: voice.played == ["Song 1"])
    bot.recommender.next = short("auto")
    await player.autoplay_now()
    assert player.queue.is_empty


async def test_destroy_disconnects_and_unregisters(setup):
    bot, player, voice = setup
    player.enqueue([short(1)])
    await until(lambda: voice.played)
    await player.destroy("bye")
    assert not voice.connected
    assert bot.players.get(77) is None
    assert player._task.done()
    assert [o for _, o in await outcomes(bot.db)] == ["stopped"]


async def test_idle_timeout_leaves(setup):
    bot, player, voice = setup
    player.settings.inactivity_timeout = 0.1  # type: ignore[assignment]
    player.enqueue([short(1)])
    await until(lambda: voice.played)
    voice.finish()
    await until(lambda: player.destroyed, timeout=3)
    assert not voice.connected


async def test_alone_pauses_then_leaves(setup):
    bot, player, voice = setup
    bot.config = dataclasses.replace(bot.config, alone_timeout_seconds=0.1)
    player.enqueue([short(1)])
    await until(lambda: voice.played)
    voice.channel.members = [m for m in voice.channel.members if m.bot]
    player.check_alone()
    assert voice.is_paused()
    await until(lambda: player.destroyed, timeout=3)


async def test_listener_returning_resumes(setup):
    bot, player, voice = setup
    bot.config = dataclasses.replace(bot.config, alone_timeout_seconds=5)
    player.enqueue([short(1)])
    await until(lambda: voice.played)
    humans = [m for m in voice.channel.members if not m.bot]
    voice.channel.members = [m for m in voice.channel.members if m.bot]
    player.check_alone()
    assert voice.is_paused()
    voice.channel.members += humans
    player.check_alone()
    assert voice.is_playing() and not player.destroyed


async def test_repeated_failures_pause_autoplay(setup):
    bot, player, voice = setup
    player.autoplay = True
    tracks = [short(i) for i in range(5)]
    bot.resolver.fail |= {t.title for t in tracks}
    player.enqueue(tracks)
    await until(lambda: not player.autoplay)
    assert any("Autoplay is paused" in (m or "") for m in player.text_channel.sent)
