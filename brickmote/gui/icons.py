"""Line icons (24x24, stroke based) drawn for BrickMote, rendered from SVG."""

from __future__ import annotations

import functools

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS = {
    "home": '<path d="M3 11 12 4l9 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5h4v5"/>',
    "devices": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.6 2.7-6 6-6s6 2.4 6 6"/>'
    '<circle cx="17" cy="9" r="2.4"/><path d="M16 14.3c2.9.3 5 2.4 5 5.7"/>',
    "shield": '<path d="M12 3 20 6v6c0 4.6-3.4 8-8 9-4.6-1-8-4.4-8-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    "settings": '<path d="M4 6h3M11 6h9M4 12h9M17 12h3M4 18h1M9 18h11"/>'
    '<circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="7" cy="18" r="2"/>',
    "log": '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M9 11h7M9 15h7M9 19h4"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    "wifi": '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.8 16a4.8 4.8 0 0 1 6.4 0"/>'
    '<circle cx="12" cy="19.3" r=".9" fill="currentColor"/>',
    "link": '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1"/>'
    '<path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
    "server": '<rect x="3.5" y="4" width="17" height="7" rx="2"/><rect x="3.5" y="13" width="17" height="7" rx="2"/>'
    '<path d="M7.5 7.5h.01M7.5 16.5h.01"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "power": '<path d="M12 3v8"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M12 18v-6M9 15l3-3 3 3"/>',
    "chat": '<path d="M20.5 12a8.5 8.5 0 0 1-12.3 7.6L3.5 21l1.4-4.6A8.5 8.5 0 1 1 20.5 12z"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7.5 14h9"/>',
    "fullscreen": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4.5H15"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "clipboard": '<rect x="5.5" y="4.5" width="13" height="16.5" rx="2"/><rect x="9" y="2.5" width="6" height="4" rx="1"/>'
    '<path d="M9 12h6M9 16h4"/>',
    "fit": '<path d="M15 3.5h5.5V9M9 20.5H3.5V15M20.5 3.5 14 10M3.5 20.5 10 14"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "alert": '<path d="M12 3.5 21.5 20h-19z"/><path d="M12 10v4M12 17h.01"/>',
    "more": '<path d="M6 12h.01M12 12h.01M18 12h.01"/>',
    "plug": '<path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0zM12 16.5V21"/>',
}

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
    'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" color="{color}">{body}</svg>'
)


def _dpr() -> float:
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app else None
    return max(2.0, screen.devicePixelRatio() if screen else 1.0)


@functools.lru_cache(maxsize=512)
def pixmap(name: str, color: str = "#1E2228", size: int = 20, width: float = 1.8) -> QPixmap:
    svg = _SVG.format(color=color, width=width, body=_PATHS[name])
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    dpr = _dpr()
    pix = QPixmap(int(size * dpr), int(size * dpr))
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, pix.width(), pix.height()))
    p.end()
    pix.setDevicePixelRatio(dpr)
    return pix


def icon(name: str, color: str = "#1E2228", size: int = 20, active: str | None = None) -> QIcon:
    """QIcon with an optional colour for the checked ("On") state."""
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    ic.addPixmap(pixmap(name, active or color, size), QIcon.Normal, QIcon.On)
    ic.addPixmap(pixmap(name, "#9CA3AF", size), QIcon.Disabled, QIcon.Off)
    return ic


NAMES = tuple(_PATHS)
