from bot.models import Platform, Track
from tests.conftest import make_track


def test_track_dict_roundtrip_leaves_out_stream_link():
    t = make_track(1, reasons=["You liked it"], via="autoplay", origin_platform=Platform.SPOTIFY)
    t.stream_url, t.stream_headers, t.stream_expires_at = "https://stream", {"A": "b"}, 9e9
    data = t.to_dict()
    assert "stream_url" not in data and "stream_headers" not in data
    back = Track.from_dict(data)
    assert back.title == t.title and back.platform is Platform.YOUTUBE
    assert back.origin_platform is Platform.SPOTIFY
    assert back.reasons == ["You liked it"] and back.via == "autoplay"
    assert back.requester_id == t.requester_id and back.duration == t.duration
    assert back.stream_url is None


def test_track_from_dict_tolerates_unknown_and_bad_values():
    data = make_track(1).to_dict() | {"added_in_a_future_version": 1, "platform": "nope"}
    back = Track.from_dict(data)
    assert back.platform is Platform.YOUTUBE
