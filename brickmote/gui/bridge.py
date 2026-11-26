"""Glue between the asyncio network thread and the Qt GUI thread.

Network code never touches widgets: it emits Qt signals, which Qt delivers
in the GUI thread (queued connections). The GUI calls into the network
thread with ``CoreThread.submit()``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..crypto.handshake import PeerInfo
from ..session import SessionEvents


class CoreThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="brickmote-net", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self, timeout: float = 3.0) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)


@dataclass
class PendingRequest:
    """A question the network thread asks the user (accept? TOTP? trust?)."""

    kind: str
    peer: PeerInfo
    sas: str = ""
    _resolve: Optional[Callable[[Any], None]] = None
    closed: bool = False
    extra: dict = field(default_factory=dict)

    def answer(self, value: Any) -> None:
        if not self.closed and self._resolve is not None:
            self.closed = True
            self._resolve(value)


class UiSignals(QObject):
    request = Signal(object)  # PendingRequest
    request_closed = Signal(object)
    relay_status = Signal(str, str)
    host_session_started = Signal(object)
    lan_peers = Signal(object)
    pointers = Signal(object)  # [(color, name, x, y)] viewers' pointers, desktop pixels


class SessionSignals(QObject):
    closed = Signal(object, str)
    chat = Signal(object, str)
    clipboard = Signal(object, str)
    permissions = Signal(object, object)
    file_progress = Signal(object, int, str, int, int, bool)
    file_done = Signal(object, int, str, object, object, bool)
    info = Signal(object, object)
    frame = Signal(object, int, int, object, object)  # session, w, h, cursor, [(x, y, QImage)]
    waiting = Signal(object, str)
    connect_result = Signal(object)  # concurrent.futures.Future of core.connect()
    status = Signal(object, str)  # session, "ok" | "locked" | "unavailable"
    cursors = Signal(object, object, object, bool)  # session, system (x, y) | None, [(color, x, y, name)], local priority


class QtSessionEvents(SessionEvents):
    def __init__(self) -> None:
        self.sig = SessionSignals()

    def on_closed(self, s, reason):
        self.sig.closed.emit(s, reason)

    def on_chat(self, s, text):
        self.sig.chat.emit(s, text)

    def on_clipboard(self, s, text):
        self.sig.clipboard.emit(s, text)

    def on_permissions(self, s, perms):
        self.sig.permissions.emit(s, dict(perms))

    def on_file_progress(self, s, tid, name, done, total, incoming):
        self.sig.file_progress.emit(s, tid, name, done, total, incoming)

    def on_file_done(self, s, tid, name, path, error, incoming):
        self.sig.file_done.emit(s, tid, name, path, error, incoming)

    def on_info(self, s, info):
        self.sig.info.emit(s, info)

    def on_status(self, s, status):
        self.sig.status.emit(s, status)

    def on_cursors(self, s, system, pointers, local=False):
        self.sig.cursors.emit(s, system, pointers, bool(local))

    def on_frame(self, s, fid, width, height, cursor, rects):
        # JPEG decoding happens here, in the network thread (QImage is
        # thread-safe), so the GUI thread only has to paint.
        images = []
        for x, y, w, h, data in rects:
            img = QImage.fromData(data, "JPG")
            if not img.isNull() and img.width() == w and img.height() == h:
                images.append((x, y, img))
        self.sig.frame.emit(s, width, height, cursor, images)


class QtCoreUI:
    """Implements ``brickmote.core.CoreUI`` with Qt signals."""

    def __init__(self) -> None:
        self.sig = UiSignals()
        self.host_events = QtSessionEvents()
        self.last_relay_status = ("disabled", "")
        self.last_lan_peers: dict = {}

    async def _ask(self, kind: str, peer: PeerInfo, sas: str = "", **extra):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def resolve(value):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(value))

        req = PendingRequest(kind, peer, sas, resolve, extra=extra)
        self.sig.request.emit(req)
        try:
            return await fut
        finally:
            req.closed = True
            self.sig.request_closed.emit(req)

    async def prompt_incoming(self, peer: PeerInfo, sas: str):
        return await self._ask("incoming", peer, sas)

    async def ask_totp(self, peer: PeerInfo):
        return await self._ask("totp", peer)

    async def confirm_new_host(self, peer: PeerInfo) -> bool:
        return bool(await self._ask("trust", peer))

    async def ask_password(self, peer: PeerInfo, interactive_ok: bool):
        return await self._ask("password", peer, interactive_ok=interactive_ok)

    def relay_status(self, state: str, detail: str) -> None:
        self.last_relay_status = (state, detail)
        self.sig.relay_status.emit(state, detail)

    def host_session_started(self, session) -> None:
        self.sig.host_session_started.emit(session)

    def pointers_changed(self, pointers: list) -> None:
        self.sig.pointers.emit(list(pointers))

    def lan_peers_changed(self, peers: dict) -> None:
        self.last_lan_peers = dict(peers)
        self.sig.lan_peers.emit(dict(peers))
