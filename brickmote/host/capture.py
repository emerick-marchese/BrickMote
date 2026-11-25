"""Screen capture and incremental (tile-diff) JPEG encoding."""

from __future__ import annotations

import io
import struct
from typing import Optional, Protocol

import numpy as np
from PIL import Image

TILE = 64

# name -> (max frame width, JPEG quality, chroma subsampling 0=4:4:4 2=4:2:0)
QUALITY_PRESETS = {
    "low": (1280, 45, 2),
    "balanced": (1920, 70, 2),
    "high": (3840, 88, 0),
}

FRAME_HEADER = struct.Struct(">BIHHHHH")  # type, frame id, w, h, cursor x, cursor y, n rects
RECT_HEADER = struct.Struct(">HHHHBI")  # x, y, w, h, format, length
FMT_JPEG = 1
NO_CURSOR = 0xFFFF


class CaptureSource(Protocol):
    def monitors(self) -> list[dict]: ...
    def grab(self, index: int) -> tuple[int, int, bytes]: ...
    def close(self) -> None: ...


class MSSCapture:
    """Cross-platform capture (Windows GDI, macOS CoreGraphics, Linux X11).

    Must always be used from the same thread (mss handles are thread-bound).
    """

    def __init__(self) -> None:
        self._sct = None

    def _get(self):
        if self._sct is None:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            self._sct = factory()
        return self._sct

    def monitors(self) -> list[dict]:
        mons = self._get().monitors
        out = []
        for idx, mon in enumerate(mons[1:], start=1):
            out.append(
                {
                    "index": idx,
                    "left": mon["left"],
                    "top": mon["top"],
                    "width": mon["width"],
                    "height": mon["height"],
                }
            )
        if not out and mons:
            m = mons[0]
            out.append({"index": 0, "left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]})
        return out

    def grab(self, index: int) -> tuple[int, int, bytes]:
        sct = self._get()
        mons = sct.monitors
        if not 0 <= index < len(mons):
            index = 1 if len(mons) > 1 else 0
        shot = sct.grab(mons[index])
        return shot.width, shot.height, shot.raw  # BGRA

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None


def target_size(width: int, height: int, quality: str) -> tuple[int, int]:
    max_w = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])[0]
    if width <= max_w:
        return width, height
    ratio = max_w / width
    return max_w, max(1, int(round(height * ratio)))


class FrameEncoder:
    """Keeps the previous frame and emits only the changed regions."""

    def __init__(self, tile: int = TILE, full_ratio: float = 0.45, max_rects: int = 48):
        self.tile = tile
        self.full_ratio = full_ratio
        self.max_rects = max_rects
        self._prev: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._prev = None

    def prepare(self, width: int, height: int, raw: bytes, quality: str) -> np.ndarray:
        """Return an (H, W, 4) BGRA array, scaled down according to ``quality``."""
        tw, th = target_size(width, height, quality)
        if (tw, th) == (width, height):
            return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
        img = Image.frombuffer("RGBA", (width, height), raw, "raw", "RGBA", 0, 1)
        return np.asarray(img.resize((tw, th), Image.BILINEAR))

    def changed_rects(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        h, w = frame.shape[:2]
        prev = self._prev
        self._prev = frame
        if prev is None or prev.shape != frame.shape:
            return [(0, 0, w, h)]
        cur32 = np.ascontiguousarray(frame).view(np.uint32).reshape(h, w)
        prev32 = np.ascontiguousarray(prev).view(np.uint32).reshape(h, w)
        diff = cur32 != prev32
        t = self.tile
        rows, cols = -(-h // t), -(-w // t)
        padded = np.zeros((rows * t, cols * t), dtype=bool)
        padded[:h, :w] = diff
        tiles = padded.reshape(rows, t, cols, t).any(axis=(1, 3))
        count = int(tiles.sum())
        if count == 0:
            return []
        if count >= self.full_ratio * rows * cols:
            return [(0, 0, w, h)]
        rects = _merge_tiles(tiles)
        if len(rects) > self.max_rects:
            return [(0, 0, w, h)]
        out = []
        for r0, c0, r1, c1 in rects:
            x, y = c0 * t, r0 * t
            out.append((x, y, min(c1 * t, w) - x, min(r1 * t, h) - y))
        return out

    def encode(self, frame: np.ndarray, rects, quality: str) -> list[tuple[int, int, int, int, bytes]]:
        _, q, subsampling = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])
        out = []
        for x, y, w, h in rects:
            sub = np.ascontiguousarray(frame[y : y + h, x : x + w])
            img = Image.frombuffer("RGB", (w, h), sub.tobytes(), "raw", "BGRX", 0, 1)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, subsampling=subsampling, optimize=False)
            out.append((x, y, w, h, buf.getvalue()))
        return out


def _merge_tiles(tiles: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Merge changed tiles into rectangles: horizontal runs, then vertical."""
    rows = tiles.shape[0]
    done: list[tuple[int, int, int, int]] = []
    active: dict[tuple[int, int], int] = {}
    for r in range(rows + 1):
        runs = set()
        if r < rows:
            row = tiles[r]
            c = 0
            n = len(row)
            while c < n:
                if row[c]:
                    start = c
                    while c < n and row[c]:
                        c += 1
                    runs.add((start, c))
                else:
                    c += 1
        for run in list(active):
            if run not in runs:
                done.append((active.pop(run), run[0], r, run[1]))
        for run in runs:
            active.setdefault(run, r)
    return done


def pack_frame(frame_id: int, width: int, height: int, cursor, rects) -> bytes:
    cx, cy = cursor if cursor else (NO_CURSOR, NO_CURSOR)
    parts = [FRAME_HEADER.pack(0x02, frame_id & 0xFFFFFFFF, width, height, cx, cy, len(rects))]
    for x, y, w, h, data in rects:
        parts.append(RECT_HEADER.pack(x, y, w, h, FMT_JPEG, len(data)))
        parts.append(data)
    return b"".join(parts)


def unpack_frame(data: bytes) -> tuple[int, int, int, Optional[tuple[int, int]], list]:
    """Parse a frame message; raise ValueError on any inconsistency."""
    if len(data) < FRAME_HEADER.size:
        raise ValueError("short frame")
    kind, fid, fw, fh, cx, cy, n = FRAME_HEADER.unpack_from(data, 0)
    if kind != 0x02 or fw == 0 or fh == 0:
        raise ValueError("bad frame header")
    off = FRAME_HEADER.size
    rects = []
    for _ in range(n):
        if off + RECT_HEADER.size > len(data):
            raise ValueError("truncated rect")
        x, y, w, h, fmt, length = RECT_HEADER.unpack_from(data, off)
        off += RECT_HEADER.size
        if fmt != FMT_JPEG or w == 0 or h == 0 or x + w > fw or y + h > fh:
            raise ValueError("rect out of bounds")
        if off + length > len(data):
            raise ValueError("truncated rect data")
        rects.append((x, y, w, h, data[off : off + length]))
        off += length
    if off != len(data):
        raise ValueError("trailing data")
    cursor = None if cx == NO_CURSOR else (cx, cy)
    return fid, fw, fh, cursor, rects
