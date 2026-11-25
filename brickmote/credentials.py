"""Remembered unattended-access passwords (viewer side).

The password itself is never stored: only the secret derived from it for one
given host (scrypt with that host's salt), which is what the SPAKE2 exchange
needs. It is useless for any other host and does not reveal the password.
On Windows it is additionally encrypted with DPAPI (bound to the Windows
user account); elsewhere the file is readable by the owner only.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
from pathlib import Path
from typing import Optional

from .util import b64d, b64e, secure_write

_ENTROPY = b"BrickMote saved password v1"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    data_in, _keep1 = blob(data)
    entropy, _keep2 = blob(_ENTROPY)
    out = Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(secret: bytes) -> str:
    if sys.platform.startswith("win"):
        try:
            return "dpapi:" + b64e(_dpapi(secret, True))
        except Exception:  # noqa: BLE001 - fall back to the per-user file permissions
            pass
    return "plain:" + b64e(secret)


def unprotect(value: str) -> Optional[bytes]:
    try:
        kind, _, data = value.partition(":")
        raw = base64.b64decode(data, validate=True)
        if kind == "dpapi":
            return _dpapi(raw, False) if sys.platform.startswith("win") else None
        if kind == "plain":
            return raw
    except Exception:  # noqa: BLE001 - corrupted or from another account: ask again
        return None
    return None


class SavedPasswords:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def _write(self) -> None:
        secure_write(self.path, json.dumps(self._data, indent=2).encode("utf-8"))

    def has(self, device_id: str) -> bool:
        return device_id in self._data

    def get(self, device_id: str) -> Optional[tuple]:
        entry = self._data.get(device_id)
        if not isinstance(entry, dict):
            return None
        secret = unprotect(str(entry.get("secret", "")))
        params = entry.get("params")
        if secret is None or len(secret) != 32 or not isinstance(params, dict):
            return None
        try:
            b64d(params.get("salt"), 16)
        except ValueError:
            return None
        return params, secret

    def save(self, device_id: str, params: dict, secret: bytes) -> None:
        with self._lock:
            self._data[device_id] = {"params": dict(params), "secret": protect(secret)}
            self._write()

    def forget(self, device_id: str) -> None:
        with self._lock:
            if self._data.pop(device_id, None) is not None:
                self._write()

    def devices(self) -> set:
        return set(self._data)
