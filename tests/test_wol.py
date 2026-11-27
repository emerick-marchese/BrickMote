import asyncio
import socket

import pytest

from brickmote.net.wol import (
    WolError,
    check_host,
    format_mac,
    local_wol_info,
    magic_packet,
    merge_wol,
    parse_mac,
    parse_secureon,
    public_ipv4,
    sanitize_wol,
    send_magic_packet,
    send_wake,
    wake_targets,
)

from .test_session import make_sessions

MAC = "1C:69:7A:0B:12:EF"
MAC2 = "AA:BB:CC:DD:EE:02"


def test_parse_mac_formats():
    raw = bytes.fromhex("1C697A0B12EF")
    for text in ("1C:69:7A:0B:12:EF", "1c-69-7a-0b-12-ef", "1C69.7A0B.12EF", "1c697a0b12ef", " 1C 69 7A 0B 12 EF "):
        assert parse_mac(text) == raw
    assert format_mac("1c697a0b12ef") == MAC
    for bad in ("", "1C:69:7A:0B:12", "ZZ:69:7A:0B:12:EF", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "1C;69;7A;0B;12;EF"):
        with pytest.raises(WolError):
            parse_mac(bad)


def test_magic_packet_layout():
    pkt = magic_packet(MAC)
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:] == parse_mac(MAC) * 16
    assert magic_packet(MAC, "01:02:03:04:05:06")[-6:] == bytes([1, 2, 3, 4, 5, 6])
    assert magic_packet(MAC, "192.168.0.1")[-4:] == bytes([192, 168, 0, 1])
    with pytest.raises(WolError):
        parse_secureon("not-a-password")


def test_send_on_chosen_port():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    port = rx.getsockname()[1]
    sent = send_magic_packet(MAC, "127.0.0.1", port, repeat=3)
    assert sent == [f"127.0.0.1:{port}"]
    packets = [rx.recv(200) for _ in range(3)]
    rx.close()
    assert all(p == magic_packet(MAC) for p in packets)


def test_send_rejects_bad_input():
    with pytest.raises(WolError):
        send_magic_packet(MAC, "127.0.0.1", 0)
    with pytest.raises(WolError):
        send_magic_packet(MAC, "127.0.0.1", 70000)
    with pytest.raises(WolError):
        send_magic_packet(MAC, "nom-qui-n-existe-pas.invalid", 9)


def test_sanitize_wol_from_untrusted_host():
    assert sanitize_wol({"mac": "1c697a0b12ef", "broadcast": "192.168.1.255", "box": "MaBox.DDNS.net", "port": 7}) == {
        "mac": MAC,
        "macs": [MAC],
        "broadcast": "192.168.1.255",
        "box": "mabox.ddns.net",
        "port": 7,
    }
    evil = sanitize_wol(
        {"mac": MAC, "broadcast": "evil.example.com", "box": "a b; rm -rf", "port": 99999, "macs": ["x"] * 3 + [MAC2] + [MAC] * 40}
    )
    assert evil == {"mac": MAC, "macs": [MAC, MAC2], "broadcast": "255.255.255.255", "box": "", "port": 9}
    assert sanitize_wol({"mac": "<script>"}) is None
    assert sanitize_wol("nope") is None


def test_local_wol_info_shape():
    info = local_wol_info()
    if info is not None:
        assert parse_mac(info["mac"])
        assert info["broadcast"].count(".") == 3


async def test_host_shares_mac_with_viewer(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    host.wol_info = {
        "mac": "1c-69-7a-0b-12-ef",
        "broadcast": "192.168.1.255",
        "interface": "eth0",
        "box": "mabox.ddns.net",
        "port": 7,
    }
    info = (await vrec.wait_for("info"))[1]
    assert info["wol"] == {"mac": MAC, "macs": [MAC], "broadcast": "192.168.1.255", "box": "mabox.ddns.net", "port": 7}
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)


def _listener():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    return rx, rx.getsockname()[1]


def test_check_host():
    assert check_host("") == ""
    assert check_host(" 86.12.34.56 ") == "86.12.34.56"
    assert check_host("MaBox.FreeboxOS.fr.") == "mabox.freeboxos.fr"
    assert check_host("2001:db8::1") == "2001:db8::1"
    for bad in ("a b", "x" * 300, "box;rm", "-box.fr", "http://box.fr"):
        with pytest.raises(WolError):
            check_host(bad)


def test_wake_targets_box_first_then_lan():
    wol = {"mac": MAC, "box": "mabox.ddns.net", "address": "192.168.1.255", "local": True}
    assert wake_targets(wol) == ["mabox.ddns.net", "192.168.1.255", "255.255.255.255"]
    assert wake_targets({**wol, "local": False}) == ["mabox.ddns.net"]
    assert wake_targets({"mac": MAC}) == ["255.255.255.255"]


def test_send_wake_to_the_box_port():
    rx, port = _listener()
    sent, errors = send_wake({"mac": MAC, "box": "127.0.0.1", "port": port, "local": False})
    assert sent == [f"127.0.0.1:{port}"] and errors == []
    assert [rx.recv(200) for _ in range(3)] == [magic_packet(MAC)] * 3
    rx.close()


def test_unreachable_box_does_not_block_local_wake():
    rx, port = _listener()
    sent, errors = send_wake(
        {"mac": MAC, "box": "box-introuvable.invalid", "port": port, "local": True, "address": "127.0.0.1"}
    )
    assert f"127.0.0.1:{port}" in sent
    assert any("box-introuvable.invalid" in e for e in errors)
    assert rx.recv(200) == magic_packet(MAC)
    rx.close()
    with pytest.raises(WolError):
        send_wake({"mac": MAC, "box": "", "local": False})


def test_merge_keeps_manual_settings():
    learned = {"mac": MAC, "broadcast": "192.168.1.255", "box": "86.12.34.56", "port": 9}
    auto = merge_wol(None, learned)
    assert auto["box"] == "86.12.34.56" and auto["auto"] is True
    # the host's public IP changed: automatic settings follow it
    assert merge_wol(auto, {**learned, "box": "86.99.99.99"})["box"] == "86.99.99.99"
    # but a configuration typed by the user is kept, MAC included (copied from
    # a phone app: the card BrickMote saw may be a VPN / Wi-Fi card); the
    # computer's cards are only added as extra destinations
    manual = {**auto, "box": "mabox.ddns.net", "port": 7, "auto": False}
    merged = merge_wol(manual, {**learned, "mac": "AA:BB:CC:DD:EE:01", "macs": ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]})
    assert merged["box"] == "mabox.ddns.net" and merged["port"] == 7
    assert merged["mac"] == MAC
    assert merged["extra_macs"] == ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]


def test_public_ipv4():
    assert public_ipv4("::ffff:8.8.8.8") == "8.8.8.8"
    assert public_ipv4("86.12.34.56") == "86.12.34.56"
    for private in ("192.168.1.2", "10.0.0.1", "127.0.0.1", "100.64.0.1", "::1", None, "x"):
        assert public_ipv4(private) is None


def test_ipv4_is_preferred_and_all_addresses_are_used(monkeypatch):
    import brickmote.net.wol as wol

    fake = [
        (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::1", 9, 0, 0)),
        (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 9)),
    ]
    monkeypatch.setattr(wol.socket, "getaddrinfo", lambda *a, **k: fake)
    order = [s[0] for _, s in wol._resolve("mabox.ddns.net", 9)]
    assert order == ["127.0.0.1", "2001:db8::1"]


def test_box_hostname_reaches_its_ipv4(monkeypatch):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    port = rx.getsockname()[1]
    sent, errors = send_wake({"mac": MAC, "box": "localhost", "port": port, "local": False})
    assert sent and rx.recv(200) == magic_packet(MAC)
    rx.close()


def test_wake_reaches_every_card_of_the_computer(monkeypatch):
    import brickmote.net.wol as wol_mod

    sent = []
    monkeypatch.setattr(wol_mod, "_send", lambda packet, targets, port, repeat: (sent.append(packet[6:12]) or ["x"], []))
    wol_mod.send_wake({"mac": MAC, "extra_macs": ["AA:BB:CC:DD:EE:01", "pas une mac"], "box": "1.2.3.4", "port": 9})
    assert sent == [bytes.fromhex("1C697A0B12EF"), bytes.fromhex("AABBCCDDEE01")]


def test_cards_ignore_virtual_adapters_and_prefer_the_internet_card():
    import socket
    from types import SimpleNamespace as NS

    import brickmote.net.wol as wol_mod

    link = getattr(socket, "AF_PACKET", None) or -1
    fake = NS(
        AF_LINK=link,
        net_if_addrs=lambda: {
            "VMware Network Adapter VMnet8": [NS(family=link, address="00-50-56-C0-00-08"),
                                              NS(family=socket.AF_INET, address="192.168.155.1", broadcast=None, netmask="255.255.255.0")],
            "Radmin VPN": [NS(family=link, address="02-50-F2-00-00-01"),
                           NS(family=socket.AF_INET, address="10.60.62.42", broadcast=None, netmask="255.0.0.0")],
            "Wi-Fi": [NS(family=link, address="3C-A9-F4-11-22-33"),
                      NS(family=socket.AF_INET, address="192.168.1.30", broadcast=None, netmask="255.255.255.0")],
            "Ethernet": [NS(family=link, address="1C-69-7A-0B-12-EF"),
                         NS(family=socket.AF_INET, address="192.168.1.20", broadcast=None, netmask="255.255.255.0")],
        },
        net_if_stats=lambda: {},
    )
    cards = wol_mod._cards(fake)
    assert [c["name"] for c in cards] == ["Wi-Fi", "Ethernet"], "VM and VPN cards cannot wake a PC"
    ranked = sorted(cards, key=lambda c: wol_mod._rank(c, "192.168.1.20"))
    assert ranked[0]["mac"] == MAC and ranked[0]["broadcast"] == "192.168.1.255"
    # Internet through Wi-Fi: Wi-Fi first, Ethernet still in the list
    ranked = sorted(cards, key=lambda c: wol_mod._rank(c, "192.168.1.30"))
    assert [c["name"] for c in ranked] == ["Wi-Fi", "Ethernet"]
