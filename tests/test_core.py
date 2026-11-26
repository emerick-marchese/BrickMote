"""Integration tests of the full engine (two BrickMoteCore instances + relay)."""

import asyncio
import functools
import json

import pytest

import brickmote.core as core_mod
from brickmote.config import Paths
from brickmote.core import BrickMoteCore, ConnectError, parse_address
from brickmote.crypto.identity import Identity
from brickmote.crypto.password import PasswordVerifier
from brickmote.host.session import HostSession
from brickmote.net.tls import generate_self_signed, server_context
from brickmote.server.relay import DeviceStore, RelayServer
from brickmote.session import SessionEvents
from brickmote.util import b64e

from .test_session import FakeCapture, FakeInjector, Recorder

PASSWORD = "Brique-Rouge-2026"


class FakeUI:
    def __init__(self, accept=True):
        self.host_events = SessionEvents()
        self.accept = accept
        self.trust_asked = []
        self.prompts = []
        self.host_sessions = []

    async def prompt_incoming(self, peer, sas):
        self.prompts.append((peer, sas))
        return {"control": True, "clipboard": False, "files": False} if self.accept else None

    async def ask_totp(self, peer):
        return None

    async def confirm_new_host(self, peer):
        self.trust_asked.append(peer)
        return True

    def relay_status(self, state, detail):
        pass

    def host_session_started(self, session):
        self.host_sessions.append(session)

    def lan_peers_changed(self, peers):
        pass


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fake_screen(monkeypatch):
    monkeypatch.setattr(
        core_mod,
        "HostSession",
        functools.partial(HostSession, capture_factory=FakeCapture, injector_factory=FakeInjector),
    )


def make_core(tmp_path, name, **settings):
    home = tmp_path / name
    home.mkdir()
    conf = {"device_name": name, "direct_port": free_port(), "discovery_enabled": False, **settings}
    (home / "settings.json").write_text(json.dumps(conf))
    ui = FakeUI()
    return BrickMoteCore(Paths(home), ui), ui


@pytest.fixture
async def relay(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    fp = generate_self_signed(cert, key, ["127.0.0.1"])
    server = RelayServer(DeviceStore(tmp_path / "db.sqlite3"))
    port = (await server.start("127.0.0.1", [0], server_context(cert, key)))[0]
    yield port, fp
    await server.stop()


async def wait_online(core):
    await asyncio.wait_for(core._relay.online.wait(), 10)


async def test_connect_by_id_through_relay_with_password(tmp_path, relay):
    port, fp = relay
    relay_conf = {"relay_host": "127.0.0.1", "relay_ports": [port], "relay_fingerprint": fp}
    pv = PasswordVerifier.create(PASSWORD, n=2**14)
    host, host_ui = make_core(
        tmp_path, "host", unattended_enabled=True, password=pv.to_dict(), direct_enabled=False, **relay_conf
    )
    viewer, viewer_ui = make_core(tmp_path, "viewer", **relay_conf)
    await host.start()
    await viewer.start()
    await wait_online(host)
    await wait_online(viewer)

    events = Recorder()
    session = await viewer.connect(host.identity.display_id, PASSWORD, events)
    assert session.peer.device_id == host.identity.device_id
    assert session.method == "password"
    assert host_ui.prompts == [], "unattended access must not prompt"
    assert viewer.known_hosts.get(host.identity.device_id) == host.identity.public_bytes
    await events.wait_for("frame")

    with pytest.raises(ConnectError) as err:
        await viewer.connect(host.identity.device_id, "mauvais-mot-de-passe", Recorder())
    assert "Mot de passe incorrect" in str(err.value)

    await session.close()
    await viewer.stop()
    await host.stop()


async def test_connect_by_ip_and_key_pinning(tmp_path):
    host, host_ui = make_core(tmp_path, "host")
    viewer, viewer_ui = make_core(tmp_path, "viewer")
    await host.start()
    await viewer.start()
    target = f"127.0.0.1:{host.direct_port}"

    session = await viewer.connect(target, None, Recorder())
    assert [p.device_id for p in viewer_ui.trust_asked] == [host.identity.device_id]
    assert len(host_ui.prompts) == 1
    assert session.permissions == {"control": True, "clipboard": False, "files": False}
    await session.close()

    # second time: the key is pinned, no question asked
    session = await viewer.connect(target, None, Recorder())
    assert len(viewer_ui.trust_asked) == 1
    await session.close()

    # the pinned key changes (impersonation attempt): refused
    viewer.known_hosts.remember(host.identity.device_id, Identity.generate().public_bytes, "x")
    with pytest.raises(ConnectError) as err:
        await viewer.connect(target, None, Recorder())
    assert "clé du poste distant a changé" in str(err.value)

    await viewer.stop()
    await host.stop()


async def test_refused_request(tmp_path):
    host, host_ui = make_core(tmp_path, "host")
    host_ui.accept = False
    viewer, _ = make_core(tmp_path, "viewer")
    await host.start()
    await viewer.start()
    with pytest.raises(ConnectError) as err:
        await viewer.connect(f"127.0.0.1:{host.direct_port}", None, Recorder())
    assert "refusée" in str(err.value)
    assert any(e["event"] == "request" for e in host.audit.tail())
    await viewer.stop()
    await host.stop()


async def test_unknown_id_without_relay(tmp_path):
    viewer, _ = make_core(tmp_path, "viewer")
    await viewer.start()
    with pytest.raises(ConnectError) as err:
        await viewer.connect("123 456 789", None, Recorder())
    assert "relais" in str(err.value)
    with pytest.raises(ConnectError):
        await viewer.connect(viewer.identity.device_id, None, Recorder())
    await viewer.stop()


def test_parse_address():
    assert parse_address("192.168.1.5") == ("192.168.1.5", 7070)
    assert parse_address("192.168.1.5:8000") == ("192.168.1.5", 8000)
    assert parse_address("[::1]:7070") == ("::1", 7070)
    assert parse_address("fe80::1") == ("fe80::1", 7070)
    assert parse_address("host.local") == ("host.local", 7070)
    assert parse_address("bad host") is None


async def test_lan_discovery_finds_peer(tmp_path):
    from brickmote.net.discovery import Discovery

    port = free_port()
    seen = asyncio.Event()
    a = Discovery("111111111", lambda: 7070, port=port)
    b = Discovery("222222222", lambda: 7071, port=port, on_change=seen.set)
    try:
        await a.start()
        await b.start()
    except OSError:
        pytest.skip("UDP broadcast unavailable in this environment")
    # unicast announcement on loopback (broadcast may be filtered in CI)
    a._send(a._announcement(), ("127.0.0.1", port))
    try:
        await asyncio.wait_for(seen.wait(), 3)
    except asyncio.TimeoutError:
        pytest.skip("UDP loopback unavailable in this environment")
    found = b.lookup("111111111")
    assert found is not None and found[1] == 7070  # via broadcast or loopback
    assert a.lookup("111111111") is None  # own announcements are ignored
    await a.stop()
    await b.stop()


async def test_host_sends_box_address_for_wake(tmp_path):
    host, _ = make_core(tmp_path, "host", wol_box_address="mabox.ddns.net", wol_box_port=7)
    viewer, _ = make_core(tmp_path, "viewer")
    await host.start()
    await viewer.start()
    events = Recorder()
    session = await viewer.connect(f"127.0.0.1:{host.direct_port}", None, events)
    info = (await events.wait_for("info"))[1]
    if info["wol"] is None:
        pytest.skip("no network interface with a MAC address in this environment")
    assert info["wol"]["box"] == "mabox.ddns.net"
    assert info["wol"]["port"] == 7
    await session.close()
    await viewer.stop()
    await host.stop()


async def test_builtin_relay_address_is_never_shown(tmp_path, relay, monkeypatch):
    from brickmote.defaults import BuiltinRelay

    port, fp = relay
    monkeypatch.setattr(core_mod, "builtin_relay", lambda: BuiltinRelay("127.0.0.1", (port,), fp.replace(":", "").lower()))
    statuses = []

    host, host_ui = make_core(tmp_path, "host", direct_enabled=False)
    viewer, viewer_ui = make_core(tmp_path, "viewer", direct_enabled=False)
    viewer_ui.relay_status = lambda state, detail: statuses.append((state, detail))
    await host.start()
    await viewer.start()
    await wait_online(host)
    await wait_online(viewer)
    assert viewer.relay_masked
    session = await viewer.connect(host.identity.device_id, None, Recorder())
    assert session.peer.device_id == host.identity.device_id
    await session.close()
    await host.stop()
    # the relay goes away: the error must not reveal its address either
    await viewer._relay.stop()
    viewer._relay.online.set()
    viewer._relay._writer = None
    with pytest.raises(ConnectError) as err:
        await viewer.connect(host.identity.device_id, None, Recorder())
    shown = " ".join(d for _, d in statuses) + " " + str(err.value)
    assert statuses and "127.0.0.1" not in shown and str(port) not in shown
    await viewer.stop()


async def test_custom_relay_mode_shows_its_address(tmp_path, relay):
    port, fp = relay
    statuses = []
    viewer, viewer_ui = make_core(
        tmp_path, "viewer", relay_mode="custom", relay_host="127.0.0.1", relay_ports=[port], relay_fingerprint=fp
    )
    viewer_ui.relay_status = lambda state, detail: statuses.append((state, detail))
    await viewer.start()
    await wait_online(viewer)
    assert not viewer.relay_masked
    assert any("127.0.0.1" in d for s, d in statuses if s == "online")
    await viewer.stop()


async def test_second_connection_goes_direct_instead_of_relay(tmp_path, relay):
    port, fp = relay
    conf = {"relay_mode": "custom", "relay_host": "127.0.0.1", "relay_ports": [port], "relay_fingerprint": fp}
    host, host_ui = make_core(tmp_path, "host", **conf)
    viewer, _ = make_core(tmp_path, "viewer", direct_enabled=False, **conf)
    await host.start()
    await viewer.start()
    await wait_online(host)
    await wait_online(viewer)

    events = Recorder()
    first = await viewer.connect(host.identity.device_id, None, events)
    assert first.peer.address == "relais"
    await events.wait_for("info")
    learned = viewer.known_hosts.entries()[host.identity.device_id].get("direct")
    assert learned and learned["port"] == host.direct_port and learned["ips"]
    await first.close()

    second = await viewer.connect(host.identity.device_id, None, Recorder())
    assert second.peer.address != "relais", "the direct path must be preferred"
    await second.close()
    await viewer.stop()
    await host.stop()


async def test_race_connect_keeps_the_first_answer():
    from brickmote.core import _race_connect

    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    good = server.sockets[0].getsockname()[1]
    won = await _race_connect([("127.0.0.1", free_port()), ("127.0.0.1", good), ("10.255.255.1", 7070)], 1.0)
    assert won is not None and won[3] == good
    won[1].close()
    assert await _race_connect([("127.0.0.1", free_port())], 0.5) is None
    server.close()


async def test_viewers_see_each_others_pointers(tmp_path):
    host, host_ui = make_core(tmp_path, "host")
    alice, _ = make_core(tmp_path, "Alice")
    bob, _ = make_core(tmp_path, "Bob")
    seen_by_host = []
    host_ui.pointers_changed = seen_by_host.append
    for c in (host, alice, bob):
        await c.start()
    target = f"127.0.0.1:{host.direct_port}"
    a_events, b_events = Recorder(), Recorder()
    a = await alice.connect(target, None, a_events)
    b = await bob.connect(target, None, b_events)
    a_info = (await a_events.wait_for("info"))[1]
    b_info = (await b_events.wait_for("info"))[1]
    assert a_info["cursors"] and {a_info["color"], b_info["color"]} == {0, 1}

    # Bob only watches (no control needed to point): Alice sees his pointer
    b.view_only = True
    await b.mouse("move", 32768, 16384)
    while True:
        _, system, pointers, _local = await a_events.wait_for("cursors")
        if pointers:
            break
    color, x, y, name = pointers[0]
    assert (color, name) == (b_info["color"], "Bob")
    assert abs(x - 32768) < 200 and abs(y - 16384) < 200
    assert any(p and p[0][1] == "Bob" for p in seen_by_host)  # the person at the computer too

    # Alice moves the real cursor: Bob sees her colored pointer
    await a.mouse("move", 1000, 2000)
    while True:
        _, system, pointers, _local = await b_events.wait_for("cursors")
        if pointers and pointers[0][0] == a_info["color"]:
            break
    assert pointers[0][3] == "Alice"

    # Bob leaves the remote screen: his pointer disappears for Alice
    await b.pointer_out()
    while True:
        _, system, pointers, _local = await a_events.wait_for("cursors")
        if not any(p[0] == b_info["color"] for p in pointers):
            break
    await b.close()
    await a.close()
    for c in (alice, bob, host):
        await c.stop()
    assert host.cursor_hub.members == {}


async def test_presence_through_relay(tmp_path, relay):
    port, fp = relay
    conf = {"relay_mode": "custom", "relay_host": "127.0.0.1", "relay_ports": [port], "relay_fingerprint": fp}
    host, _ = make_core(tmp_path, "host", **conf)
    viewer, _ = make_core(tmp_path, "viewer", **conf)
    await host.start()
    await viewer.start()
    await wait_online(host)
    await wait_online(viewer)
    absent = "123456789" if host.identity.device_id != "123456789" else "987654321"
    status = await viewer.presence([host.identity.device_id, absent, "not-an-id"])
    assert status == {host.identity.device_id: True, absent: False}
    await host.stop()
    for _ in range(50):  # the relay notices the host left
        status = await viewer.presence([host.identity.device_id])
        if status[host.identity.device_id] is False:
            break
        await asyncio.sleep(0.1)
    assert status == {host.identity.device_id: False}
    await viewer.stop()


async def test_presence_unknown_without_relay(tmp_path):
    viewer, _ = make_core(tmp_path, "viewer")
    await viewer.start()
    assert await viewer.presence(["123456789"]) == {"123456789": None}
    await viewer.stop()


async def test_presence_with_an_older_relay(tmp_path, relay, monkeypatch):
    async def ignore(self, client, req):  # relays before 1.0.6 silently ignore the request
        return None

    monkeypatch.setattr(RelayServer, "_on_presence", ignore)
    port, fp = relay
    conf = {"relay_mode": "custom", "relay_host": "127.0.0.1", "relay_ports": [port], "relay_fingerprint": fp}
    viewer, _ = make_core(tmp_path, "viewer", **conf)
    await viewer.start()
    await wait_online(viewer)
    assert await viewer._relay.presence(["123456789"], timeout=0.3) is None
    assert viewer._relay._routes == {}  # nothing left waiting
    await viewer.stop()
