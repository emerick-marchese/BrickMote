"""LAN discovery on UDP 50001.

Announcements only carry the device ID and its direct port. They do not need
to be trusted: IDs are derived from public keys, so a spoofed announcement can
only lead to a handshake that fails the ID check.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from typing import Callable, Optional

from ..constants import DISCOVERY_PORT
from ..util import RateLimiter, normalize_id

ANNOUNCE_INTERVAL = 5.0
ENTRY_TTL = 20.0
MAX_ENTRIES = 512


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "Discovery"):
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner._received(data, addr)


class Discovery:
    def __init__(
        self,
        own_id: str,
        direct_port_fn: Callable[[], Optional[int]],
        port: int = DISCOVERY_PORT,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.own_id = own_id
        self.direct_port_fn = direct_port_fn  # None = do not announce
        self.port = port
        self.on_change = on_change or (lambda: None)
        self.peers: dict[str, dict] = {}
        self._query_limiter = RateLimiter(limit=10, window=60, max_keys=1024)
        self._transport = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.port))
        sock.setblocking(False)
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        self._task = loop.create_task(self._announce_loop())
        self.query()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._transport:
            self._transport.close()
        self._transport = None

    def _send(self, payload: dict, addr=("255.255.255.255", None)) -> None:
        if self._transport is None:
            return
        target = (addr[0], addr[1] or self.port)
        try:
            self._transport.sendto(json.dumps(payload).encode(), target)
        except OSError:
            pass

    def _announcement(self) -> Optional[dict]:
        port = self.direct_port_fn()
        if not port:
            return None
        return {"bm": 1, "id": self.own_id, "port": port}

    def query(self) -> None:
        self._send({"bm": 1, "q": 1})

    async def _announce_loop(self) -> None:
        while True:
            ann = self._announcement()
            if ann:
                self._send(ann)
            self._expire()
            await asyncio.sleep(ANNOUNCE_INTERVAL)

    def _expire(self) -> None:
        now = time.monotonic()
        stale = [k for k, v in self.peers.items() if now - v["seen"] > ENTRY_TTL]
        for k in stale:
            del self.peers[k]
        if stale:
            self.on_change()

    def _received(self, data: bytes, addr) -> None:
        if len(data) > 512:
            return
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("bm") != 1:
            return
        if msg.get("q") == 1:
            if not self._query_limiter.allow(addr[0]):
                return  # no reflection/amplification
            ann = self._announcement()
            if ann:
                self._send(ann, (addr[0], addr[1]))
            return
        dev = normalize_id(str(msg.get("id", "")))
        port = msg.get("port")
        if not dev or dev == self.own_id or not isinstance(port, int) or not 0 < port < 65536:
            return
        if dev not in self.peers and len(self.peers) >= MAX_ENTRIES:
            return
        new = dev not in self.peers or self.peers[dev]["ip"] != addr[0]
        self.peers[dev] = {"ip": addr[0], "port": port, "seen": time.monotonic()}
        if new:
            self.on_change()

    def lookup(self, device_id: str) -> Optional[tuple[str, int]]:
        entry = self.peers.get(device_id)
        if entry and time.monotonic() - entry["seen"] <= ENTRY_TTL:
            return entry["ip"], entry["port"]
        return None
