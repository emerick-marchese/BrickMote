"""BrickMote relay / rendezvous server (self-hosted, no limits).

* Clients keep a TLS 1.3 control connection and register with their Ed25519
  key (proof of possession: they sign a fresh server challenge). The server
  binds the 9-digit ID (derived from the key) to that key forever.
* When a viewer asks for an ID, both sides open a *data* connection and prove
  they are the expected parties; the server then pipes the bytes. Those bytes
  are end-to-end encrypted: the relay cannot read or modify sessions.

Run:  python -m brickmote.server --ports 6568,443,80
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import secrets
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..constants import MAX_CONTROL_FRAME, RELAY_PORTS
from ..crypto.identity import id_from_public, verify_signature
from ..net.framing import FrameError, close_writer, read_json, write_json
from ..net.tls import file_fingerprint, generate_self_signed, server_context
from ..util import RateLimiter, b64d, b64e

log = logging.getLogger("brickmote.relay")

REGISTER_CTX = b"BrickMote relay register v1\x00"
JOIN_CTX = b"BrickMote relay join v1\x00"

PAIR_TIMEOUT = 30.0
FIRST_MESSAGE_TIMEOUT = 10.0
CONTROL_IDLE_TIMEOUT = 90.0
PIPE_IDLE_TIMEOUT = 300.0
PIPE_CHUNK = 256 * 1024


@dataclass
class ControlClient:
    device_id: str
    pub: bytes
    writer: asyncio.StreamWriter
    address: str

    async def send(self, obj: dict) -> None:
        await write_json(self.writer, obj)


@dataclass
class PendingSession:
    token: str
    host_id: str
    host_pub: bytes
    viewer_id: str
    viewer_pub: bytes
    viewer: ControlClient
    created: float = field(default_factory=time.monotonic)
    streams: dict = field(default_factory=dict)  # role -> (reader, writer)
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    pipe_done: asyncio.Event = field(default_factory=asyncio.Event)
    failed: Optional[str] = None


class DeviceStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, pub BLOB NOT NULL UNIQUE,"
            " first_seen INTEGER, last_seen INTEGER)"
        )
        self.db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def lookup(self, device_id: str) -> Optional[bytes]:
        row = self.db.execute("SELECT pub FROM devices WHERE id=?", (device_id,)).fetchone()
        return bytes(row[0]) if row else None

    def bind(self, device_id: str, pub: bytes) -> bool:
        """Bind an ID to a key on first sight. False if the ID belongs to another key."""
        existing = self.lookup(device_id)
        now = int(time.time())
        if existing is None:
            self.db.execute("INSERT INTO devices VALUES (?,?,?,?)", (device_id, pub, now, now))
        elif not hmac.compare_digest(existing, pub):
            return False
        else:
            self.db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
        self.db.commit()
        return True

    def known(self, pub: bytes) -> bool:
        return self.db.execute("SELECT 1 FROM devices WHERE pub=?", (pub,)).fetchone() is not None


class RelayServer:
    def __init__(
        self,
        store: DeviceStore,
        max_connections: int = 10000,
        max_sessions_per_device: int = 8,
    ):
        self.store = store
        self.online: dict[str, ControlClient] = {}
        self.pending: dict[str, PendingSession] = {}
        self.max_connections = max_connections
        self.max_sessions_per_device = max_sessions_per_device
        self.connections = 0
        self.active_pipes = 0
        self.ip_limiter = RateLimiter(limit=120, window=60)
        self.new_key_limiter = RateLimiter(limit=20, window=3600)
        self.connect_limiter = RateLimiter(limit=30, window=60)
        self.servers: list[asyncio.base_events.Server] = []

    async def start(self, host: str, ports: list[int], ssl_ctx) -> list[int]:
        bound = []
        for port in ports:
            try:
                srv = await asyncio.start_server(self._handle, host, port, ssl=ssl_ctx, limit=PIPE_CHUNK)
            except OSError as exc:
                log.warning("cannot listen on port %s: %s", port, exc)
                continue
            self.servers.append(srv)
            bound.extend(sock.getsockname()[1] for sock in srv.sockets)
            log.info("listening on %s:%s", host or "*", port)
        return sorted(set(bound))

    async def stop(self) -> None:
        for srv in self.servers:
            srv.close()
        for client in list(self.online.values()):
            await close_writer(client.writer)
        for srv in self.servers:
            try:
                await asyncio.wait_for(srv.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    # ----------------------------------------------------------- dispatch
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if self.connections >= self.max_connections or not self.ip_limiter.allow(ip):
            await close_writer(writer)
            return
        self.connections += 1
        try:
            nonce = os.urandom(32)
            await write_json(writer, {"t": "challenge", "nonce": b64e(nonce), "v": 1})
            msg = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), FIRST_MESSAGE_TIMEOUT)
            kind = msg.get("t")
            if kind == "register":
                await self._control(reader, writer, msg, nonce, ip)
            elif kind == "join":
                await self._join(reader, writer, msg, nonce, ip)
        except (asyncio.TimeoutError, ConnectionError, FrameError, ValueError, OSError) as exc:
            log.debug("connection from %s ended: %s", ip, exc)
        except Exception:  # noqa: BLE001
            log.exception("unexpected error with %s", ip)
        finally:
            self.connections -= 1
            await close_writer(writer)

    # ------------------------------------------------------------ control
    async def _control(self, reader, writer, msg: dict, nonce: bytes, ip: str) -> None:
        pub = b64d(msg.get("pub"), 32)
        if not verify_signature(pub, b64d(msg.get("sig"), 64), REGISTER_CTX + nonce):
            await write_json(writer, {"t": "error", "code": "bad_signature"})
            return
        if not self.store.known(pub) and not self.new_key_limiter.allow(ip):
            await write_json(writer, {"t": "error", "code": "rate_limited"})
            return
        device_id = await asyncio.to_thread(id_from_public, pub)
        if not self.store.bind(device_id, pub):
            await write_json(writer, {"t": "error", "code": "id_conflict"})
            return
        client = ControlClient(device_id, pub, writer, ip)
        old = self.online.get(device_id)
        self.online[device_id] = client
        if old is not None:
            await close_writer(old.writer)
        log.info("device %s online from %s", device_id, ip)
        await client.send({"t": "registered", "id": device_id})
        try:
            while True:
                req = await asyncio.wait_for(read_json(reader, MAX_CONTROL_FRAME), CONTROL_IDLE_TIMEOUT)
                kind = req.get("t")
                if kind == "ping":
                    await client.send({"t": "pong"})
                elif kind == "connect":
                    await self._on_connect(client, req)
                elif kind == "decline":
                    session = self.pending.get(req.get("token"))
                    if session and session.host_id == device_id:
                        session.failed = "declined"
                        session.paired.set()
        finally:
            if self.online.get(device_id) is client:
                del self.online[device_id]
                log.info("device %s offline", device_id)

    async def _on_connect(self, viewer: ControlClient, req: dict) -> None:
        rid = req.get("rid") if isinstance(req.get("rid"), int) else 0
        target = req.get("target")
        if not isinstance(target, str) or not target.isdigit() or len(target) != 9:
            await viewer.send({"t": "error", "rid": rid, "code": "bad_request"})
            return
        if target == viewer.device_id:
            await viewer.send({"t": "error", "rid": rid, "code": "self"})
            return
        if not self.connect_limiter.allow(viewer.device_id):
            await viewer.send({"t": "error", "rid": rid, "code": "rate_limited"})
            return
        host = self.online.get(target)
        if host is None:
            await viewer.send({"t": "error", "rid": rid, "code": "offline"})
            return
        mine = sum(1 for s in self.pending.values() if s.viewer_id == viewer.device_id)
        if mine >= self.max_sessions_per_device:
            await viewer.send({"t": "error", "rid": rid, "code": "rate_limited"})
            return
        token = secrets.token_hex(16)
        session = PendingSession(token, host.device_id, host.pub, viewer.device_id, viewer.pub, viewer)
        self.pending[token] = session
        asyncio.get_running_loop().call_later(PAIR_TIMEOUT + 5, self._expire, token)
        await host.send({"t": "incoming", "token": token, "from": viewer.device_id, "pub": b64e(viewer.pub)})
        await viewer.send({"t": "route", "rid": rid, "token": token, "target": target, "pub": b64e(host.pub)})
        log.info("route %s -> %s", viewer.device_id, target)

    def _expire(self, token: str) -> None:
        session = self.pending.get(token)
        if session and not session.paired.is_set():
            session.failed = session.failed or "timeout"
            session.paired.set()
        if session and len(session.streams) < 2:
            self.pending.pop(token, None)

    # --------------------------------------------------------------- data
    async def _join(self, reader, writer, msg: dict, nonce: bytes, ip: str) -> None:
        token = msg.get("token")
        session = self.pending.get(token) if isinstance(token, str) else None
        if session is None:
            await write_json(writer, {"t": "error", "code": "unknown_token"})
            return
        pub = b64d(msg.get("pub"), 32)
        if not verify_signature(pub, b64d(msg.get("sig"), 64), JOIN_CTX + nonce + token.encode()):
            await write_json(writer, {"t": "error", "code": "bad_signature"})
            return
        if hmac.compare_digest(pub, session.host_pub):
            role = "host"
        elif hmac.compare_digest(pub, session.viewer_pub):
            role = "viewer"
        else:
            await write_json(writer, {"t": "error", "code": "not_a_party"})
            return
        if role in session.streams:
            await write_json(writer, {"t": "error", "code": "duplicate"})
            return
        session.streams[role] = (reader, writer)
        if len(session.streams) == 2:
            session.paired.set()
        try:
            await asyncio.wait_for(session.paired.wait(), PAIR_TIMEOUT)
        except asyncio.TimeoutError:
            session.failed = "timeout"
        if session.failed or len(session.streams) < 2:
            await write_json(writer, {"t": "error", "code": session.failed or "timeout"})
            self.pending.pop(token, None)
            return
        if role == "viewer":
            # the viewer's task drives the pipe; the host's task just waits
            self.pending.pop(token, None)
            await self._pipe(session)
        else:
            await session.pipe_done.wait()

    async def _pipe(self, session: PendingSession) -> None:
        (hr, hw), (vr, vw) = session.streams["host"], session.streams["viewer"]
        self.active_pipes += 1
        try:
            await write_json(hw, {"t": "paired"})
            await write_json(vw, {"t": "paired"})

            async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
                while True:
                    data = await asyncio.wait_for(src.read(PIPE_CHUNK), PIPE_IDLE_TIMEOUT)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()

            tasks = [asyncio.create_task(copy(hr, vw)), asyncio.create_task(copy(vr, hw))]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            self.active_pipes -= 1
            await close_writer(hw)
            await close_writer(vw)
            session.pipe_done.set()
            log.info("session %s -> %s ended", session.viewer_id, session.host_id)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="BrickMote relay server")
    parser.add_argument("--host", default="", help="adresse d'écoute (défaut : toutes)")
    parser.add_argument(
        "--ports", default=",".join(map(str, RELAY_PORTS)), help="ports TCP (défaut : 6568,443,80)"
    )
    parser.add_argument("--data-dir", default=os.environ.get("BRICKMOTE_RELAY_DATA", "./relay-data"))
    parser.add_argument("--cert", help="certificat TLS (PEM). Auto-généré si absent.")
    parser.add_argument("--key", help="clé privée TLS (PEM)")
    parser.add_argument("--hostname", action="append", default=[], help="nom/IP public du serveur (SAN)")
    parser.add_argument("--max-connections", type=int, default=10000)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    cert = Path(args.cert) if args.cert else data / "relay-cert.pem"
    key = Path(args.key) if args.key else data / "relay-key.pem"
    if not cert.exists() or not key.exists():
        if args.cert or args.key:
            parser.error("certificat ou clé introuvable")
        generate_self_signed(cert, key, args.hostname or ["localhost"])
        log.info("certificat auto-signé créé")
    fp = file_fingerprint(cert)
    print("=" * 72)
    print(" BrickMote Relay - empreinte du certificat (à saisir dans les clients) :")
    print(" " + fp)
    print("=" * 72, flush=True)

    ports = [int(p) for p in args.ports.split(",") if p.strip()]

    async def run():
        server = RelayServer(DeviceStore(data / "devices.sqlite3"), max_connections=args.max_connections)
        bound = await server.start(args.host or None, ports, server_context(cert, key))
        if not bound:
            raise SystemExit("aucun port n'a pu être ouvert")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await stop.wait()
        await server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
