import asyncio
import socket

import pytest

from brickmote.net.wol import (
    WolError,
    format_mac,
    local_wol_info,
    magic_packet,
    parse_mac,
    parse_secureon,
    sanitize_wol,
    send_magic_packet,
)

from .test_session import make_sessions

MAC = "1C:69:7A:0B:12:EF"


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
    assert sanitize_wol({"mac": "1c697a0b12ef", "broadcast": "192.168.1.255"}) == {
        "mac": MAC,
        "broadcast": "192.168.1.255",
    }
    assert sanitize_wol({"mac": MAC, "broadcast": "evil.example.com"})["broadcast"] == "255.255.255.255"
    assert sanitize_wol({"mac": "<script>"}) is None
    assert sanitize_wol("nope") is None


def test_local_wol_info_shape():
    info = local_wol_info()
    if info is not None:
        assert parse_mac(info["mac"])
        assert info["broadcast"].count(".") == 3


async def test_host_shares_mac_with_viewer(tmp_path):
    host, viewer, hrec, vrec, tasks = await make_sessions(tmp_path)
    host.wol_info = {"mac": "1c-69-7a-0b-12-ef", "broadcast": "192.168.1.255", "interface": "eth0"}
    info = (await vrec.wait_for("info"))[1]
    assert info["wol"] == {"mac": MAC, "broadcast": "192.168.1.255"}
    await viewer.close()
    await asyncio.gather(*tasks, return_exceptions=True)
