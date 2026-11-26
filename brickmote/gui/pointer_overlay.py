"""Viewers' pointers shown on this computer's own screens.

A transparent, click-through window per screen draws the colored pointer and
name of each connected viewer, so the person sitting at the computer sees who
points where.

The overlay must stay invisible to the screen capture, otherwise viewers would
get it back in the video, late and doubled. Windows 10 (2004) and later can do
that (WDA_EXCLUDEFROMCAPTURE); elsewhere, or if Windows refuses, the overlay is
simply not shown.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from .pointers import PointerSet, bounds, color_of, draw_pointer, label_font

log = logging.getLogger(__name__)

WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(widget: QWidget) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return bool(user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(widget.winId())), WDA_EXCLUDEFROMCAPTURE))
    except Exception:  # noqa: BLE001
        return False


class _ScreenLayer(QWidget):
    def __init__(self, screen):
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.pointers = PointerSet()
        self._font = label_font(self.font())
        self._area = QRegion()
        self._timer = QTimer(self, interval=8, timeout=self._tick)
        self._timer.setTimerType(Qt.PreciseTimer)

    def set_targets(self, targets: dict) -> None:
        self.pointers.update(targets)
        self._tick()
        if self.pointers.animating() and not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        area = QRegion(self._area)
        for _, x, y, color, name in self.pointers.drawn():
            area += bounds(x, y, name, self._font).toAlignedRect()
        if not area.isEmpty():
            self.update(area)
        if not self.pointers.animating():
            self._timer.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.fillRect(event.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        area = QRegion()
        for _, x, y, color, name in self.pointers.drawn():
            draw_pointer(p, x, y, color_of(color), name, self._font)
            area += bounds(x, y, name, self._font).toAlignedRect()
        self._area = area
        p.end()


class PointerOverlay:
    def __init__(self) -> None:
        self.supported = sys.platform.startswith("win")
        self._layers: dict = {}  # QScreen -> _ScreenLayer

    def _layer(self, screen) -> Optional[_ScreenLayer]:
        layer = self._layers.get(screen)
        if layer is None:
            layer = _ScreenLayer(screen)
            if not exclude_from_capture(layer):
                # would be captured and sent back to the viewers: do not show it
                log.info("pointer overlay unavailable (cannot exclude it from capture)")
                layer.deleteLater()
                self.supported = False
                return None
            self._layers[screen] = layer
            screen.destroyed.connect(lambda *_: self._drop(screen))
        return layer

    def _drop(self, screen) -> None:
        layer = self._layers.pop(screen, None)
        if layer is not None:
            layer.deleteLater()

    def update(self, pointers: list) -> None:
        """``pointers``: [(color, name, x, y)] in physical desktop pixels."""
        if not self.supported:
            return
        per_screen: dict = {}
        for color, name, x, y in pointers:
            for screen in QGuiApplication.screens():
                g, ratio = screen.geometry(), screen.devicePixelRatio()
                # Qt keeps each screen's native top-left and scales its size
                phys = QRectF(g.x(), g.y(), g.width() * ratio, g.height() * ratio)
                if phys.contains(x, y):
                    local = ((x - g.x()) / ratio, (y - g.y()) / ratio, color, name)
                    per_screen.setdefault(screen, {})[color] = local
                    break
        for screen in set(self._layers) | set(per_screen):
            targets = per_screen.get(screen, {})
            layer = self._layer(screen) if targets else self._layers.get(screen)
            if layer is None:
                if not self.supported:
                    return
                continue
            layer.set_targets(targets)
            if targets and not layer.isVisible():
                layer.setGeometry(screen.geometry())
                layer.show()
            elif not targets and layer.isVisible():
                layer.hide()

    def close(self) -> None:
        for layer in self._layers.values():
            layer.close()
            layer.deleteLater()
        self._layers.clear()
