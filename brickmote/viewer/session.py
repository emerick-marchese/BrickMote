"""Viewer side of a session: receives frames, sends input."""

from __future__ import annotations

from pathlib import Path

from ..crypto.handshake import HandshakeResult, sanitize_permissions
from ..host.capture import unpack_frame
from ..host.inputs import BUTTONS, COORD_MAX, SPECIAL_KEYS
from ..net.wol import sanitize_wol
from ..session import PeerSession, SessionEvents


class ViewerSession(PeerSession):
    role = "viewer"

    def __init__(self, result: HandshakeResult, events: SessionEvents, downloads: Path):
        super().__init__(result, events, downloads)
        self.info: dict = {}
        self.frames_received = 0
        self.view_only = False
        self.direct_hook = None  # set by the core: remembers how to reach the host directly

    async def start(self) -> None:
        await self.send_json({"t": "ready", "v": 1})

    async def handle_frame(self, data: bytes) -> None:
        fid, fw, fh, cursor, rects = unpack_frame(data)
        self.frames_received += 1
        self.events.on_frame(self, fid, fw, fh, cursor, rects)
        await self.send_json({"t": "ack", "fid": fid})

    async def handle_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "info":
            monitors = []
            for m in msg.get("monitors") or []:
                if isinstance(m, dict) and all(isinstance(m.get(k), int) for k in ("index", "width", "height")):
                    monitors.append({"index": m["index"], "width": m["width"], "height": m["height"]})
            self.info = {
                "monitors": monitors[:16],
                "monitor": msg.get("monitor") if isinstance(msg.get("monitor"), int) else 1,
                "quality": msg.get("quality") if isinstance(msg.get("quality"), str) else "balanced",
                "fps": msg.get("fps") if isinstance(msg.get("fps"), int) else 20,
                "wol": sanitize_wol(msg.get("wol")),
                "cursors": msg.get("cursors") is True,
                "color": msg.get("color") if _is_color(msg.get("color")) else None,
            }
            self.permissions = sanitize_permissions(msg.get("perms"))
            direct = _sanitize_direct(msg.get("direct"))
            if direct and self.direct_hook is not None:
                try:
                    self.direct_hook(direct)
                except Exception:  # noqa: BLE001 - best effort
                    pass
            self.events.on_info(self, dict(self.info))
            self.events.on_permissions(self, dict(self.permissions))
        elif t == "cursors":
            parsed = _sanitize_cursors(msg)
            if parsed is not None:
                self.events.on_cursors(self, *parsed)
        elif t == "status":
            status = msg.get("s")
            if status in ("ok", "locked", "unavailable", "secure", "uac"):
                self.events.on_status(self, status)
        elif t == "perm":
            self.permissions = sanitize_permissions(msg.get("perms"))
            self.events.on_permissions(self, dict(self.permissions))

    # ------------------------------------------------------------- input
    def _can_control(self) -> bool:
        return self.permissions.get("control", False) and not self.view_only

    async def mouse(self, action: str, nx: int, ny: int, button: str | None = None) -> None:
        if not self._can_control():
            if action == "move":  # watching only: still show the others where we point
                await self.send_json({"t": "ptr", "x": _clamp(nx), "y": _clamp(ny)})
            return
        msg = {"t": "mouse", "a": action, "x": _clamp(nx), "y": _clamp(ny)}
        if action in ("down", "up"):
            if button not in BUTTONS:
                return
            msg["b"] = button
        await self.send_json(msg)

    async def pointer_out(self) -> None:
        """Our mouse left the remote screen: hide our pointer for the others."""
        await self.send_json({"t": "ptr", "out": True})

    async def wheel(self, dx: int, dy: int) -> None:
        if self._can_control():
            await self.send_json({"t": "wheel", "dx": int(dx), "dy": int(dy)})

    async def key(self, down: bool, key: str | None = None, char: str | None = None) -> None:
        if not self._can_control():
            return
        msg = {"t": "key", "a": "down" if down else "up"}
        if key in SPECIAL_KEYS:
            msg["key"] = key
        elif isinstance(char, str) and len(char) == 1:
            msg["char"] = char
        else:
            return
        await self.send_json(msg)

    async def set_monitor(self, index: int) -> None:
        await self.send_json({"t": "monitor", "index": int(index)})

    async def set_quality(self, quality: str) -> None:
        await self.send_json({"t": "quality", "q": quality})

    async def set_viewport(self, width: int) -> None:
        """Width (physical pixels) at which the remote screen is displayed: the
        host does not send more pixels than that."""
        await self.send_json({"t": "viewport", "w": max(320, min(16384, int(width)))})

    async def set_fps(self, fps: int) -> None:
        await self.send_json({"t": "fps", "fps": int(fps)})

    async def refresh(self) -> None:
        await self.send_json({"t": "refresh"})


def _clamp(v) -> int:
    return max(0, min(COORD_MAX, int(v)))


def _is_coord(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= COORD_MAX


def _is_color(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 8


def _sanitize_cursors(msg: dict):
    """Pointers announced by the host (untrusted input)."""
    system = msg.get("sys")
    if not (isinstance(system, list) and len(system) == 2 and all(_is_coord(v) for v in system)):
        system = None
    pointers = []
    raw = msg.get("p")
    if not isinstance(raw, list):
        return None
    for item in raw[:16]:
        if not (isinstance(item, list) and len(item) == 4):
            continue
        color, x, y, name = item
        if _is_color(color) and _is_coord(x) and _is_coord(y) and isinstance(name, str):
            name = "".join(ch for ch in name if ch.isprintable()).strip()[:32] or "Utilisateur"
            pointers.append((color, x, y, name))
    return (tuple(system) if system else None), pointers, msg.get("local") is True


def _sanitize_direct(value: object):
    """Direct addresses announced by the host (untrusted input)."""
    import ipaddress

    if not isinstance(value, dict) or not isinstance(value.get("port"), int) or not 0 < value["port"] < 65536:
        return None
    ips = []
    for ip in value.get("ips") or []:
        try:
            addr = ipaddress.IPv4Address(ip)
        except (ValueError, TypeError):
            continue
        if not (addr.is_loopback or addr.is_multicast or addr.is_unspecified) and str(addr) not in ips:
            ips.append(str(addr))
    public = ""
    try:
        pub = ipaddress.IPv4Address(value.get("public") or "")
        if pub.is_global:
            public = str(pub)
    except ValueError:
        pass
    if not ips and not public:
        return None
    return {"port": value["port"], "ips": ips[:8], "public": public}
