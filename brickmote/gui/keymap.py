"""Translate Qt key events into BrickMote wire key events."""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt

_MAC = sys.platform == "darwin"

_SPECIAL = {
    Qt.Key_Return: "enter",
    Qt.Key_Enter: "enter",
    Qt.Key_Escape: "esc",
    Qt.Key_Tab: "tab",
    Qt.Key_Backtab: "tab",
    Qt.Key_Backspace: "backspace",
    Qt.Key_Delete: "delete",
    Qt.Key_Insert: "insert",
    Qt.Key_Home: "home",
    Qt.Key_End: "end",
    Qt.Key_PageUp: "page_up",
    Qt.Key_PageDown: "page_down",
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
    Qt.Key_Space: "space",
    Qt.Key_Shift: "shift",
    # On macOS Qt maps the Command key to Key_Control and Control to Key_Meta.
    Qt.Key_Control: "cmd" if _MAC else "ctrl",
    Qt.Key_Meta: "ctrl" if _MAC else "cmd",
    Qt.Key_Super_L: "cmd",
    Qt.Key_Super_R: "cmd_r",
    Qt.Key_Alt: "alt",
    Qt.Key_AltGr: "alt_gr",
    Qt.Key_CapsLock: "caps_lock",
    Qt.Key_NumLock: "num_lock",
    Qt.Key_ScrollLock: "scroll_lock",
    Qt.Key_Print: "print_screen",
    Qt.Key_Pause: "pause",
    Qt.Key_Menu: "menu",
    Qt.Key_MediaTogglePlayPause: "media_play_pause",
    Qt.Key_MediaPlay: "media_play_pause",
    Qt.Key_VolumeMute: "media_volume_mute",
    Qt.Key_VolumeUp: "media_volume_up",
    Qt.Key_VolumeDown: "media_volume_down",
    Qt.Key_MediaNext: "media_next",
    Qt.Key_MediaPrevious: "media_previous",
}
for _i in range(1, 21):
    _SPECIAL[getattr(Qt, f"Key_F{_i}")] = f"f{_i}"

_SHORTCUT_MODS = Qt.ControlModifier | Qt.MetaModifier


def translate(key: int, text: str, modifiers) -> Optional[tuple[Optional[str], Optional[str]]]:
    """Return (special_key_name, char) or None if the key cannot be sent."""
    special = _SPECIAL.get(key)
    if special:
        return special, None
    shortcut = bool(modifiers & _SHORTCUT_MODS) or (
        bool(modifiers & Qt.AltModifier) and not (modifiers & Qt.GroupSwitchModifier)
    )
    if Qt.Key_A <= key <= Qt.Key_Z and (shortcut or not text or not text.isprintable()):
        return None, chr(key - Qt.Key_A + ord("a"))
    if Qt.Key_0 <= key <= Qt.Key_9 and shortcut:
        return None, chr(key - Qt.Key_0 + ord("0"))
    if text and len(text) == 1 and text.isprintable():
        return None, text
    return None
