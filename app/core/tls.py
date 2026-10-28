"""TLS certificate for the optional HTTPS listener (PW_HTTPS=yes).

Uses PW_HTTPS_CERT / PW_HTTPS_KEY when both are set. Otherwise generates a
self-signed certificate once, unique to this install, in the data directory
(/data/tls when the Docker /data volume exists, else data/tls) and reuses it,
so a browser exception or trust survives restarts. An expired generated
certificate is replaced. `cryptography` is already a pypowerwall dependency.
"""

import datetime
import ipaddress
import logging
import os
import socket
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_CERT_NAME = "server.crt"
_KEY_NAME = "server.key"
_VALID_DAYS = 3650


def default_tls_dir() -> str:
    """Where the generated certificate lives (same volume as the history DB)."""
    return os.path.join("/data" if os.path.isdir("/data") else "data", "tls")


def certificate_files(
    cert: Optional[str] = None,
    key: Optional[str] = None,
    directory: Optional[str] = None,
) -> Tuple[str, str]:
    """(certfile, keyfile) for the HTTPS listener.

    A user certificate (both cert and key) wins. Otherwise the generated one
    in `directory` is reused while valid, and created when missing or expired.
    """
    if cert or key:
        if not (cert and key):
            raise ValueError("PW_HTTPS_CERT and PW_HTTPS_KEY must be set together")
        return cert, key
    directory = directory or default_tls_dir()
    cert_path = os.path.join(directory, _CERT_NAME)
    key_path = os.path.join(directory, _KEY_NAME)
    if not _valid(cert_path) or not os.path.isfile(key_path):
        _generate(cert_path, key_path)
        logger.info("Generated a self-signed HTTPS certificate in %s", directory)
    return cert_path, key_path


def _valid(cert_path: str) -> bool:
    """True when the generated certificate exists and hasn't expired."""
    from cryptography import x509

    try:
        with open(cert_path, "rb") as file:
            certificate = x509.load_pem_x509_certificate(file.read())
    except (OSError, ValueError):
        return False
    expires = getattr(certificate, "not_valid_after_utc", None)  # cryptography 42+
    if expires is None:
        expires = certificate.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return expires > datetime.datetime.now(datetime.timezone.utc)


def _generate(cert_path: str, key_path: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    hostname = socket.gethostname()
    names = [x509.DNSName("localhost")]
    if hostname and hostname != "localhost":
        names.append(x509.DNSName(hostname))
    names += [
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pypowerwall-server")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    # The private key is readable by the owner only, from before anything is
    # written, including when an older key file is being replaced
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as file:
        file.write(certificate.public_bytes(serialization.Encoding.PEM))
