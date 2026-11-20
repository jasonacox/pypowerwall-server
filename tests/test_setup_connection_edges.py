"""Diagnostic-path coverage for setup test_connection without live gateways."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core import setup as setup_core
from app.models.setup import SetupGatewayInput


@pytest.fixture
def conn_env(monkeypatch, tmp_path):
    authpath = tmp_path / "auth"
    authpath.mkdir()
    old_gateways = settings.gateways
    old_control = settings.control_secret
    old_private = dict(settings.__pydantic_private__)
    monkeypatch.setattr(settings, "gateways", [])
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "pw_authpath", str(authpath), raising=False)
    settings.__pydantic_private__.update(
        {
            "_config_source": "none",
            "_config_path": None,
            "_config_error": None,
            "_config_warnings": [],
            "_config_revision": None,
            "_config_partial": False,
        }
    )
    try:
        yield authpath
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _local_socket(monkeypatch):
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.91.1", 443))],
    )
    monkeypatch.setattr(
        setup_core.socket, "create_connection", lambda *args, **kwargs: FakeSocket()
    )


def test_test_connection_rejects_invalid_gateway():
    with pytest.raises(ValueError, match="Host is required"):
        setup_core.test_gateway_connection(
            SetupGatewayInput(id="home", mode="tedapi", gw_pwd="secret")
        )


def test_cloud_test_connection_missing_auth_file(conn_env):
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "auth_file"
    assert result.steps[-1].status == "failed"


def test_fleet_test_connection_missing_auth_file(conn_env):
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="fleet",
            mode="fleetapi",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "auth_file"


def test_fleet_test_connection_requires_unambiguous_site(conn_env, monkeypatch):
    (conn_env / ".pypowerwall.fleetapi").write_text(
        json.dumps({"site_id": None}), encoding="utf-8"
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites_with_refresh",
        lambda path, config: [
            SimpleNamespace(siteid="1"),
            SimpleNamespace(siteid="2"),
        ],
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="fleet",
            mode="fleetapi",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert result.detected_mode == "FleetAPI"
    assert result.steps[-1].status == "warning"
    assert "Select an energy site" in result.steps[-1].detail


def test_fleet_test_connection_failure_is_reported(conn_env, monkeypatch):
    (conn_env / ".pypowerwall.fleetapi").write_text(
        json.dumps({"site_id": 123}), encoding="utf-8"
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda *args: (_ for _ in ()).throw(RuntimeError("fleet failed")),
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="fleet",
            mode="fleetapi",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "api"
    assert result.steps[-1].status == "failed"


def test_cloud_test_connection_file_without_account(conn_env):
    (conn_env / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "api"
    assert "connection test failed" in result.steps[-1].detail


def test_cloud_test_connection_requested_account_missing(conn_env):
    (conn_env / ".pypowerwall.auth").write_text(
        json.dumps({"other@example.com": {"sso": {"access_token": "x"}}}),
        encoding="utf-8",
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def _cloud_fake(monkeypatch, connect=True, sites=None):
    class FakeCloud:
        def __init__(self, email, authpath="", siteid=None):
            self.email = email
            self.authpath = authpath
            self.siteid = siteid

        def connect(self):
            return connect

        def getsites(self):
            return sites or []

    import pypowerwall.cloud.pypowerwall_cloud as cloud_module

    monkeypatch.setattr(cloud_module, "PyPowerwallCloud", FakeCloud)
    (eng := Path(settings.pw_authpath) / ".pypowerwall.auth").write_text(
        json.dumps({"owner@example.com": {"sso": {"access_token": "cloud-token"}}}),
        encoding="utf-8",
    )
    return eng


def test_cloud_test_connection_connect_failure(conn_env, monkeypatch):
    _cloud_fake(monkeypatch, connect=False)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_cloud_test_connection_no_sites(conn_env, monkeypatch):
    _cloud_fake(monkeypatch, sites=[])
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_cloud_test_connection_site_not_available(conn_env, monkeypatch):
    _cloud_fake(monkeypatch, sites=[{"energy_site_id": 123}])
    (conn_env / ".pypowerwall.site").write_text("999", encoding="utf-8")
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_cloud_test_connection_uses_site_file(conn_env, monkeypatch):
    _cloud_fake(monkeypatch, sites=[{"energy_site_id": 123}])
    (conn_env / ".pypowerwall.site").write_text("123", encoding="utf-8")
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="cloud",
            mode="cloud",
            email="owner@example.com",
            authpath=str(conn_env),
        )
    )
    assert result.ok is True
    assert result.detected_mode == "Cloud"


def test_test_connection_rejects_public_dns_result(conn_env, monkeypatch):
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="home",
            mode="local",
            host="gateway.example.com",
            password="customer",
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "dns"
    assert "not private" in result.steps[-1].detail


def test_test_connection_dns_failure(conn_env, monkeypatch):
    def fail_dns(*args, **kwargs):
        raise socket.gaierror("unknown host")

    monkeypatch.setattr(setup_core.socket, "getaddrinfo", fail_dns)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="home",
            mode="local",
            host="gateway.example.com",
            password="customer",
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "dns"


def test_test_connection_tcp_failure(conn_env, monkeypatch):
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.91.1", 443))],
    )
    monkeypatch.setattr(
        setup_core.socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("refused")),
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="home",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "tcp"


def test_v1r_test_connection_uses_gateway_password_suffix(conn_env, monkeypatch):
    key = conn_env / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    _local_socket(monkeypatch)
    seen = {}

    class FakeV1r:
        def __init__(self, host, password, rsa_key_path, timeout=5, poolmaxsize=5):
            seen["password"] = password

        def login(self):
            return False

    import pypowerwall.tedapi.tedapi_v1r as v1r_module

    monkeypatch.setattr(v1r_module, "TEDAPIv1r", FakeV1r)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="v1r",
            mode="tedapi_v1r",
            host="192.168.91.1",
            gw_pwd="full-password",
            rsa_key_path=str(key),
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "auth"
    assert "login failed" in result.steps[-1].detail


def test_v1r_test_connection_din_failure(conn_env, monkeypatch):
    key = conn_env / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    _local_socket(monkeypatch)

    class FakeV1r:
        def __init__(self, *args, **kwargs):
            pass

        def login(self):
            return True

        def get_din(self):
            return None

    import pypowerwall.tedapi.tedapi_v1r as v1r_module

    monkeypatch.setattr(v1r_module, "TEDAPIv1r", FakeV1r)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="v1r",
            mode="tedapi_v1r",
            host="192.168.91.1",
            password="customer",
            rsa_key_path=str(key),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_v1r_test_connection_empty_config(conn_env, monkeypatch):
    key = conn_env / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    _local_socket(monkeypatch)

    class FakeV1r:
        def __init__(self, *args, **kwargs):
            pass

        def login(self):
            return True

        def get_din(self):
            return "DIN"

        def get_config_v1r(self, din):
            return {}

    import pypowerwall.tedapi.tedapi_v1r as v1r_module

    monkeypatch.setattr(v1r_module, "TEDAPIv1r", FakeV1r)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="v1r",
            mode="tedapi_v1r",
            host="192.168.91.1",
            password="customer",
            rsa_key_path=str(key),
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


class _FakePowerwall:
    tedapi_mode = "TEDAPI"
    connected = True
    aggregates = {"site": {}, "solar": {}}
    fail_version = False
    fail_site_name = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def is_connected(self):
        return self.connected

    def poll(self, path, force=False):
        return self.aggregates

    def version(self):
        if self.fail_version:
            raise RuntimeError("no version")
        return "26.42.0"

    def site_name(self):
        if self.fail_site_name:
            raise RuntimeError("no site")
        return "Home"


def _fake_powerwall(monkeypatch, cls=None):
    _local_socket(monkeypatch)
    monkeypatch.setattr(setup_core.pypowerwall, "Powerwall", cls or _FakePowerwall)


def test_test_connection_local_with_all_optional_fields(conn_env, monkeypatch):
    key = conn_env / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    _fake_powerwall(monkeypatch)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
            rsa_key_path=str(key),
            wifi_host="192.168.91.2",
            email="owner@example.com",
            authpath=str(conn_env),
            siteid="123",
        )
    )
    assert result.ok is True
    assert result.detected_mode == "TEDAPI"


def test_test_connection_uses_existing_credentials(conn_env, monkeypatch):
    _fake_powerwall(monkeypatch)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="home",
            mode="local",
            host="192.168.91.1",
        ),
        {"password": "customer"},
    )
    assert result.ok is True


def test_test_connection_uses_runtime_gateway_secrets(conn_env, monkeypatch):
    _fake_powerwall(monkeypatch)
    from app.config import GatewayConfig

    monkeypatch.setattr(
        settings,
        "gateways",
        [
            GatewayConfig(
                id="home",
                host="192.168.91.1",
                password="customer",
            )
        ],
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(id="home", mode="local", host="192.168.91.1")
    )
    assert result.ok is True


def test_test_connection_rejects_removed_bearer_mode():
    with pytest.raises(ValueError):
        SetupGatewayInput(
            id="inverter",
            mode="tedapi_bearer",
            host="192.168.91.1",
            gw_pwd="secret",
            type="inverter",
        )


def test_test_connection_falls_back_to_derived_mode(conn_env, monkeypatch):
    class NoMode(_FakePowerwall):
        tedapi_mode = None

    _fake_powerwall(monkeypatch, NoMode)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is True
    assert result.detected_mode == "Local/Basic LAN"


def test_test_connection_reports_authentication_failure(conn_env, monkeypatch):
    class NotConnected(_FakePowerwall):
        connected = False

    _fake_powerwall(monkeypatch, NotConnected)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is False
    assert result.steps[-1].key == "auth"


def test_test_connection_reports_meter_errors(conn_env, monkeypatch):
    class MeterError(_FakePowerwall):
        aggregates = {"error": "meter unavailable"}

    _fake_powerwall(monkeypatch, MeterError)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail

    class EmptyMeter(_FakePowerwall):
        aggregates = {}

    _fake_powerwall(monkeypatch, EmptyMeter)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_test_connection_tolerates_version_and_name_failures(conn_env, monkeypatch):
    class MetadataFailure(_FakePowerwall):
        fail_version = True
        fail_site_name = True

    _fake_powerwall(monkeypatch, MetadataFailure)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is True
    assert result.firmware is None
    assert result.site_name is None


def test_test_connection_handles_powerwall_constructor_exception(conn_env, monkeypatch):
    _local_socket(monkeypatch)

    def fail(**kwargs):
        raise RuntimeError("constructor failed")

    monkeypatch.setattr(setup_core.pypowerwall, "Powerwall", fail)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="customer",
        )
    )
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_test_connection_does_not_echo_upstream_exception_text(conn_env, monkeypatch):
    _local_socket(monkeypatch)

    def fail(**kwargs):
        raise RuntimeError(
            "login failed password=secret-password "
            "access_token=abc123 Bearer tok-abc unlabeled-" + ("A" * 48)
        )

    monkeypatch.setattr(setup_core.pypowerwall, "Powerwall", fail)
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="local",
            mode="local",
            host="192.168.91.1",
            password="secret-password",
        )
    )
    detail = result.steps[-1].detail
    assert result.ok is False
    assert detail == "RuntimeError: connection test failed"
    assert "login failed" not in detail
    assert "secret-password" not in detail
    assert "abc123" not in detail
    assert "tok-abc" not in detail
    assert "A" * 48 not in detail


def test_diagnostic_detail_includes_safe_http_status():
    class UpstreamError(Exception):
        status_code = 401

    assert setup_core._diagnostic_detail(UpstreamError("secret")) == (
        "UpstreamError (HTTP 401): connection test failed"
    )
    assert "secret" not in setup_core._diagnostic_detail(RuntimeError("short-secret"))
