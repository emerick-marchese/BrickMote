"""Client side of the relay protocol (control connection + data connections)."""

from __future__ import annotations

import asyncio
import itertools
import ssl
import logging
from typing import Awaitable, Callable, Optional

from ..constants import MAX_CONTROL_FRAME
from ..crypto.identity import Identity
from ..net.framing import FrameError, close_writer, read_json, write_json
from ..net.tls import PinError, open_tls
from ..net.wol import public_ipv4
from ..util import b64d, b64e

log = logging.getLogger(__name__)

REGISTER_CTX = b"BrickMote relay register v1\x00"
JOIN_CTX = b"BrickMote relay join v1\x00"

RELAY_ERRORS = {
    "offline": "Le poste distant est hors ligne (non connecté au serveur relais).",
    "rate_limited": "Trop de demandes. Patientez avant de réessayer.",
    "self": "Vous ne pouvez pas vous connecter à vous-même.",
    "declined": "Le poste distant a refusé la connexion.",
    "timeout": "Le poste distant n'a pas répondu.",
    "id_conflict": "Conflit d'ID sur le serveur relais.",
    "bad_signature": "Signature refusée par le serveur relais.",
    "unavailable": "Serveur relais injoignable ou non configuré.",
}


class RelayError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(RELAY_ERRORS.get(code, code))


IncomingHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, str, bytes], Awaitable[None]]
StatusHandler = Callable[[str, str], None]  # state ("connecting"|"online"|"offline"|"disabled"), detail


class RelayClient:
    def __init__(
        self,
        identity: Identity,
        host: str,
        ports: list[int],
        fingerprint: str,
        on_incoming: Optional[IncomingHandler],
        on_status: Optional[StatusHandler] = None,
        display_name: Optional[str] = None,
    ):
        self.identity = identity
        self.host = host
        self.ports = list(ports)
        self.fingerprint = fingerprint
        self.on_incoming = on_incoming
        self.on_status = on_status or (lambda s, d: None)
        # When set (built-in server), the address and raw network errors —
        # which contain the IP — are never reported: only this name is.
        self.display_name = display_name
        self._writer: Optional[asyncio.StreamWriter] = None
        self._port: Optional[int] = None
        self.features: set[str] = set()  # what the relay supports (announced at registration)
        self.server_version = ""
        self._rid = itertools.count(1)
        self._routes: dict[int, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self.online = asyncio.Event()
        self.device_id: Optional[str] = None
        self.public_ip: Optional[str] = None  # our box's public IPv4, as seen by the relay

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await close_writer(self._writer)
        self.on_status("disabled", "")

    async def _open(self):
        last: Exception = RelayError("unavailable")
        for port in self.ports:
            try:
                reader, writer = await open_tls(self.host, port, self.fingerprint)
                return reader, writer, port
            except PinError:
                raise
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                last = exc
        raise last

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopped:
            self.on_status("connecting", self.display_name or self.host)
            try:
                reader, writer, port = await self._open()
                challenge = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), 10)
                nonce = b64d(challenge.get("nonce"), 32)
                await write_json(
                    writer,
                    {
                        "t": "register",
                        "pub": b64e(self.identity.public_bytes),
                        "sig": b64e(self.identity.sign(REGISTER_CTX + nonce)),
                    },
                )
                reply = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), 15)
                if reply.get("t") != "registered":
                    raise RelayError(str(reply.get("code", "unavailable")))
                self.device_id = reply.get("id")
                features = reply.get("features")
                self.features = {f for f in features if isinstance(f, str)} if isinstance(features, list) else set()
                self.server_version = reply.get("v") if isinstance(reply.get("v"), str) else ""
                self.public_ip = public_ipv4(reply.get("addr"))
                self._writer, self._port = writer, port
                self.online.set()
                backoff = 1.0
                self.on_status("online", self.display_name or f"{self.host}:{port}")
                await self._read_loop(reader, writer)
            except asyncio.CancelledError:
                raise
            except PinError as exc:
                self.on_status("offline", str(exc))
                backoff = 60.0
            except RelayError as exc:
                self.on_status("offline", str(exc))
            except (OSError, ConnectionError, asyncio.TimeoutError, FrameError, ValueError) as exc:
                self.on_status("offline", self.describe(exc))
            finally:
                self.online.clear()
                await close_writer(self._writer)
                self._writer = None
                for fut in self._routes.values():
                    if not fut.done():
                        fut.set_exception(RelayError("unavailable"))
                self._routes.clear()
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def _read_loop(self, reader, writer) -> None:
        ping = asyncio.get_running_loop().create_task(self._pinger(writer))
        try:
            while True:
                msg = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), 60)
                kind = msg.get("t")
                if kind == "incoming":
                    self._on_incoming(msg)
                elif kind in ("route", "presence", "error"):
                    fut = self._routes.pop(msg.get("rid"), None)
                    if fut and not fut.done():
                        if kind in ("route", "presence"):
                            fut.set_result(msg)
                        else:
                            fut.set_exception(RelayError(str(msg.get("code"))))
        finally:
            ping.cancel()

    async def _pinger(self, writer) -> None:
        while True:
            await asyncio.sleep(25)
            await write_json(writer, {"t": "ping"})

    # ------------------------------------------------------------ data link
    async def _open_data(self, token: str):
        reader, writer = await open_tls(self.host, self._port or self.ports[0], self.fingerprint)
        try:
            challenge = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), 10)
            nonce = b64d(challenge.get("nonce"), 32)
            await write_json(
                writer,
                {
                    "t": "join",
                    "token": token,
                    "pub": b64e(self.identity.public_bytes),
                    "sig": b64e(self.identity.sign(JOIN_CTX + nonce + token.encode())),
                },
            )
            reply = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), 40)
            if reply.get("t") != "paired":
                raise RelayError(str(reply.get("code", "timeout")))
            return reader, writer
        except BaseException:
            await close_writer(writer)
            raise

    def _on_incoming(self, msg: dict) -> None:
        token, from_id = msg.get("token"), msg.get("from")
        try:
            from_pub = b64d(msg.get("pub"), 32)
        except ValueError:
            return
        if not isinstance(token, str) or not isinstance(from_id, str) or self.on_incoming is None:
            return

        async def handle():
            try:
                reader, writer = await self._open_data(token)
            except Exception as exc:  # noqa: BLE001
                log.info("incoming relay connection failed: %s", self.describe(exc))
                return
            await self.on_incoming(reader, writer, from_id, from_pub)

        asyncio.get_running_loop().create_task(handle())

    def describe(self, exc: BaseException) -> str:
        """Human readable error, without the server address when it is masked."""
        if isinstance(exc, ssl.SSLCertVerificationError):
            return (
                "certificat du serveur relais non reconnu : renseignez l'empreinte TLS "
                "affichée par le serveur (Paramètres > Réseau)"
            )
        if isinstance(exc, ssl.SSLError):
            return "échec de la connexion sécurisée (TLS) avec le serveur relais"
        if self.display_name:
            if isinstance(exc, asyncio.TimeoutError):
                return "délai dépassé (vérifiez votre connexion Internet)"
            return "injoignable (vérifiez votre connexion Internet)"
        return str(exc) or exc.__class__.__name__

    async def decline(self, token: str) -> None:
        if self._writer is not None:
            await write_json(self._writer, {"t": "decline", "token": token})

    async def presence(self, device_ids: list[str], timeout: float = 5.0) -> Optional[set[str]]:
        """IDs among ``device_ids`` currently connected to the relay, or None
        when the relay cannot tell (offline, or an older relay that does not
        know this request)."""
        if self._writer is None or not self.online.is_set() or "presence" not in self.features:
            return None  # an older relay would not answer
        rid = next(self._rid)
        fut = asyncio.get_running_loop().create_future()
        self._routes[rid] = fut
        try:
            await write_json(self._writer, {"t": "presence", "ids": list(device_ids)[:128], "rid": rid})
            msg = await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, RelayError, ConnectionError, OSError):
            return None
        finally:
            self._routes.pop(rid, None)
        online = msg.get("online")
        if not isinstance(online, list):
            return None
        return {i for i in online if isinstance(i, str) and i in device_ids}

    async def connect_to(self, target_id: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        """Ask the relay for a route to ``target_id``. Returns the stream and
        the public key the relay has on record for that ID."""
        if self._writer is None or not self.online.is_set():
            raise RelayError("unavailable")
        rid = next(self._rid)
        fut = asyncio.get_running_loop().create_future()
        self._routes[rid] = fut
        await write_json(self._writer, {"t": "connect", "target": target_id, "rid": rid})
        try:
            route = await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError as exc:
            self._routes.pop(rid, None)
            raise RelayError("timeout") from exc
        target_pub = b64d(route.get("pub"), 32)
        reader, writer = await self._open_data(str(route.get("token")))
        return reader, writer, target_pub
