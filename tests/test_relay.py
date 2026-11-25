import asyncio

import pytest

from brickmote.config import AuditLog, Settings
from brickmote.crypto.handshake import initiator_handshake, responder_handshake
from brickmote.crypto.identity import Identity
from brickmote.host.authority import HostPolicy
from brickmote.net.relay_client import RelayClient, RelayError
from brickmote.net.tls import PinError, generate_self_signed, server_context
from brickmote.server.relay import DeviceStore, RelayServer


@pytest.fixture
async def relay(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    fp = generate_self_signed(cert, key, ["127.0.0.1", "localhost"])
    server = RelayServer(DeviceStore(tmp_path / "db.sqlite3"))
    ports = await server.start("127.0.0.1", [0], server_context(cert, key))
    yield server, ports[0], fp
    await server.stop()


async def _online(client: RelayClient):
    await asyncio.wait_for(client.online.wait(), 10)


async def test_relay_end_to_end(relay, tmp_path):
    server, port, fp = relay
    host_ident, viewer_ident = Identity.generate(), Identity.generate()
    settings = Settings(device_name="Hote")

    async def accept(peer, sas):
        return {"control": True}

    policy = HostPolicy(host_ident, lambda: settings, AuditLog(tmp_path / "a.log"), prompt=accept)
    host_results = asyncio.Queue()

    async def on_incoming(reader, writer, from_id, from_pub):
        assert from_id == viewer_ident.device_id
        res = await responder_handshake(reader, writer, policy, "relay")
        await host_results.put(res)

    host = RelayClient(host_ident, "127.0.0.1", [port], fp, on_incoming)
    viewer = RelayClient(viewer_ident, "127.0.0.1", [port], fp, None)
    host.start()
    viewer.start()
    await _online(host)
    await _online(viewer)
    assert host.device_id == host_ident.device_id
    assert host.public_ip is None  # seen from 127.0.0.1: not a public address

    reader, writer, pub = await viewer.connect_to(host_ident.device_id)
    assert pub == host_ident.public_bytes
    v = await initiator_handshake(
        reader, writer, viewer_ident, "Viewer", expected_id=host_ident.device_id, expected_public=pub
    )
    h = await asyncio.wait_for(host_results.get(), 10)
    await v.channel.send(b"via relay")
    assert await h.channel.recv() == b"via relay"
    await h.channel.send(b"x" * 500_000)
    assert await v.channel.recv() == b"x" * 500_000

    with pytest.raises(RelayError) as err:
        await viewer.connect_to("123456789")
    assert err.value.code == "offline"
    with pytest.raises(RelayError) as err:
        await viewer.connect_to(viewer_ident.device_id)
    assert err.value.code == "self"

    await v.channel.close()
    await h.channel.close()
    await host.stop()
    await viewer.stop()


async def test_relay_rejects_wrong_pin(relay):
    _, port, _ = relay
    client = RelayClient(Identity.generate(), "127.0.0.1", [port], "AA" * 32, None)
    states = []
    client.on_status = lambda s, d: states.append((s, d))
    client.start()
    for _ in range(50):
        if any(s == "offline" for s, _ in states):
            break
        await asyncio.sleep(0.1)
    assert not client.online.is_set()
    assert any("empreinte" in d for s, d in states if s == "offline")
    await client.stop()


async def test_relay_join_requires_party_key(relay, tmp_path):
    """A third party that learns a session token cannot hijack it."""
    server, port, fp = relay
    host_ident, viewer_ident, intruder = Identity.generate(), Identity.generate(), Identity.generate()
    host = RelayClient(host_ident, "127.0.0.1", [port], fp, lambda *a: asyncio.sleep(0))
    viewer = RelayClient(viewer_ident, "127.0.0.1", [port], fp, None)
    host.start()
    viewer.start()
    await _online(host)
    await _online(viewer)
    # intercept the token the relay sends to the viewer
    token_holder = {}
    original = viewer._open_data

    async def spy(token):
        token_holder["token"] = token
        raise RelayError("cancelled")

    viewer._open_data = spy
    with pytest.raises(RelayError):
        await viewer.connect_to(host_ident.device_id)
    viewer._open_data = original
    thief = RelayClient(intruder, "127.0.0.1", [port], fp, None)
    with pytest.raises(RelayError) as err:
        await thief._open_data(token_holder["token"])
    assert err.value.code == "not_a_party"
    await host.stop()
    await viewer.stop()


def test_device_store_binds_id_to_key(tmp_path):
    store = DeviceStore(tmp_path / "d.sqlite3")
    a, b = Identity.generate(), Identity.generate()
    assert store.bind("111111111", a.public_bytes)
    assert store.bind("111111111", a.public_bytes)
    assert not store.bind("111111111", b.public_bytes)
    assert store.lookup("111111111") == a.public_bytes
