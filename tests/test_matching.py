from bot.sources.errors import SourceBlockedError, TrackUnavailableError, UnsupportedLinkError, friendly_ytdlp_error
from bot.sources.matching import Candidate, best_match, normalize, score_candidate, search_query


def cand(title, channel, duration, id="x"):
    return Candidate(title=title, channel=channel, duration=duration, url=f"https://youtu.be/{id}", id=id)


def test_normalize_strips_noise():
    assert normalize("Héllo (Official Music Video) [HD]") == "hello"
    assert normalize("Song feat. Someone") == "song"
    assert normalize("Rock & Roll") == "rock and roll"


def test_topic_channel_beats_everything():
    candidates = [
        cand("One More Time (Live)", "Daft Punk", 330, "live"),
        cand("Daft Punk - One More Time (Official Video)", "Daft Punk", 322, "video"),
        cand("One More Time", "Daft Punk - Topic", 320, "topic"),
        cand("One More Time - Piano Cover", "PianoGuy", 300, "cover"),
    ]
    match, score = best_match("One More Time", ["Daft Punk"], 320, candidates)
    assert match.id == "topic"


def test_rejects_covers_and_wrong_duration():
    candidates = [
        cand("One More Time (Piano Cover)", "PianoGuy", 320, "cover"),
        cand("One More Time 1 HOUR LOOP", "Loops", 3600, "loop"),
    ]
    assert best_match("One More Time", ["Daft Punk"], 320, candidates) is None


def test_versions_allowed_when_target_is_that_version():
    c = cand("Song Name (Live at Wembley)", "Band", 300)
    assert score_candidate("Song Name - Live at Wembley", ["Band"], 300, c) > score_candidate(
        "Song Name", ["Band"], 300, c
    )


def test_remaster_in_title_is_fine():
    c = cand("Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)", "Rick Astley", 213)
    assert best_match("Never Gonna Give You Up", ["Rick Astley"], 213, [c]) is not None


def test_no_duration_still_matches_on_title_and_artist():
    c = cand("Artist X - Great Song", "Artist X", None)
    assert best_match("Great Song", ["Artist X"], None, [c]) is not None


def test_search_query():
    assert search_query("Song", ["A", "B"]) == "A - Song"
    assert search_query("Song", []) == "Song"


def test_friendly_errors():
    assert isinstance(friendly_ytdlp_error("ERROR: Private video. Sign in"), TrackUnavailableError)
    assert "age" in str(friendly_ytdlp_error("Sign in to confirm your age"))
    assert isinstance(friendly_ytdlp_error("Sign in to confirm you’re not a bot"), SourceBlockedError)
    assert "region" in str(friendly_ytdlp_error("This video is not available in your country"))
    assert "deleted" in str(friendly_ytdlp_error("ERROR: [youtube] x: This video is unavailable"))
    assert isinstance(friendly_ytdlp_error("Unsupported URL: x"), UnsupportedLinkError)
    assert isinstance(friendly_ytdlp_error("HTTP Error 429: Too Many Requests"), SourceBlockedError)
