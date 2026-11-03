"""Per-server queue logic. Pure Python, no Discord dependencies, fully unit-tested."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from enum import StrEnum

from .models import Track


class LoopMode(StrEnum):
    OFF = "off"
    SONG = "song"
    QUEUE = "queue"


class QueueError(Exception):
    """A user-facing queue problem (bad position, full queue...)."""


class QueueFullError(QueueError):
    pass


class MusicQueue:
    def __init__(self, max_size: int = 500, history_size: int = 50) -> None:
        if max_size < 1:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self.current: Track | None = None
        self._loop = LoopMode.OFF
        self._upcoming: list[Track] = []
        self.history: deque[Track] = deque(maxlen=history_size)
        # Called after anything that may change which song plays next (the player
        # uses it to get that song ready in the background).
        self.on_change: Callable[[], None] | None = None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    @property
    def loop(self) -> LoopMode:
        return self._loop

    @loop.setter
    def loop(self, mode: LoopMode) -> None:
        self._loop = mode
        self._changed()

    # ----- inspection -------------------------------------------------
    @property
    def upcoming(self) -> list[Track]:
        return list(self._upcoming)

    def __len__(self) -> int:
        return len(self._upcoming)

    @property
    def is_empty(self) -> bool:
        return not self._upcoming

    @property
    def space_left(self) -> int:
        return max(0, self.max_size - len(self._upcoming))

    def total_duration(self) -> int:
        return sum(t.duration or t.expected_duration or 0 for t in self._upcoming)

    def peek(self) -> Track | None:
        return self._upcoming[0] if self._upcoming else None

    # ----- mutation ---------------------------------------------------
    def add(self, track: Track, *, front: bool = False) -> int:
        """Add one track. Returns its 1-based position in the upcoming list."""
        if len(self._upcoming) >= self.max_size:
            raise QueueFullError(f"The queue is full ({self.max_size} songs).")
        if front:
            self._upcoming.insert(0, track)
            self._changed()
            return 1
        self._upcoming.append(track)
        self._changed()
        return len(self._upcoming)

    def add_many(self, tracks: list[Track], *, front: bool = False) -> tuple[int, int]:
        """Add as many tracks as fit. Returns ``(added, dropped)``."""
        accepted = tracks[: self.space_left]
        if front:
            self._upcoming[0:0] = accepted
        else:
            self._upcoming.extend(accepted)
        if accepted:
            self._changed()
        return len(accepted), len(tracks) - len(accepted)

    def _index(self, position: int) -> int:
        if not 1 <= position <= len(self._upcoming):
            if not self._upcoming:
                raise QueueError("The queue is empty.")
            raise QueueError(f"Position must be between 1 and {len(self._upcoming)}.")
        return position - 1

    def remove(self, position: int) -> Track:
        track = self._upcoming.pop(self._index(position))
        self._changed()
        return track

    def move(self, from_pos: int, to_pos: int) -> Track:
        src = self._index(from_pos)
        dst = self._index(to_pos)
        track = self._upcoming.pop(src)
        self._upcoming.insert(dst, track)
        self._changed()
        return track

    def shuffle(self, rng: random.Random | None = None) -> None:
        (rng or random).shuffle(self._upcoming)
        self._changed()

    def clear(self) -> int:
        """Remove upcoming tracks (the current song keeps playing)."""
        count = len(self._upcoming)
        self._upcoming.clear()
        self._changed()
        return count

    def stop(self) -> None:
        """Clear everything including the current track."""
        self._upcoming.clear()
        self.current = None
        self._changed()

    def drop_current(self) -> None:
        """Forget the current track without re-queueing it (used after failures)."""
        if self.current is not None:
            self.history.append(self.current)
        self.current = None

    def advance(self, *, skipped: bool = False) -> Track | None:
        """Move to the next track according to the loop mode and return it.

        * ``song`` loop repeats the current track unless it was skipped.
        * ``queue`` loop sends the finished track to the back of the queue.
        """
        finished = self.current
        if finished is not None:
            if self.loop is LoopMode.SONG and not skipped:
                # Keep its stream link: the player refreshes it only if it's about to expire.
                return finished
            self.history.append(finished)
            if self.loop is LoopMode.QUEUE:
                self._upcoming.append(
                    finished.copy_for(finished.requester_id, finished.requester_name, keep_stream=True)
                )

        self.current = self._upcoming.pop(0) if self._upcoming else None
        self._changed()
        return self.current
