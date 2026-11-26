"""Colored pointers of the people in a session, drawn smoothly.

Positions arrive about 60 times per second over the network, never at a
perfectly regular pace. Each pointer glides at constant speed from where it is
drawn to its latest position, over the time that separated the last two
updates: the motion stays fluid without adding visible delay.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from .glide import MAX_GLIDE, MIN_GLIDE, Glide, PointerSet  # noqa: F401 (re-exported)

PALETTE = ("#2F80ED", "#27AE60", "#F2994A", "#9B51E0", "#E84393", "#00B8A9", "#F2C94C", "#EB5757")

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
    if name:
        p.setFont(font)
        fm = QFontMetricsF(font)
        box = QRectF(x + LABEL_DX, y + LABEL_DY, fm.horizontalAdvance(name) + 12, fm.height() + 6)
        if color is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            text = _text_color(color)
        else:  # the computer's own cursor: dark label, white outline
            p.setPen(QPen(QColor("white"), 1))
            p.setBrush(QColor(17, 19, 23, 235))
            text = QColor("white")
        p.drawRect(box)  # square corners, like the rest of the app
        p.setPen(text)
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
