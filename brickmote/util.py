"""Small shared helpers (secure file writes, base64, rate limiting)."""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
import time
from collections import deque
from pathlib import Path


def secure_write(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # Windows has no fchmod
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(value: object, expected_len: int | None = None, max_len: int = 4096) -> bytes:
    """Strictly decode base64; raise ValueError on anything unexpected."""
    if not isinstance(value, str) or len(value) > max_len * 2:
        raise ValueError("invalid base64 field")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 field") from exc
    if expected_len is not None and len(raw) != expected_len:
        raise ValueError("unexpected field length")
    if len(raw) > max_len:
        raise ValueError("field too long")
    return raw


def clean_text(value: object, max_len: int = 64, default: str = "") -> str:
    """Return a printable, length-limited version of an untrusted string."""
    if not isinstance(value, str):
        return default
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:max_len].strip() or default


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` events per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float, max_keys: int = 100_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._events: dict[object, deque[float]] = {}

    def allow(self, key: object, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.max_keys:
                self._purge(now)
                if len(self._events) >= self.max_keys:
                    return False
            events = self._events[key] = deque()
        while events and now - events[0] > self.window:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True

    def _purge(self, now: float) -> None:
        for key in [k for k, ev in self._events.items() if not ev or now - ev[-1] > self.window]:
            del self._events[key]


def format_id(device_id: str) -> str:
    digits = "".join(ch for ch in device_id if ch.isdigit())
    return " ".join(digits[i : i + 3] for i in range(0, len(digits), 3))


def normalize_id(text: str) -> str | None:
    """Return a 9 digit ID from user input like '123 456 789', or None."""
    digits = "".join(ch for ch in text if not ch.isspace())
    if len(digits) == 9 and digits.isdigit() and digits[0] != "0":
        return digits
    return None
