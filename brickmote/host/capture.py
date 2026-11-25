"""Screen capture and incremental (tile-diff) JPEG encoding."""

from __future__ import annotations

import io
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np
from PIL import Image

TILE = 64


@dataclass(frozen=True)
class Profile:
    """Performance profile chosen by the viewer. Higher = smoother and sharper,
    but more bandwidth and CPU on both computers."""

    name: str
    label: str
    fps: int
    max_width: int  # widest frame sent (before adaptive reduction)
    q_max: int  # JPEG quality when the link is good
    q_min: int  # lowest JPEG quality the adaptive control may use
    in_flight: int  # frames sent but not yet acknowledged
    target_ms: int  # acknowledgement delay the adaptive control aims for
    threads: int  # parallel encoders
    full_chroma: bool  # 4:4:4 colours (sharper text) at high quality
    mouse_ms: int  # viewer: mouse move sending interval


PROFILES = {
    "eco": Profile("eco", "Économie", 12, 1280, 55, 30, 1, 350, 1, False, 30),
    "balanced": Profile("balanced", "Équilibré", 24, 1920, 70, 35, 2, 220, 2, False, 15),
    "performance": Profile("performance", "Performance", 40, 2560, 80, 40, 2, 150, 4, False, 8),
    "max": Profile("max", "Maximum", 60, 3840, 90, 50, 3, 120, 4, True, 8),
}
_ALIASES = {"low": "eco", "high": "max"}  # names used by BrickMote <= 1.0.4
QUALITY_PRESETS = set(PROFILES) | set(_ALIASES)


def get_profile(name: object) -> Profile:
    name = _ALIASES.get(name, name)
    return PROFILES.get(name, PROFILES["balanced"])


# Scale factors are multiples of 1/64 so that every 64-pixel tile boundary of
# the screen falls on a whole pixel of the scaled frame: changed tiles can be
# scaled independently and still join without seams.
SCALE_STEPS = (64, 56, 48, 40, 32, 24, 16)


def scale_for(width: int, max_width: int) -> int:
    for m in SCALE_STEPS:
        if width * m / 64 <= max_width:
            return m
    return SCALE_STEPS[-1]


def scaled(v: int, m: int) -> int:
    return (v * m + 32) // 64


class AdaptiveQuality:
    """Adjusts JPEG quality, then resolution, to the measured acknowledgement
    delay: on a slow link frames get lighter instead of piling up (which is
    what makes remote control feel laggy)."""

    def __init__(self, profile: Profile):
        self.set_profile(profile)

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        hi, lo = profile.q_max, profile.q_min
        mid = (hi + lo) // 2
        self.levels = [(hi, 1.0), (mid, 1.0), (lo, 1.0), (mid, 0.75), (lo, 0.75), (lo, 0.5)]
        self.level = 0
        self.ewma: Optional[float] = None
        self.good = 0
        self.last_change = 0.0

    def current(self) -> tuple[int, float]:
        return self.levels[self.level]

    def on_ack(self, delay: float, now: float) -> None:
        self.ewma = delay if self.ewma is None else 0.7 * self.ewma + 0.3 * delay
        target = self.profile.target_ms / 1000
        if self.ewma > target * 1.5:
            self.good = 0
            if self.level < len(self.levels) - 1 and now - self.last_change > 0.5:
                self.level += 1
                self.last_change = now
        elif self.ewma < target * 0.7:
            self.good += 1
            if self.good >= 15 and self.level > 0 and now - self.last_change > 2.0:
                self.level -= 1
                self.last_change = now
                self.good = 0
        else:
            self.good = 0


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


class FrameEncoder:
    """Keeps the previous frame and emits only the changed regions.

    Changes are detected on the full-resolution capture (cheap), and only the
    changed tiles are scaled down and JPEG-encoded, in parallel for large
    areas. Scaling a whole 1440p/4K screen on every frame cost 50-120 ms.
    """

    def __init__(self, tile: int = TILE, full_ratio: float = 0.45, max_rects: int = 48):
        self.tile = tile
        self.full_ratio = full_ratio
        self.max_rects = max_rects
        self._prev: Optional[np.ndarray] = None
        self._pool: Optional[ThreadPoolExecutor] = None

    def reset(self) -> None:
        self._prev = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    @staticmethod
    def as_array(width: int, height: int, raw: bytes) -> np.ndarray:
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)

    def changed_rects(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        h, w = frame.shape[:2]
        prev = self._prev
        self._prev = frame
        if prev is None or prev.shape != frame.shape:
            return [(0, 0, w, h)]
        diff = frame.view(np.uint32).reshape(h, w) != prev.view(np.uint32).reshape(h, w)
        t = self.tile
        full_r, full_c = h // t, w // t
        rows = diff[: full_r * t].reshape(full_r, t, w).any(axis=1)
        if h % t:
            rows = np.vstack([rows, diff[full_r * t :].any(axis=0, keepdims=True)])
        tiles = rows[:, : full_c * t].reshape(rows.shape[0], full_c, t).any(axis=2)
        if w % t:
            tiles = np.hstack([tiles, rows[:, full_c * t :].any(axis=1, keepdims=True)])
        count = int(tiles.sum())
        if count == 0:
            return []
        if count >= self.full_ratio * tiles.size:
            return [(0, 0, w, h)]
        rects = _merge_tiles(tiles)
        if len(rects) > self.max_rects:
            return [(0, 0, w, h)]
        out = []
        for r0, c0, r1, c1 in rects:
            x, y = c0 * t, r0 * t
            out.append((x, y, min(c1 * t, w) - x, min(r1 * t, h) - y))
        return out

    def _jobs(self, rects, threads: int):
        """Split large regions into tile-aligned horizontal strips for parallel encoding."""
        jobs = []
        for x, y, w, h in rects:
            n = threads if threads > 1 and w * h >= 400_000 else 1
            step = max(self.tile, -(-h // n // self.tile) * self.tile)
            for sy in range(y, y + h, step):
                jobs.append((x, sy, w, min(step, y + h - sy)))
        return jobs

    def encode(self, frame: np.ndarray, rects, m: int, quality: int, subsampling: int, threads: int = 1):
        """Encode source ``rects`` of ``frame`` scaled by m/64. Returns rects in
        scaled coordinates: (x, y, w, h, jpeg)."""
        jobs = self._jobs(rects, threads)

        def run(job):
            x, y, w, h = job
            sub = np.ascontiguousarray(frame[y : y + h, x : x + w])
            img = Image.frombuffer("RGB", (w, h), sub.tobytes(), "raw", "BGRX", 0, 1)
            dx, dy = scaled(x, m), scaled(y, m)
            dw, dh = max(1, scaled(x + w, m) - dx), max(1, scaled(y + h, m) - dy)
            if m != 64:
                img = img.resize((dw, dh), Image.BOX)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, subsampling=subsampling, optimize=False)
            return dx, dy, dw, dh, buf.getvalue()

        if len(jobs) > 1 and threads > 1:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(4, thread_name_prefix="bm-jpeg")
            return list(self._pool.map(run, jobs))
        return [run(j) for j in jobs]


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
