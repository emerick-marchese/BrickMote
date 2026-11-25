"""Built-in default relay server (embedded at build time, see
packaging/embed_relay.py). Its address is never shown in the interface."""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass
from typing import Optional

DISPLAY_NAME = "serveur BrickMote"


@dataclass(frozen=True)
class BuiltinRelay:
    host: str
    ports: tuple
    fingerprint: str

    def __repr__(self) -> str:  # never leak the address in logs or tracebacks
        return "BuiltinRelay(<masqué>)"


def decode(key_b85: str, data_b85: str) -> Optional[BuiltinRelay]:
    try:
        key = base64.b85decode(key_b85)
        raw = base64.b85decode(data_b85)
        packed = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        cfg = json.loads(zlib.decompress(packed))
        host, ports, fp = cfg["h"], cfg["p"], cfg["f"]
        if not isinstance(host, str) or not host or not isinstance(fp, str) or len(fp) != 64:
            return None
        ports = tuple(p for p in ports if isinstance(p, int) and 0 < p < 65536)
        return BuiltinRelay(host, ports, fp) if ports else None
    except Exception:  # noqa: BLE001 - corrupted data: behave as if absent
        return None


def builtin_relay() -> Optional[BuiltinRelay]:
    try:
        from . import _embedded  # generated at build time, absent from the sources
    except ImportError:
        return None
    return decode(getattr(_embedded, "K", ""), getattr(_embedded, "D", ""))
