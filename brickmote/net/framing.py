"""Length-prefixed framing over asyncio streams.

Every frame on the wire is ``uint32_be length || payload``. Lengths above the
caller-provided maximum abort the connection *before* any allocation, which
protects against memory exhaustion attacks.
"""

from __future__ import annotations

import asyncio
import json
import struct

_LEN = struct.Struct(">I")


class FrameError(Exception):
    """Raised on a malformed or oversized frame."""


async def read_frame(reader: asyncio.StreamReader, max_size: int) -> bytes:
    try:
        header = await reader.readexactly(_LEN.size)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed") from exc
    (length,) = _LEN.unpack(header)
    if length > max_size:
        raise FrameError(f"frame too large ({length} > {max_size})")
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed") from exc


def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(_LEN.pack(len(payload)) + payload)


def encode_json(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def decode_json(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError("invalid JSON frame") from exc
    if not isinstance(obj, dict):
        raise FrameError("JSON frame is not an object")
    return obj


async def read_json(reader: asyncio.StreamReader, max_size: int) -> dict:
    return decode_json(await read_frame(reader, max_size))


async def write_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    write_frame(writer, encode_json(obj))
    await writer.drain()


async def close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except Exception:  # noqa: BLE001 - best effort close
        pass
