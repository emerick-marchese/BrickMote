"""Pointer sharing: coordinates, untrusted input, smooth gliding."""

import os

import pytest

from brickmote.host.cursors import CursorHub, clean_name, to_abs, to_norm
from brickmote.host.inputs import COORD_MAX
from brickmote.viewer.session import _sanitize_cursors

LEFT = {"index": 1, "left": 0, "top": 0, "width": 1920, "height": 1080}
RIGHT = {"index": 2, "left": 1920, "top": 0, "width": 2560, "height": 1440}


def test_coordinates_round_trip_between_monitors():
    x, y = to_abs(RIGHT, COORD_MAX // 2, COORD_MAX // 2)
    assert (x, y) == (1920 + 1279, 719)  # same mapping as the input injection
    assert to_norm(LEFT, x, y) is None  # not on the monitor this viewer watches
    nx, ny = to_norm(RIGHT, x, y)
    assert abs(nx - COORD_MAX // 2) <= 30 and abs(ny - COORD_MAX // 2) <= 30
    assert to_abs(LEFT, -5, 10**9) == (0, 1079)  # clamped


def test_names_are_cleaned():
    assert clean_name("Bob\x00\n le PC") == "Bob le PC"
    assert clean_name("") == "Utilisateur"
    assert len(clean_name("x" * 500)) == 32


def test_untrusted_cursor_messages_are_filtered():
    msg = {
        "sys": [10, 20],
        "p": [[1, 5, 6, "Alice"], [9, 1, 1, "bad color"], [2, -1, 1, "bad x"], [3, 1, 1, 42], "junk", [4, 1, 1, "\x1b[31mEve"]],
    }
    system, pointers = _sanitize_cursors(msg)
    assert system == (10, 20)
    assert pointers == [(1, 5, 6, "Alice"), (4, 1, 1, "[31mEve")]
    assert _sanitize_cursors({"sys": [True, 1], "p": []}) == (None, [])
    assert _sanitize_cursors({"p": "x"}) is None


class _Session:
    closed = False

    def __init__(self, monitor):
        self.monitor = monitor

    def current_monitor(self):
        return self.monitor


async def test_hub_colors_and_driver():
    hub = CursorHub()
    a, b, c = _Session(LEFT), _Session(LEFT), _Session(RIGHT)
    assert [hub.join(s, n) for s, n in ((a, "A"), (b, "B"), (c, "C"))] == [0, 1, 2]
    hub.leave(b)
    assert hub.join(_Session(LEFT), "D") == 1  # freed colors are reused
    hub.system = (100, 100)
    assert hub.view_for(a)["sys"] is not None
    hub.point(a, LEFT, 1000, 1000, drove=True)
    view = hub.view_for(c)
    assert view["sys"] is None, "while someone drives, the real cursor is their colored pointer"
    assert view["p"] == []  # A points on the left monitor, C watches the right one
    hub.point(c, RIGHT, 0, 0)
    assert hub.view_for(a)["p"] == []  # C points on the right monitor, A watches the left one
    for s in list(hub.members):
        hub.leave(s)
    await hub._task


def test_glide_is_smooth_and_bounded():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from brickmote.gui.pointers import MAX_GLIDE, PointerSet

    ps = PointerSet()
    ps.update({1: (0.0, 0.0, 1, "A")}, now=10.0)
    ps.update({1: (100.0, 0.0, 1, "A")}, now=10.016)  # 60 Hz updates
    (_, x_mid, _, _, _), = ps.drawn(now=10.024)
    assert 0 < x_mid < 100  # in between: no jump
    (_, x_end, _, _, _), = ps.drawn(now=10.016 + MAX_GLIDE)
    assert x_end == 100  # never late by more than MAX_GLIDE
    assert not ps.animating(now=10.5)
    ps.update({}, now=11)
    assert ps.drawn() == []
