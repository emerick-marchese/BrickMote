"""Smooth pointer motion, without Qt (testable anywhere).

Each pointer glides at constant speed from where it is drawn to its latest
position, over the time that separated the last two updates, capped so it
never trails far behind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Hashable, Optional

MIN_GLIDE = 0.012
MAX_GLIDE = 0.12  # never trail more than this behind the real position


@dataclass
class Glide:
    x: float
    y: float
    x0: float = 0.0
    y0: float = 0.0
    t0: float = 0.0
    duration: float = 0.0
    last: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.x0, self.y0 = self.x, self.y

    def at(self, now: float) -> tuple[float, float]:
        if self.duration <= 0 or now >= self.t0 + self.duration:
            return self.x, self.y
        k = (now - self.t0) / self.duration
        return self.x0 + (self.x - self.x0) * k, self.y0 + (self.y - self.y0) * k

    def retarget(self, x: float, y: float, now: float) -> None:
        if (x, y) == (self.x, self.y):
            return
        self.x0, self.y0 = self.at(now)
        self.x, self.y = x, y
        self.t0 = now
        self.duration = min(MAX_GLIDE, max(MIN_GLIDE, now - self.last))
        self.last = now

    def moving(self, now: float) -> bool:
        return self.duration > 0 and now < self.t0 + self.duration


@dataclass
class Pointer:
    glide: Glide
    color: Optional[int]
    name: Optional[str]


class PointerSet:
    """Pointers keyed by participant (color index) or "sys" (the computer's cursor)."""

    def __init__(self) -> None:
        self.items: dict[Hashable, Pointer] = {}

    def update(self, targets: dict, now: Optional[float] = None) -> None:
        """``targets``: key -> (x, y, color index | None, name | None)."""
        now = time.monotonic() if now is None else now
        for key in list(self.items):
            if key not in targets:
                del self.items[key]
        for key, (x, y, color, name) in targets.items():
            item = self.items.get(key)
            if item is None:
                self.items[key] = Pointer(Glide(x, y), color, name)  # appears where it is
            else:
                item.glide.retarget(x, y, now)
                item.color, item.name = color, name

    def drawn(self, now: Optional[float] = None) -> list[tuple[Hashable, float, float, Optional[int], Optional[str]]]:
        now = time.monotonic() if now is None else now
        out = []
        for key, item in self.items.items():
            x, y = item.glide.at(now)
            out.append((key, x, y, item.color, item.name))
        return out

    def animating(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return any(item.glide.moving(now) for item in self.items.values())
