"""End-to-end authenticated key exchange between a viewer and a host.

The relay server (or anything on the network path) only ever sees ciphertext.

Protocol (I = initiator/viewer, R = responder/host)::

    1. I -> R  hello   {eph_I, nonce_I}                       (plaintext)
    2. R -> I  hello   {eph_R, nonce_R}                       (plaintext)
       both:   shared = X25519(eph_I, eph_R)
               handshake keys = HKDF(shared, salt=TH)         -> AES-256-GCM
    3. R -> I  host    {ik_R, name, auth methods, scrypt params, totp}
       R -> I  sig     Ed25519(ik_R, "host-auth" || TH)
       I: verify signature, verify ID == id_from_public(ik_R), verify pin
    4. I -> R  viewer  {ik_I, name, method, SPAKE2 msg A (password mode)}
       I -> R  sig     Ed25519(ik_I, "viewer-auth" || TH)
    5a. interactive: the host user sees the request + security code and
        explicitly accepts (with chosen permissions) or refuses.
    5b. password:    SPAKE2 exchange + key confirmation (+ optional TOTP).
    6. R -> I  accept  {permissions}
       both:   session keys = HKDF(shared || K_spake, salt=TH_final)

TH is the SHA-256 of every handshake message so far. The signatures are
SIGMA-style: each side proves possession of its long-term identity key over
the full transcript (ephemeral keys included), which gives mutual
authentication and forward secrecy. In password mode the SPAKE2 key is mixed
into the session keys, so even an attacker able to impersonate an identity
key cannot read the session without the password, and cannot test passwords
offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B
from spake2.spake2 import SPAKEError

from ..constants import (
    CREDENTIAL_PROMPT_TIMEOUT,
    HANDSHAKE_STEP_TIMEOUT,
    INTERACTIVE_ACCEPT_TIMEOUT,
    MAX_HANDSHAKE_FRAME,
    MAX_SESSION_FRAME,
    PERMISSION_KEYS,
    PROTOCOL_NAME,
)
from ..net.framing import FrameError, decode_json, encode_json, read_frame, write_frame
from ..util import b64d, b64e, clean_text
from .channel import ChannelError, SecureChannel
from .identity import Identity, fingerprint, id_from_public, verify_signature
from .password import PasswordVerifier, derive_secret_from_params

METHOD_INTERACTIVE = "interactive"
METHOD_PASSWORD = "password"

_HOST_SIG_CTX = b"BrickMote v1 host-auth\x00"
_VIEWER_SIG_CTX = b"BrickMote v1 viewer-auth\x00"

# Human readable explanations for rejection codes (French UI).
REJECT_MESSAGES = {
    "rejected": "La demande a été refusée par l'utilisateur distant.",
    "timeout": "L'utilisateur distant n'a pas répondu à temps.",
    "auth_failed": "Mot de passe incorrect.",
    "totp_failed": "Code de double authentification (2FA) invalide.",
    "locked": "Trop d'échecs : accès temporairement bloqué. Réessayez plus tard.",
    "busy": "Le poste distant traite déjà une demande. Réessayez.",
    "acl": "Votre poste n'est pas autorisé (liste de contrôle d'accès).",
    "blocked": "Votre poste a été bloqué par le poste distant.",
    "no_unattended": "L'accès sans surveillance n'est pas activé sur le poste distant.",
    "no_interactive": "Le poste distant n'accepte que l'accès par mot de passe.",
    "protocol": "Erreur de protocole (version incompatible ou données corrompues).",
    "id_mismatch": "L'identité du poste distant ne correspond pas à l'ID demandé !",
    "host_key_mismatch": "ALERTE : la clé du poste distant a changé (possible usurpation).",
    "host_key_rejected": "Identité du poste distant non approuvée.",
    "rate_limited": "Trop de tentatives de connexion. Réessayez plus tard.",
    "cancelled": "Connexion annulée.",
    "password_offered": "Ce poste accepte une connexion par mot de passe.",
}


class HandshakeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        self.peer: Optional["PeerInfo"] = None  # set for "password_offered"
        self.interactive_ok = False
        super().__init__(detail or REJECT_MESSAGES.get(code, code))

    @property
    def user_message(self) -> str:
        return REJECT_MESSAGES.get(self.code, self.detail or self.code)


@dataclass
class PeerInfo:
    device_id: str
    public_key: bytes
    name: str
    address: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)


@dataclass
class HandshakeResult:
    channel: SecureChannel
    peer: PeerInfo
    permissions: dict
    method: str
    sas: str
    extra: dict = field(default_factory=dict)


def sanitize_permissions(perms: object) -> dict:
    perms = perms if isinstance(perms, dict) else {}
    return {key: perms.get(key) is True for key in PERMISSION_KEYS}


class HostAuthority(Protocol):
    """What the host side must provide to run a handshake."""

    identity: Identity
    name: str

    def offered_methods(self) -> list[str]: ...
    def password_verifier(self) -> Optional[PasswordVerifier]: ...
    def totp_required(self) -> bool: ...
    def check_totp(self, code: object) -> bool: ...
    async def precheck(self, peer: PeerInfo, method: str) -> Optional[str]: ...
    def record_failure(self, peer: PeerInfo, reason: str) -> None: ...
    def record_success(self, peer: PeerInfo, method: str) -> None: ...
    async def request_interactive(self, peer: PeerInfo, sas: str) -> Optional[dict]: ...
    def unattended_permissions(self) -> dict: ...


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _Transcript:
    def __init__(self) -> None:
        self._h = hashlib.sha256(b"BrickMote-transcript-v1")

    def add(self, data: bytes) -> None:
        self._h.update(len(data).to_bytes(4, "big"))
        self._h.update(data)

    def digest(self) -> bytes:
        return self._h.copy().digest()


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _sas(secret: bytes, th: bytes) -> str:
    value = int.from_bytes(_hkdf(secret, th, b"BrickMote v1 SAS", 8), "big") % 1_000_000
    s = str(value).zfill(6)
    return f"{s[:3]} {s[3:]}"


def _x25519_shared(priv: X25519PrivateKey, peer_pub: bytes) -> bytes:
    try:
        shared = priv.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    except ValueError as exc:
        raise HandshakeError("protocol", "invalid ephemeral key") from exc
    if shared == b"\x00" * 32:
        raise HandshakeError("protocol", "low order ephemeral key")
    return shared


def _x25519_public(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class _HandshakeIO:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.transcript = _Transcript()
        self.channel: Optional[SecureChannel] = None

    async def send(self, obj: dict) -> None:
        data = encode_json(obj)
        self.transcript.add(data)
        if self.channel is not None:
            await self.channel.send(data)
        else:
            write_frame(self.writer, data)
            await self.writer.drain()

    async def recv(self, timeout: float = HANDSHAKE_STEP_TIMEOUT) -> dict:
        try:
            if self.channel is not None:
                data = await asyncio.wait_for(self.channel.recv(), timeout)
            else:
                data = await asyncio.wait_for(read_frame(self.reader, MAX_HANDSHAKE_FRAME), timeout)
        except asyncio.TimeoutError as exc:
            raise HandshakeError("timeout", "handshake timeout") from exc
        except (FrameError, ChannelError) as exc:
            raise HandshakeError("protocol", str(exc)) from exc
        except (ConnectionError, OSError) as exc:
            raise HandshakeError("cancelled", "connection closed") from exc
        self.transcript.add(data)
        try:
            msg = decode_json(data)
        except FrameError as exc:
            raise HandshakeError("protocol", str(exc)) from exc
        if msg.get("t") == "reject":
            code = msg.get("code")
            raise HandshakeError(code if code in REJECT_MESSAGES else "rejected")
        return msg

    async def expect(self, kind: str, timeout: float = HANDSHAKE_STEP_TIMEOUT) -> dict:
        msg = await self.recv(timeout)
        if msg.get("t") != kind:
            raise HandshakeError("protocol", f"expected {kind!r}")
        return msg

    async def reject(self, code: str) -> None:
        try:
            await asyncio.wait_for(self.send({"t": "reject", "code": code}), 5)
        except Exception:  # noqa: BLE001
            pass

    def start_encryption(self, send_key: bytes, recv_key: bytes) -> None:
        self.channel = SecureChannel(self.reader, self.writer, send_key, recv_key, MAX_HANDSHAKE_FRAME)

    def session_channel(self, send_key: bytes, recv_key: bytes) -> SecureChannel:
        return SecureChannel(self.reader, self.writer, send_key, recv_key, MAX_SESSION_FRAME)


def _hello() -> tuple[X25519PrivateKey, dict]:
    eph = X25519PrivateKey.generate()
    return eph, {
        "t": "hello",
        "proto": PROTOCOL_NAME,
        "eph": b64e(_x25519_public(eph)),
        "nonce": b64e(os.urandom(32)),
    }


def _check_hello(msg: dict) -> bytes:
    if msg.get("t") != "hello" or msg.get("proto") != PROTOCOL_NAME:
        raise HandshakeError("protocol", "unsupported protocol version")
    b64d(msg.get("nonce"), 32)
    return b64d(msg.get("eph"), 32)


def _spake_ids(viewer_pub: bytes, host_pub: bytes) -> dict:
    return {"idA": b"BrickMote-viewer:" + viewer_pub, "idB": b"BrickMote-host:" + host_pub}


def _confirm_tags(k_spake: bytes, th: bytes, msg_b: bytes) -> tuple[bytes, bytes]:
    kc = _hkdf(k_spake, th, b"BrickMote v1 pake-confirm", 32)
    host_tag = hmac.new(kc, b"host" + th + msg_b, hashlib.sha256).digest()
    viewer_tag = hmac.new(kc, b"viewer" + th + msg_b, hashlib.sha256).digest()
    return host_tag, viewer_tag


# --------------------------------------------------------------------------
# viewer side
# --------------------------------------------------------------------------


async def initiator_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    identity: Identity,
    name: str,
    *,
    expected_id: Optional[str] = None,
    expected_public: Optional[bytes] = None,
    verify_host: Optional[Callable[[PeerInfo], Awaitable[bool]]] = None,
    password: Optional[str] = None,
    want: str = "auto",
    get_totp: Optional[Callable[[PeerInfo], Awaitable[Optional[str]]]] = None,
    on_waiting: Optional[Callable[[PeerInfo, str], None]] = None,
    address: Optional[str] = None,
    saved_secret: Optional[tuple] = None,
) -> HandshakeResult:
    """``want``: "password", "interactive", "auto" (password if one is given)
    or "ask": if the host accepts a password and we have none, stop right
    after authenticating the host and raise ``HandshakeError("password_offered")``
    so the caller can ask the user, then reconnect. The host user never sees
    that first attempt.

    ``saved_secret``: (public scrypt parameters, derived secret) remembered from
    a previous connection; used instead of the password if the host's
    parameters still match (i.e. its password did not change)."""
    io = _HandshakeIO(reader, writer)
    try:
        eph, hello = _hello()
        await io.send(hello)
        eph_r = _check_hello(await io.recv())
        shared = _x25519_shared(eph, eph_r)
        th = io.transcript.digest()
        hs = _hkdf(shared, th, b"BrickMote v1 handshake", 64)
        io.start_encryption(send_key=hs[:32], recv_key=hs[32:])

        # 3. host identity + signature
        host_msg = await io.expect("host")
        th_host = io.transcript.digest()
        sig_msg = await io.expect("sig")
        host_pub = b64d(host_msg.get("ik"), 32)
        if not verify_signature(host_pub, b64d(sig_msg.get("sig"), 64), _HOST_SIG_CTX + th_host):
            raise HandshakeError("protocol", "bad host signature")
        host_id = await asyncio.to_thread(id_from_public, host_pub)
        peer = PeerInfo(host_id, host_pub, clean_text(host_msg.get("name"), 64, "Poste distant"), address)
        if expected_id is not None and host_id != expected_id:
            raise HandshakeError("id_mismatch")
        if expected_public is not None and not hmac.compare_digest(host_pub, expected_public):
            raise HandshakeError("host_key_mismatch")
        if verify_host is not None and not await verify_host(peer):
            raise HandshakeError("host_key_rejected")

        offered = host_msg.get("auth")
        offered = [m for m in offered if isinstance(m, str)] if isinstance(offered, list) else []
        pw_params = host_msg.get("pw")
        secret = None
        if saved_secret is not None and isinstance(pw_params, dict) and saved_secret[0] == pw_params:
            secret = saved_secret[1]
        if want == "ask" and METHOD_PASSWORD in offered and not password and secret is None:
            err = HandshakeError("password_offered")
            err.peer = peer
            err.interactive_ok = METHOD_INTERACTIVE in offered
            raise err
        if want == METHOD_PASSWORD or (want in ("auto", "ask") and (password or secret is not None)):
            if METHOD_PASSWORD not in offered:
                raise HandshakeError("no_unattended")
            if not password and secret is None:
                err = HandshakeError("password_offered")  # saved password no longer valid
                err.peer = peer
                err.interactive_ok = METHOD_INTERACTIVE in offered
                raise err
            method = METHOD_PASSWORD
        else:
            if METHOD_INTERACTIVE not in offered:
                raise HandshakeError("no_interactive")
            method = METHOD_INTERACTIVE

        # 4. our identity (+ SPAKE2 message A)
        viewer_msg = {"t": "viewer", "ik": b64e(identity.public_bytes), "name": name[:64], "method": method}
        spake = None
        if method == METHOD_PASSWORD:
            if secret is None:
                try:
                    secret = await asyncio.to_thread(derive_secret_from_params, password, pw_params)
                except (ValueError, TypeError) as exc:
                    raise HandshakeError("protocol", "bad password parameters") from exc
            spake = SPAKE2_A(secret, **_spake_ids(identity.public_bytes, host_pub))
            viewer_msg["spake"] = b64e(spake.start())
        await io.send(viewer_msg)
        await io.send({"t": "sig", "sig": b64e(identity.sign(_VIEWER_SIG_CTX + io.transcript.digest()))})

        k_spake = b""
        if method == METHOD_PASSWORD:
            th_auth = io.transcript.digest()
            pake = await io.expect("pake", timeout=HANDSHAKE_STEP_TIMEOUT + 10)
            msg_b = b64d(pake.get("spake"), 33)
            try:
                k_spake = spake.finish(msg_b)
            except (SPAKEError, ValueError) as exc:
                raise HandshakeError("protocol", "bad SPAKE2 message") from exc
            host_tag, viewer_tag = _confirm_tags(k_spake, th_auth, msg_b)
            if not hmac.compare_digest(host_tag, b64d(pake.get("confirm"), 32)):
                raise HandshakeError("auth_failed")
            code = None
            if host_msg.get("totp") is True:
                if get_totp is None:
                    raise HandshakeError("totp_failed")
                code = await asyncio.wait_for(get_totp(peer), CREDENTIAL_PROMPT_TIMEOUT)
                if not code:
                    raise HandshakeError("cancelled")
            await io.send({"t": "pake-ok", "confirm": b64e(viewer_tag), "totp": code})

        sas = _sas(shared + k_spake, io.transcript.digest())
        if on_waiting is not None:
            on_waiting(peer, sas)

        timeout = INTERACTIVE_ACCEPT_TIMEOUT + 15 if method == METHOD_INTERACTIVE else HANDSHAKE_STEP_TIMEOUT
        accept = await io.expect("accept", timeout=timeout)
        permissions = sanitize_permissions(accept.get("perms"))
        keys = _hkdf(shared + k_spake, io.transcript.digest(), b"BrickMote v1 session", 64)
        channel = io.session_channel(send_key=keys[:32], recv_key=keys[32:])
        extra = {"pw_secret": (dict(pw_params), secret)} if method == METHOD_PASSWORD else {}
        return HandshakeResult(channel, peer, permissions, method, sas, extra)
    except HandshakeError:
        raise
    except asyncio.TimeoutError as exc:
        raise HandshakeError("timeout") from exc
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HandshakeError("protocol", str(exc)) from exc


# --------------------------------------------------------------------------
# host side
# --------------------------------------------------------------------------


async def responder_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    authority: HostAuthority,
    address: Optional[str] = None,
) -> HandshakeResult:
    io = _HandshakeIO(reader, writer)
    peer: Optional[PeerInfo] = None
    try:
        eph_i = _check_hello(await io.recv())
        eph, hello = _hello()
        await io.send(hello)
        shared = _x25519_shared(eph, eph_i)
        hs = _hkdf(shared, io.transcript.digest(), b"BrickMote v1 handshake", 64)
        io.start_encryption(send_key=hs[32:], recv_key=hs[:32])

        # 3. our identity + signature
        identity = authority.identity
        methods = authority.offered_methods()
        verifier = authority.password_verifier() if METHOD_PASSWORD in methods else None
        host_msg = {
            "t": "host",
            "ik": b64e(identity.public_bytes),
            "name": authority.name[:64],
            "auth": methods,
            "pw": verifier.public_params() if verifier else None,
            "totp": bool(verifier and authority.totp_required()),
        }
        await io.send(host_msg)
        await io.send({"t": "sig", "sig": b64e(identity.sign(_HOST_SIG_CTX + io.transcript.digest()))})

        # 4. viewer identity + signature
        viewer_msg = await io.expect("viewer")
        th_viewer = io.transcript.digest()
        sig_msg = await io.expect("sig")
        viewer_pub = b64d(viewer_msg.get("ik"), 32)
        if not verify_signature(viewer_pub, b64d(sig_msg.get("sig"), 64), _VIEWER_SIG_CTX + th_viewer):
            raise HandshakeError("protocol", "bad viewer signature")
        if hmac.compare_digest(viewer_pub, identity.public_bytes):
            raise HandshakeError("protocol", "connection to self")
        viewer_id = await asyncio.to_thread(id_from_public, viewer_pub)
        peer = PeerInfo(viewer_id, viewer_pub, clean_text(viewer_msg.get("name"), 64, "Inconnu"), address)

        method = viewer_msg.get("method")
        if method not in methods:
            await io.reject("no_unattended" if method == METHOD_PASSWORD else "no_interactive")
            raise HandshakeError("protocol", "method not offered")
        refusal = await authority.precheck(peer, method)
        if refusal:
            await io.reject(refusal)
            raise HandshakeError(refusal)

        k_spake = b""
        if method == METHOD_PASSWORD:
            assert verifier is not None
            msg_a = b64d(viewer_msg.get("spake"), 33)
            th_auth = io.transcript.digest()
            spake = SPAKE2_B(verifier.secret, **_spake_ids(viewer_pub, identity.public_bytes))
            msg_b = spake.start()
            try:
                k_spake = spake.finish(msg_a)
            except (SPAKEError, ValueError) as exc:
                authority.record_failure(peer, "bad_pake")
                await io.reject("protocol")
                raise HandshakeError("protocol", "bad SPAKE2 message") from exc
            host_tag, viewer_tag = _confirm_tags(k_spake, th_auth, msg_b)
            await io.send({"t": "pake", "spake": b64e(msg_b), "confirm": b64e(host_tag)})
            try:
                confirm = await io.expect("pake-ok", timeout=CREDENTIAL_PROMPT_TIMEOUT + 10)
            except HandshakeError:
                # A viewer with a wrong password detects it from our tag and
                # hangs up: that attempt must still count as a failure,
                # otherwise guesses could be tested without triggering lockout.
                authority.record_failure(peer, "auth_aborted")
                raise
            try:
                tag = b64d(confirm.get("confirm"), 32)
            except ValueError:
                tag = b""
            if not hmac.compare_digest(tag, viewer_tag):
                authority.record_failure(peer, "auth_failed")
                await asyncio.sleep(1.0)  # slow down online guessing
                await io.reject("auth_failed")
                raise HandshakeError("auth_failed")
            if host_msg["totp"] and not authority.check_totp(confirm.get("totp")):
                authority.record_failure(peer, "totp_failed")
                await asyncio.sleep(1.0)
                await io.reject("totp_failed")
                raise HandshakeError("totp_failed")
            permissions = sanitize_permissions(authority.unattended_permissions())
            sas = _sas(shared + k_spake, io.transcript.digest())
        else:
            sas = _sas(shared, io.transcript.digest())
            try:
                granted = await asyncio.wait_for(
                    authority.request_interactive(peer, sas), INTERACTIVE_ACCEPT_TIMEOUT
                )
            except asyncio.TimeoutError:
                granted = None
                await io.reject("timeout")
                raise HandshakeError("timeout")
            if granted is None:
                await io.reject("rejected")
                raise HandshakeError("rejected")
            permissions = sanitize_permissions(granted)

        authority.record_success(peer, method)
        await io.send({"t": "accept", "perms": permissions})
        keys = _hkdf(shared + k_spake, io.transcript.digest(), b"BrickMote v1 session", 64)
        channel = io.session_channel(send_key=keys[32:], recv_key=keys[:32])
        return HandshakeResult(channel, peer, permissions, method, sas)
    except HandshakeError:
        raise
    except asyncio.TimeoutError as exc:
        raise HandshakeError("timeout") from exc
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HandshakeError("protocol", str(exc)) from exc
