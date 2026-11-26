import asyncio
import io
import os
import sys

import numpy as np
from PIL import Image

from brickmote.config import AuditLog, Settings
from brickmote.crypto.handshake import initiator_handshake, responder_handshake
from brickmote.crypto.identity import Identity
from brickmote.files import safe_filename
from brickmote.host.authority import HostPolicy
from brickmote.host.capture import FrameEncoder, pack_frame, unpack_frame
from brickmote.host.session import HostSession
from brickmote.session import SessionEvents
from brickmote.viewer.session import ViewerSession

from .conftest import stream_pair

W, H = 320, 200


class FakeCapture:
    def __init__(self):
        self.n = 0

    def monitors(self):
        return [{"index": 1, "left": 0, "top": 0, "width": W, "height": H}]

    def grab(self, index):
        self.n += 1
        img = np.zeros((H, W, 4), dtype=np.uint8)
        x = (self.n * 7) % (W - 20)
        img[50:70, x : x + 20, 2] = 255  # moving red square
        return W, H, img.tobytes()

    def close(self):
        pass


class FakeInjector:
    log: list = []

    def set_monitor(self, mon):
        pass

    def cursor(self):
        return (0.5, 0.25)

    def mouse_move(self, x, y):
        self.log.append(("move", x, y))

    def mouse_button(self, b, down, x=None, y=None):
        self.log.append(("button", b, down))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))

    def key(self, down, key=None, char=None):
        self.log.append(("key", down, key, char))

    def release_all(self):
        self.log.append(("release_all",))


class Recorder(SessionEvents):
    def __init__(self):
        self.events = asyncio.Queue()

    def _put(self, *item):
        self.events.put_nowait(item)

    def on_closed(self, s, reason):
        self._put("closed", reason)

    def on_chat(self, s, text):
        self._put("chat", text)

    def on_clipboard(self, s, text):
        self._put("clip", text)

    def on_permissions(self, s, perms):
        self._put("perm", perms)

    def on_file_done(self, s, tid, name, path, error, incoming):
        self._put("file_done", name, path, error, incoming)

    def on_info(self, s, info):
        self._put("info", info)

    def on_frame(self, s, fid, w, h, cursor, rects):
        self._put("frame", fid, w, h, cursor, rects)

    def on_status(self, s, status):
        self._put("status", status)

    def on_cursors(self, s, system, pointers, local=False):
        self._put("cursors", system, pointers, local)

    async def wait_for(self, kind, timeout=10):
        while True:
            item = await asyncio.wait_for(self.events.get(), timeout)
            if item[0] == kind:
                return item


async def make_sessions(tmp_path):
    settings = Settings()
    host_ident = Identity.generate()

    async def accept(peer, sas):
        return {"control": True, "clipboard": True, "files": True}

    policy = HostPolicy(host_ident, lambda: settings, AuditLog(tmp_path / "a.log"), prompt=accept)
    (vr, vw), (hr, hw) = await stream_pair()
    host_task = asyncio.create_task(responder_handshake(hr, hw, policy, "test"))
    vres = await initiator_handshake(vr, vw, Identity.generate(), "Viewer", expected_id=host_ident.device_id)
    hres = await host_task
    FakeInjector.log = []
    hrec, vrec = Recorder(), Recorder()
    host = HostSession(
        hres, hrec, tmp_path / "host_dl", fps=30, capture_factory=FakeCapture, injector_factory=FakeInjector
    )
    viewer = ViewerSession(vres, vrec, tmp_path / "viewer_dl")
    tasks = [asyncio.create_task(host.run()), asyncio.create_task(viewer.run())]
    return host, viewer, hrec, vrec, tasks


async def wait_log(pred, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if any(pred(e) for e in FakeInjector.log):
            return True
        await asyncio.sleep(0.02)
    return False


async def test_full_session(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)

    info = await vrec.wait_for("info")
    assert info[1]["monitors"] == [{"index": 1, "width": W, "height": H}]
    frame = await vrec.wait_for("frame")
    _, fid, fw, fh, cursor, rects = frame
    assert (fw, fh) == (W, H) and cursor == (160, 50)
    x, y, w, h, jpeg = rects[0]
    assert Image.open(io.BytesIO(jpeg)).size == (w, h)
    # incremental updates keep flowing (flow control with acks)
    for _ in range(3):
        await vrec.wait_for("frame")

    # remote control
    await viewer.mouse("move", 1000, 2000)
    await viewer.mouse("down", 1000, 2000, "left")
    await viewer.key(True, key="ctrl")
    await viewer.key(True, char="c")
    assert await wait_log(lambda e: e == ("move", 1000, 2000))
    assert await wait_log(lambda e: e == ("button", "left", True))
    assert await wait_log(lambda e: e == ("key", True, "ctrl", None))

    # clipboard + chat
    await viewer.send_clipboard("copié depuis le viewer")
    assert (await hrec.wait_for("clip"))[1] == "copié depuis le viewer"
    await host.send_chat("Bonjour !")
    assert (await vrec.wait_for("chat"))[1] == "Bonjour !"

    # file transfer viewer -> host, content verified
    src = tmp_path / "rapport.bin"
    payload = os.urandom(300_000)
    src.write_bytes(payload)
    await viewer.send_file(str(src))
    done = await hrec.wait_for("file_done")
    assert done[3] is None and done[4] is True
    assert open(done[2], "rb").read() == payload
    assert os.path.dirname(done[2]) == str(tmp_path / "host_dl")

    # host revokes control: input is ignored even if the viewer forces it
    await host.set_permissions({"control": False, "clipboard": True, "files": True})
    perm = await vrec.wait_for("perm")
    assert perm[1]["control"] is False
    assert await wait_log(lambda e: e == ("release_all",))
    FakeInjector.log.clear()
    await viewer.send_json({"t": "key", "a": "down", "key": "enter"})
    await viewer.send_json({"t": "mouse", "a": "move", "x": 1, "y": 1})
    await asyncio.sleep(0.3)
    assert not any(e[0] in ("key", "move") for e in FakeInjector.log)

    # disconnect
    await viewer.close("fin du test")
    closed = await hrec.wait_for("closed")
    assert "fin du test" in closed[1]
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


async def test_malicious_file_names_are_neutralized(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("info")
    await viewer.send_json({"t": "file_offer", "tid": 77, "name": "../../../.bashrc", "size": 3})
    await asyncio.sleep(0.2)
    await viewer.channel.send(b"\x03" + (77).to_bytes(4, "big") + b"abc")
    import hashlib

    await viewer.send_json({"t": "file_end", "tid": 77, "sha256": hashlib.sha256(b"abc").hexdigest()})
    done = await hrec.wait_for("file_done")
    assert done[2] == str(tmp_path / "host_dl" / "bashrc")
    assert not (tmp_path / ".bashrc").exists()
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_files_refused_without_permission(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("info")
    await host.set_permissions({"control": True, "clipboard": False, "files": False})
    await vrec.wait_for("perm")
    # bypass the viewer-side check to make sure the host enforces it
    viewer.permissions["files"] = True
    src = tmp_path / "x.txt"
    src.write_text("data")
    await viewer.send_file(str(src))
    done = await vrec.wait_for("file_done")
    assert done[3] == "refusé par le poste distant"
    assert not (tmp_path / "host_dl").exists() or not list((tmp_path / "host_dl").iterdir())
    await viewer.send_json({"t": "clip", "text": "secret"})
    await asyncio.sleep(0.2)
    assert all(item[0] != "clip" for item in list(hrec.events._queue))
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\Windows\\System32\\evil.dll") == "evil.dll"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename("") == "fichier"
    assert safe_filename("a\x00b<>.txt") == "ab__.txt"
    assert len(safe_filename("x" * 1000 + ".pdf")) <= 180
    assert safe_filename(None) == "fichier"


def test_encoder_only_sends_changes():
    enc = FrameEncoder(tile=16)
    a = np.zeros((64, 64, 4), dtype=np.uint8)
    assert enc.changed_rects(a) == [(0, 0, 64, 64)]
    assert enc.changed_rects(a.copy()) == []
    b = a.copy()
    b[20, 40] = 255
    assert enc.changed_rects(b) == [(32, 16, 16, 16)]
    rects = enc.encode(b, [(32, 16, 16, 16)], 64, 70, 2)
    packed = pack_frame(5, 64, 64, None, rects)
    fid, fw, fh, cursor, parsed = unpack_frame(packed)
    assert (fid, fw, fh, cursor) == (5, 64, 64, None)
    assert parsed[0][:4] == (32, 16, 16, 16)


def test_unpack_rejects_out_of_bounds_rect():
    import struct

    from brickmote.host.capture import FRAME_HEADER, RECT_HEADER

    bad = FRAME_HEADER.pack(2, 1, 100, 100, 0, 0, 1) + RECT_HEADER.pack(90, 90, 20, 20, 1, 0)
    try:
        unpack_frame(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("out of bounds rect accepted")


def test_scaled_tiles_join_without_gaps():
    from brickmote.host.capture import scale_for, scaled

    enc = FrameEncoder()
    w, h = 2560, 1440
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    m = scale_for(w, 1920)
    assert m == 48 and (scaled(w, m), scaled(h, m)) == (1920, 1080)
    tiles = [(x, y, 64, 64) for x in range(0, 256, 64) for y in range(0, 128, 64)]
    out = enc.encode(frame, tiles, m, 70, 2)
    covered = set()
    for x, y, cw, ch, data in out:
        assert (cw, ch) == (48, 48)
        for px in range(x, x + cw):
            for py in range(y, y + ch):
                assert (px, py) not in covered, "overlap"
                covered.add((px, py))
    assert len(covered) == 192 * 96, "gap between scaled tiles"


def test_large_regions_are_encoded_in_parallel_strips():
    enc = FrameEncoder()
    frame = np.full((1080, 1920, 4), 128, dtype=np.uint8)
    out = enc.encode(frame, [(0, 0, 1920, 1080)], 64, 70, 2, threads=4)
    assert len(out) == 4
    assert sum(r[3] for r in out) == 1080 and all(r[2] == 1920 for r in out)
    enc.close()


def test_adaptive_quality_follows_the_link():
    from brickmote.host.capture import AdaptiveQuality, get_profile

    a = AdaptiveQuality(get_profile("performance"))
    q0, s0 = a.current()
    t = 0.0
    for _ in range(20):  # slow link: 600 ms per frame
        t += 0.6
        a.on_ack(0.6, t)
    q1, s1 = a.current()
    assert q1 < q0 and s1 < s0, "lighter frames on a slow link"
    for _ in range(200):  # fast link again
        t += 0.05
        a.on_ack(0.02, t)
    assert a.current() == (q0, s0), "back to full quality"
    assert get_profile("low").name == "eco" and get_profile("high").name == "max"
    assert get_profile("???").name == "balanced"


async def test_viewport_limits_the_frame_size(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "W", 1280)
    monkeypatch.setattr(sys.modules[__name__], "H", 800)
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("info")
    await viewer.set_viewport(640)  # small window: the host must not send 1280 px
    await viewer.refresh()
    for _ in range(20):
        frame = await vrec.wait_for("frame")
        if frame[2] < 1280:
            break
    assert (frame[2], frame[3]) == (640, 400)
    await viewer.set_quality("max")
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_lock_screen_is_reported_to_the_viewer(tmp_path, monkeypatch):
    import brickmote.host.session as host_mod

    locked = {"on": False}
    monkeypatch.setattr(host_mod, "session_locked", lambda: locked["on"])
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("frame")
    locked["on"] = True
    assert (await vrec.wait_for("status"))[1] == "locked"
    locked["on"] = False
    assert (await vrec.wait_for("status"))[1] == "ok"
    await vrec.wait_for("frame")  # the image comes back by itself
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_broken_capture_is_recreated(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("frame")
    created = []
    original = host._capture_factory

    class Broken(FakeCapture):
        def grab(self, index):
            raise OSError("BitBlt failed")

    def factory():
        created.append(1)
        return original() if len(created) > 3 else Broken()

    host._capture_factory = factory
    host._capture_exec.submit(host._close_capture).result()
    statuses = []
    while True:
        item = await vrec.wait_for("status")
        statuses.append(item[1])
        if item[1] == "ok":
            break
    assert statuses == ["unavailable", "ok"] and len(created) >= 4
    await vrec.wait_for("frame")
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_key_releases_are_never_dropped(tmp_path):
    import brickmote.host.session as host_mod

    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    await vrec.wait_for("frame")
    with host._pending_lock:
        host._pending_input = host_mod.MAX_PENDING_INPUT  # simulate a flood
    ran = []
    host._inject(lambda: ran.append("press"))
    host._inject(lambda: ran.append("release"), droppable=False)
    host._input_exec.submit(lambda: None).result()
    assert ran == ["release"]
    assert host._pending_input == host_mod.MAX_PENDING_INPUT  # back to where it was: no drift
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)
