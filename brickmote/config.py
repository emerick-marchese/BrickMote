"""Persistent settings, known-host pins and audit log."""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from .constants import DIRECT_PORT, RELAY_PORTS
from .crypto.password import PasswordVerifier
from .util import b64d, b64e, secure_write


def config_dir() -> Path:
    override = os.environ.get("BRICKMOTE_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home())) / "BrickMote"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BrickMote"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "brickmote"


def downloads_dir() -> Path:
    base = Path.home() / "Downloads"
    if not base.exists():
        base = Path.home()
    return base / "BrickMote"


def _default_perms(control: bool = True) -> dict:
    return {"control": control, "clipboard": True, "files": False}


@dataclass
class Settings:
    device_name: str = field(default_factory=lambda: platform.node() or "BrickMote")
    # Relay / rendezvous server (self-hosted). Empty host = LAN/direct only.
    relay_host: str = ""
    relay_ports: list = field(default_factory=lambda: list(RELAY_PORTS))
    relay_fingerprint: str = ""  # SHA-256 of the relay TLS certificate (pinning)
    # Direct connections (TCP 7070) and LAN discovery (UDP 50001)
    direct_enabled: bool = True
    direct_port: int = DIRECT_PORT
    discovery_enabled: bool = True
    # Interactive access (someone in front of the screen accepts)
    interactive_enabled: bool = True
    interactive_permissions: dict = field(default_factory=_default_perms)
    # Unattended access (password, optionally + TOTP)
    unattended_enabled: bool = False
    password: Optional[dict] = None
    totp_secret: Optional[str] = None
    unattended_permissions: dict = field(default_factory=_default_perms)
    # Access control list: only these IDs may connect (when enabled)
    acl_enabled: bool = False
    acl: list = field(default_factory=list)  # [{"id": "...", "note": "..."}]
    blocked: list = field(default_factory=list)  # list of IDs
    # Display
    quality: str = "balanced"  # low | balanced | high
    fps: int = 20

    def password_verifier(self) -> Optional[PasswordVerifier]:
        if not self.password:
            return None
        try:
            return PasswordVerifier.from_dict(self.password)
        except (KeyError, ValueError, TypeError):
            return None

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known})
        s.validate()
        return s

    def validate(self) -> None:
        self.relay_ports = [p for p in self.relay_ports if isinstance(p, int) and 0 < p < 65536] or list(
            RELAY_PORTS
        )
        if not isinstance(self.direct_port, int) or not 0 < self.direct_port < 65536:
            self.direct_port = DIRECT_PORT
        if self.quality not in ("low", "balanced", "high"):
            self.quality = "balanced"
        self.fps = max(1, min(60, int(self.fps))) if isinstance(self.fps, int) else 20
        for name in ("interactive_permissions", "unattended_permissions"):
            perms = getattr(self, name)
            if not isinstance(perms, dict):
                setattr(self, name, _default_perms())
        if self.unattended_enabled and self.password_verifier() is None:
            self.unattended_enabled = False

    def save(self, path: Path) -> None:
        secure_write(Path(path), json.dumps(asdict(self), indent=2).encode("utf-8"))


class KnownHosts:
    """Viewer-side pinning of host keys (trust on first use + ID binding)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, device_id: str) -> Optional[bytes]:
        entry = self._data.get(device_id)
        if not isinstance(entry, dict):
            return None
        try:
            return b64d(entry.get("pub"), 32)
        except ValueError:
            return None

    def entries(self) -> dict:
        return dict(self._data)

    def remember(self, device_id: str, public_key: bytes, name: str, address: Optional[str] = None) -> None:
        with self._lock:
            entry = self._data.get(device_id) or {"first_seen": int(time.time())}
            entry.update({"pub": b64e(public_key), "name": name, "last_seen": int(time.time())})
            if address:
                entry["address"] = address
            self._data[device_id] = entry
            secure_write(self.path, json.dumps(self._data, indent=2).encode("utf-8"))

    def forget(self, device_id: str) -> None:
        with self._lock:
            self._data.pop(device_id, None)
            secure_write(self.path, json.dumps(self._data, indent=2).encode("utf-8"))


class AuditLog:
    """Append-only security log of every connection attempt."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: str, **info) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **info}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                if new:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
            except OSError:
                pass

    def tail(self, count: int = 200) -> list[dict]:
        try:
            lines = self.path.read_text("utf-8").splitlines()[-count:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


class Paths:
    def __init__(self, base: Optional[Path] = None):
        self.base = Path(base) if base else config_dir()
        self.identity = self.base / "identity.pem"
        self.settings = self.base / "settings.json"
        self.known_hosts = self.base / "known_hosts.json"
        self.audit = self.base / "audit.log"
