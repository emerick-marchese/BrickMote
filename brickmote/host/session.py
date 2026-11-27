"""Host side of a session: streams the screen, applies remote input."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from ..crypto.handshake import HandshakeResult, sanitize_permissions
from ..session import PeerSession, SessionEvents
from . import securedesk
from .capture import session_locked
from .cursors import to_abs
from .capture import QUALITY_PRESETS, AdaptiveQuality, FrameEncoder, MSSCapture, get_profile, pack_frame, scale_for, scaled
from .inputs import InputInjector

log = logging.getLogger(__name__)

MAX_SEND_BUFFER = 2 * 1024 * 1024
MAX_PENDING_INPUT = 500


class HostSession(PeerSession):
    role = "host"

    def __init__(
        self,
        result: HandshakeResult,
        events: SessionEvents,
        downloads: Path,
        quality: str = "balanced",
        fps: int = 20,
        capture_factory: Callable = MSSCapture,
        injector_factory: Callable = InputInjector,
        wol_info: Optional[dict] = None,
        direct_info: Optional[dict] = None,
        cursor_hub=None,
        local_activity=None,
        secure_factory: Optional[Callable] = None,
    ):
        super().__init__(result, events, downloads)
        self.wol_info = wol_info
        self.cursor_hub = cursor_hub
        self.local_activity = local_activity  # priority to the person at the computer
        # lock screen / Ctrl+Alt+Del (Windows): seen and controlled through the
        # SYSTEM helper while it is displayed
        if secure_factory is None and securedesk.available():
            secure_factory = securedesk.SecureDesktopClient
        self._secure = secure_factory() if secure_factory is not None else None
        self._secure_active = False
        self._screen = "ok"  # ok | locked | secure | uac (set by the capture thread)
        self.color: Optional[int] = None  # this viewer's pointer color (multi-user)
        self.direct_info = direct_info
        self.profile = get_profile(quality)
        self.adaptive = AdaptiveQuality(self.profile)
        self.fps = self.profile.fps
        self.viewport_width: Optional[int] = None  # viewer window width (physical pixels)
        self._m = 64  # current scale (m/64)
        self._sent: dict[int, tuple[float, int]] = {}
        self._idle_since: Optional[float] = None
        self._min_q = 100  # lowest JPEG quality used since the last full frame
        self._capture_factory = capture_factory
        self._capture = None
        self._capture_exec = ThreadPoolExecutor(1, thread_name_prefix="bm-capture")
        self._input_exec = ThreadPoolExecutor(1, thread_name_prefix="bm-input")
        self._encoder = FrameEncoder()
        self._injector = injector_factory()  # only touched from the input thread
        self._cursor_reader = injector_factory()  # only touched from the capture thread
        self._last_cursor = None
        self._force_full = False
        self.monitors: list[dict] = []
        self.monitor_index = 1
        self._ready = asyncio.Event()
        self._ack = asyncio.Event()
        self._in_flight = 0
        self._pending_input = 0
        self._pending_lock = threading.Lock()
        self.frames_sent = 0
        self._status = "ok"  # what the viewer was told: ok | locked | unavailable
        self._failures = 0

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self.monitors = await loop.run_in_executor(self._capture_exec, self._list_monitors)
        except Exception as exc:  # noqa: BLE001
            log.error("screen capture unavailable: %s", exc)
            self.monitors = []
        if not self.monitors:
            self.spawn(self.close("capture d'écran impossible sur le poste hôte"))
            return
        self._select_monitor(self.monitors[0]["index"])
        if self.cursor_hub is not None:
            self.color = self.cursor_hub.join(self, self.peer.name)
        self.spawn(self._video_loop())

    def _list_monitors(self) -> list[dict]:
        if self._capture is None:
            self._capture = self._capture_factory()
        return self._capture.monitors()

    def current_monitor(self) -> Optional[dict]:
        return next((m for m in self.monitors if m["index"] == self.monitor_index), None)

    def _point(self, msg: dict, drove: bool) -> None:
        monitor = self.current_monitor()
        if self.cursor_hub is not None:
            self.cursor_hub.point(self, monitor, msg.get("x"), msg.get("y"), drove)
        x, y = msg.get("x"), msg.get("y")
        if (
            drove
            and self.local_activity is not None
            and monitor is not None
            and isinstance(x, int)
            and isinstance(y, int)
            and not isinstance(x, bool)
        ):
            self.local_activity.injected_at(*to_abs(monitor, x, y))  # not the local mouse

    def local_has_priority(self) -> bool:
        return self.local_activity is not None and self.local_activity.active()

    def release_remote_input(self) -> None:
        """The person at the computer took over: let go of what we hold."""
        try:
            self._input_exec.submit(self._injector.release_all)
        except RuntimeError:  # closing
            pass

    def _close_capture(self) -> None:
        """Runs in the capture thread."""
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass

    def _select_monitor(self, index: int) -> bool:
        mon = next((m for m in self.monitors if m["index"] == index), None)
        if mon is None:
            return False
        self.monitor_index = index
        self._cursor_reader.set_monitor(mon)
        self._input_exec.submit(self._injector.set_monitor, mon)
        if self._secure is not None:
            self._secure.set_monitor(mon)
        self._force_full = True
        return True

    async def on_close(self) -> None:
        if self.cursor_hub is not None:
            self.cursor_hub.leave(self)
        if self._secure is not None:
            try:
                self._input_exec.submit(self._secure.close)
            except RuntimeError:
                pass
        try:  # executors refuse new work while the application exits
            self._input_exec.submit(self._injector.release_all)
            self._capture_exec.submit(self._close_capture)
            self._capture_exec.submit(self._encoder.close)
        except RuntimeError:
            pass
        self._input_exec.shutdown(wait=False)
        self._capture_exec.shutdown(wait=False)

    def _info(self) -> dict:
        info = {
            "t": "info",
            "monitors": [{"index": m["index"], "width": m["width"], "height": m["height"]} for m in self.monitors],
            "monitor": self.monitor_index,
            "perms": self.permissions,
            "quality": self.profile.name,
            "fps": self.fps,
            "cursors": self.cursor_hub is not None,  # live pointers are sent separately
        }
        if self.color is not None:
            info["color"] = self.color
        if self.direct_info:
            info["direct"] = self.direct_info
        if self.wol_info:
            info["wol"] = {
                "mac": self.wol_info.get("mac"),
                "macs": list(self.wol_info.get("macs") or [])[:4],
                "broadcast": self.wol_info.get("broadcast"),
                "box": self.wol_info.get("box", ""),
                "port": self.wol_info.get("port", 9),
            }
        return info

    async def set_permissions(self, perms: dict) -> None:
        """Called from the host UI to change rights during a session."""
        self.permissions = sanitize_permissions(perms)
        if not self.permissions["control"]:
            self._input_exec.submit(self._injector.release_all)
        await self.send_json({"t": "perm", "perms": self.permissions})
        self.events.on_permissions(self, self.permissions)

    # --------------------------------------------------------------- video
    async def _video_loop(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), 15)
        except asyncio.TimeoutError:
            await self.close("le poste distant n'a pas confirmé la session")
            return
        loop = asyncio.get_running_loop()
        frame_id = 0
        while not self.closed:
            limit = self.profile.in_flight if self.adaptive.level < 3 else 1  # congested: one at a time
            if self._in_flight >= limit or self.channel.buffered_bytes() > MAX_SEND_BUFFER:
                self._ack.clear()
                try:
                    await asyncio.wait_for(self._ack.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            started = loop.time()
            try:
                payload = await loop.run_in_executor(self._capture_exec, self._grab_encode, frame_id + 1)
                self._failures = 0
            except RuntimeError:  # executor shut down while closing
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("capture failed: %s", exc)
                payload = None
                self._failures += 1
                if self._failures >= 3:
                    await self._set_status("unavailable")
                await asyncio.sleep(0.5)
                continue
            await self._set_status(self._screen)
            if self._screen == "locked":
                await asyncio.sleep(0.5)
                continue
            if payload is not None and not self.closed:
                frame_id += 1
                self._in_flight += 1
                self.frames_sent += 1
                self._sent[frame_id] = (loop.time(), len(payload))
                if len(self._sent) > 64:  # never acknowledged (should not happen over TCP)
                    for old in sorted(self._sent)[:-32]:
                        self._sent.pop(old, None)
                await self.channel.send(payload)
            elapsed = loop.time() - started
            # the lock screen goes through the helper (raw images): 10 per second is plenty
            fps = min(self.fps, 10) if self._screen in ("secure", "uac") else self.fps
            await asyncio.sleep(max(0.002, 1.0 / fps - elapsed))

    async def _set_status(self, status: str) -> None:
        """Tell the viewer when the screen cannot be shown (lock screen...)."""
        if status != self._status:
            self._status = status
            if status in ("ok", "secure", "uac"):
                self._force_full = True  # another desktop: send it whole
            await self.send_json({"t": "status", "s": status})

    def _on_ack(self, fid: object) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        sent = self._sent.pop(fid, None) if isinstance(fid, int) else None
        if sent is not None:
            now = asyncio.get_running_loop().time()
            self.adaptive.on_ack(now - sent[0], now)
        self._ack.set()

    def _grab_encode(self, frame_id: int) -> Optional[bytes]:
        if self._capture is None:
            self._capture = self._capture_factory()
        secure = False
        if session_locked():
            # lock screen, Ctrl+Alt+Del, UAC: only the SYSTEM helper can see it
            if self._secure is None:
                self._secure_active, self._screen = False, "locked"
                return None
            try:
                width, height, raw = self._secure.grab(self.monitor_index)
            except securedesk.SecureDeskError as exc:
                log.debug("secure desktop: %s", exc)
                self._secure_active, self._screen = False, "locked"
                return None
            self._secure_active, secure = True, True
            self._screen = "uac" if self._secure.state.get("uac") else "secure"
        else:
            self._secure_active, self._screen = False, "ok"
            try:
                width, height, raw = self._capture.grab(self.monitor_index)
            except Exception:
                # a capture handle can stay broken after a desktop switch (lock,
                # UAC, resolution change): start again with a fresh one
                self._close_capture()
                raise
        frame = FrameEncoder.as_array(width, height, raw)
        profile = self.profile
        quality, level_scale = self.adaptive.current()
        max_w = profile.max_width if not self.viewport_width else min(profile.max_width, self.viewport_width)
        m = scale_for(width, max(320, int(max_w * level_scale)))
        if m != self._m or self._force_full:
            self._m, self._force_full = m, False
            self._encoder.reset()
        rects = self._encoder.changed_rects(frame)
        fw, fh = scaled(width, m), scaled(height, m)
        cur = None if secure else self._cursor_reader.cursor()
        cursor = (min(fw - 1, int(cur[0] * fw)), min(fh - 1, int(cur[1] * fh))) if cur else None
        now = time.monotonic()
        if not rects:
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since > 1.0 and self._min_q < quality - 5:
                # the screen is still: resend it once at the (now better) quality
                self._encoder.reset()
                rects = self._encoder.changed_rects(frame)
                self._min_q = 100
            if not rects and cursor == self._last_cursor:
                return None
        else:
            self._idle_since = None
        if rects == [(0, 0, width, height)]:
            self._min_q = quality
        elif rects:
            self._min_q = min(self._min_q, quality)
        self._last_cursor = cursor
        subsampling = 0 if profile.full_chroma and quality >= 85 else 2
        encoded = self._encoder.encode(frame, rects, m, quality, subsampling, profile.threads)
        return pack_frame(frame_id, fw, fh, cursor, encoded)

    # ------------------------------------------------------------- control
    def _inject(self, method: str, *args, droppable: bool = True) -> None:
        # The counter is shared with the input thread: always under the lock,
        # or it could drift and end up dropping every event.
        with self._pending_lock:
            if droppable and self._pending_input >= MAX_PENDING_INPUT:
                return  # viewer flooding us: drop moves/presses, never releases
            self._pending_input += 1

        def run():
            try:
                # on the lock screen, input goes through the SYSTEM helper
                target = self._secure if self._secure_active and self._secure is not None else self._injector
                getattr(target, method)(*args)
            finally:
                with self._pending_lock:
                    self._pending_input -= 1

        try:
            self._input_exec.submit(run)
        except RuntimeError:  # closing
            with self._pending_lock:
                self._pending_input -= 1

    async def handle_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "ready":
            if not self._ready.is_set():
                self._ready.set()
                await self.send_json(self._info())
            return
        if not self._ready.is_set():
            return
        if t == "ack":
            self._on_ack(msg.get("fid"))
        elif t == "ptr":
            # where this viewer points, shown to the others (allowed in view-only)
            if msg.get("out"):
                if self.cursor_hub is not None:
                    self.cursor_hub.hide(self)
            else:
                self._point(msg, drove=False)
        elif t in ("mouse", "wheel", "key"):
            if not self.permissions.get("control"):
                return  # view-only: silently ignore any input
            if self.local_has_priority():
                # the person at the computer is using the mouse: remote input
                # waits, except releases (nothing must stay pressed)
                action = msg.get("a")
                if t == "mouse" and action == "up":
                    self._inject("mouse_button", msg.get("b"), False, None, None, droppable=False)
                elif t == "key" and action == "up":
                    self._inject("key", False, msg.get("key"), msg.get("char"), droppable=False)
                elif t == "mouse" and action == "move":
                    self._point(msg, drove=False)  # still show where this viewer points
                return
            if t == "mouse":
                action = msg.get("a")
                if action in ("move", "down", "up"):
                    self._point(msg, drove=True)
                if action == "move":
                    self._inject("mouse_move", msg.get("x"), msg.get("y"))
                elif action in ("down", "up"):
                    self._inject(
                        "mouse_button", msg.get("b"), action == "down", msg.get("x"), msg.get("y"),
                        droppable=action == "down",
                    )
            elif t == "wheel":
                self._inject("scroll", msg.get("dx"), msg.get("dy"))
            else:
                action = msg.get("a")
                if action in ("down", "up"):
                    self._inject(
                        "key", action == "down", msg.get("key"), msg.get("char"),
                        droppable=action == "down",
                    )
        elif t == "monitor":
            index = msg.get("index")
            if isinstance(index, int) and self._select_monitor(index):
                await self.send_json(self._info())
        elif t in ("quality", "profile"):
            name = msg.get("q") if t == "quality" else msg.get("p")
            if name in QUALITY_PRESETS:
                self.profile = get_profile(name)
                self.adaptive.set_profile(self.profile)
                self.fps = self.profile.fps
                self._force_full = True
        elif t == "viewport":
            w = msg.get("w")
            if isinstance(w, int) and 320 <= w <= 16384:
                self.viewport_width = w
        elif t == "fps":
            fps = msg.get("fps")
            if isinstance(fps, int) and 1 <= fps <= 60:
                self.fps = min(fps, self.profile.fps)
        elif t == "refresh":
            self._force_full = True
        # unknown messages are ignored (forward compatibility)
