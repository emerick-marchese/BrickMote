"""Authenticated encrypted channel (AES-256-GCM, one key per direction).

Each frame is encrypted with a nonce built from a strictly increasing 64-bit
counter. The receiver uses its own expected counter, so any replayed,
reordered, dropped, truncated or modified frame makes decryption fail and the
connection is closed immediately.
"""

from __future__ import annotations

import asyncio

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..net.framing import FrameError, close_writer, read_frame, write_frame

_MAX_COUNTER = 2**64 - 1
_TAG = 16


class ChannelError(Exception):
    """Integrity failure: the connection must be dropped."""


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")


class SecureChannel:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send_key: bytes,
        recv_key: bytes,
        max_frame: int,
    ):
        if len(send_key) != 32 or len(recv_key) != 32:
            raise ValueError("AES-256 keys required")
        self.reader = reader
        self.writer = writer
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self.max_frame = max_frame
        self.bytes_sent = 0
        self.bytes_received = 0
        self.closed = False

    def send_nowait(self, plaintext: bytes) -> None:
        """Encrypt and queue a frame. Atomic w.r.t. the event loop (no await)."""
        if self.closed:
            raise ConnectionError("channel closed")
        if len(plaintext) + _TAG > self.max_frame:
            raise FrameError("frame too large")
        if self._send_ctr >= _MAX_COUNTER:
            raise ChannelError("nonce space exhausted")
        ct = self._send.encrypt(_nonce(self._send_ctr), plaintext, None)
        self._send_ctr += 1
        write_frame(self.writer, ct)
        self.bytes_sent += len(ct) + 4

    async def send(self, plaintext: bytes) -> None:
        self.send_nowait(plaintext)
        await self.writer.drain()

    async def recv(self) -> bytes:
        ct = await read_frame(self.reader, self.max_frame)
        if len(ct) < _TAG:
            raise ChannelError("short frame")
        if self._recv_ctr >= _MAX_COUNTER:
            raise ChannelError("nonce space exhausted")
        try:
            pt = self._recv.decrypt(_nonce(self._recv_ctr), ct, None)
        except InvalidTag as exc:
            self.closed = True
            raise ChannelError("integrity check failed (tampering or replay)") from exc
        self._recv_ctr += 1
        self.bytes_received += len(ct) + 4
        return pt

    def buffered_bytes(self) -> int:
        transport = self.writer.transport
        try:
            return transport.get_write_buffer_size()
        except Exception:  # noqa: BLE001
            return 0

    async def close(self) -> None:
        self.closed = True
        await close_writer(self.writer)
