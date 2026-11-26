"""Colored pointers of the people in a session, drawn smoothly.

Positions arrive about 60 times per second over the network, never at a
perfectly regular pace. Each pointer glides at constant speed from where it is
drawn to its latest position, over the time that separated the last two
updates: the motion stays fluid without adding visible delay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Hashable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

PALETTE = ("#2F80ED", "#27AE60", "#F2994A", "#9B51E0", "#E84393", "#00B8A9", "#F2C94C", "#EB5757")
MIN_GLIDE = 0.012
MAX_GLIDE = 0.12  # never trail more than this behind the real position

_ARROW = [(0, 0), (0, 17), (4.6, 13.2), (7.6, 20.2), (10.4, 19), (7.4, 12.2), (12.6, 12.2)]
LABEL_DX, LABEL_DY = 13.0, 17.0


def color_of(index: int) -> QColor:
    return QColor(PALETTE[index % len(PALETTE)])


def _text_color(color: QColor) -> QColor:
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor("#111317") if luminance > 160 else QColor("white")


def label_font(base: QFont) -> QFont:
    f = QFont(base)
    f.setPointSizeF(8.5)
    f.setBold(True)
    return f


def arrow_path(x: float, y: float, scale: float = 1.0) -> QPainterPath:
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x + px * scale, y + py * scale) for px, py in _ARROW]))
    path.closeSubpath()
    return path


def bounds(x: float, y: float, name: Optional[str], font: QFont) -> QRectF:
    """Area covered by a pointer drawn at (x, y): what must be repainted."""
    r = QRectF(x - 3, y - 3, 20, 28)
    if name:
        fm = QFontMetricsF(font)
        r = r.united(QRectF(x + LABEL_DX - 2, y + LABEL_DY - 2, fm.horizontalAdvance(name) + 16, fm.height() + 10))
    return r


def draw_pointer(p: QPainter, x: float, y: float, color: Optional[QColor], name: Optional[str], font: QFont) -> None:
    """``color`` None draws the computer's own cursor (white arrow, no label)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    fill = color if color is not None else QColor("white")
    outline = QColor("white") if color is not None else QColor("#111317")
    p.setPen(QPen(outline, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(fill)
    p.drawPath(arrow_path(x, y))
    if name and color is not None:
        p.setFont(font)
        fm = QFontMetricsF(font)
        box = QRectF(x + LABEL_DX, y + LABEL_DY, fm.horizontalAdvance(name) + 12, fm.height() + 6)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRect(box)  # square corners, like the rest of the app
        p.setPen(_text_color(color))
        p.drawText(box, Qt.AlignCenter, name)
    p.restore()


def pointer_cursor(index: int, ratio: float = 2.0) -> QCursor:
    """Our own mouse cursor, in the color the others see us with."""
    size = 24
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    draw_pointer(p, 1.5, 1.5, color_of(index), None, QFont())
    p.end()
    return QCursor(pm, 2, 2)


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
