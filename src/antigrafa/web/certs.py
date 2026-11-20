"""The HTTPS certificate: the one configured, or a self-signed one created on first start.

The self-signed certificate names this machine (host names and addresses), uses an
ECDSA P-256 key and is valid 397 days. Antigrafa renews it on its own 30 days before
it expires; a certificate it did not create is never touched.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .files import write_private

log = logging.getLogger(__name__)

ISSUER = "Antigrafa self-signed"  # organization name: marks the certificates Antigrafa made
VALIDITY = timedelta(days=397)
RENEW_BEFORE = timedelta(days=30)


class CertificateError(Exception):
    pass


@dataclass
class CertificateInfo:
    fingerprint: str  # SHA-256, AA:BB:..., to check what the browser shows
    not_after: datetime
    self_signed: bool  # made by Antigrafa
    names: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(cert: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(cert.read_bytes())
    except ValueError as exc:
        raise CertificateError(f"{cert}: not a PEM certificate ({exc})") from None


def _made_here(c: x509.Certificate) -> bool:
    orgs = c.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return c.issuer == c.subject and any(o.value == ISSUER for o in orgs)


def describe(cert: Path) -> CertificateInfo:
    c = _load(cert)
    try:
        names = [str(n.value) for n in c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value]
    except x509.ExtensionNotFound:
        names = []
    digest = c.fingerprint(hashes.SHA256()).hex().upper()
    return CertificateInfo(
        fingerprint=":".join(digest[i : i + 2] for i in range(0, len(digest), 2)),
        not_after=c.not_valid_after_utc,
        self_signed=_made_here(c),
        names=names,
    )


def ensure_certificate(cert: Path, key: Path, hosts: list[str], now: datetime | None = None) -> str | None:
    """Create a self-signed certificate if needed: why ("missing", "expiring"), or None."""
    now = now or _now()
    have_cert, have_key = cert.exists(), key.exists()
    if have_cert != have_key:
        present, absent = (cert, key) if have_cert else (key, cert)
        raise CertificateError(
            f"{absent} is missing while {present} exists: restore it, or remove both to get a new self-signed certificate"
        )
    if have_cert:
        current = _load(cert)
        if not _made_here(current) or current.not_valid_after_utc - now > RENEW_BEFORE:
            return None
        reason = "expiring"
    else:
        reason = "missing"
    create_self_signed(cert, key, hosts, now)
    return reason


def _san(hosts: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for host in dict.fromkeys(hosts):  # unique, in order
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def create_self_signed(cert: Path, key: Path, hosts: list[str], now: datetime | None = None) -> None:
    if not hosts:
        raise CertificateError("a certificate needs at least one host name")
    now = now or _now()
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hosts[0][:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ISSUER),
        ]
    )
    built = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))  # clocks a little behind
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.SubjectAlternativeName(_san(hosts)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private, hashes.SHA256())
    )
    write_private(
        key,
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(built.public_bytes(serialization.Encoding.PEM))
