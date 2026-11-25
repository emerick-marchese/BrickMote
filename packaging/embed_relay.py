"""Embed the default relay server into the build, without it ever being in
the (public) source code.

The CI reads two GitHub secrets and writes ``brickmote/_embedded.py`` (ignored
by git) just before PyInstaller runs:

* BRICKMOTE_DEFAULT_RELAY     ``host:port`` or ``host:port1,port2``
* BRICKMOTE_DEFAULT_RELAY_FP  SHA-256 fingerprint printed by the relay

The address is stored scrambled (zlib + XOR with a random per-build key +
base85), so it never appears in clear in the executable. This is
obfuscation, not encryption: the app has to connect to that server, so a
determined person with network tools can always see where it connects.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import zlib
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "brickmote" / "_embedded.py"
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[[0-9A-Fa-f:]+\]$")


def parse(value: str, fingerprint: str) -> dict:
    value = value.strip()
    host, sep, ports_text = value.rpartition(":")
    if not sep or not host:
        raise ValueError("format attendu : hote:port (ex. relais.exemple.fr:25001)")
    if not _HOST.match(host):
        raise ValueError("nom d'hôte ou IP invalide")
    ports = [int(p) for p in ports_text.split(",") if p.strip()]
    if not ports or not all(0 < p < 65536 for p in ports):
        raise ValueError("port invalide")
    fp = "".join(c for c in fingerprint.lower() if c in "0123456789abcdef")
    if len(fp) != 64:
        raise ValueError("empreinte SHA-256 invalide (64 caractères hexadécimaux attendus)")
    return {"h": host.strip("[]"), "p": ports, "f": fp}


def build_module(config: dict, key: bytes | None = None) -> str:
    key = key or os.urandom(32)
    packed = zlib.compress(json.dumps(config, separators=(",", ":")).encode(), 9)
    scrambled = bytes(b ^ key[i % len(key)] for i, b in enumerate(packed))
    return (
        "# Generated at build time by packaging/embed_relay.py - do not commit.\n"
        f"K = {base64.b85encode(key).decode()!r}\n"
        f"D = {base64.b85encode(scrambled).decode()!r}\n"
    )


def main() -> int:
    value = os.environ.get("BRICKMOTE_DEFAULT_RELAY", "")
    if not value.strip():
        if TARGET.exists():
            TARGET.unlink()
        print("Aucun serveur relais par défaut (secret BRICKMOTE_DEFAULT_RELAY absent).")
        return 0
    try:
        config = parse(value, os.environ.get("BRICKMOTE_DEFAULT_RELAY_FP", ""))
    except ValueError as exc:  # never echo the secret itself
        print(f"Secret du serveur relais par défaut invalide : {exc}", file=sys.stderr)
        return 1
    TARGET.write_text(build_module(config), encoding="utf-8")
    print(f"Serveur relais par défaut intégré ({len(config['p'])} port(s), empreinte épinglée).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
