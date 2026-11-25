import asyncio

import pytest

from brickmote.config import AuditLog, Settings
from brickmote.crypto.handshake import (
    HandshakeError,
    initiator_handshake,
    responder_handshake,
)
from brickmote.crypto.identity import Identity
from brickmote.crypto.password import PasswordVerifier, generate_totp_secret, totp
from brickmote.host.authority import HostPolicy

from .conftest import stream_pair

PASSWORD = "Brique-Rouge-2026"


def make_host(tmp_path, prompt=None, **settings):
    s = Settings(device_name="Hote")
    for k, v in settings.items():
        setattr(s, k, v)
    ident = Identity.generate()
    policy = HostPolicy(ident, lambda: s, AuditLog(tmp_path / "audit.log"), prompt=prompt)
    return ident, s, policy


def unattended(tmp_path, **extra):
    pv = PasswordVerifier.create(PASSWORD, n=2**14)
    return make_host(
        tmp_path,
        unattended_enabled=True,
        password=pv.to_dict(),
        unattended_permissions={"control": True, "clipboard": True, "files": True},
        **extra,
    )


async def run_pair(policy, viewer=None, **viewer_kwargs):
    viewer = viewer or Identity.generate()
    (vr, vw), (hr, hw) = await stream_pair()
    host_task = asyncio.create_task(responder_handshake(hr, hw, policy, "127.0.0.1"))
    try:
        v = await initiator_handshake(vr, vw, viewer, "Viewer", **viewer_kwargs)
    except HandshakeError:
        vw.close()
        try:
            await asyncio.wait_for(host_task, 10)
        except (HandshakeError, asyncio.TimeoutError):
            pass
        hw.close()
        raise
    h = await host_task
    return v, h


async def accept_all(peer, sas):
    return {"control": True, "clipboard": False, "files": True}


async def test_interactive_accept(tmp_path):
    seen = {}

    async def prompt(peer, sas):
        seen["peer"], seen["sas"] = peer, sas
        return {"control": False, "clipboard": True, "files": False}

    ident, _, policy = make_host(tmp_path, prompt=prompt)
    viewer = Identity.generate()
    waiting = {}
    v, h = await run_pair(
        policy,
        viewer,
        expected_id=ident.device_id,
        on_waiting=lambda peer, sas: waiting.update(sas=sas),
    )
    assert v.peer.device_id == ident.device_id
    assert h.peer.device_id == viewer.device_id
    assert seen["peer"].public_key == viewer.public_bytes
    assert v.sas == h.sas == seen["sas"] == waiting["sas"]
    assert v.permissions == h.permissions == {"control": False, "clipboard": True, "files": False}
    # session channel works both ways
    await v.channel.send(b"ping")
    assert await h.channel.recv() == b"ping"
    await h.channel.send(b"pong")
    assert await v.channel.recv() == b"pong"


async def test_interactive_reject(tmp_path):
    async def prompt(peer, sas):
        return None

    _, _, policy = make_host(tmp_path, prompt=prompt)
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy)
    assert err.value.code == "rejected"


async def test_password_success(tmp_path):
    ident, _, policy = unattended(tmp_path)
    v, h = await run_pair(policy, password=PASSWORD, want="password", expected_id=ident.device_id)
    assert v.method == h.method == "password"
    assert v.permissions == {"control": True, "clipboard": True, "files": True}
    assert v.sas == h.sas
    await v.channel.send(b"x")
    assert await h.channel.recv() == b"x"


async def test_wrong_password_and_lockout(tmp_path):
    _, _, policy = unattended(tmp_path)
    viewer = Identity.generate()
    for _ in range(5):
        with pytest.raises(HandshakeError) as err:
            await run_pair(policy, viewer, password="mauvais-mot-de-passe", want="password")
        assert err.value.code == "auth_failed"
    # even the right password is now refused for this ID
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, viewer, password=PASSWORD, want="password")
    assert err.value.code == "locked"
    # another device is not affected
    v, _ = await run_pair(policy, password=PASSWORD, want="password")
    assert v.method == "password"


async def test_totp_required(tmp_path):
    secret = generate_totp_secret()
    _, _, policy = unattended(tmp_path, totp_secret=secret)

    async def good(peer):
        return totp(secret)

    async def bad(peer):
        return "000000" if totp(secret) != "000000" else "111111"

    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, password=PASSWORD, want="password", get_totp=bad)
    assert err.value.code == "totp_failed"
    v, _ = await run_pair(policy, password=PASSWORD, want="password", get_totp=good)
    assert v.method == "password"


async def test_unattended_disabled(tmp_path):
    _, _, policy = make_host(tmp_path, prompt=accept_all)
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, password=PASSWORD, want="password")
    assert err.value.code == "no_unattended"


async def test_expected_id_mismatch(tmp_path):
    _, _, policy = make_host(tmp_path, prompt=accept_all)
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, expected_id="123456789")
    assert err.value.code == "id_mismatch"


async def test_expected_public_key_mismatch(tmp_path):
    _, _, policy = make_host(tmp_path, prompt=accept_all)
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, expected_public=Identity.generate().public_bytes)
    assert err.value.code == "host_key_mismatch"


async def test_acl_and_blocklist(tmp_path):
    viewer = Identity.generate()
    _, s, policy = make_host(tmp_path, prompt=accept_all, acl_enabled=True, acl=[{"id": "999999999"}])
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, viewer)
    assert err.value.code == "acl"
    s.acl = [{"id": viewer.device_id}]
    v, _ = await run_pair(policy, viewer)
    assert v.method == "interactive"
    s.blocked = [viewer.device_id]
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy, viewer)
    assert err.value.code == "blocked"


async def test_mitm_with_own_key_is_detected(tmp_path):
    """An attacker relays the viewer to the real host but terminates the
    crypto with its own identity: the viewer must notice."""
    real_ident, _, real_policy = make_host(tmp_path, prompt=accept_all)
    mitm_ident, _, mitm_policy = make_host(tmp_path, prompt=accept_all)
    with pytest.raises(HandshakeError) as err:
        await run_pair(mitm_policy, expected_id=real_ident.device_id)
    assert err.value.code == "id_mismatch"


async def test_passive_relay_sees_no_secrets(tmp_path):
    ident, _, policy = unattended(tmp_path)
    (vr, vw), (r1, w1) = await stream_pair()
    (r2, w2), (hr, hw) = await stream_pair()
    captured = bytearray()

    async def pipe(src, dst):
        while data := await src.read(65536):
            captured.extend(data)
            dst.write(data)
            await dst.drain()
        dst.close()

    tasks = [asyncio.create_task(pipe(r1, w2)), asyncio.create_task(pipe(r2, w1))]
    host_task = asyncio.create_task(responder_handshake(hr, hw, policy, "relay"))
    v = await initiator_handshake(
        vr, vw, Identity.generate(), "Viewer", password=PASSWORD, want="password", expected_id=ident.device_id
    )
    h = await host_task
    await v.channel.send(b"TOP-SECRET-CONTENT")
    assert await h.channel.recv() == b"TOP-SECRET-CONTENT"
    for t in tasks:
        t.cancel()
    assert PASSWORD.encode() not in captured
    assert b"TOP-SECRET-CONTENT" not in captured
    assert b"Hote" not in captured, "host name must only travel encrypted"


async def test_busy_when_prompt_pending(tmp_path):
    gate = asyncio.Event()

    async def slow_prompt(peer, sas):
        await gate.wait()
        return {"control": True}

    _, _, policy = make_host(tmp_path, prompt=slow_prompt)
    first = asyncio.create_task(run_pair(policy))
    await asyncio.sleep(0.5)
    with pytest.raises(HandshakeError) as err:
        await run_pair(policy)
    assert err.value.code == "busy"
    gate.set()
    v, _ = await first
    assert v.permissions["control"] is True


async def test_garbage_is_rejected(tmp_path):
    _, _, policy = make_host(tmp_path, prompt=accept_all)
    (vr, vw), (hr, hw) = await stream_pair()
    host_task = asyncio.create_task(responder_handshake(hr, hw, policy, "x"))
    vw.write(b"\x00\x00\x00\x05hello")
    await vw.drain()
    with pytest.raises(HandshakeError) as err:
        await host_task
    assert err.value.code == "protocol"
