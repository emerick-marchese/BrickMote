"""Host side of a session: streams the screen, applies remote input."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from ..crypto.handshake import HandshakeResult, sanitize_permissions
from ..session import PeerSession, SessionEvents
from .capture import QUALITY_PRESETS, FrameEncoder, MSSCapture, pack_frame
from .inputs import InputInjector

log = logging.getLogger(__name__)

MAX_IN_FLIGHT = 2  # frames sent but not yet acknowledged (latency control)
MAX_SEND_BUFFER = 4 * 1024 * 1024
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
    ):
        super().__init__(result, events, downloads)
        self.quality = quality if quality in QUALITY_PRESETS else "balanced"
        self.fps = max(1, min(60, fps))
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
        self.frames_sent = 0

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
        self.spawn(self._video_loop())

    def _list_monitors(self) -> list[dict]:
        if self._capture is None:
            self._capture = self._capture_factory()
        return self._capture.monitors()

    def _select_monitor(self, index: int) -> bool:
        mon = next((m for m in self.monitors if m["index"] == index), None)
        if mon is None:
            return False
        self.monitor_index = index
        self._cursor_reader.set_monitor(mon)
        self._input_exec.submit(self._injector.set_monitor, mon)
        self._force_full = True
        return True

    async def on_close(self) -> None:
        self._input_exec.submit(self._injector.release_all)
        self._input_exec.shutdown(wait=False)
        if self._capture is not None:
            self._capture_exec.submit(self._capture.close)
        self._capture_exec.shutdown(wait=False)

    def _info(self) -> dict:
        return {
            "t": "info",
            "monitors": [{"index": m["index"], "width": m["width"], "height": m["height"]} for m in self.monitors],
            "monitor": self.monitor_index,
            "perms": self.permissions,
            "quality": self.quality,
            "fps": self.fps,
        }

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
            if self._in_flight >= MAX_IN_FLIGHT or self.channel.buffered_bytes() > MAX_SEND_BUFFER:
                self._ack.clear()
                try:
                    await asyncio.wait_for(self._ack.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            started = loop.time()
            try:
                payload = await loop.run_in_executor(self._capture_exec, self._grab_encode, frame_id + 1)
            except RuntimeError:  # executor shut down while closing
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("capture failed: %s", exc)
                payload = None
                await asyncio.sleep(0.5)
            if payload is not None and not self.closed:
                frame_id += 1
                self._in_flight += 1
                self.frames_sent += 1
                await self.channel.send(payload)
            elapsed = loop.time() - started
            await asyncio.sleep(max(0.005, 1.0 / self.fps - elapsed))

    def _grab_encode(self, frame_id: int) -> Optional[bytes]:
        if self._capture is None:
            self._capture = self._capture_factory()
        width, height, raw = self._capture.grab(self.monitor_index)
        quality = self.quality
        frame = self._encoder.prepare(width, height, raw, quality)
        if self._force_full:
            self._force_full = False
            self._encoder.reset()
        rects = self._encoder.changed_rects(frame)
        fh, fw = frame.shape[:2]
        cur = self._cursor_reader.cursor()
        cursor = (min(fw - 1, int(cur[0] * fw)), min(fh - 1, int(cur[1] * fh))) if cur else None
        if not rects and cursor == self._last_cursor:
            return None
        self._last_cursor = cursor
        return pack_frame(frame_id, fw, fh, cursor, self._encoder.encode(frame, rects, quality))

    # ------------------------------------------------------------- control
    def _inject(self, fn, *args) -> None:
        if self._pending_input >= MAX_PENDING_INPUT:
            return  # viewer flooding us: drop events
        self._pending_input += 1

        def run():
            try:
                fn(*args)
            finally:
                self._pending_input -= 1

        try:
            self._input_exec.submit(run)
        except RuntimeError:
            pass

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
            self._in_flight = max(0, self._in_flight - 1)
            self._ack.set()
        elif t in ("mouse", "wheel", "key"):
            if not self.permissions.get("control"):
                return  # view-only: silently ignore any input
            if t == "mouse":
                action = msg.get("a")
                if action == "move":
                    self._inject(self._injector.mouse_move, msg.get("x"), msg.get("y"))
                elif action in ("down", "up"):
                    self._inject(
                        self._injector.mouse_button, msg.get("b"), action == "down", msg.get("x"), msg.get("y")
                    )
            elif t == "wheel":
                self._inject(self._injector.scroll, msg.get("dx"), msg.get("dy"))
            else:
                action = msg.get("a")
                if action in ("down", "up"):
                    self._inject(self._injector.key, action == "down", msg.get("key"), msg.get("char"))
        elif t == "monitor":
            index = msg.get("index")
            if isinstance(index, int) and self._select_monitor(index):
                await self.send_json(self._info())
        elif t == "quality":
            q = msg.get("q")
            if q in QUALITY_PRESETS:
                self.quality = q
                self._force_full = True
        elif t == "fps":
            fps = msg.get("fps")
            if isinstance(fps, int) and 1 <= fps <= 60:
                self.fps = fps
        elif t == "refresh":
            self._force_full = True
        # unknown messages are ignored (forward compatibility)
