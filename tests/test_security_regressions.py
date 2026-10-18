"""Regression tests for the security-review fixes.

Covers:
- Credentials (gw_pwd, email) never appear in serialized API responses
- POST proxy requires the control token
- Empty/malformed control payloads are rejected (no silent reserve=0)
- /api/status works when cached status is a string (no din AttributeError)
- Aggregate SOE averages over SOE contributors, not all online gateways
- /stats masks PII and filesystem paths
"""
import json

import pytest

from app.core.gateway_manager import gateway_manager

_CONTROL_TOKEN = "test-secret-token"


@pytest.fixture
def control_client(monkeypatch):
    """Test client with control features enabled via monkeypatched settings."""
    from app.config import settings
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "control_secret", _CONTROL_TOKEN)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Credential leak prevention
# ---------------------------------------------------------------------------

def _assert_no_credentials(payload: str):
    assert "TEST_PASSWORD" not in payload
    assert "gw_pwd" not in payload
    # email is excluded from Gateway serialization entirely
    assert '"email"' not in payload


def test_gateway_list_has_no_credentials(client, connected_gateway):
    """gw_pwd/email must never serialize in /api/gateways/ responses."""
    response = client.get("/api/gateways/")
    assert response.status_code == 200
    _assert_no_credentials(response.text)


def test_gateway_detail_has_no_credentials(client, connected_gateway):
    response = client.get("/api/gateways/test-gateway")
    assert response.status_code == 200
    _assert_no_credentials(response.text)


def test_aggregate_has_no_credentials(client, connected_gateway):
    """AggregateData embeds GatewayStatus - must not leak credentials either."""
    response = client.get("/api/aggregate/")
    assert response.status_code == 200
    _assert_no_credentials(response.text)


def test_model_dump_excludes_credentials(connected_gateway):
    """Direct model serialization (used by WebSocket streams) excludes secrets."""
    dumped = connected_gateway.model_dump()
    assert "gw_pwd" not in dumped["gateway"]
    assert "email" not in dumped["gateway"]
    assert "rsa_key_path" not in dumped["gateway"]
    text = connected_gateway.model_dump_json()
    _assert_no_credentials(text)


# ---------------------------------------------------------------------------
# POST proxy auth gate
# ---------------------------------------------------------------------------

def test_post_proxy_requires_control_enabled(client, connected_gateway):
    """Without PW_CONTROL_SECRET the POST proxy must refuse (403)."""
    response = client.post(
        "/api/gateways/test-gateway/api/operation",
        json={"real_mode": "backup", "backup_reserve_percent": 100},
    )
    assert response.status_code == 403


def test_post_proxy_requires_token(control_client, connected_gateway):
    """With control enabled, the POST proxy requires the bearer token."""
    response = control_client.post(
        "/api/gateways/test-gateway/api/operation",
        json={"real_mode": "backup"},
    )
    assert response.status_code == 401

    response = control_client.post(
        "/api/gateways/test-gateway/api/operation",
        json={"real_mode": "backup"},
        headers={"Authorization": "wrong-token"},
    )
    assert response.status_code == 401


def test_post_proxy_works_with_token(control_client, connected_gateway, monkeypatch):
    """Valid token passes through to the gateway API."""
    async def mock_call_api(gateway_id, method, *args, **kwargs):
        return {"result": "ok"}

    monkeypatch.setattr(gateway_manager, "call_api", mock_call_api)
    response = control_client.post(
        "/api/gateways/test-gateway/api/operation",
        json={"real_mode": "backup"},
        headers={"Authorization": f"Bearer {_CONTROL_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"result": "ok"}


def test_get_proxy_still_readable(client, connected_gateway, monkeypatch):
    """The GET proxy remains unauthenticated (read-only, LAN trust model)."""
    async def mock_call_api(gateway_id, method, *args, **kwargs):
        return {"reading": 42}

    monkeypatch.setattr(gateway_manager, "call_api", mock_call_api)
    response = client.get("/api/gateways/test-gateway/api/meters/aggregates")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Control payload validation (no silent reserve=0 / mode flip)
# ---------------------------------------------------------------------------

def test_control_reserve_empty_body_rejected(control_client, connected_gateway):
    """POST /control/reserve with {} used to silently set reserve to 0."""
    response = control_client.post(
        "/control/reserve",
        json={},
        headers={"Authorization": _CONTROL_TOKEN},
    )
    assert response.status_code == 400


def test_control_reserve_typoed_key_rejected(control_client, connected_gateway):
    response = control_client.post(
        "/control/reserve",
        json={"val": 20},
        headers={"Authorization": _CONTROL_TOKEN},
    )
    assert response.status_code == 400


def test_control_reserve_out_of_range_rejected(control_client, connected_gateway):
    response = control_client.post(
        "/control/reserve",
        json={"value": 150},
        headers={"Authorization": _CONTROL_TOKEN},
    )
    assert response.status_code == 400


def test_control_mode_empty_body_rejected(control_client, connected_gateway):
    """POST /control/mode with {} used to silently set self_consumption."""
    response = control_client.post(
        "/control/mode",
        json={},
        headers={"Authorization": _CONTROL_TOKEN},
    )
    assert response.status_code == 400


def test_control_mode_invalid_value_rejected(control_client, connected_gateway):
    response = control_client.post(
        "/control/mode",
        json={"value": "warp_speed"},
        headers={"Authorization": _CONTROL_TOKEN},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /api/status din regression (500 when cached status is a string)
# ---------------------------------------------------------------------------

def test_api_status_with_string_status(client, connected_gateway):
    """conftest sets data.status='Running' (a string); this used to 500 via
    an AttributeError on the non-existent Gateway.din field."""
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    # falls back to PowerwallData.din
    assert body["din"] == connected_gateway.data.din


# ---------------------------------------------------------------------------
# Aggregate SOE denominator
# ---------------------------------------------------------------------------

def test_aggregate_soe_ignores_gateways_without_soe(connected_gateway):
    """A solar-only inverter (soe=None) must not dilute the battery average."""
    from app.models.gateway import Gateway, GatewayStatus, PowerwallData

    inverter = Gateway(id="inverter", name="Inverter", host="192.168.91.2", type="inverter")
    inverter_data = PowerwallData(
        aggregates={"solar": {"instant_power": 4000}}, timestamp=1234567890.0
    )
    gateway_manager.gateways["inverter"] = inverter
    gateway_manager.cache["inverter"] = GatewayStatus(
        gateway=inverter, data=inverter_data, online=True, last_updated=1234567890.0
    )

    aggregate = gateway_manager.get_aggregate_data()
    assert aggregate.num_online == 2
    # Average must equal the single battery gateway's SOE, not half of it
    assert aggregate.total_battery_percent == pytest.approx(
        connected_gateway.data.soe
    )
    assert aggregate.total_battery_percent_raw == pytest.approx(
        connected_gateway.data.soe_raw
    )


# ---------------------------------------------------------------------------
# WebSocket gateway cache must not grow for unknown gateway IDs
# ---------------------------------------------------------------------------

def test_ws_gateway_cache_bounded(connected_gateway):
    """Random gateway IDs must not create persistent cache entries."""
    from app.api import websockets as ws

    ws._gateway_cache.clear()
    for i in range(50):
        result = ws._gateway_json(f"bogus-{i}")
        assert "Gateway not found" in result
    assert len(ws._gateway_cache) == 0

    # Configured gateways do get cached
    ws._gateway_json("test-gateway")
    assert set(ws._gateway_cache) == {"test-gateway"}
    ws._gateway_cache.clear()


# ---------------------------------------------------------------------------
# /stats masking
# ---------------------------------------------------------------------------

def test_stats_masks_pii(client, connected_gateway, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pw_email", "secret@example.com")
    monkeypatch.setattr(settings, "siteid", "123456")
    response = client.get("/stats")
    assert response.status_code == 200
    text = response.text
    assert "secret@example.com" not in text
    assert json.loads(text)["config"]["PW_EMAIL"] == "**********"
    assert json.loads(text)["config"]["PW_SITEID"] == "**********"


def test_stats_masks_top_level_siteid_and_cache_file(
    client, connected_gateway, monkeypatch
):
    """The proxy-compatible top-level siteid and cf keys stay, but masked like
    PW_SITEID and PW_CACHE_FILE in config (cf is a path under PW_AUTH_PATH)."""
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "cache_file", "/secret/auth/.powerwall")
    monkeypatch.setattr(gateway_manager.gateways["test-gateway"], "site_id", "987654")
    response = client.get("/stats")
    assert response.status_code == 200
    assert "/secret/auth" not in response.text
    assert "987654" not in response.text
    stats = response.json()
    assert stats["cf"] == "**********"
    assert stats["siteid"] == "**********"
    assert stats["config"]["PW_CACHE_FILE"] == "**********"


def test_stats_unset_siteid_and_cache_file_are_null(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "cache_file", None)
    stats = client.get("/stats").json()
    assert stats["cf"] is None
    assert stats["siteid"] is None


# ---------------------------------------------------------------------------
# Rate limiting - disabled by default (see tests/test_rate_limit_middleware.py
# for enabled-state behavior; the middleware is wired in at import time from
# PW_RATE_LIMIT_ENABLED, so it can't be toggled here via monkeypatched settings)
# ---------------------------------------------------------------------------

def test_rate_limiting_disabled_by_default(client):
    """A burst of requests must never 429 unless rate limiting is explicitly enabled."""
    for _ in range(200):
        response = client.get("/health")
        assert response.status_code != 429
