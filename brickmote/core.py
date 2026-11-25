"""UI-independent engine: identity, settings, listeners, relay, sessions."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from typing import Awaitable, Callable, Optional, Protocol

from .credentials import SavedPasswords
from .config import AuditLog, KnownHosts, Paths, Settings, downloads_dir
from .constants import DIRECT_PORT
from .crypto.handshake import (
    HandshakeError,
    PeerInfo,
    initiator_handshake,
    responder_handshake,
)
from .crypto.identity import Identity
from .host.authority import HostPolicy
from .host.session import HostSession
from .net.discovery import Discovery
from .net.framing import close_writer
from .defaults import DISPLAY_NAME, builtin_relay
from .net.relay_client import RelayClient, RelayError
from .net.wol import local_wol_info
from .session import SessionEvents
from .util import RateLimiter, normalize_id
from .viewer.session import ViewerSession

log = logging.getLogger(__name__)

MAX_HOST_SESSIONS = 4
MAX_PENDING_HANDSHAKES = 8


class CoreUI(Protocol):
    """Implemented by the user interface. Called from the network thread."""

    host_events: SessionEvents

    async def prompt_incoming(self, peer: PeerInfo, sas: str) -> Optional[dict]: ...
    async def ask_totp(self, peer: PeerInfo) -> Optional[str]: ...
    async def confirm_new_host(self, peer: PeerInfo) -> bool: ...
    async def ask_password(self, peer: PeerInfo, interactive_ok: bool) -> Optional[dict]: ...
    def relay_status(self, state: str, detail: str) -> None: ...
    def host_session_started(self, session: HostSession) -> None: ...
    def lan_peers_changed(self, peers: dict) -> None: ...


class ConnectError(Exception):
    pass


_HOSTPORT = re.compile(r"^\[?([0-9A-Za-z.\-:%]+?)\]?(?::(\d{1,5}))?$")


def parse_address(text: str) -> Optional[tuple[str, int]]:
    text = text.strip()
    if text.count(":") > 1 and not text.startswith("["):
        return text, DIRECT_PORT  # bare IPv6
    m = _HOSTPORT.match(text)
    if not m:
        return None
    port = int(m.group(2)) if m.group(2) else DIRECT_PORT
    if not 0 < port < 65536:
        return None
    return m.group(1), port


class BrickMoteCore:
    def __init__(self, paths: Paths, ui: CoreUI):
        self.paths = paths
        self.ui = ui
        self.identity = Identity.load_or_create(paths.identity)
        self.settings = Settings.load(paths.settings)
        self.known_hosts = KnownHosts(paths.known_hosts)
        self.saved_passwords = SavedPasswords(paths.saved_passwords)
        self.audit = AuditLog(paths.audit)
        self.policy = HostPolicy(self.identity, lambda: self.settings, self.audit, prompt=ui.prompt_incoming)
        self.host_sessions: set[HostSession] = set()
        self.viewer_sessions: set[ViewerSession] = set()
        self._direct: Optional[asyncio.base_events.Server] = None
        self._direct_port: Optional[int] = None
        self._relay: Optional[RelayClient] = None
        self._relay_conf: Optional[tuple] = None
        self.builtin_relay = builtin_relay()
        self._discovery: Optional[Discovery] = None
        self._ip_limiter = RateLimiter(limit=20, window=60)
        self._pending = 0

    # ----------------------------------------------------------- settings
    def save_settings(self) -> None:
        self.settings.validate()
        self.settings.save(self.paths.settings)

    async def start(self) -> None:
        self.audit.write("start", id=self.identity.device_id)
        await self.apply_settings()

    async def apply_settings(self) -> None:
        s = self.settings
        # direct listener (TCP 7070)
        want_port = s.direct_port if s.direct_enabled else None
        if want_port != self._direct_port:
            if self._direct is not None:
                self._direct.close()
                self._direct = None
                self._direct_port = None
            if want_port:
                try:
                    self._direct = await asyncio.start_server(self._on_direct, None, want_port)
                    self._direct_port = want_port
                except OSError as exc:
                    log.warning("cannot listen on TCP %s: %s", want_port, exc)
        # relay
        conf = self._relay_config()
        if conf != self._relay_conf:
            if self._relay is not None:
                await self._relay.stop()
                self._relay = None
            self._relay_conf = conf
            if conf:
                host, ports, fingerprint, masked = conf
                self._relay = RelayClient(
                    self.identity,
                    host,
                    list(ports),
                    fingerprint,
                    self._on_relay_incoming,
                    self.ui.relay_status,
                    display_name=DISPLAY_NAME if masked else None,
                )
                self._relay.start()
            else:
                self.ui.relay_status("disabled", "")
        # LAN discovery (UDP 50001)
        if s.discovery_enabled and self._discovery is None:
            disc = Discovery(
                self.identity.device_id,
                lambda: self._direct_port,
                on_change=lambda: self.ui.lan_peers_changed(dict(self._discovery.peers) if self._discovery else {}),
            )
            try:
                await disc.start()
                self._discovery = disc
            except OSError as exc:
                log.warning("LAN discovery unavailable: %s", exc)
        elif not s.discovery_enabled and self._discovery is not None:
            await self._discovery.stop()
            self._discovery = None
            self.ui.lan_peers_changed({})

    def _relay_config(self) -> Optional[tuple]:
        """(host, ports, fingerprint, masked) of the relay to use, or None."""
        s = self.settings
        if s.relay_mode == "off":
            return None
        if s.relay_mode == "custom":
            host = s.relay_host.strip()
            return (host, tuple(s.relay_ports), s.relay_fingerprint.strip(), False) if host else None
        builtin = self.builtin_relay
        if builtin is None:
            return None
        return (builtin.host, builtin.ports, builtin.fingerprint, True)

    @property
    def relay_masked(self) -> bool:
        return bool(self._relay_conf and self._relay_conf[3])

    @property
    def direct_port(self) -> Optional[int]:
        return self._direct_port

    @property
    def public_ip(self) -> Optional[str]:
        """Public IPv4 of our box, as observed by the relay server (if any)."""
        return self._relay.public_ip if self._relay is not None else None

    async def stop(self) -> None:
        for session in list(self.host_sessions) + list(self.viewer_sessions):
            await session.close("fermeture de BrickMote")
        if self._direct is not None:
            self._direct.close()
        if self._relay is not None:
            await self._relay.stop()
        if self._discovery is not None:
            await self._discovery.stop()
        self.audit.write("stop")

    # ---------------------------------------------------------- host side
    async def _on_direct(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if not self._ip_limiter.allow(ip):
            self.audit.write("refused", ip=ip, reason="rate_limited")
            await close_writer(writer)
            return
        await self._serve_host(reader, writer, ip)

    async def _on_relay_incoming(self, reader, writer, from_id: str, from_pub: bytes) -> None:
        await self._serve_host(reader, writer, f"relais ({from_id})")

    async def _serve_host(self, reader, writer, address: str) -> None:
        if len(self.host_sessions) >= MAX_HOST_SESSIONS or self._pending >= MAX_PENDING_HANDSHAKES:
            await close_writer(writer)
            return
        self._pending += 1
        try:
            result = await responder_handshake(reader, writer, self.policy, address)
        except HandshakeError as exc:
            log.info("incoming connection from %s refused: %s", address, exc.code)
            await close_writer(writer)
            return
        except Exception:  # noqa: BLE001
            log.exception("handshake crashed")
            await close_writer(writer)
            return
        finally:
            self._pending -= 1
        wol = None
        if self.settings.share_wol_info:
            try:
                wol = await asyncio.to_thread(local_wol_info)
            except Exception:  # noqa: BLE001 - best effort, never blocks a session
                wol = None
            if wol is not None:
                wol["box"] = self.settings.wol_box_address.strip() or (self.public_ip or "")
                wol["port"] = self.settings.wol_box_port
        session = HostSession(
            result,
            self.ui.host_events,
            downloads_dir(),
            quality=self.settings.quality,
            fps=self.settings.fps,
            wol_info=wol,
        )
        self.host_sessions.add(session)
        self.audit.write("session_start", peer=result.peer.device_id, ip=address, method=result.method)
        try:
            self.ui.host_session_started(session)
            await session.run()
        finally:
            self.host_sessions.discard(session)
            self.audit.write("session_end", peer=result.peer.device_id, reason=session.close_reason)

    # -------------------------------------------------------- viewer side
    def lan_peers(self) -> dict:
        return dict(self._discovery.peers) if self._discovery else {}

    async def _routes(self, target: str):
        """Yield (reader, writer, expected_id, expected_pub, address) candidates."""
        device_id = normalize_id(target)
        if device_id is None:
            parsed = parse_address(target)
            if parsed is None:
                raise ConnectError("Adresse invalide. Saisissez un ID à 9 chiffres ou une adresse IP[:port].")
            host, port = parsed
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 8)
            yield reader, writer, None, None, f"{host}:{port}"
            return
        if device_id == self.identity.device_id:
            raise ConnectError("Vous ne pouvez pas vous connecter à vous-même.")
        tried = set()
        lan = self._discovery.lookup(device_id) if self._discovery else None
        known = self.known_hosts.entries().get(device_id, {})
        candidates = [lan] if lan else []
        addr = known.get("address") if isinstance(known, dict) else None
        if isinstance(addr, str) and parse_address(addr):
            candidates.append(parse_address(addr))
        for host, port in candidates:
            if (host, port) in tried:
                continue
            tried.add((host, port))
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            except (OSError, asyncio.TimeoutError):
                continue
            yield reader, writer, device_id, None, f"{host}:{port}"
        if self._relay is None:
            raise ConnectError(
                "Poste introuvable sur le réseau local et aucun serveur relais n'est configuré "
                "(Paramètres > Réseau)."
            )
        try:
            reader, writer, pub = await self._relay.connect_to(device_id)
        except RelayError as exc:
            raise ConnectError(str(exc)) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectError(f"Serveur relais {self._relay.describe(exc)}") from exc
        yield reader, writer, device_id, pub, "relais"

    async def connect(
        self,
        target: str,
        password: Optional[str],
        events: SessionEvents,
        on_waiting: Optional[Callable[[PeerInfo, str], None]] = None,
        remember: bool = False,
    ) -> ViewerSession:
        try:
            return await self._connect(target, password, events, on_waiting, remember)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectError(f"Connexion impossible : {exc or exc.__class__.__name__}") from exc

    async def _connect(self, target, password, events, on_waiting, remember) -> ViewerSession:
        """Authentication strategy:
        1. explicit password, else a password remembered for this device;
        2. otherwise, if the host accepts a password, ask the user (password,
           or "ask for authorisation" when the host also accepts that);
        3. a remembered password that no longer works is forgotten and the
           user is asked again."""
        hint = normalize_id(target)
        saved = self.saved_passwords.get(hint) if hint and not password else None
        saved_id = hint if saved is not None else None
        tried_saved = saved is not None
        want = "password" if password else "ask"
        for _ in range(5):
            try:
                session, result = await self._connect_routes(target, events, on_waiting, password, want, saved)
            except HandshakeError as exc:
                if exc.code == "password_offered" and exc.peer is not None:
                    if saved is not None:  # the host's password changed: forget the old one
                        self.saved_passwords.forget(exc.peer.device_id)
                        saved = None
                    elif not tried_saved and not password:
                        # connected by IP address: we only learn the device ID now
                        tried_saved = True
                        candidate = self.saved_passwords.get(exc.peer.device_id)
                        if candidate is not None:
                            saved, saved_id = candidate, exc.peer.device_id
                            continue
                    answer = await self.ui.ask_password(exc.peer, exc.interactive_ok)
                    if not answer:
                        raise ConnectError("Connexion annulée.") from exc
                    if answer.get("interactive"):
                        password, want = None, "interactive"
                    else:
                        password, want = answer.get("password") or None, "password"
                        remember = bool(answer.get("remember"))
                    continue
                if exc.code == "auth_failed" and saved is not None and not password:
                    self.saved_passwords.forget(saved_id)  # remembered password refused: ask again
                    saved, want = None, "ask"
                    continue
                raise ConnectError(exc.user_message) from exc
            if remember and result.extra.get("pw_secret"):
                params, secret = result.extra["pw_secret"]
                self.saved_passwords.save(result.peer.device_id, params, secret)
            return session
        raise ConnectError("Connexion impossible.")

    async def _connect_routes(self, target, events, on_waiting, password, want, saved):
        last_error: Optional[Exception] = None
        async for reader, writer, expected_id, expected_pub, address in self._routes(target):

            async def verify(peer: PeerInfo, _expected=expected_id) -> bool:
                pinned = self.known_hosts.get(peer.device_id)
                if pinned is not None:
                    if not hmac.compare_digest(pinned, peer.public_key):
                        raise HandshakeError("host_key_mismatch")
                    return True
                if _expected is None:
                    return await self.ui.confirm_new_host(peer)
                return True

            try:
                result = await initiator_handshake(
                    reader,
                    writer,
                    self.identity,
                    self.settings.device_name,
                    expected_id=expected_id,
                    expected_public=expected_pub,
                    verify_host=verify,
                    password=password or None,
                    want=want,
                    get_totp=self.ui.ask_totp,
                    on_waiting=on_waiting,
                    address=address,
                    saved_secret=saved,
                )
            except HandshakeError as exc:
                await close_writer(writer)
                if exc.code in ("protocol", "timeout") and expected_id and address != "relais":
                    last_error = exc  # stale LAN entry: try next route
                    continue
                raise
            except (OSError, ConnectionError) as exc:
                await close_writer(writer)
                last_error = exc
                continue
            direct_addr = address if address != "relais" else None
            self.known_hosts.remember(result.peer.device_id, result.peer.public_key, result.peer.name, direct_addr)
            session = ViewerSession(result, events, downloads_dir())
            self.viewer_sessions.add(session)

            async def runner(s=session):
                try:
                    await s.run()
                finally:
                    self.viewer_sessions.discard(s)

            asyncio.get_running_loop().create_task(runner())
            return session, result
        raise ConnectError(f"Connexion impossible : {last_error}" if last_error else "Connexion impossible.")
