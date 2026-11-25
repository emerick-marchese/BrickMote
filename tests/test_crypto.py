import asyncio
import os

import pytest

from brickmote.crypto.channel import ChannelError, SecureChannel
from brickmote.crypto.identity import Identity, fingerprint, id_from_public
from brickmote.crypto.password import (
    PasswordVerifier,
    TotpVerifier,
    derive_secret_from_params,
    hotp,
    password_problems,
    totp,
)
from brickmote.net.framing import FrameError, write_frame

from .conftest import stream_pair


def test_identity_id_is_stable_and_well_formed(tmp_path):
    ident = Identity.load_or_create(tmp_path / "id.pem")
    again = Identity.load_or_create(tmp_path / "id.pem")
    assert ident.public_bytes == again.public_bytes
    assert ident.device_id == again.device_id
    assert len(ident.device_id) == 9 and ident.device_id.isdigit() and ident.device_id[0] != "0"
    assert ident.display_id.count(" ") == 2
    if os.name == "posix":
        assert (tmp_path / "id.pem").stat().st_mode & 0o077 == 0
    other = Identity.generate()
    assert other.device_id != ident.device_id
    assert fingerprint(other.public_bytes) != fingerprint(ident.public_bytes)


def test_id_from_public_rejects_bad_length():
    with pytest.raises(ValueError):
        id_from_public(b"short")


def test_hotp_rfc4226_vectors():
    key = b"12345678901234567890"
    expected = ["755224", "287082", "359152", "969429", "338314"]
    assert [hotp(key, i) for i in range(5)] == expected


def test_totp_rfc6238_vector():
    key = b"12345678901234567890"
    assert hotp(key, 59 // 30, digits=8) == "94287082"
    assert hotp(key, 1111111109 // 30, digits=8) == "07081804"


def test_totp_verifier_window_and_replay():
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    v = TotpVerifier(secret)
    now = 1_700_000_000
    code = totp(secret, now)
    assert v.verify(code, now)
    assert not v.verify(code, now), "replayed code must be refused"
    assert not v.verify("000000x", now)
    assert not v.verify(None, now)
    v2 = TotpVerifier(secret)
    assert v2.verify(totp(secret, now - 30), now)  # previous step accepted
    assert not TotpVerifier(secret).verify(totp(secret, now - 120), now)


def test_password_verifier_roundtrip():
    pv = PasswordVerifier.create("Correct-Horse-9", n=2**14)
    same = PasswordVerifier.from_dict(pv.to_dict())
    assert same.secret == pv.secret
    assert derive_secret_from_params("Correct-Horse-9", pv.public_params()) == pv.secret
    assert derive_secret_from_params("wrong", pv.public_params()) != pv.secret
    assert "secret" not in pv.public_params()


def test_password_params_are_bounded():
    pv = PasswordVerifier.create("Correct-Horse-9", n=2**14)
    params = pv.public_params()
    params["n"] = 2**24  # a malicious host asking for 16 GiB of RAM
    with pytest.raises(ValueError):
        derive_secret_from_params("x", params)


def test_password_strength():
    assert password_problems("abc")
    assert password_problems("aaaaaaaaaaaaaaaa")
    assert password_problems("Brique-Rouge-2026") == []


async def _channel_pair():
    (ra, wa), (rb, wb) = await stream_pair()
    k1, k2 = os.urandom(32), os.urandom(32)
    a = SecureChannel(ra, wa, k1, k2, 1 << 20)
    b = SecureChannel(rb, wb, k2, k1, 1 << 20)
    return a, b


async def test_channel_roundtrip():
    a, b = await _channel_pair()
    for i in range(5):
        await a.send(b"hello %d" % i)
        assert await b.recv() == b"hello %d" % i
        await b.send(b"back")
        assert await a.recv() == b"back"
    await a.close()
    await b.close()


async def test_channel_detects_tampering():
    a, b = await _channel_pair()
    write_frame(a.writer, b"\x00" * 40)  # forged ciphertext
    await a.writer.drain()
    with pytest.raises(ChannelError):
        await b.recv()


async def test_channel_detects_replay():
    (ra, wa), (rb, wb) = await stream_pair()
    k1, k2 = os.urandom(32), os.urandom(32)
    sender = SecureChannel(ra, wa, k1, k2, 1 << 20)
    receiver = SecureChannel(rb, wb, k2, k1, 1 << 20)
    captured = []
    original = wa.write
    wa.write = lambda data: (captured.append(data), original(data))
    await sender.send(b"transfer 100 EUR")
    assert await receiver.recv() == b"transfer 100 EUR"
    original(captured[0])  # attacker re-injects the same frame
    await wa.drain()
    with pytest.raises(ChannelError):
        await receiver.recv()


async def test_channel_rejects_oversized_frame():
    (ra, wa), (rb, wb) = await stream_pair()
    receiver = SecureChannel(rb, wb, os.urandom(32), os.urandom(32), 1024)
    wa.write((10_000_000).to_bytes(4, "big"))
    await wa.drain()
    with pytest.raises(FrameError):
        await receiver.recv()
