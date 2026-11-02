from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.models import Platform, Track  # noqa: E402


def make_track(n: int | str = 1, **kw) -> Track:
    defaults = dict(
        title=f"Song {n}",
        artist=kw.pop("artist", "Artist"),
        url=f"https://www.youtube.com/watch?v=vid{n}",
        platform=Platform.YOUTUBE,
        source_id=f"vid{n}",
        duration=200,
        requester_id=111,
        requester_name="tester",
    )
    defaults.update(kw)
    return Track(**defaults)


@pytest.fixture
def track_factory():
    return make_track
