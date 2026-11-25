"""Keyboard / mouse injection on the host (pynput).

Only a fixed allow-list of keys is accepted from the network, coordinates are
clamped to the shared monitor, and every key still held down is released when
a session ends (no stuck Ctrl/Alt after a disconnect).
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

SPECIAL_KEYS = {
    "enter", "esc", "tab", "backspace", "delete", "insert", "home", "end",
    "page_up", "page_down", "left", "right", "up", "down", "space",
    "shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r", "alt_gr", "cmd", "cmd_r",
    "caps_lock", "num_lock", "scroll_lock", "print_screen", "pause", "menu",
    "media_play_pause", "media_volume_mute", "media_volume_up", "media_volume_down",
    "media_next", "media_previous",
} | {f"f{i}" for i in range(1, 21)}

BUTTONS = {"left", "right", "middle"}
COORD_MAX = 65535


class InputInjector:
    def __init__(self) -> None:
        self._mouse = None
        self._keyboard = None
        self._pynput = None
        self._monitor = {"left": 0, "top": 0, "width": 1, "height": 1}
        self._pressed_keys: set = set()
        self._pressed_buttons: set = set()
        self.available = True

    def _ensure(self) -> bool:
        if self._mouse is not None:
            return True
        if not self.available:
            return False
        try:
            from pynput import keyboard, mouse

            self._pynput = (keyboard, mouse)
            self._mouse = mouse.Controller()
            self._keyboard = keyboard.Controller()
            return True
        except Exception as exc:  # noqa: BLE001 - no display / Wayland / permissions
            log.warning("input injection unavailable: %s", exc)
            self.available = False
            return False

    def set_monitor(self, monitor: dict) -> None:
        self._monitor = dict(monitor)

    def _abs(self, nx: object, ny: object) -> Optional[tuple[int, int]]:
        if not isinstance(nx, int) or not isinstance(ny, int):
            return None
        nx = min(max(nx, 0), COORD_MAX)
        ny = min(max(ny, 0), COORD_MAX)
        m = self._monitor
        x = m["left"] + round(nx * (m["width"] - 1) / COORD_MAX)
        y = m["top"] + round(ny * (m["height"] - 1) / COORD_MAX)
        return x, y

    def cursor(self) -> Optional[tuple[float, float]]:
        """Cursor position relative to the monitor, as fractions (0..1)."""
        if not self._ensure():
            return None
        try:
            x, y = self._mouse.position
        except Exception:  # noqa: BLE001
            return None
        m = self._monitor
        fx = (x - m["left"]) / max(1, m["width"])
        fy = (y - m["top"]) / max(1, m["height"])
        if 0 <= fx < 1 and 0 <= fy < 1:
            return fx, fy
        return None

    def mouse_move(self, nx, ny) -> None:
        pos = self._abs(nx, ny)
        if pos and self._ensure():
            self._mouse.position = pos

    def mouse_button(self, button: object, down: bool, nx=None, ny=None) -> None:
        if button not in BUTTONS or not self._ensure():
            return
        if nx is not None:
            self.mouse_move(nx, ny)
        btn = getattr(self._pynput[1].Button, button)
        if down:
            self._mouse.press(btn)
            self._pressed_buttons.add(btn)
        else:
            self._mouse.release(btn)
            self._pressed_buttons.discard(btn)

    def scroll(self, dx: object, dy: object) -> None:
        if not isinstance(dx, int) or not isinstance(dy, int) or not self._ensure():
            return
        self._mouse.scroll(max(-20, min(20, dx)), max(-20, min(20, dy)))

    def _resolve_key(self, key: object, char: object):
        keyboard = self._pynput[0]
        if isinstance(key, str) and key in SPECIAL_KEYS:
            return getattr(keyboard.Key, key, None)
        if isinstance(char, str) and len(char) == 1 and char.isprintable():
            return keyboard.KeyCode.from_char(char)
        return None

    def key(self, down: bool, key: object = None, char: object = None) -> None:
        if not self._ensure():
            return
        k = self._resolve_key(key, char)
        if k is None:
            return
        try:
            if down:
                self._keyboard.press(k)
                self._pressed_keys.add(k)
            else:
                self._keyboard.release(k)
                self._pressed_keys.discard(k)
        except Exception as exc:  # noqa: BLE001 - unmappable key on this layout
            log.debug("key injection failed: %s", exc)

    def release_all(self) -> None:
        if self._mouse is None:
            return
        for k in list(self._pressed_keys):
            try:
                self._keyboard.release(k)
            except Exception:  # noqa: BLE001
                pass
        for b in list(self._pressed_buttons):
            try:
                self._mouse.release(b)
            except Exception:  # noqa: BLE001
                pass
        self._pressed_keys.clear()
        self._pressed_buttons.clear()
