"""Optional HTTPS listener (PW_HTTPS=yes) and its self-signed certificate."""

import asyncio
import datetime
import os
import socket
import stat
import sys
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from app.core import tls
from app.core.https_listener import start_https_listener, stop_https_listener

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits"
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _settings(tmp_path, **overrides):
    cert, key = tls.certificate_files(directory=str(tmp_path / "tls"))
    values = dict(
        https_mode=True, https_port=_free_port(), server_port=8675,
        server_host="127.0.0.1", https_cert=cert, https_key=key, debug=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --- certificate ---


def test_generates_a_private_certificate_once_and_reuses_it(tmp_path):
    directory = str(tmp_path / "tls")
    cert, key = tls.certificate_files(directory=directory)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600, "owner-only key"
    with open(cert, "rb") as f:
        first = f.read()
    certificate = x509.load_pem_x509_certificate(first)
    sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in sans.get_values_for_type(x509.DNSName)
    assert "127.0.0.1" in {str(ip) for ip in sans.get_values_for_type(x509.IPAddress)}
    assert tls.certificate_files(directory=directory) == (cert, key)
    with open(cert, "rb") as f:
        assert f.read() == first, "reused, not regenerated"


@posix_only
def test_a_replaced_key_file_becomes_owner_only(tmp_path):
    directory = tmp_path / "tls"
    directory.mkdir()
    old = directory / "server.key"
    old.write_text("old")
    old.chmod(0o644)
    _, key = tls.certificate_files(directory=str(directory))  # no valid cert yet
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


def test_each_install_gets_its_own_key(tmp_path):
    _, a = tls.certificate_files(directory=str(tmp_path / "a"))
    _, b = tls.certificate_files(directory=str(tmp_path / "b"))
    assert open(a, "rb").read() != open(b, "rb").read()


def test_an_expired_generated_certificate_is_replaced(tmp_path):
    directory = tmp_path / "tls"
    cert, key = tls.certificate_files(directory=str(directory))
    # Sign with the existing key so expiry is the only reason to replace it
    private = serialization.load_pem_private_key(open(key, "rb").read(), None)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old")])
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    expired = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private.public_key()).serial_number(1)
        .not_valid_before(past).not_valid_after(past + datetime.timedelta(days=1))
        .sign(private, hashes.SHA256())
    )
    with open(cert, "wb") as f:
        f.write(expired.public_bytes(serialization.Encoding.PEM))
    tls.certificate_files(directory=str(directory))
    renewed = x509.load_pem_x509_certificate(open(cert, "rb").read())
    assert renewed.subject != name


def test_user_certificate_wins_and_needs_both_files(tmp_path):
    cert, key = tls.certificate_files(directory=str(tmp_path / "mine"))
    other = str(tmp_path / "unused")
    assert tls.certificate_files(cert, key, directory=other) == (cert, key)
    assert not os.path.exists(other)
    with pytest.raises(ValueError):
        tls.certificate_files(cert, None)


def test_an_encrypted_user_key_is_rejected_without_prompting(tmp_path):
    """uvicorn would wait for a passphrase on the terminal, blocking startup."""
    cert, key = tls.certificate_files(directory=str(tmp_path / "tls"))
    private = serialization.load_pem_private_key(open(key, "rb").read(), None)
    encrypted = str(tmp_path / "encrypted.key")
    with open(encrypted, "wb") as f:
        f.write(private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        ))
    with pytest.raises(ValueError, match="encrypted"):
        tls.certificate_files(cert, encrypted)


def test_default_directory_is_the_data_volume(monkeypatch):
    monkeypatch.setattr(tls.os.path, "isdir", lambda path: path == "/data")
    assert tls.default_tls_dir() == "/data/tls"
    monkeypatch.setattr(tls.os.path, "isdir", lambda path: False)
    assert tls.default_tls_dir() == os.path.join("data", "tls")


# --- listener ---


@pytest.mark.asyncio
async def test_serves_the_app_over_https_with_secure_cookies(tmp_path):
    from app.main import app

    import signal

    settings = _settings(tmp_path)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    listener = await start_https_listener(app, settings)
    assert listener is not None
    server, _ = listener
    # Startup/shutdown run once (in the main server), which also owns signals
    assert server.config.lifespan == "off"
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    try:
        async with httpx.AsyncClient(verify=False) as client:
            r = await client.get(f"https://127.0.0.1:{settings.https_port}/health")
        assert r.status_code == 200
        cookies = [v for k, v in r.headers.multi_items() if k == "set-cookie"]
        assert cookies and all("Secure" in c and "samesite=none" in c.lower() for c in cookies)
    finally:
        await stop_https_listener(listener)
    assert listener[1].done()


@pytest.mark.asyncio
async def test_a_port_in_use_logs_and_keeps_the_loop_running(tmp_path, caplog):
    """uvicorn calls sys.exit() on a bind error; it must not stop HTTP."""
    from app.main import app

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        settings = _settings(tmp_path, https_port=busy.getsockname()[1])
        assert await start_https_listener(app, settings) is None
    assert "HTTPS not started" in caplog.text
    await asyncio.sleep(0)  # still running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"https_mode": False},
        {"https_port": 8675},  # same as PW_PORT
        {"https_cert": "/nonexistent.crt", "https_key": "/nonexistent.key"},
    ],
)
async def test_off_clashing_or_broken_settings_start_nothing(tmp_path, overrides):
    from app.main import app

    assert await start_https_listener(app, _settings(tmp_path, **overrides)) is None


# --- cookies and /stats ---


def test_cookies_follow_the_request_scheme(client):
    cookie = client.post("/api/login/Basic", headers={}).headers["set-cookie"]
    assert "Secure" not in cookie and "samesite=lax" in cookie.lower()
    client.base_url = "https://testserver"
    cookie = client.post("/api/login/Basic").headers["set-cookie"]
    assert "Secure" in cookie and "samesite=none" in cookie.lower()


def test_stats_reports_the_https_settings(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "https_mode", True)
    monkeypatch.setattr(settings, "https_port", 8775)
    config = client.get("/stats").json()["config"]
    assert (config["PW_HTTPS"], config["PW_HTTPS_PORT"]) == ("yes", 8775)


def test_generates_without_fchmod(tmp_path, monkeypatch):
    """Windows has no os.fchmod: generation still works."""
    monkeypatch.delattr(tls.os, "fchmod", raising=False)
    cert, key = tls.certificate_files(directory=str(tmp_path / "tls"))
    assert os.path.isfile(cert) and os.path.isfile(key)


@pytest.mark.parametrize("damage", ["empty", "truncated", "other_key"])
def test_a_broken_or_mismatched_key_is_regenerated(tmp_path, damage):
    """A still-valid certificate with an unusable key would fail every restart."""
    directory = str(tmp_path / "tls")
    cert, key = tls.certificate_files(directory=directory)
    if damage == "other_key":
        _, other = tls.certificate_files(directory=str(tmp_path / "other"))
        content = open(other, "rb").read()
    else:
        content = b"" if damage == "empty" else open(key, "rb").read()[:40]
    with open(key, "wb") as f:
        f.write(content)
    assert not tls._usable(cert, key)
    tls.certificate_files(directory=directory)
    assert tls._usable(cert, key)


def test_a_non_ascii_hostname_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(tls.socket, "gethostname", lambda: "café")
    cert, _ = tls.certificate_files(directory=str(tmp_path / "tls"))
    sans = x509.load_pem_x509_certificate(open(cert, "rb").read()).extensions
    names = sans.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]


@pytest.mark.asyncio
async def test_a_stalled_start_gives_up_instead_of_hanging(tmp_path, monkeypatch):
    from app.core import https_listener
    from app.main import app

    async def never_starts(self, sockets=None):
        while not self.should_exit:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(https_listener, "_START_TIMEOUT", 0.2)
    monkeypatch.setattr(https_listener._EmbeddedServer, "serve", never_starts)
    assert await start_https_listener(app, _settings(tmp_path)) is None
