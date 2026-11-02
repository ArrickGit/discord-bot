import pytest

from bot.sources.detect import Kind, Service, detect
from bot.sources.errors import UnsupportedLinkError


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=tracking", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("<https://www.youtube.com/watch?v=dQw4w9WgXcQ>", "dQw4w9WgXcQ"),
        # A video inside a playlist plays the video (use the playlist link for the whole list).
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890", "dQw4w9WgXcQ"),
    ],
)
def test_youtube_videos(url, expected):
    d = detect(url)
    assert (d.service, d.kind, d.id) == (Service.YOUTUBE, Kind.TRACK, expected)
    assert d.url == f"https://www.youtube.com/watch?v={expected}"


def test_youtube_playlist_canonicalised():
    d = detect("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI&si=x")
    assert d.kind is Kind.PLAYLIST and d.is_collection
    assert d.url == "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


def test_youtube_music_playlist():
    d = detect("https://music.youtube.com/playlist?list=OLAK5uy_abcdefghijklmnop")
    assert d.kind is Kind.PLAYLIST


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/results?search_query=hi",
        "https://youtu.be/",
    ],
)
def test_youtube_rejects(url):
    with pytest.raises(UnsupportedLinkError):
        detect(url)


def test_soundcloud_track_and_set():
    d = detect("https://soundcloud.com/forss/flickermood?utm_source=x")
    assert (d.service, d.kind, d.url) == (Service.SOUNDCLOUD, Kind.TRACK, "https://soundcloud.com/forss/flickermood")
    d = detect("https://m.soundcloud.com/forss/sets/soulhack")
    assert d.kind is Kind.PLAYLIST and d.url == "https://soundcloud.com/forss/sets/soulhack"
    d = detect("https://soundcloud.com/user/secret-track/s-AbCdE123")
    assert d.kind is Kind.TRACK


@pytest.mark.parametrize(
    "url", ["https://soundcloud.com/forss", "https://soundcloud.com/forss/likes", "https://soundcloud.com/discover/x"]
)
def test_soundcloud_rejects_profiles(url):
    with pytest.raises(UnsupportedLinkError):
        detect(url)


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc", Kind.TRACK),
        ("https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT", Kind.TRACK),
        ("https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE", Kind.ALBUM),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", Kind.PLAYLIST),
        ("spotify:track:4cOdK2wGLETKBW3PvgPWqT", Kind.TRACK),
    ],
)
def test_spotify(url, kind):
    d = detect(url)
    assert d.service is Service.SPOTIFY and d.kind is kind and len(d.id) == 22


def test_spotify_rejects_artists():
    with pytest.raises(UnsupportedLinkError):
        detect("https://open.spotify.com/artist/0gxyHStUsqpMadRV0Di1Qt")


def test_shortlinks():
    assert detect("https://spotify.link/AbCd123").kind is Kind.SHORTLINK
    assert detect("https://on.soundcloud.com/xyz").kind is Kind.SHORTLINK


def test_search_text():
    d = detect("  daft punk one more time ")
    assert d.service is Service.SEARCH and d.url == "daft punk one more time"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "https://example.com/song.mp3",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "https://user:pass@youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com:8080/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "x" * 400,
    ],
)
def test_rejects_unsafe_or_unsupported(bad):
    with pytest.raises(UnsupportedLinkError):
        detect(bad)
