"""The person sitting at the computer has priority over remote viewers."""

import asyncio

import brickmote.host.local_input as li
from brickmote.host.local_input import LocalActivity

from .test_core import fake_screen, make_core  # noqa: F401 (fake_screen: autouse fixture)
from .test_session import FakeInjector, Recorder


def test_only_moves_nobody_asked_for_are_local(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(li.time, "monotonic", lambda: clock[0])
    act = LocalActivity()
    act.observe((10, 10))  # first sample: nothing to compare with
    assert not act.active()
    act.injected_at(500, 300)
    act.observe((501, 299))  # where a viewer put it
    act.observe((501, 299))  # still
    assert not act.active()
    act.observe((800, 600))  # nobody asked for that: the real mouse
    assert act.active()
    clock[0] += li.HOLD + 0.01
    assert not act.active(), "control comes back once the local mouse is still"
    clock[0] += li.OURS_WINDOW
    act.observe((501, 299))  # an old viewer position is not an excuse forever
    assert act.active()


def test_disabled_never_blocks():
    act = LocalActivity(enabled=False)
    act.observe((1, 1))
    act.observe((2, 2))
    assert not act.active()


class Reader:
    """Stands for the real cursor of the host computer."""

    pos = (0, 0)

    def position(self):
        return Reader.pos


async def test_remote_input_waits_while_the_local_mouse_moves(tmp_path):
    host, _ = make_core(tmp_path, "host")
    viewer, _ = make_core(tmp_path, "viewer")
    host.cursor_hub._reader_factory = Reader
    await host.start()
    await viewer.start()
    events = Recorder()
    session = await viewer.connect(f"127.0.0.1:{host.direct_port}", None, events)
    await events.wait_for("info")
    log = FakeInjector.log
    log.clear()

    for k in range(5):  # someone at the computer moves the mouse
        Reader.pos = (3 + k, 3)
        await asyncio.sleep(0.03)
    while True:
        item = await events.wait_for("cursors")
        if item[3]:
            break  # the viewer is told
    await session.mouse("move", 30000, 30000)
    await session.key(True, key="enter")
    await session.key(False, key="enter")
    await session.mouse("up", 30000, 30000, "left")
    await asyncio.sleep(0.3)
    assert ("move", 30000, 30000) not in log
    assert ("key", True, "enter", None) not in log
    assert ("key", False, "enter", None) in log, "releases still go through"
    assert ("button", "left", False) in log

    host.local_activity.last = 0  # the local mouse has been still long enough
    while True:
        item = await events.wait_for("cursors")
        if not item[3]:
            break
    await session.mouse("move", 1000, 2000)
    for _ in range(50):
        if ("move", 1000, 2000) in log:
            break
        await asyncio.sleep(0.02)
    assert ("move", 1000, 2000) in log, "control is back"

    # the viewer's own moves never count as local activity
    from brickmote.host.cursors import to_abs

    mon = next(iter(host.host_sessions)).current_monitor()
    for k in range(10):
        x, y = 5000 + k * 300, 9000
        await session.mouse("move", x, y)
        await asyncio.sleep(0.03)
        Reader.pos = to_abs(mon, x, y)  # the cursor follows the viewer
    await asyncio.sleep(0.1)
    assert not host.local_activity.active()
    await session.close()
    await viewer.stop()
    await host.stop()
