"""Behaviour shared by host and viewer sessions: control messages, keep-alive,
chat, clipboard and file transfer (both directions)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import struct
from pathlib import Path
from typing import Optional

from .constants import FILE_CHUNK_SIZE, MAX_FILE_SIZE, SESSION_IDLE_TIMEOUT, SESSION_KEEPALIVE
from .crypto.channel import ChannelError
from .crypto.handshake import HandshakeResult, sanitize_permissions
from .files import open_unique, safe_filename
from .net.framing import FrameError, decode_json, encode_json

log = logging.getLogger(__name__)

MSG_JSON = 0x01
MSG_FRAME = 0x02
MSG_FILE_CHUNK = 0x03
_CHUNK_HDR = struct.Struct(">BI")

MAX_CLIPBOARD = 1024 * 1024
MAX_CHAT = 4000
MAX_INCOMING_FILES = 4


class SessionEvents:
    """Callbacks fired from the network thread. Override what you need."""

    def on_closed(self, session, reason: str) -> None: ...
    def on_chat(self, session, text: str) -> None: ...
    def on_clipboard(self, session, text: str) -> None: ...
    def on_permissions(self, session, perms: dict) -> None: ...
    def on_file_progress(self, session, tid: int, name: str, done: int, total: int, incoming: bool) -> None: ...
    def on_file_done(self, session, tid: int, name: str, path: Optional[str], error: Optional[str], incoming: bool) -> None: ...
    # viewer only
    def on_info(self, session, info: dict) -> None: ...
    def on_frame(self, session, frame_id: int, width: int, height: int, cursor, rects: list) -> None: ...


class _Incoming:
    def __init__(self, name: str, size: int, final: Path, part: Path, fd: int):
        self.name = name
        self.size = size
        self.final = final
        self.part = part
        self.fh = os.fdopen(fd, "wb")
        self.received = 0
        self.hash = hashlib.sha256()


class PeerSession:
    role = "peer"

    def __init__(self, result: HandshakeResult, events: SessionEvents, downloads: Path):
        self.channel = result.channel
        self.peer = result.peer
        self.method = result.method
        self.sas = result.sas
        self.permissions = sanitize_permissions(result.permissions)
        self.events = events
        self.downloads = Path(downloads)
        self.closed = False
        self.close_reason = ""
        self._tasks: list[asyncio.Task] = []
        self._tid = itertools.count(1)
        self._incoming: dict[int, _Incoming] = {}
        self._outgoing: dict[int, asyncio.Future] = {}
        self._cancelled_out: set[int] = set()
        self._done = asyncio.Event()

    # ------------------------------------------------------------------ io
    async def send_json(self, obj: dict) -> None:
        if self.closed:
            return
        try:
            await self.channel.send(bytes([MSG_JSON]) + encode_json(obj))
        except (ConnectionError, OSError, FrameError) as exc:
            await self.close(f"connexion perdue ({exc})")

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if not task.cancelled() and task.exception() is not None and not self.closed:
            log.exception("session task failed", exc_info=task.exception())
            asyncio.get_running_loop().create_task(self.close("erreur interne"))

    async def run(self) -> None:
        """Run until the session ends."""
        await self.start()
        self.spawn(self._keepalive())
        try:
            while not self.closed:
                try:
                    data = await asyncio.wait_for(self.channel.recv(), SESSION_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self.close("délai d'inactivité dépassé")
                    break
                if not data:
                    continue
                kind = data[0]
                if kind == MSG_JSON:
                    msg = decode_json(data[1:])
                    await self._dispatch(msg)
                elif kind == MSG_FILE_CHUNK:
                    await self._on_file_chunk(data)
                elif kind == MSG_FRAME:
                    await self.handle_frame(data)
                else:
                    raise FrameError("unknown message type")
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            await self.close("connexion fermée par le poste distant")
        except (ChannelError, FrameError, ValueError) as exc:
            log.warning("protocol violation from %s: %s", self.peer.device_id, exc)
            await self.close("violation de protocole - session coupée")
        except asyncio.CancelledError:
            await self.close("session annulée")
            raise
        finally:
            if not self.closed:
                await self.close("session terminée")

    async def wait_closed(self) -> None:
        await self._done.wait()

    async def close(self, reason: str = "session terminée", notify: bool = True) -> None:
        if self.closed:
            return
        if notify:
            try:
                self.channel.send_nowait(bytes([MSG_JSON]) + encode_json({"t": "bye", "reason": reason[:200]}))
                await asyncio.wait_for(self.channel.writer.drain(), 1.0)
            except Exception:  # noqa: BLE001
                pass
        self.closed = True
        self.close_reason = reason
        for inc in self._incoming.values():
            self._discard_incoming(inc)
        self._incoming.clear()
        for fut in self._outgoing.values():
            if not fut.done():
                fut.set_result(False)
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        await self.channel.close()
        await self.on_close()
        self._done.set()
        try:
            self.events.on_closed(self, reason)
        except Exception:  # noqa: BLE001
            log.exception("on_closed callback failed")

    async def _keepalive(self) -> None:
        while not self.closed:
            await asyncio.sleep(SESSION_KEEPALIVE)
            await self.send_json({"t": "ping"})

    # ------------------------------------------------------- subclass hooks
    async def start(self) -> None: ...
    async def on_close(self) -> None: ...
    async def handle_frame(self, data: bytes) -> None:
        raise FrameError("unexpected frame")

    async def handle_message(self, msg: dict) -> None:
        raise FrameError(f"unexpected message {msg.get('t')!r}")

    # ---------------------------------------------------------- dispatching
    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "ping":
            return
        if t == "bye":
            await self.close(str(msg.get("reason", "le poste distant a mis fin à la session"))[:200], notify=False)
        elif t == "chat":
            text = msg.get("text")
            if isinstance(text, str) and text.strip():
                self.events.on_chat(self, "".join(c for c in text[:MAX_CHAT] if c.isprintable() or c == "\n"))
        elif t == "clip":
            text = msg.get("text")
            if self.permissions.get("clipboard") and isinstance(text, str) and len(text) <= MAX_CLIPBOARD:
                self.events.on_clipboard(self, text)
        elif t == "file_offer":
            await self._on_file_offer(msg)
        elif t == "file_end":
            await self._on_file_end(msg)
        elif t == "file_cancel":  # sender gave up: refers to one of our incoming transfers
            tid = msg.get("tid")
            inc = self._incoming.pop(tid, None) if isinstance(tid, int) else None
            if inc:
                self._discard_incoming(inc)
                self.events.on_file_done(self, tid, inc.name, None, "annulé par l'expéditeur", True)
        elif t == "file_abort":  # receiver gave up: refers to one of our outgoing transfers
            tid = msg.get("tid")
            if isinstance(tid, int) and tid in self._outgoing:
                self._cancelled_out.add(tid)
                fut = self._outgoing[tid]
                if not fut.done():
                    fut.set_result(False)
        elif t in ("file_accept", "file_reject"):
            tid = msg.get("tid")
            fut = self._outgoing.get(tid) if isinstance(tid, int) else None
            if fut and not fut.done():
                fut.set_result(t == "file_accept")
        else:
            await self.handle_message(msg)

    # ----------------------------------------------------------- clipboard
    async def send_clipboard(self, text: str) -> None:
        if self.permissions.get("clipboard") and isinstance(text, str) and len(text) <= MAX_CLIPBOARD:
            await self.send_json({"t": "clip", "text": text})

    async def send_chat(self, text: str) -> None:
        await self.send_json({"t": "chat", "text": text[:MAX_CHAT]})

    # ------------------------------------------------------- file transfer
    async def send_file(self, path: str) -> None:
        name = safe_filename(Path(path).name)
        tid = next(self._tid)
        if not self.permissions.get("files"):
            self.events.on_file_done(self, tid, name, None, "transfert de fichiers non autorisé", False)
            return
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError as exc:
            self.events.on_file_done(self, tid, name, None, f"lecture impossible : {exc}", False)
            return
        try:
            if size > MAX_FILE_SIZE:
                self.events.on_file_done(self, tid, name, None, "fichier trop volumineux", False)
                return
            fut = asyncio.get_running_loop().create_future()
            self._outgoing[tid] = fut
            await self.send_json({"t": "file_offer", "tid": tid, "name": name, "size": size})
            try:
                accepted = await asyncio.wait_for(fut, 60)
            except asyncio.TimeoutError:
                accepted = False
            if not accepted:
                self.events.on_file_done(self, tid, name, None, "refusé par le poste distant", False)
                return
            digest = hashlib.sha256()
            done = 0
            while not self.closed and tid not in self._cancelled_out:
                chunk = await asyncio.to_thread(fh.read, FILE_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                await self.channel.send(_CHUNK_HDR.pack(MSG_FILE_CHUNK, tid) + chunk)
                done += len(chunk)
                self.events.on_file_progress(self, tid, name, done, size, False)
                if done > size:
                    break
            if self.closed or tid in self._cancelled_out:
                self.events.on_file_done(self, tid, name, None, "transfert interrompu", False)
                return
            await self.send_json({"t": "file_end", "tid": tid, "sha256": digest.hexdigest()})
            self.events.on_file_done(self, tid, name, path, None, False)
        except (ConnectionError, OSError) as exc:
            self.events.on_file_done(self, tid, name, None, f"erreur : {exc}", False)
        finally:
            fh.close()
            self._outgoing.pop(tid, None)

    async def _on_file_offer(self, msg: dict) -> None:
        tid, size = msg.get("tid"), msg.get("size")
        if not isinstance(tid, int) or tid in self._incoming:
            raise FrameError("bad file offer")
        reason = None
        if not self.permissions.get("files"):
            reason = "not_allowed"
        elif not isinstance(size, int) or not 0 <= size <= MAX_FILE_SIZE:
            reason = "too_large"
        elif len(self._incoming) >= MAX_INCOMING_FILES:
            reason = "busy"
        if reason is None:
            name = safe_filename(msg.get("name"))
            try:
                final, final_fd = await asyncio.to_thread(open_unique, self.downloads, name)
                os.close(final_fd)
                part, fd = await asyncio.to_thread(open_unique, self.downloads, final.name + ".part")
            except (OSError, ValueError) as exc:
                log.warning("cannot create download file: %s", exc)
                reason = "io_error"
            else:
                self._incoming[tid] = _Incoming(final.name, size, final, part, fd)
                await self.send_json({"t": "file_accept", "tid": tid})
                self.events.on_file_progress(self, tid, final.name, 0, size, True)
                return
        await self.send_json({"t": "file_reject", "tid": tid, "reason": reason})

    async def _on_file_chunk(self, data: bytes) -> None:
        if len(data) < _CHUNK_HDR.size:
            raise FrameError("short chunk")
        _, tid = _CHUNK_HDR.unpack_from(data, 0)
        inc = self._incoming.get(tid)
        if inc is None:
            return  # transfer cancelled locally; ignore late chunks
        chunk = data[_CHUNK_HDR.size :]
        inc.received += len(chunk)
        if inc.received > inc.size:
            self._incoming.pop(tid, None)
            self._discard_incoming(inc)
            await self.send_json({"t": "file_abort", "tid": tid})
            self.events.on_file_done(self, tid, inc.name, None, "taille annoncée dépassée", True)
            return
        inc.hash.update(chunk)
        await asyncio.to_thread(inc.fh.write, chunk)
        self.events.on_file_progress(self, tid, inc.name, inc.received, inc.size, True)

    async def _on_file_end(self, msg: dict) -> None:
        tid = msg.get("tid")
        inc = self._incoming.pop(tid, None) if isinstance(tid, int) else None
        if inc is None:
            return
        try:
            inc.fh.close()
        except OSError:
            pass
        expected = msg.get("sha256")
        if inc.received != inc.size or not isinstance(expected, str) or expected != inc.hash.hexdigest():
            self._discard_incoming(inc)
            self.events.on_file_done(self, tid, inc.name, None, "fichier corrompu (empreinte SHA-256)", True)
            return
        try:
            os.replace(inc.part, inc.final)
        except OSError as exc:
            self._discard_incoming(inc)
            self.events.on_file_done(self, tid, inc.name, None, f"erreur d'écriture : {exc}", True)
            return
        self.events.on_file_done(self, tid, inc.name, str(inc.final), None, True)

    def _discard_incoming(self, inc: _Incoming) -> None:
        try:
            inc.fh.close()
        except OSError:
            pass
        for p in (inc.part, inc.final):
            try:
                if p.exists() and (p == inc.part or p.stat().st_size == 0):
                    p.unlink()
            except OSError:
                pass

    async def cancel_transfer(self, tid: int, incoming: bool) -> None:
        if incoming:
            inc = self._incoming.pop(tid, None)
            if inc:
                self._discard_incoming(inc)
                await self.send_json({"t": "file_abort", "tid": tid})
                self.events.on_file_done(self, tid, inc.name, None, "annulé", True)
        elif tid in self._outgoing:
            self._cancelled_out.add(tid)
            fut = self._outgoing[tid]
            if not fut.done():
                fut.set_result(False)
            await self.send_json({"t": "file_cancel", "tid": tid})
