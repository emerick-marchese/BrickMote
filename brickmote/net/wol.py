"""Wake-on-LAN: build and send "magic packets", find this PC's MAC address.

A magic packet is 6 x 0xFF followed by the target MAC address repeated 16
times, optionally followed by a SecureOn password (4 or 6 bytes). It is sent
over UDP (port 9 by default, any port can be chosen) to a broadcast address
on the local network, or to a public address/hostname whose router forwards
that UDP port to the local network (like phone Wake-on-LAN apps).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Optional

DEFAULT_PORT = 9
DEFAULT_ADDRESS = "255.255.255.255"

_HEX = re.compile(r"[^0-9A-Fa-f]")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.?$")


class WolError(ValueError):
    pass


def parse_mac(text: str) -> bytes:
    """Accept AA:BB:CC:DD:EE:FF, AA-BB-..., AABB.CCDD.EEFF or AABBCCDDEEFF."""
    if not isinstance(text, str):
        raise WolError("adresse MAC invalide")
    cleaned = text.strip()
    if not re.fullmatch(r"[0-9A-Fa-f:.\- ]+", cleaned):
        raise WolError("adresse MAC invalide")
    digits = _HEX.sub("", cleaned)
    if len(digits) != 12:
        raise WolError("une adresse MAC comporte 12 chiffres hexadécimaux (ex. 1C:69:7A:0B:12:EF)")
    mac = bytes.fromhex(digits)
    if mac == b"\x00" * 6 or mac == b"\xff" * 6:
        raise WolError("adresse MAC invalide")
    return mac


def format_mac(mac: bytes | str) -> str:
    raw = parse_mac(mac) if isinstance(mac, str) else mac
    return ":".join(f"{b:02X}" for b in raw)


def parse_secureon(text: str) -> bytes:
    """SecureOn password: 6 bytes written like a MAC, or 4 bytes like an IPv4."""
    text = (text or "").strip()
    if not text:
        return b""
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text):
        try:
            return ipaddress.IPv4Address(text).packed
        except ValueError as exc:
            raise WolError("mot de passe SecureOn invalide") from exc
    digits = _HEX.sub("", text)
    if len(digits) != 12 or not re.fullmatch(r"[0-9A-Fa-f:.\- ]+", text):
        raise WolError("mot de passe SecureOn : 6 octets (AA:BB:CC:DD:EE:FF) ou 4 octets (192.168.0.1)")
    return bytes.fromhex(digits)


def magic_packet(mac: str | bytes, secureon: str = "") -> bytes:
    raw = parse_mac(mac) if isinstance(mac, str) else mac
    if len(raw) != 6:
        raise WolError("adresse MAC invalide")
    return b"\xff" * 6 + raw * 16 + parse_secureon(secureon)


def check_port(port: object) -> int:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise WolError("port UDP invalide (1 à 65535)")
    return port


def check_host(text: object) -> str:
    """Validate the box address: an IP address or a (DynDNS) host name. '' = none."""
    if not isinstance(text, str):
        raise WolError("adresse de la box invalide")
    text = text.strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text.strip("[]")))
    except ValueError:
        pass
    if _HOSTNAME.match(text):
        return text.lower().rstrip(".")
    raise WolError("adresse de la box invalide (IP publique ou nom, ex. mabox.ddns.net)")


def wake_targets(wol: dict) -> list[str]:
    """Destinations of a wake: the box (public address) first, then the LAN broadcast."""
    targets: list[str] = []
    box = (wol.get("box") or "").strip()
    if box:
        targets.append(box)
    if wol.get("local", True):
        for addr in (wol.get("address") or DEFAULT_ADDRESS, DEFAULT_ADDRESS):
            if addr and addr not in targets:
                targets.append(addr)
    return targets


def _interface_broadcasts() -> list[tuple[str, str]]:
    """(local IPv4, directed broadcast) of every network card: Windows sends a
    limited broadcast (255.255.255.255) on a single card, often a virtual
    one (VMware, VirtualBox, Hyper-V...), so we also broadcast on each card."""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family != socket.AF_INET or not a.address or a.address.startswith("127."):
                continue
            bcast = a.broadcast
            if not bcast and a.netmask:
                try:
                    bcast = str(ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False).broadcast_address)
                except ValueError:
                    bcast = None
            if bcast:
                out.append((a.address, bcast))
    return out


def _resolve(addr: str, port: int) -> list[tuple]:
    """Every address of ``addr``, IPv4 first (port forwards on boxes are IPv4)."""
    infos = socket.getaddrinfo(addr, port, type=socket.SOCK_DGRAM)
    seen, out = set(), []
    for family, _, _, _, sockaddr in sorted(infos, key=lambda i: i[0] != socket.AF_INET):
        if sockaddr[0] not in seen:
            seen.add(sockaddr[0])
            out.append((family, sockaddr))
    return out


def _send(packet: bytes, targets: list[str], port: int, repeat: int) -> tuple[list[str], list[str]]:
    sent, errors = [], []
    for addr in targets:
        try:
            resolved = _resolve(addr, port)
        except socket.gaierror as exc:
            errors.append(f"{addr} : nom introuvable ({exc.strerror or exc})")
            continue
        ok = []
        for family, sockaddr in resolved:
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as sock:
                    if family == socket.AF_INET:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    for _ in range(max(1, repeat)):
                        sock.sendto(packet, sockaddr)
                ok.append(sockaddr[0])
            except OSError as exc:
                errors.append(f"{addr} ({sockaddr[0]}) : {exc.strerror or exc}")
        if addr == DEFAULT_ADDRESS:
            for local_ip, bcast in _interface_broadcasts():
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        sock.bind((local_ip, 0))
                        for _ in range(max(1, repeat)):
                            sock.sendto(packet, (bcast, port))
                    ok.append(bcast)
                except OSError:
                    pass  # disconnected or virtual card: ignore
        if ok:
            shown = addr if ok == [addr] else f"{addr} → {', '.join(dict.fromkeys(ok))}"
            sent.append(f"{shown}:{port}" if ok == [addr] else f"{shown} (port {port})")
    return sent, errors


def send_wake(wol: dict, repeat: int = 3) -> tuple[list[str], list[str]]:
    """Send a wake for a stored configuration ({mac, box, port, address, local,
    secureon}). Returns (destinations reached, errors)."""
    packet = magic_packet(wol.get("mac", ""), wol.get("secureon", ""))
    port = check_port(int(wol.get("port") or DEFAULT_PORT))
    targets = wake_targets(wol)
    if not targets:
        raise WolError("aucune destination : renseignez l'adresse de la box ou activez le réseau local")
    sent, errors = _send(packet, targets, port, repeat)
    if not sent:
        raise WolError("envoi impossible — " + " ; ".join(errors))
    return sent, errors


def send_magic_packet(
    mac: str,
    address: str = DEFAULT_ADDRESS,
    port: int = DEFAULT_PORT,
    secureon: str = "",
    repeat: int = 3,
    extra_addresses: tuple[str, ...] = (),
) -> list[str]:
    """Send the packet (several times, UDP is lossy). Returns the targets used.

    ``address`` may be a broadcast address (255.255.255.255, 192.168.1.255),
    an IP or a hostname (e.g. your router's public name with UDP forwarding).
    """
    packet = magic_packet(mac, secureon)
    port = check_port(port)
    targets = []
    for addr in (address or DEFAULT_ADDRESS, *extra_addresses):
        addr = (addr or "").strip()
        if addr and addr not in targets:
            targets.append(addr)
    sent, errors = _send(packet, targets, port, repeat)
    if not sent:
        raise WolError("envoi impossible — " + " ; ".join(errors))
    return sent


def local_ipv4s() -> list[str]:
    """This computer's IPv4 addresses on real networks (loopback and link-local
    excluded), the one used to reach the Internet first."""
    out = []
    primary = _primary_ipv4()
    if primary:
        out.append(primary)
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.address not in out:
                    ip = ipaddress.IPv4Address(a.address)
                    if not (ip.is_loopback or ip.is_link_local):
                        out.append(a.address)
    except (ImportError, ValueError):
        pass
    return out[:8]


def _primary_ipv4() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return None


def local_wol_info() -> Optional[dict]:
    """MAC address and broadcast address of the interface used on the LAN."""
    ip = _primary_ipv4()
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None and ip:
        link_families = {getattr(psutil, "AF_LINK", None), getattr(socket, "AF_PACKET", None)} - {None}
        for name, addrs in psutil.net_if_addrs().items():
            v4 = next((a for a in addrs if a.family == socket.AF_INET and a.address == ip), None)
            if v4 is None:
                continue
            link = next((a for a in addrs if a.family in link_families), None)
            if link is None:
                continue
            try:
                mac = format_mac(link.address)
            except WolError:
                continue
            broadcast = v4.broadcast
            if not broadcast and v4.netmask:
                try:
                    broadcast = str(ipaddress.IPv4Network(f"{ip}/{v4.netmask}", strict=False).broadcast_address)
                except ValueError:
                    broadcast = None
            return {"mac": mac, "broadcast": broadcast or DEFAULT_ADDRESS, "interface": name, "ip": ip}
    # Fallback without psutil: uuid.getnode() (ignored when it is a random value)
    import uuid

    node = uuid.getnode()
    if node >> 40 & 0x01:
        return None
    return {"mac": format_mac(node.to_bytes(6, "big")), "broadcast": DEFAULT_ADDRESS, "interface": "", "ip": ip or ""}


def sanitize_wol(value: object) -> Optional[dict]:
    """Validate Wake-on-LAN details received from a remote host."""
    if not isinstance(value, dict):
        return None
    try:
        mac = format_mac(str(value.get("mac", "")))
    except WolError:
        return None
    broadcast = value.get("broadcast")
    try:
        broadcast = str(ipaddress.IPv4Address(broadcast)) if broadcast else DEFAULT_ADDRESS
    except ValueError:
        broadcast = DEFAULT_ADDRESS
    try:
        box = check_host(value.get("box") or "")
    except WolError:
        box = ""
    port = value.get("port")
    port = port if isinstance(port, int) and 1 <= port <= 65535 else DEFAULT_PORT
    return {"mac": mac, "broadcast": broadcast, "box": box, "port": port}


def merge_wol(current: Optional[dict], learned: dict) -> dict:
    """Combine what a host told us with what we already have.

    Settings the user edited by hand ("auto": False) are kept; only the MAC
    address is refreshed. Otherwise the host's latest values win.
    """
    fresh = {
        "mac": learned["mac"],
        "address": learned.get("broadcast") or DEFAULT_ADDRESS,
        "box": learned.get("box", ""),
        "port": learned.get("port") or DEFAULT_PORT,
        "local": True,
        "secureon": "",
        "auto": True,
    }
    if not current:
        return fresh
    if current.get("auto") is False:
        return {**current, "mac": learned["mac"]}
    return {**fresh, "secureon": current.get("secureon", ""), "local": current.get("local", True)}


def public_ipv4(addr: object) -> Optional[str]:
    """Return ``addr`` if it is a public IPv4 address (as seen by the relay)."""
    if not isinstance(addr, str):
        return None
    addr = addr.removeprefix("::ffff:")
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return None
    return str(ip) if ip.is_global else None
