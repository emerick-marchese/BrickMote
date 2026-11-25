"""Sign a release checksum list for BrickMote's automatic updates.

    BRICKMOTE_UPDATE_SIGNING_KEY=<base64 Ed25519 private key> python packaging/sign_release.py SHA256SUMS.txt

Writes SHA256SUMS.txt.sig next to the file and checks the signature against
the public keys pinned in brickmote/updater.py (a release signed with an
unknown key would be refused by every installed copy).
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(path: str) -> int:
    try:
        seed = base64.b64decode(os.environ.get("BRICKMOTE_UPDATE_SIGNING_KEY", "").strip(), validate=True)
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except (binascii.Error, ValueError):
        print("Clé de signature invalide (32 octets en base64 attendus).", file=sys.stderr)
        return 1
    data = Path(path).read_bytes()
    signature = key.sign(data)
    pub = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()

    from brickmote.updater import UPDATE_PUBLIC_KEYS

    if pub not in UPDATE_PUBLIC_KEYS:
        print("Cette clé ne correspond à aucune clé publique de brickmote/updater.py.", file=sys.stderr)
        return 1
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(signature, data)
    Path(path + ".sig").write_text(base64.b64encode(signature).decode() + "\n")
    print(f"Signature écrite : {path}.sig")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
