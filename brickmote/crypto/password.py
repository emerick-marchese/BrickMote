"""Unattended-access password and TOTP (2FA) helpers.

The host never stores the password itself, only ``scrypt(password, salt)``.
That value is the secret fed into the SPAKE2 password-authenticated key
exchange, so the password is never sent over the network (not even hashed),
and an attacker who intercepts a connection cannot run an offline dictionary
attack: each guess requires a new online attempt, which the host rate-limits.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
import urllib.parse
from dataclasses import dataclass

from ..util import b64d, b64e

# scrypt parameters chosen for ~0.1-0.3 s on a desktop CPU.
DEFAULT_N = 2**15
DEFAULT_R = 8
DEFAULT_P = 1
# Bounds a viewer accepts from a host (a malicious host must not be able to
# make the viewer allocate gigabytes of RAM).
MIN_N, MAX_N = 2**14, 2**17
MAX_R, MAX_P = 16, 4

MIN_PASSWORD_LENGTH = 10


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=256 * 1024 * 1024,
    )


def check_params(n: int, r: int, p: int) -> None:
    if not all(isinstance(v, int) for v in (n, r, p)):
        raise ValueError("invalid scrypt parameters")
    if not (MIN_N <= n <= MAX_N) or n & (n - 1):
        raise ValueError("scrypt N out of bounds")
    if not (1 <= r <= MAX_R) or not (1 <= p <= MAX_P):
        raise ValueError("scrypt r/p out of bounds")


def password_problems(password: str) -> list[str]:
    """Return a list of reasons why ``password`` is too weak (empty = OK)."""
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"au moins {MIN_PASSWORD_LENGTH} caractères")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        problems.append("au moins 3 types parmi minuscules, majuscules, chiffres, symboles")
    if len(set(password)) < 5:
        problems.append("trop de caractères répétés")
    return problems


@dataclass
class PasswordVerifier:
    salt: bytes
    n: int
    r: int
    p: int
    secret: bytes  # scrypt output, used as the SPAKE2 password

    @classmethod
    def create(cls, password: str, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P):
        check_params(n, r, p)
        salt = os.urandom(16)
        return cls(salt=salt, n=n, r=r, p=p, secret=_scrypt(password, salt, n, r, p))

    def public_params(self) -> dict:
        return {"salt": b64e(self.salt), "n": self.n, "r": self.r, "p": self.p}

    def to_dict(self) -> dict:
        return {**self.public_params(), "secret": b64e(self.secret)}

    @classmethod
    def from_dict(cls, d: dict) -> "PasswordVerifier":
        check_params(d["n"], d["r"], d["p"])
        return cls(
            salt=b64d(d["salt"], 16),
            n=d["n"],
            r=d["r"],
            p=d["p"],
            secret=b64d(d["secret"], 32),
        )


def derive_secret_from_params(password: str, params: dict) -> bytes:
    """Viewer side: recompute the host's secret from the advertised parameters."""
    if not isinstance(params, dict):
        raise ValueError("invalid password parameters")
    n, r, p = params.get("n"), params.get("r"), params.get("p")
    check_params(n, r, p)
    salt = b64d(params.get("salt"), 16)
    return _scrypt(password, salt, n, r, p)


# --------------------------------------------------------------------------
# TOTP (RFC 6238) - compatible with Google Authenticator, Aegis, 2FAS, ...
# --------------------------------------------------------------------------

TOTP_STEP = 30
TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _b32decode(secret: str) -> bytes:
    secret = secret.strip().replace(" ", "").upper()
    secret += "=" * (-len(secret) % 8)
    return base64.b32decode(secret, casefold=True)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS, algo: str = "sha1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), algo).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp(secret_b32: str, at: float | None = None) -> str:
    at = time.time() if at is None else at
    return hotp(_b32decode(secret_b32), int(at // TOTP_STEP))


class TotpVerifier:
    """Verifies codes with a +/-1 step window and refuses replays."""

    def __init__(self, secret_b32: str):
        self._key = _b32decode(secret_b32)
        self._last_step = -1

    def verify(self, code: object, at: float | None = None) -> bool:
        if not isinstance(code, str):
            return False
        code = code.strip().replace(" ", "")
        if len(code) != TOTP_DIGITS or not code.isdigit():
            return False
        at = time.time() if at is None else at
        current = int(at // TOTP_STEP)
        ok_step = None
        for step in (current - 1, current, current + 1):
            if hmac.compare_digest(hotp(self._key, step), code):
                ok_step = step
        if ok_step is None or ok_step <= self._last_step:
            return False
        self._last_step = ok_step
        return True


def totp_uri(secret_b32: str, account: str, issuer: str = "BrickMote") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    query = urllib.parse.urlencode(
        {"secret": secret_b32, "issuer": issuer, "digits": TOTP_DIGITS, "period": TOTP_STEP}
    )
    return f"otpauth://totp/{label}?{query}"
