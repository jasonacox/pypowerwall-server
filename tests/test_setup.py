"""Tests for the setup wizard API and atomic configuration."""

import hashlib
import json
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml

from app.config import settings
from app.core import setup as setup_core
from app.core.setup import (
    apply_setup_request,
    get_setup_status,
)
from app.models.setup import (
    SetupAuthFileRequest,
    SetupGatewayInput,
    SetupAuthTokensRequest,
    SetupCloudAuthUseRequest,
    SetupConfigureRequest,
    SetupEnvImportRequest,
    SetupFleetCompleteRequest,
    SetupFleetKeyRequest,
    SetupFleetSiteRequest,
    SetupFleetStartRequest,
    SetupScanRequest,
    SetupV1rAuthFileRequest,
    SetupV1rRegisterRequest,
    SetupV1rStatusRequest,
)


@pytest.fixture
def setup_env(monkeypatch, tmp_path):
    path = tmp_path / "gateways.yaml"
    monkeypatch.setattr(setup_core, "default_config_path", lambda: str(path))
    old_gateways = settings.gateways
    old_control = settings.control_secret
    old_private = dict(settings.__pydantic_private__)
    old_token = setup_core._SETUP_TOKEN
    settings.gateways = []
    settings.control_secret = None
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
    setup_core._SETUP_TOKEN = "test-setup-token"
    try:
        yield path
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)
        setup_core._SETUP_TOKEN = old_token


def _payload(**overrides):
    payload = {
        "gateways": [
            {
                "id": "home",
                "name": "Home",
                "mode": "tedapi",
                "host": "192.168.91.1",
                "gw_pwd": "secret-password",
                "timezone": "Europe/Berlin",
            }
        ],
        "options": {},
        "expected_revision": None,
    }
    payload.update(overrides)
    return payload


def _request(**overrides):
    return SetupConfigureRequest(**_payload(**overrides))


def test_configure_writes_yaml_and_status_hides_secret(setup_env, client):
    response = client.post(
        "/api/setup/configure",
        json=_payload(),
        headers={"X-PW-Setup-Token": "test-setup-token"},
    )
    assert response.status_code == 200, response.text
    assert setup_env.exists()
    doc = yaml.safe_load(setup_env.read_text())
    assert doc["gateways"][0]["gw_pwd"] == "secret-password"
    assert doc["gateways"][0]["host"] == "192.168.91.1"
    assert doc["gateways"][0]["timezone"] == "Europe/Berlin"

    from app.config import GatewayConfig

    settings.gateways = [
        GatewayConfig(
            id="home",
            host="192.168.91.1",
            gw_pwd="secret-password",
        )
    ]
    status = get_setup_status()
    assert "secret-password" not in str(status.model_dump())
    settings.gateways = []


def test_configure_rename_preserves_stored_secret(setup_env):
    apply_setup_request(_request())
    revision = hashlib.sha256(setup_env.read_bytes()).hexdigest()
    result = apply_setup_request(
        SetupConfigureRequest(
            gateways=[
                {
                    "id": "renamed",
                    "original_id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
    assert doc["gateways"][0]["id"] == "renamed"
    assert doc["gateways"][0]["gw_pwd"] == "secret-password"


def test_configure_hybrid_fleetapi_preserves_local_fields(setup_env):
    authpath = setup_env.parent / "auth"
    authpath.mkdir(exist_ok=True)
    result = apply_setup_request(
        SetupConfigureRequest(
            gateways=[
                {
                    "id": "hybrid",
                    "mode": "hybrid_fleetapi",
                    "host": "192.168.91.1",
                    "gw_pwd": "secret",
                    "email": "owner@example.com",
                    "authpath": str(authpath),
                }
            ]
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
    gateway = doc["gateways"][0]
    assert gateway["host"] == "192.168.91.1"
    assert gateway["gw_pwd"] == "secret"
    assert gateway["fleetapi"] is True
    assert gateway["cloud_mode"] is False


def test_configure_allows_empty_gateway_list(setup_env):
    result = apply_setup_request(SetupConfigureRequest(gateways=[]))
    assert result.saved is True
    doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
    assert doc["gateways"] == []


def test_configure_merges_existing_secret_and_server_options(setup_env):
    first = _request()
    apply_setup_request(first)
    revision = hashlib.sha256(setup_env.read_bytes()).hexdigest()
    settings._config_source = "default_file"
    settings._config_path = str(setup_env)
    second = _request(
        gateways=[
            {
                "id": "home",
                "mode": "tedapi",
                "host": "192.168.91.1",
                "timezone": "Europe/Berlin",
            }
        ],
        options={
            "server_port": 9999,
            "cors_origins": ["http://localhost:3000"],
            "mqtt_ha_discovery": False,
        },
        expected_revision=revision,
    )
    result = apply_setup_request(second)
    assert result.saved is True
    doc = yaml.safe_load(setup_env.read_text())
    assert doc["gateways"][0]["gw_pwd"] == "secret-password"
    assert doc["server"]["port"] == 9999
    assert doc["server"]["cors_origins"] == ["http://localhost:3000"]
    assert doc["settings"]["mqtt_ha_discovery"] is False
    assert setup_env.with_suffix(".yaml.bak").exists()


def test_setup_schema_lists_all_server_timezones(setup_env):
    schema = setup_core.get_setup_schema()
    timezones = schema["timezones"]
    assert len(timezones) > 100
    assert "Europe/Berlin" in timezones
    assert "America/Los_Angeles" in timezones
    assert "UTC" in timezones
    assert timezones == sorted(timezones)


def test_env_managed_config_cannot_be_overwritten(setup_env, monkeypatch):
    monkeypatch.setattr(settings, "_config_source", "legacy_env")
    with pytest.raises(ValueError, match="environment"):
        apply_setup_request(_request())


def test_revision_conflict_is_rejected(setup_env):
    apply_setup_request(_request())
    with pytest.raises(ValueError, match="changed"):
        apply_setup_request(_request(expected_revision="wrong"))


def test_validate_rejects_unknown_option(setup_env):
    result, _, _ = setup_core.validate_setup_request(
        _request(options={"not_a_setting": True}), {}
    )
    assert result.valid is False
    assert any("unknown" in issue.message.lower() for issue in result.errors)


def test_validate_requires_v1r_key(setup_env, tmp_path):
    request = _request(
        gateways=[
            {
                "id": "home",
                "mode": "tedapi_v1r",
                "host": "10.42.1.40",
                "gw_pwd": "secret-password",
            }
        ]
    )
    result, _, _ = setup_core.validate_setup_request(request, {})
    assert result.valid is False
    assert any(issue.field == "rsa_key_path" for issue in result.errors)


def test_api_requires_setup_token(setup_env, client):
    assert client.get("/api/setup/status").status_code == 401
    assert (
        client.get(
            "/api/setup/status",
            headers={"X-PW-Setup-Token": "wrong"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/setup/status",
            headers={"X-PW-Setup-Token": "test-setup-token"},
        ).status_code
        == 200
    )


def test_setup_validation_errors_do_not_echo_input(setup_env, client):
    response = client.post(
        "/api/setup/validate",
        json={
            "gateways": [{"id": "home", "mode": "tedapi", "port": "secret-value"}],
            "options": {},
        },
        headers={"X-PW-Setup-Token": "test-setup-token"},
    )
    assert response.status_code == 422
    assert "secret-value" not in response.text
    assert "input" not in response.text


def test_setup_page_and_console_link(setup_env, client):
    page = client.get("/setup")
    assert page.status_code == 200
    assert "PyPowerwall Server Setup" in page.text
    assert "{PROXY_BASE}" not in page.text
    console = client.get("/console")
    assert "Open setup" in console.text


def test_test_connection_reports_steps(setup_env, monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakePowerwall:
        tedapi_mode = "full"

        def is_connected(self):
            return True

        def poll(self, path, force=False):
            assert path == "/api/meters/aggregates"
            return {"site": {}, "solar": {}, "battery": {}, "load": {}}

        def version(self):
            return "26.42.0"

        def site_name(self):
            return "Home"

    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.91.1", 443))],
    )
    monkeypatch.setattr(
        setup_core.socket, "create_connection", lambda *args, **kwargs: FakeSocket()
    )
    monkeypatch.setattr(
        setup_core.pypowerwall, "Powerwall", lambda **kwargs: FakePowerwall()
    )
    result = setup_core.test_gateway_connection(
        setup_core.SetupGatewayInput(
            id="home",
            mode="tedapi",
            host="192.168.91.1",
            gw_pwd="secret-password",
        )
    )
    assert result.ok is True
    assert result.firmware == "26.42.0"
    assert result.site_name == "Home"
    assert [step.key for step in result.steps] == [
        "config",
        "dns",
        "tcp",
        "auth",
        "data",
    ]


def test_invalid_existing_file_can_be_repaired_with_backup(setup_env):
    setup_env.write_text("gateways: [not: valid", encoding="utf-8")
    digest = hashlib.sha256(setup_env.read_bytes()).hexdigest()
    settings._config_source = "default_file"
    settings._config_path = str(setup_env)
    settings._config_error = "Unable to read config file (ScannerError)"
    result = apply_setup_request(_request(expected_revision=digest))
    assert result.saved is True
    assert setup_env.with_suffix(".yaml.bak").exists()
    doc = yaml.safe_load(setup_env.read_text())
    assert doc["gateways"][0]["id"] == "home"


def test_configure_repairs_non_list_gateways_via_api(setup_env, client):
    setup_env.write_text("gateways: 5\n", encoding="utf-8")
    digest = hashlib.sha256(setup_env.read_bytes()).hexdigest()
    settings._config_source = "default_file"
    settings._config_path = str(setup_env)
    settings._config_error = "Config file has no 'gateways' list"
    settings._config_revision = digest
    settings._config_warnings = []
    settings._config_partial = False
    settings.gateways = []

    headers = {"X-PW-Setup-Token": "test-setup-token"}
    validation = client.post("/api/setup/validate", json=_payload(), headers=headers)
    assert validation.status_code == 200, validation.text
    assert any(
        "not a list" in warning["message"] for warning in validation.json()["warnings"]
    )

    response = client.post(
        "/api/setup/configure",
        json=_payload(expected_revision=digest),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
    assert isinstance(doc["gateways"], list)
    assert doc["gateways"][0]["id"] == "home"


def test_cloud_auth_flow_uses_existing_helpers(setup_env, monkeypatch, tmp_path):
    from pypowerwall import tesla_auth

    monkeypatch.setattr(
        tesla_auth,
        "_build_auth_url",
        lambda region="us": ("https://auth.example/login", "verifier", "state-1"),
    )
    monkeypatch.setattr(
        tesla_auth,
        "_exchange_code",
        lambda code, verifier, region="us": {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    class FakeCloud:
        def __init__(self, email, authpath="", siteid=None):
            self.email = email
            self.authpath = authpath
            self.siteid = siteid

        def connect(self):
            return True

        def getsites(self):
            return [{"energy_site_id": 123, "site_name": "Home"}]

    import pypowerwall.cloud.pypowerwall_cloud as cloud_module

    monkeypatch.setattr(cloud_module, "PyPowerwallCloud", FakeCloud)
    start = setup_core.cloud_auth_start("owner@example.com", str(tmp_path))
    assert "verifier" not in start.model_dump()
    assert "state-1" in setup_core._CLOUD_AUTH_STATES
    result = setup_core.cloud_auth_complete(
        setup_core.SetupCloudAuthCompleteRequest(
            callback_url="tesla://auth/callback?code=abc&state=state-1"
        )
    )
    assert result.saved is True
    assert result.siteid == "123"
    assert (tmp_path / ".pypowerwall.site").read_text() == "123"
    assert "access_token" not in result.model_dump()


def test_cloud_auth_use_reuses_existing_auth_and_selects_site(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-cloud-use"
    root.mkdir(parents=True, exist_ok=True)
    auth = {
        "owner@example.com": {
            "sso": {
                "access_token": "existing-at",
                "refresh_token": "existing-rt",
                "expires_at": int(time.time() + 3600),
            }
        }
    }
    (root / ".pypowerwall.auth").write_text(json.dumps(auth), encoding="utf-8")

    class FakeCloud:
        def __init__(self, email, authpath="", siteid=None):
            self.email = email
            self.authpath = authpath
            self.siteid = siteid

        def connect(self):
            return True

        def getsites(self):
            return [{"energy_site_id": 123, "site_name": "Home"}]

    import pypowerwall.cloud.pypowerwall_cloud as cloud_module

    monkeypatch.setattr(cloud_module, "PyPowerwallCloud", FakeCloud)
    try:
        result = setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(root)))
        assert result.saved is True
        assert result.email == "owner@example.com"
        assert result.siteid == "123"
        assert result.sites[0]["siteid"] == "123"
        assert (root / ".pypowerwall.site").read_text(encoding="utf-8") == "123"
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_diagnostic_is_mocked_without_real_credentials(
    setup_env, monkeypatch, tmp_path
):
    key = tmp_path / "tedapi_rsa_private.pem"
    key.write_text("not-a-real-key", encoding="utf-8")

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeV1r:
        def __init__(self, host, password, rsa_key_path, timeout=5, poolmaxsize=5):
            self.host = host
            self.password = password
            self.rsa_key_path = rsa_key_path

        def login(self):
            return True

        def get_din(self):
            return "DIN-TEST"

        def get_config_v1r(self, din):
            return {"site_info": {"site_name": "Mocked"}}

    import pypowerwall.tedapi.tedapi_v1r as v1r_module

    monkeypatch.setattr(v1r_module, "TEDAPIv1r", FakeV1r)
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.91.1", 443))],
    )
    monkeypatch.setattr(
        setup_core.socket,
        "create_connection",
        lambda *args, **kwargs: FakeSocket(),
    )
    result = setup_core.test_gateway_connection(
        setup_core.SetupGatewayInput(
            id="v1r",
            mode="tedapi_v1r",
            host="192.168.91.1",
            password="dummy-customer-password",
            rsa_key_path=str(key),
        )
    )
    assert result.ok is True
    assert result.detected_mode == "TEDAPI v1r"
    assert [step.key for step in result.steps] == [
        "config",
        "dns",
        "tcp",
        "auth",
        "data",
    ]


def test_v1r_key_generation_is_mocked(setup_env, monkeypatch, tmp_path):
    def fake_generate(path):
        return (None, b"public-der")

    import pypowerwall.v1r_register as register_module

    monkeypatch.setattr(register_module, "generate_rsa_key", fake_generate)
    key_dir = Path.cwd() / "data" / "test-v1r-key"
    try:
        result = setup_core.v1r_key(str(key_dir))
    finally:
        if key_dir.exists():
            import shutil

            shutil.rmtree(key_dir)
    assert result["registration_required"] is True
    assert result["private_key_path"].endswith("tedapi_rsa_private.pem")
    assert len(result["fingerprint"]) == 64


def _write_v1r_auth(root, expires_at=None):
    auth = {
        "owner@example.com": {
            "sso": {
                "access_token": "owner-access-token",
                "refresh_token": "owner-refresh-token",
                "expires_at": int(
                    expires_at if expires_at is not None else time.time() + 3600
                ),
            }
        }
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / ".pypowerwall.auth").write_text(json.dumps(auth), encoding="utf-8")


def _fake_v1r_register_module(monkeypatch, sites, register_state=1, status_state=3):
    import pypowerwall.v1r_register as register_module

    calls = []

    monkeypatch.setattr(
        register_module,
        "generate_rsa_key",
        lambda path: (None, b"public-der"),
    )

    def fake_api_call(url, method="GET", data=None, headers=None, token=None):
        calls.append({"url": url, "method": method, "data": data, "token": token})
        if url.endswith("/api/1/products"):
            return 200, {"response": sites}
        authorization = (
            (data or {})
            .get("command_properties", {})
            .get("message", {})
            .get("authorization", {})
        )
        if "add_authorized_client_request" in authorization:
            state = register_state
        else:
            state = status_state
        return 200, {
            "response": {
                "message": {
                    "Payload": {
                        "Authorization": {
                            "Message": {
                                "AddAuthorizedClientResponse": {
                                    "client": {"state": state}
                                }
                            }
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(register_module, "api_call", fake_api_call)
    return calls


def test_v1r_registration_uses_existing_auth_and_reports_state(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-v1r-register"
    try:
        _write_v1r_auth(root)
        calls = _fake_v1r_register_module(
            monkeypatch,
            [
                {
                    "energy_site_id": 123,
                    "site_name": "Home",
                    "gateway_id": "DIN-123",
                }
            ],
        )
        result = setup_core.v1r_register(SetupV1rRegisterRequest(authpath=str(root)))
        assert result.pending is True
        assert result.verified is False
        assert result.siteid == "123"
        assert result.email == "owner@example.com"
        assert result.sites[0].siteid == "123"
        assert "On/Off" in (result.proof_hint or "")
        assert "access_token" not in result.model_dump()
        assert [call["token"] for call in calls] == [
            "owner-access-token",
            "owner-access-token",
        ]
        assert calls[0]["url"].endswith("/api/1/products")
        assert calls[1]["url"].endswith("/api/1/energy_sites/123/command")
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_registration_refreshes_expired_owner_token(setup_env, monkeypatch):
    import pypowerwall.tesla_auth as auth_module

    monkeypatch.setattr(
        auth_module,
        "_refresh_access_token",
        lambda token: {
            "access_token": "refreshed-owner-token",
            "refresh_token": "refreshed-refresh-token",
            "expires_in": 3600,
        },
    )
    root = Path.cwd() / "data" / "test-v1r-refresh"
    try:
        _write_v1r_auth(root, expires_at=time.time() - 10)
        calls = _fake_v1r_register_module(
            monkeypatch,
            [{"energy_site_id": 123, "site_name": "Home"}],
        )
        result = setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(root), siteid="123")
        )
        assert result.pending is True
        assert [call["token"] for call in calls] == [
            "refreshed-owner-token",
            "refreshed-owner-token",
        ]
        cached = json.loads((root / ".pypowerwall.auth").read_text(encoding="utf-8"))[
            "owner@example.com"
        ]["sso"]
        assert cached["access_token"] == "refreshed-owner-token"
        assert cached["access_token"] != "owner-access-token"
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_registration_status_reports_verified(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-v1r-status"
    try:
        _write_v1r_auth(root)
        _fake_v1r_register_module(
            monkeypatch,
            [
                {
                    "energy_site_id": 123,
                    "site_name": "Home",
                    "gateway_id": "DIN-123",
                }
            ],
            status_state=3,
        )
        result = setup_core.v1r_registration_status(
            SetupV1rStatusRequest(authpath=str(root), siteid="123")
        )
        assert result.verified is True
        assert result.state == 3
        assert result.message == "Key is VERIFIED and ready for use."
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_registration_requires_site_selection(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-v1r-multi"
    try:
        _write_v1r_auth(root)
        calls = _fake_v1r_register_module(
            monkeypatch,
            [
                {"energy_site_id": 123, "site_name": "Home"},
                {"energy_site_id": 456, "site_name": "Cabin"},
            ],
        )
        result = setup_core.v1r_register(SetupV1rRegisterRequest(authpath=str(root)))
        assert result.needs_site is True
        assert [site.siteid for site in result.sites] == ["123", "456"]
        assert len(calls) == 1
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_registration_requires_tesla_login(setup_env):
    root = Path.cwd() / "data" / "test-v1r-no-auth"
    try:
        with pytest.raises(ValueError, match="Tesla login required"):
            setup_core.v1r_register(SetupV1rRegisterRequest(authpath=str(root)))
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_registration_rejects_expired_token(setup_env, monkeypatch):
    import pypowerwall.tesla_auth as auth_module

    def fail_refresh(token):
        raise RuntimeError("refresh rejected")

    monkeypatch.setattr(auth_module, "_refresh_access_token", fail_refresh)
    root = Path.cwd() / "data" / "test-v1r-expired"
    try:
        _write_v1r_auth(root, expires_at=time.time() - 10)
        with pytest.raises(ValueError, match="expired"):
            setup_core.v1r_register(SetupV1rRegisterRequest(authpath=str(root)))
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_auth_file_import_writes_existing_login(setup_env):
    root = Path.cwd() / "data" / "test-v1r-upload"
    content = json.dumps(
        {
            "owner@example.com": {
                "sso": {
                    "access_token": "owner-access-token",
                    "refresh_token": "owner-refresh-token",
                    "expires_at": int(time.time() + 3600),
                }
            }
        }
    )
    try:
        result = setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content=content)
        )
        assert result.saved is True
        assert result.email == "owner@example.com"
        assert result.accounts == 1
        target = root / ".pypowerwall.auth"
        assert target.read_text(encoding="utf-8").startswith("{")
        assert "owner-access-token" in target.read_text(encoding="utf-8")
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_v1r_auth_file_import_rejects_invalid_content(setup_env):
    root = Path.cwd() / "data" / "test-v1r-upload-invalid"
    try:
        with pytest.raises(ValueError, match="valid .pypowerwall.auth"):
            setup_core.v1r_auth_file(
                SetupV1rAuthFileRequest(authpath=str(root), content="not-json")
            )
        with pytest.raises(ValueError, match="no Tesla account"):
            setup_core.v1r_auth_file(
                SetupV1rAuthFileRequest(authpath=str(root), content="{}")
            )
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


class _FleetResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _fake_fleet_http(monkeypatch, products=None, public_key_status=200):
    import pypowerwall.fleetapi.fleetapi as fleet_module

    calls = []

    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    def fake_http(
        method,
        url,
        *,
        headers=None,
        data=None,
        json_data=None,
        params=None,
        timeout=None,
        verify=True,
    ):
        calls.append({"method": method, "url": url, "data": data})
        if url.endswith("com.tesla.3p.public-key.pem"):
            return _FleetResponse(public_key_status, text="pem")
        if url.endswith("/oauth2/v3/token"):
            if (
                isinstance(data, dict)
                and data.get("grant_type") == "client_credentials"
            ):
                return _FleetResponse(200, {"access_token": "partner-token"})
            return _FleetResponse(
                200,
                {
                    "access_token": "fleet-access-token",
                    "refresh_token": "fleet-refresh-token",
                },
            )
        if url.endswith("/api/1/partner_accounts"):
            return _FleetResponse(200, {"response": {"domain": "example.com"}})
        if url.endswith("/api/1/products"):
            return _FleetResponse(200, {"response": products or []})
        raise AssertionError("unexpected FleetAPI URL: " + url)

    monkeypatch.setattr(fleet_module, "_http2_request", fake_http)

    def fake_public_key(url):
        calls.append({"method": "GET", "url": url, "data": None})
        return public_key_status

    monkeypatch.setattr(setup_core, "_fleet_fetch_public_key", fake_public_key)
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda path, siteid=None: {"site_name": "Home", "battery_percent": 50},
    )
    return calls


def test_fleetapi_setup_start_and_complete(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-fleet-setup"
    try:
        calls = _fake_fleet_http(
            monkeypatch,
            products=[
                {
                    "energy_site_id": 123,
                    "site_name": "Home",
                    "resource_type": "battery",
                }
            ],
        )
        start = setup_core.fleet_setup_start(
            SetupFleetStartRequest(
                authpath=str(root),
                client_id="client-id",
                client_secret="client-secret",
                domain="example.com",
            )
        )
        assert start.auth_url.startswith("https://auth.tesla.com/oauth2/v3/authorize?")
        assert start.public_key_url.endswith(
            "/.well-known/appspecific/com.tesla.3p.public-key.pem"
        )
        config_file = Path(start.config_file)
        assert config_file.is_file()
        config = json.loads(config_file.read_text(encoding="utf-8"))
        assert config["CLIENT_ID"] == "client-id"
        assert config["CLIENT_SECRET"] == "client-secret"
        assert config["partner_token"] == "partner-token"
        assert config["access_token"] == ""
        assert [call["url"] for call in calls][0].endswith(
            "com.tesla.3p.public-key.pem"
        )

        parsed = urllib.parse.urlparse(start.auth_url)
        state = urllib.parse.parse_qs(parsed.query)["state"][0]
        result = setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(root),
                callback_url="https://example.com/access?code=abc&state=" + state,
            )
        )
        assert result.saved is True
        assert result.needs_site is False
        assert result.siteid == "123"
        config = json.loads(config_file.read_text(encoding="utf-8"))
        assert config["access_token"] == "fleet-access-token"
        assert config["refresh_token"] == "fleet-refresh-token"
        assert config["site_id"] == 123
        assert "client-secret" not in json.dumps(result.model_dump())
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_start_keeps_existing_tokens_for_same_app(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-fleet-preserve"
    root.mkdir(parents=True, exist_ok=True)
    existing = {
        "CLIENT_ID": "client-id",
        "CLIENT_SECRET": "client-secret",
        "DOMAIN": "example.com",
        "REDIRECT_URI": "https://example.com/access",
        "AUDIENCE": "https://fleet-api.example.com",
        "partner_account": {"domain": "example.com"},
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "site_id": 123,
    }
    (root / ".pypowerwall.fleetapi").write_text(json.dumps(existing), encoding="utf-8")
    try:
        _fake_fleet_http(monkeypatch)
        setup_core.fleet_setup_start(
            SetupFleetStartRequest(
                authpath=str(root),
                client_id="client-id",
                client_secret="client-secret",
                domain="example.com",
                redirect_uri="https://example.com/access",
            )
        )
        saved = json.loads((root / ".pypowerwall.fleetapi").read_text(encoding="utf-8"))
        assert saved["access_token"] == "old-access"
        assert saved["refresh_token"] == "old-refresh"
        assert saved["site_id"] == 123
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_setup_requires_published_public_key(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-fleet-no-pem"
    try:
        _fake_fleet_http(monkeypatch, public_key_status=404)
        with pytest.raises(ValueError, match="public key"):
            setup_core.fleet_setup_start(
                SetupFleetStartRequest(
                    authpath=str(root),
                    client_id="client-id",
                    client_secret="client-secret",
                    domain="example.com",
                )
            )
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_site_selection_updates_config(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-fleet-site"
    try:
        _fake_fleet_http(
            monkeypatch,
            products=[
                {"energy_site_id": 123, "site_name": "Home"},
                {"energy_site_id": 456, "site_name": "Cabin"},
            ],
        )
        start = setup_core.fleet_setup_start(
            SetupFleetStartRequest(
                authpath=str(root),
                client_id="client-id",
                client_secret="client-secret",
                domain="example.com",
            )
        )
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.auth_url).query)[
            "state"
        ][0]
        complete = setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(root),
                callback_url="https://example.com/access?code=abc&state=" + state,
            )
        )
        assert complete.needs_site is True
        assert [site.siteid for site in complete.sites] == ["123", "456"]
        selected = setup_core.fleet_setup_site(
            SetupFleetSiteRequest(authpath=str(root), siteid="456")
        )
        assert selected.saved is True
        assert selected.siteid == "456"
        config = json.loads(
            (root / ".pypowerwall.fleetapi").read_text(encoding="utf-8")
        )
        assert config["site_id"] == 456
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_key_generation_writes_public_and_private_pem(setup_env):
    root = Path.cwd() / "data" / "test-fleet-key"
    try:
        result = setup_core.fleet_generate_key(SetupFleetKeyRequest(authpath=str(root)))
        assert result.created is True
        assert "BEGIN PUBLIC KEY" in result.public_key_pem
        assert Path(result.private_key_path).is_file()
        assert Path(result.public_key_path).is_file()
        assert (
            Path(result.public_key_path).read_text(encoding="ascii")
            == result.public_key_pem
        )
        reused = setup_core.fleet_generate_key(SetupFleetKeyRequest(authpath=str(root)))
        assert reused.created is False
        assert reused.public_key_pem == result.public_key_pem
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_setup_network_scan_is_bounded_and_mocked(setup_env, monkeypatch):
    import pypowerwall.scan as scan_module

    monkeypatch.setattr(
        scan_module,
        "scan",
        lambda **kwargs: [
            {"ip": "192.168.1.50", "din": "DIN-1", "firmware": "26.42.0"}
        ],
    )
    result = setup_core.scan_gateways(SetupScanRequest(cidr="192.168.1.0/24"))
    assert result.ok is True
    assert result.network == "192.168.1.0/24"
    assert result.gateways[0].ip == "192.168.1.50"
    with pytest.raises(ValueError, match="private"):
        setup_core.scan_gateways(SetupScanRequest(cidr="8.8.8.0/24"))
    with pytest.raises(ValueError, match="/24"):
        setup_core.scan_gateways(SetupScanRequest(cidr="192.168.0.0/16"))


def test_cloud_auth_file_import_writes_auth_and_site(setup_env):
    root = Path.cwd() / "data" / "test-cloud-import"
    content = json.dumps(
        {
            "owner@example.com": {
                "sso": {
                    "access_token": "cloud-at",
                    "refresh_token": "cloud-rt",
                    "expires_at": int(time.time() + 3600),
                }
            }
        }
    )
    try:
        result = setup_core.import_auth_file(
            SetupAuthFileRequest(
                mode="cloud",
                authpath=str(root),
                siteid="123",
                content=content,
            )
        )
        assert result.saved is True
        assert result.email == "owner@example.com"
        assert (root / ".pypowerwall.auth").is_file()
        assert (root / ".pypowerwall.site").read_text(encoding="utf-8") == "123"
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_auth_file_import_writes_config(setup_env):
    root = Path.cwd() / "data" / "test-fleet-import"
    content = json.dumps(
        {
            "CLIENT_ID": "client-id",
            "CLIENT_SECRET": "client-secret",
            "DOMAIN": "example.com",
            "REDIRECT_URI": "https://example.com/access",
            "AUDIENCE": "https://fleet-api.prd.na.vn.cloud.tesla.com",
            "access_token": "fleet-at",
            "refresh_token": "fleet-rt",
            "site_id": 123,
        }
    )
    try:
        result = setup_core.import_auth_file(
            SetupAuthFileRequest(mode="fleetapi", authpath=str(root), content=content)
        )
        assert result.saved is True
        assert result.siteid == "123"
        config = json.loads(
            (root / ".pypowerwall.fleetapi").read_text(encoding="utf-8")
        )
        assert config["CLIENT_ID"] == "client-id"
        assert config["access_token"] == "fleet-at"
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleetapi_v1r_registration_uses_saved_config(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-v1r-fleet"
    try:
        setup_core.v1r_key(str(root))
        config = {
            "CLIENT_ID": "client-id",
            "CLIENT_SECRET": "client-secret",
            "DOMAIN": "example.com",
            "REDIRECT_URI": "https://example.com/access",
            "AUDIENCE": "https://fleet-api.example.com",
            "access_token": "fleet-at",
            "refresh_token": "fleet-rt",
            "site_id": 123,
        }
        (root / ".pypowerwall.fleetapi").write_text(
            json.dumps(config), encoding="utf-8"
        )

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "response": {
                        "message": {
                            "payload": {
                                "authorization": {
                                    "message": {
                                        "add_authorized_client_response": {
                                            "client": {"state": 1}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

        calls = []

        def fake_http2_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

        from pypowerwall.fleetapi import fleetapi as fleet_module

        monkeypatch.setattr(fleet_module, "_http2_request", fake_http2_request)
        result = setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(root), siteid="123", method="fleetapi")
        )
        assert result.siteid == "123"
        assert result.state == 1
        assert result.email is None
        assert calls and calls[0][1].endswith("/api/1/energy_sites/123/command")
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_headless_cloud_tokens_write_auth_file(setup_env):
    root = Path.cwd() / "data" / "test-headless-auth"
    try:
        result = setup_core.import_auth_tokens(
            SetupAuthTokensRequest(
                email="owner@example.com",
                authpath=str(root),
                refresh_token="refresh-token",
                access_token="access-token",
                siteid="123",
            )
        )
        assert result.saved is True
        assert result.email == "owner@example.com"
        doc = json.loads((root / ".pypowerwall.auth").read_text(encoding="utf-8"))
        sso = doc["owner@example.com"]["sso"]
        assert sso["refresh_token"] == "refresh-token"
        assert sso["access_token"] == "access-token"
        assert (root / ".pypowerwall.site").read_text(encoding="utf-8") == "123"
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_env_import_activate_writes_effective_config(setup_env):
    from app.config import GatewayConfig

    settings.gateways = [
        GatewayConfig(
            id="home",
            name="Home",
            host="192.168.91.1",
            gw_pwd="env-secret",
        )
    ]
    settings.control_secret = "control-secret"
    settings._config_source = "legacy_env"
    preview = setup_core.env_import_preview()
    assert preview["requires_removal"] is True
    assert "***" in preview["yaml"]
    assert "env-secret" not in preview["yaml"]
    assert "control-secret" not in preview["yaml"]
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=preview["revision"])
    )
    assert result.saved is True
    assert result.config_path == str(setup_env)
    assert result.requires_removal is True
    doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
    assert doc["gateways"][0]["gw_pwd"] == "env-secret"
    assert doc["settings"]["control_secret"] == "control-secret"
    with pytest.raises(ValueError, match="changed"):
        setup_core.env_import_activate(SetupEnvImportRequest(expected_revision="wrong"))


def test_imported_auth_siteid_must_be_numeric(setup_env):
    root = Path.cwd() / "data" / "test-import-siteid"
    try:
        with pytest.raises(ValueError, match="numeric"):
            setup_core.import_auth_tokens(
                SetupAuthTokensRequest(
                    email="owner@example.com",
                    authpath=str(root),
                    refresh_token="refresh-token",
                    siteid="not-numeric",
                )
            )
        content = json.dumps(
            {
                "owner@example.com": {
                    "sso": {
                        "access_token": "at",
                        "refresh_token": "rt",
                    }
                }
            }
        )
        with pytest.raises(ValueError, match="numeric"):
            setup_core.import_auth_file(
                SetupAuthFileRequest(
                    mode="cloud",
                    authpath=str(root),
                    siteid="not-numeric",
                    content=content,
                )
            )
        assert not (root / ".pypowerwall.auth").exists()
        assert not (root / ".pypowerwall.site").exists()
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_hybrid_local_config_roundtrips_email_and_authpath(setup_env):
    authpath = Path.cwd() / "data" / "test-hybrid-auth"
    authpath.mkdir(parents=True, exist_ok=True)
    try:
        request = SetupConfigureRequest(
            gateways=[
                {
                    "id": "hybrid",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                    "gw_pwd": "gateway-secret",
                    "email": "owner@example.com",
                    "authpath": str(authpath),
                }
            ],
            options={},
            expected_revision=None,
        )
        result = apply_setup_request(request)
        assert result.saved is True
        doc = yaml.safe_load(setup_env.read_text(encoding="utf-8"))
        gateway = doc["gateways"][0]
        assert gateway["email"] == "owner@example.com"
        assert gateway["authpath"] == str(authpath)
        valid, configs, _ = setup_core.validate_setup_request(request, doc)
        assert valid.valid is True
        assert configs[0].email == "owner@example.com"
        assert configs[0].authpath == str(authpath)
    finally:
        if authpath.exists():
            import shutil

            shutil.rmtree(authpath)


def test_hybrid_email_requires_an_auth_path(setup_env):
    result, _, _ = setup_core.validate_setup_request(
        SetupConfigureRequest(
            gateways=[
                {
                    "id": "hybrid",
                    "mode": "hybrid",
                    "host": "192.168.91.1",
                    "gw_pwd": "gateway-secret",
                    "email": "owner@example.com",
                }
            ]
        ),
        {},
    )
    assert result.valid is False
    assert any(issue.field == "authpath" for issue in result.errors)


def test_status_survives_malformed_yaml(setup_env, client):
    setup_env.write_text("gateways: [\n", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(setup_env)
    settings._config_error = "Unable to read config file (ScannerError)"
    settings._config_revision = hashlib.sha256(setup_env.read_bytes()).hexdigest()
    settings._config_warnings = []
    response = client.get(
        "/api/setup/status",
        headers={"X-PW-Setup-Token": "test-setup-token"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "invalid"


def test_fleet_v1r_rejects_non_numeric_siteid():
    with pytest.raises(ValueError, match="numeric Tesla siteid"):
        setup_core._fleet_v1r_command(Path.cwd(), "../not-a-site", {})


def test_v1r_auth_import_preserves_existing_account_and_backup(setup_env):
    root = Path.cwd() / "data" / "test-v1r-merge"
    first = json.dumps({"first@example.com": {"sso": {"access_token": "first-token"}}})
    second = json.dumps(
        {"second@example.com": {"sso": {"access_token": "second-token"}}}
    )
    try:
        setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content=first)
        )
        result = setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content=second)
        )
        assert result.accounts == 2
        doc = json.loads((root / ".pypowerwall.auth").read_text(encoding="utf-8"))
        assert "first@example.com" in doc
        assert "second@example.com" in doc
        backup = json.loads(
            (root / ".pypowerwall.auth.bak").read_text(encoding="utf-8")
        )
        assert "first@example.com" in backup
        assert "second@example.com" not in backup
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_gateway_email_is_validated():
    with pytest.raises(ValueError):
        SetupGatewayInput(
            id="home",
            mode="tedapi",
            host="192.168.91.1",
            gw_pwd="secret",
            email="not-an-email",
        )


def test_cloud_test_connection_uses_live_site_lookup(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-cloud-connection"
    root.mkdir(parents=True, exist_ok=True)
    auth = {
        "owner@example.com": {
            "sso": {
                "access_token": "cloud-at",
                "refresh_token": "cloud-rt",
                "expires_at": int(time.time() + 3600),
            }
        }
    }
    (root / ".pypowerwall.auth").write_text(json.dumps(auth), encoding="utf-8")
    (root / ".pypowerwall.site").write_text("123", encoding="utf-8")

    class FakeCloud:
        def __init__(self, email, authpath="", siteid=None):
            self.siteid = siteid

        def connect(self):
            return True

        def getsites(self):
            return [{"energy_site_id": 123, "site_name": "Home"}]

    import pypowerwall.cloud.pypowerwall_cloud as cloud_module

    monkeypatch.setattr(cloud_module, "PyPowerwallCloud", FakeCloud)
    try:
        result = setup_core.test_gateway_connection(
            SetupGatewayInput(
                id="cloud",
                mode="cloud",
                email="owner@example.com",
                authpath=str(root),
                siteid="123",
            )
        )
        assert result.ok is True
        assert result.detected_mode == "Cloud"
        assert [step.key for step in result.steps] == ["config", "auth_file", "api"]
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)


def test_fleet_test_connection_uses_live_api_check(setup_env, monkeypatch):
    root = Path.cwd() / "data" / "test-fleet-connection"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".pypowerwall.fleetapi").write_text(
        json.dumps(
            {
                "CLIENT_ID": "client-id",
                "CLIENT_SECRET": "client-secret",
                "DOMAIN": "example.com",
                "REDIRECT_URI": "https://example.com/access",
                "AUDIENCE": "https://fleet-api.example.com",
                "access_token": "fleet-at",
                "refresh_token": "fleet-rt",
                "site_id": 123,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda path, siteid=None: {"site_name": "Home", "battery_percent": 50},
    )
    try:
        result = setup_core.test_gateway_connection(
            SetupGatewayInput(
                id="fleet",
                mode="fleetapi",
                email="owner@example.com",
                authpath=str(root),
                siteid="123",
            )
        )
        assert result.ok is True
        assert result.detected_mode == "FleetAPI"
        assert result.site_name == "Home"
        assert [step.key for step in result.steps] == ["config", "auth_file", "api"]
    finally:
        if root.exists():
            import shutil

            shutil.rmtree(root)
