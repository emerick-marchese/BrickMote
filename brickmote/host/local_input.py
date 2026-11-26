"""Priority to the person sitting at the computer.

As soon as the local mouse moves, remote input is ignored until it has been
still for ``HOLD`` seconds (like AnyDesk). Releases of keys and buttons still
go through, so nothing stays pressed.

Detection needs no system hook (which would make Windows wait for Python on
every local mouse event): the real cursor position, already read about 60
times per second to share pointers, is compared with the positions viewers
asked for. When the cursor is somewhere no viewer put it, the local mouse
moved it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

HOLD = 1.5  # s of local stillness before viewers get control back
OURS_WINDOW = 1.0  # s during which a position set by a viewer is recognised
TOLERANCE = 2  # px


class LocalActivity:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.last = 0.0
        self._targets: deque = deque(maxlen=256)  # (time, x, y) set by viewers
        self._lock = threading.Lock()
        self._previous: Optional[tuple[int, int]] = None

    def active(self) -> bool:
        return self.enabled and time.monotonic() - self.last < HOLD

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.last = 0.0

    def injected_at(self, x: int, y: int) -> None:
        """A viewer is moving the cursor to (x, y) (absolute desktop pixels)."""
        with self._lock:
            self._targets.append((time.monotonic(), x, y))

    def _ours(self, x: int, y: int) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(
                now - t < OURS_WINDOW and abs(x - tx) <= TOLERANCE and abs(y - ty) <= TOLERANCE
                for t, tx, ty in self._targets
            )

    def observe(self, position: Optional[tuple[int, int]]) -> None:
        """The real cursor position, sampled regularly during sessions."""
        if position is None:
            return
        previous, self._previous = self._previous, position
        if previous is None or position == previous:
            return
        if not self._ours(*position):
            self.last = time.monotonic()  # moved, and not by a viewer

    def reset(self) -> None:
        """No session any more: forget the last sample."""
        self._previous = None
