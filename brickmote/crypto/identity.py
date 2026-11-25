"""Device identity: a long-term Ed25519 key pair.

The 9-digit BrickMote ID is *derived from the public key* through scrypt, so an
ID is self-certifying: when a viewer connects to ``123 456 789`` it checks that
the key presented by the remote side really hashes to that ID. Forging a key
for a given ID requires ~10^9 scrypt evaluations (years of CPU time), and the
viewer additionally pins the full key after the first connection.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ..util import format_id, secure_write

PUBLIC_KEY_LEN = 32
SIGNATURE_LEN = 64

_ID_SALT = b"BrickMote-ID-v1"
_ID_SCRYPT = dict(n=2**14, r=8, p=1, maxmem=64 * 1024 * 1024)


@functools.lru_cache(maxsize=4096)
def id_from_public(public_key: bytes) -> str:
    """Derive the 9-digit device ID (first digit never 0) from a public key."""
    if len(public_key) != PUBLIC_KEY_LEN:
        raise ValueError("bad public key length")
    digest = hashlib.scrypt(public_key, salt=_ID_SALT, dklen=8, **_ID_SCRYPT)
    return str(100_000_000 + int.from_bytes(digest, "big") % 900_000_000)


def fingerprint(public_key: bytes) -> str:
    """Human-readable SHA-256 fingerprint (first 128 bits) of a public key."""
    digest = hashlib.sha256(b"BrickMote-FP-v1" + public_key).hexdigest()[:32]
    return ":".join(digest[i : i + 4] for i in range(0, 32, 4)).upper()


def verify_signature(public_key: bytes, signature: bytes, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


class Identity:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def generate(cls) -> "Identity":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        path = Path(path)
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("identity file does not contain an Ed25519 key")
            return cls(key)
        ident = cls.generate()
        ident.save(path)
        return ident

    def save(self, path: Path) -> None:
        pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        secure_write(Path(path), pem)

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    @property
    def device_id(self) -> str:
        return id_from_public(self.public_bytes)

    @property
    def display_id(self) -> str:
        return format_id(self.device_id)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_bytes)
