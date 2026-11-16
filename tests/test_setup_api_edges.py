"""HTTP contract tests for every graphical setup API route."""

import pytest

from app.api import setup as setup_api
from app.core import setup as setup_core
from app.models.setup import (
    SetupAuthFileResult,
    SetupAuthTokensResult,
    SetupCloudAuthCompleteResult,
    SetupCloudAuthStartResult,
    SetupCloudSiteResult,
    SetupConfigureResult,
    SetupDiagnosticResult,
    SetupEnvImportResult,
    SetupFleetCompleteResult,
    SetupFleetKeyResult,
    SetupFleetSiteResult,
    SetupFleetStartResult,
    SetupReloadResult,
    SetupScanResult,
    SetupStatus,
    SetupV1rAuthFileResult,
    SetupV1rResult,
)


@pytest.fixture
def setup_api_headers(monkeypatch):
    monkeypatch.setattr(setup_core, "_SETUP_TOKEN", "test-setup-token")
    monkeypatch.setattr(setup_core.settings, "control_secret", None)
    setup_api._RATE_BUCKETS.clear()
    yield {"X-PW-Setup-Token": "test-setup-token"}
    setup_api._RATE_BUCKETS.clear()


def _v1r_result(**overrides):
    values = {
        "state": 1,
        "state_name": "PENDING",
        "pending": True,
        "siteid": "123",
        "email": "owner@example.com",
        "private_key_path": "/data/tedapi_rsa_private.pem",
        "public_key_path": "/data/tedapi_rsa_public.der",
        "fingerprint": "abc",
        "message": "Key registered.",
    }
    values.update(overrides)
    return SetupV1rResult(**values)


_SUCCESS_CASES = [
    (
        "get",
        "/api/setup/status",
        "get_setup_status",
        None,
        SetupStatus(state="unconfigured", config_source="none"),
    ),
    (
        "get",
        "/api/setup/schema",
        "get_setup_schema",
        None,
        {"modes": [], "timezones": [], "settings": []},
    ),
    (
        "post",
        "/api/setup/auth/status",
        "auth_status",
        {"mode": "cloud", "authpath": "/data"},
        {"ready": False, "files": {}},
    ),
    (
        "post",
        "/api/setup/auth/tokens",
        "import_auth_tokens",
        {
            "email": "owner@example.com",
            "authpath": "/data",
            "refresh_token": "refresh-token",
        },
        SetupAuthTokensResult(
            saved=True,
            authpath="/data",
            auth_file="/data/.pypowerwall.auth",
            email="owner@example.com",
            message="saved",
        ),
    ),
    (
        "post",
        "/api/setup/auth/file",
        "import_auth_file",
        {"mode": "cloud", "authpath": "/data", "content": "{}"},
        SetupAuthFileResult(saved=True, authpath="/data", files={}, message="saved"),
    ),
    (
        "post",
        "/api/setup/auth/cloud/start",
        "cloud_auth_start",
        {"email": "owner@example.com", "authpath": "/data", "region": "us"},
        SetupCloudAuthStartResult(auth_url="https://auth.example", expires_in=600),
    ),
    (
        "post",
        "/api/setup/auth/cloud/complete",
        "cloud_auth_complete",
        {"callback_url": "https://example.com/callback?code=abc&state=state"},
        SetupCloudAuthCompleteResult(
            saved=True,
            email="owner@example.com",
            authpath="/data",
            siteid="123",
        ),
    ),
    (
        "post",
        "/api/setup/auth/cloud/use",
        "cloud_auth_use",
        {"authpath": "/data"},
        SetupCloudAuthCompleteResult(
            saved=True,
            email="owner@example.com",
            authpath="/data",
            siteid="123",
        ),
    ),
    (
        "post",
        "/api/setup/auth/cloud/site",
        "cloud_auth_site",
        {"email": "owner@example.com", "authpath": "/data", "siteid": "123"},
        SetupCloudSiteResult(
            saved=True, siteid="123", sitefile="/data/.pypowerwall.site"
        ),
    ),
    (
        "post",
        "/api/setup/scan",
        "scan_gateways",
        {"cidr": "127.0.0.1/32"},
        SetupScanResult(ok=True, network="127.0.0.1/32", message="none"),
    ),
    (
        "get",
        "/api/setup/docker/preflight",
        "docker_preflight",
        None,
        {"ok": True, "checks": [], "warnings": []},
    ),
    (
        "get",
        "/api/setup/import-env",
        "env_import_preview",
        None,
        {"yaml": "gateways: []", "target_path": "/data/gateways.yaml"},
    ),
    (
        "post",
        "/api/setup/import-env",
        "env_import_activate",
        {},
        SetupEnvImportResult(
            saved=True,
            config_path="/data/gateways.yaml",
            revision="rev",
        ),
    ),
    (
        "post",
        "/api/setup/v1r/key",
        "v1r_key",
        {"authpath": "/data"},
        {"registration_required": True, "private_key_path": "/data/key.pem"},
    ),
    (
        "post",
        "/api/setup/v1r/auth-file",
        "v1r_auth_file",
        {"authpath": "/data", "content": "{}"},
        SetupV1rAuthFileResult(
            saved=True,
            authpath="/data",
            auth_file="/data/.pypowerwall.auth",
            message="saved",
        ),
    ),
    (
        "post",
        "/api/setup/v1r/register",
        "v1r_register",
        {"authpath": "/data", "siteid": "123"},
        _v1r_result(),
    ),
    (
        "post",
        "/api/setup/v1r/register/status",
        "v1r_registration_status",
        {"authpath": "/data", "siteid": "123"},
        _v1r_result(state=3, verified=True, pending=False),
    ),
    (
        "post",
        "/api/setup/fleetapi/key",
        "fleet_generate_key",
        {"authpath": "/data"},
        SetupFleetKeyResult(
            created=True,
            private_key_path="/data/private.pem",
            public_key_path="/data/public.pem",
            public_key_pem="pem",
            message="created",
        ),
    ),
    (
        "post",
        "/api/setup/fleetapi/start",
        "fleet_setup_start",
        {
            "authpath": "/data",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "domain": "example.com",
        },
        SetupFleetStartResult(
            auth_url="https://auth.example",
            expires_in=600,
            public_key_url="https://example.com/key.pem",
            config_file="/data/.pypowerwall.fleetapi",
            message="started",
        ),
    ),
    (
        "post",
        "/api/setup/fleetapi/complete",
        "fleet_setup_complete",
        {
            "authpath": "/data",
            "callback_url": "https://example.com/access?code=abc&state=state",
        },
        SetupFleetCompleteResult(
            saved=True,
            authpath="/data",
            config_file="/data/.pypowerwall.fleetapi",
            siteid="123",
            message="complete",
        ),
    ),
    (
        "post",
        "/api/setup/fleetapi/site",
        "fleet_setup_site",
        {"authpath": "/data", "siteid": "123"},
        SetupFleetSiteResult(
            saved=True,
            siteid="123",
            config_file="/data/.pypowerwall.fleetapi",
            message="saved",
        ),
    ),
    (
        "post",
        "/api/setup/test-connection",
        "test_gateway_connection",
        {"id": "home", "mode": "tedapi", "host": "192.168.91.1"},
        SetupDiagnosticResult(ok=True),
    ),
    (
        "post",
        "/api/setup/configure",
        "apply_setup_request",
        {
            "gateways": [
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                    "gw_pwd": "secret",
                }
            ]
        },
        SetupConfigureResult(
            saved=True,
            config_path="/data/gateways.yaml",
            revision="rev",
        ),
    ),
]


@pytest.mark.parametrize("method,path,service,body,result", _SUCCESS_CASES)
def test_setup_api_success_contracts(
    client,
    setup_api_headers,
    monkeypatch,
    method,
    path,
    service,
    body,
    result,
):
    monkeypatch.setattr(setup_api, service, lambda *args, **kwargs: result)
    response = client.request(method, path, json=body, headers=setup_api_headers)
    assert response.status_code == 200, response.text


def test_setup_api_reload_contract(client, setup_api_headers):
    response = client.post("/api/setup/reload", headers=setup_api_headers)
    assert response.status_code == 200, response.text
    assert (
        response.json()
        == SetupReloadResult(
            restart_required=True,
            message="Restart the server/container to activate the saved configuration.",
        ).model_dump()
    )


def test_setup_api_validate_route(client, setup_api_headers):
    response = client.post(
        "/api/setup/validate",
        json={
            "gateways": [
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                    "gw_pwd": "secret",
                }
            ],
            "options": {},
        },
        headers=setup_api_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True


def test_setup_api_rate_window_resets(client, setup_api_headers, monkeypatch):
    now = setup_core.time.monotonic()
    setup_api._RATE_BUCKETS["testclient"] = (now - 120, 999)
    monkeypatch.setattr(
        setup_api,
        "get_setup_status",
        lambda: SetupStatus(state="unconfigured", config_source="none"),
    )
    response = client.get("/api/setup/status", headers=setup_api_headers)
    assert response.status_code == 200


def test_setup_api_accepts_bearer_token(client, setup_api_headers, monkeypatch):
    monkeypatch.setattr(
        setup_api,
        "get_setup_status",
        lambda: SetupStatus(state="unconfigured", config_source="none"),
    )
    response = client.get(
        "/api/setup/status",
        headers={"Authorization": "Bearer test-setup-token"},
    )
    assert response.status_code == 200, response.text


def test_setup_api_rejects_unauthenticated_requests(client, setup_api_headers):
    response = client.get("/api/setup/status")
    assert response.status_code == 401


def test_setup_api_rate_limit_and_pruning(client, setup_api_headers, monkeypatch):
    now = setup_core.time.monotonic()
    setup_api._RATE_BUCKETS.update(
        {f"old-{index}": (now - 120, 1) for index in range(1025)}
    )
    setup_api._RATE_BUCKETS["testclient"] = (now, 120)
    monkeypatch.setattr(setup_api, "_RATE_LIMIT", 120)
    response = client.get("/api/setup/status", headers=setup_api_headers)
    assert response.status_code == 429
    assert not any(key.startswith("old-") for key in setup_api._RATE_BUCKETS)


_ERROR_CASES = [
    (
        "post",
        "/api/setup/auth/tokens",
        "import_auth_tokens",
        {"refresh_token": "refresh-token", "authpath": "/data"},
        400,
        500,
    ),
    (
        "post",
        "/api/setup/auth/file",
        "import_auth_file",
        {"mode": "cloud", "authpath": "/data", "content": "{}"},
        400,
        500,
    ),
    (
        "post",
        "/api/setup/auth/cloud/start",
        "cloud_auth_start",
        {"email": "owner@example.com", "authpath": "/data"},
        400,
        None,
    ),
    (
        "post",
        "/api/setup/auth/cloud/complete",
        "cloud_auth_complete",
        {"callback_url": "https://example.com/callback?code=abc&state=state"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/auth/cloud/use",
        "cloud_auth_use",
        {"authpath": "/data"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/auth/cloud/site",
        "cloud_auth_site",
        {"email": "owner@example.com", "authpath": "/data", "siteid": "123"},
        400,
        None,
    ),
    (
        "post",
        "/api/setup/scan",
        "scan_gateways",
        {"cidr": "127.0.0.1/32"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/import-env",
        "env_import_activate",
        {},
        400,
        None,
    ),
    (
        "post",
        "/api/setup/v1r/key",
        "v1r_key",
        {"authpath": "/data"},
        400,
        500,
    ),
    (
        "post",
        "/api/setup/v1r/auth-file",
        "v1r_auth_file",
        {"authpath": "/data", "content": "{}"},
        400,
        500,
    ),
    (
        "post",
        "/api/setup/v1r/register",
        "v1r_register",
        {"authpath": "/data", "siteid": "123"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/v1r/register/status",
        "v1r_registration_status",
        {"authpath": "/data", "siteid": "123"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/fleetapi/key",
        "fleet_generate_key",
        {"authpath": "/data"},
        400,
        500,
    ),
    (
        "post",
        "/api/setup/fleetapi/start",
        "fleet_setup_start",
        {
            "authpath": "/data",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "domain": "example.com",
        },
        400,
        502,
    ),
    (
        "post",
        "/api/setup/fleetapi/complete",
        "fleet_setup_complete",
        {
            "authpath": "/data",
            "callback_url": "https://example.com/access?code=abc&state=state",
        },
        400,
        502,
    ),
    (
        "post",
        "/api/setup/fleetapi/site",
        "fleet_setup_site",
        {"authpath": "/data", "siteid": "123"},
        400,
        502,
    ),
    (
        "post",
        "/api/setup/test-connection",
        "test_gateway_connection",
        {"id": "home", "mode": "tedapi", "host": "192.168.91.1"},
        400,
        None,
    ),
    (
        "post",
        "/api/setup/configure",
        "apply_setup_request",
        {"gateways": []},
        400,
        None,
    ),
]


@pytest.mark.parametrize(
    "method,path,service,body,value_status,generic_status", _ERROR_CASES
)
def test_setup_api_error_contracts(
    client,
    setup_api_headers,
    monkeypatch,
    method,
    path,
    service,
    body,
    value_status,
    generic_status,
):
    def fail_value(*args, **kwargs):
        raise ValueError("expected validation failure")

    monkeypatch.setattr(setup_api, service, fail_value)
    response = client.request(method, path, json=body, headers=setup_api_headers)
    assert response.status_code == value_status, response.text
    assert "expected validation failure" in response.text

    if generic_status is not None:

        def fail_generic(*args, **kwargs):
            raise RuntimeError("unexpected backend failure")

        monkeypatch.setattr(setup_api, service, fail_generic)
        response = client.request(method, path, json=body, headers=setup_api_headers)
        assert response.status_code == generic_status, response.text
        assert "unexpected backend failure" not in response.text


def test_setup_api_test_connection_matches_existing_gateway(
    client, setup_api_headers, monkeypatch
):
    seen = {}

    def fake_check(gateway, existing):
        seen["gateway"] = gateway
        seen["existing"] = existing
        return SetupDiagnosticResult(ok=True)

    monkeypatch.setattr(
        setup_api,
        "load_existing_config",
        lambda: {
            "gateways": [
                "not-a-mapping",
                {"id": "home", "gw_pwd": "existing-secret"},
            ]
        },
    )
    monkeypatch.setattr(setup_api, "test_gateway_connection", fake_check)
    response = client.post(
        "/api/setup/test-connection",
        json={"id": "home", "mode": "tedapi", "host": "192.168.91.1"},
        headers=setup_api_headers,
    )
    assert response.status_code == 200, response.text
    assert seen["gateway"].id == "home"
    assert seen["existing"]["gw_pwd"] == "existing-secret"


def test_setup_api_status_schema_are_local_and_side_effect_free(
    client, setup_api_headers
):
    status = client.get("/api/setup/status", headers=setup_api_headers)
    schema = client.get("/api/setup/schema", headers=setup_api_headers)
    preflight = client.get("/api/setup/docker/preflight", headers=setup_api_headers)
    assert status.status_code == 200
    assert status.json()["state"] in {
        "unconfigured",
        "invalid",
        "env_managed",
        "partial",
        "configured",
        "unreachable",
        "auth_failed",
    }
    assert schema.status_code == 200
    assert "tedapi" in schema.json()["modes"]
    assert preflight.status_code == 200
    assert "checks" in preflight.json()


def test_setup_api_paths_do_not_accept_credentials_in_query(client, setup_api_headers):
    response = client.get(
        "/api/setup/status?token=secret-value",
        headers=setup_api_headers,
    )
    assert response.status_code == 200
    assert response.json()["config_source"]
    assert "secret-value" not in response.text
