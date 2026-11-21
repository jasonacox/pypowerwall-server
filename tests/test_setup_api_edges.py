"""HTTP contract tests for every graphical setup API route."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import setup as setup_api
from app.core import setup as setup_core
from app.models.setup import (
    SetupAuthFileResult,
    SetupAuthTokensResult,
    SetupCloudAuthCompleteResult,
    SetupCloudAuthStartResult,
    SetupCloudSiteResult,
    SetupConfigureResult,
    SetupDeleteConfigResult,
    SetupDiagnosticResult,
    SetupEnvImportResult,
    SetupFleetCompleteResult,
    SetupFleetKeyResult,
    SetupFleetSiteResult,
    SetupFleetStartResult,
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
        "/api/setup/delete-config",
        "delete_setup_config",
        {},
        SetupDeleteConfigResult(
            deleted=True,
            config_path="/data/gateways.yaml",
            message="deleted",
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


def _rate_request(key: str):
    return SimpleNamespace(client=SimpleNamespace(host=key))


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


def test_setup_api_rate_limit_caps_active_buckets(setup_api_headers, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(setup_api.time, "monotonic", lambda: now)
    setup_api._RATE_BUCKETS.update(
        {f"active-{index}": (now - index * 0.01, 1) for index in range(1025)}
    )
    setup_api._RATE_BUCKETS["current"] = (now, 50)
    setup_api._check_rate(_rate_request("current"))
    assert len(setup_api._RATE_BUCKETS) == 1024
    assert setup_api._RATE_BUCKETS["current"] == (now, 51)
    assert "active-0" in setup_api._RATE_BUCKETS
    assert "active-1023" not in setup_api._RATE_BUCKETS
    assert "active-1024" not in setup_api._RATE_BUCKETS


def test_setup_api_rate_limit_prunes_expired_before_active(
    setup_api_headers, monkeypatch
):
    now = 1000.0
    monkeypatch.setattr(setup_api.time, "monotonic", lambda: now)
    expired = {f"expired-{index}": (now - 120, 1) for index in range(600)}
    active = {f"active-{index}": (now - 1, 1) for index in range(425)}
    setup_api._RATE_BUCKETS.update(expired)
    setup_api._RATE_BUCKETS.update(active)
    setup_api._check_rate(_rate_request("current"))
    assert len(setup_api._RATE_BUCKETS) == 426
    assert all(key in setup_api._RATE_BUCKETS for key in active)
    assert not any(key.startswith("expired-") for key in setup_api._RATE_BUCKETS)
    assert setup_api._RATE_BUCKETS["current"][1] == 1


def test_setup_api_rate_limit_keeps_120_per_60_seconds(setup_api_headers, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(setup_api.time, "monotonic", lambda: now)
    request = _rate_request("limited")
    for _ in range(120):
        setup_api._check_rate(request)
    assert setup_api._RATE_BUCKETS["limited"] == (now, 120)
    with pytest.raises(HTTPException) as exc:
        setup_api._check_rate(request)
    assert exc.value.status_code == 429
    assert exc.value.detail == "Setup rate limit exceeded"
    assert setup_api._RATE_BUCKETS["limited"] == (now, 121)


def test_setup_api_rate_limit_parallel_distinct_buckets(setup_api_headers):
    def hit(index):
        setup_api._check_rate(_rate_request(f"parallel-{index}"))

    with ThreadPoolExecutor(max_workers=32) as executor:
        list(executor.map(hit, range(1200)))

    assert len(setup_api._RATE_BUCKETS) == 1024
    assert all(
        isinstance(value, tuple) and len(value) == 2 and value[1] >= 1
        for value in setup_api._RATE_BUCKETS.values()
    )


def test_setup_api_rate_limit_parallel_same_bucket(setup_api_headers, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(setup_api.time, "monotonic", lambda: now)
    monkeypatch.setattr(setup_api, "_RATE_LIMIT", 10000)

    def hit(_):
        setup_api._check_rate(_rate_request("shared"))

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(hit, range(200)))

    start, count = setup_api._RATE_BUCKETS["shared"]
    assert start == now
    assert count == 200


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
        "/api/setup/delete-config",
        "delete_setup_config",
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
    assert "hybrid_fleetapi" in schema.json()["modes"]


def test_setup_api_test_connection_uses_original_id(
    client, setup_api_headers, monkeypatch
):
    monkeypatch.setattr(
        setup_api,
        "load_existing_config",
        lambda: {
            "gateways": [
                {"id": "old", "gw_pwd": "stored"},
                {"id": "new", "gw_pwd": "fallback"},
            ]
        },
    )
    seen = []
    monkeypatch.setattr(
        setup_api,
        "test_gateway_connection",
        lambda gateway, existing: seen.append(existing)
        or SetupDiagnosticResult(ok=True),
    )
    response = client.post(
        "/api/setup/test-connection",
        json={
            "id": "new",
            "original_id": "old",
            "mode": "tedapi",
            "host": "192.168.91.1",
        },
        headers=setup_api_headers,
    )
    assert response.status_code == 200, response.text
    assert seen[0]["gw_pwd"] == "stored"

    response = client.post(
        "/api/setup/test-connection",
        json={
            "id": "new",
            "original_id": "missing",
            "mode": "tedapi",
            "host": "192.168.91.1",
        },
        headers=setup_api_headers,
    )
    assert response.status_code == 200, response.text
    assert seen[1]["gw_pwd"] == "fallback"


def test_setup_api_paths_do_not_accept_credentials_in_query(client, setup_api_headers):
    response = client.get(
        "/api/setup/status?token=secret-value",
        headers=setup_api_headers,
    )
    assert response.status_code == 200
    assert response.json()["config_source"]
    assert "secret-value" not in response.text
