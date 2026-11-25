"""TLS 1.3 helpers for the relay link (with optional certificate pinning).

Note: the relay link is only the *outer* layer. Session content is always
end-to-end encrypted between the two peers, so even a compromised relay can
neither read nor alter a session.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import ssl
from pathlib import Path

from ..util import secure_write


def normalize_fingerprint(fp: str) -> str:
    return "".join(ch for ch in (fp or "").lower() if ch in "0123456789abcdef")


def cert_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def client_context(pinned: bool) -> ssl.SSLContext:
    if pinned:
        # The certificate is verified by its SHA-256 fingerprint instead of
        # a CA chain (self-hosted relay with a self-signed certificate).
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


class PinError(ConnectionError):
    pass


async def open_tls(host: str, port: int, fingerprint: str = "", timeout: float = 8.0):
    pin = normalize_fingerprint(fingerprint)
    ctx = client_context(bool(pin))
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout
    )
    if pin:
        sslobj = writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
        actual = hashlib.sha256(der).hexdigest() if der else ""
        if not hmac.compare_digest(actual, pin):
            writer.close()
            raise PinError(
                "le certificat du serveur relais ne correspond pas à l'empreinte épinglée "
                "(attaque de l'homme du milieu possible)"
            )
    return reader, writer


def generate_self_signed(cert_path: Path, key_path: Path, hostnames: list[str]) -> str:
    """Create an ECDSA P-256 self-signed certificate. Returns its fingerprint."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    names = []
    for h in hostnames or ["localhost"]:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BrickMote Relay")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    secure_write(
        Path(key_path),
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_fingerprint(der)


def file_fingerprint(cert_path: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return cert_fingerprint(cert.public_bytes(serialization.Encoding.DER))
