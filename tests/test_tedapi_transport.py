"""Tests for the TEDAPI transport settings (auth mode / API version).

Covers PW_TEDAPI_AUTH_MODE / PW_TEDAPI_API_VERSION and the per-gateway
``tedapi_auth_mode`` / ``tedapi_api_version`` overrides: config loading,
resolution and coercion at registration, the kwargs passed to
``pypowerwall.Powerwall()``, capture of the live client's active transport,
and the ``/stats`` + ``/health`` + ``/api/gateways`` reporting.
"""
import json
import logging
from unittest.mock import Mock

import pytest

from app.config import GatewayConfig, Settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus


@pytest.fixture(autouse=True)
def _never_touch_the_network(monkeypatch, mock_pypowerwall):
    """gateway_manager.initialize() starts poll loops that would otherwise
    construct a real pypowerwall.Powerwall() against the test hosts."""
    import pypowerwall

    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pypowerwall)


def _pending_gateway(gateway_id, **transport):
    """Register a gateway the way initialize() does, ready for _poll_gateway()."""
    gw = Gateway(id=gateway_id, name=gateway_id, host="192.168.1.50", gw_pwd="pw", **transport)
    config = GatewayConfig(id=gateway_id, host="192.168.1.50", gw_pwd="pw", **transport)
    gateway_manager.gateways[gateway_id] = gw
    gateway_manager._pending_configs[gateway_id] = config
    gateway_manager.cache[gateway_id] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures[gateway_id] = 0
    gateway_manager._next_poll_time[gateway_id] = 0
    return gw


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_settings_tedapi_transport_defaults_and_env(monkeypatch):
    monkeypatch.delenv("PW_TEDAPI_AUTH_MODE", raising=False)
    monkeypatch.delenv("PW_TEDAPI_API_VERSION", raising=False)
    settings = Settings()
    assert settings.tedapi_auth_mode == "basic"
    assert settings.tedapi_api_version == "V2024_06"

    monkeypatch.setenv("PW_TEDAPI_AUTH_MODE", "bearer")
    monkeypatch.setenv("PW_TEDAPI_API_VERSION", "V2026_06")
    settings = Settings()
    assert settings.tedapi_auth_mode == "bearer"
    assert settings.tedapi_api_version == "V2026_06"


def test_pw_gateways_per_gateway_override(monkeypatch):
    monkeypatch.delenv("PW_CONFIG", raising=False)
    monkeypatch.setenv(
        "PW_GATEWAYS",
        json.dumps(
            [
                {"id": "a", "host": "192.168.1.50", "gw_pwd": "pw"},
                {
                    "id": "b",
                    "host": "192.168.1.60",
                    "gw_pwd": "pw",
                    "tedapi_auth_mode": "bearer",
                    "tedapi_api_version": "V2026_06",
                },
            ]
        ),
    )
    settings = Settings()
    by_id = {gw.id: gw for gw in settings.gateways}
    assert by_id["a"].tedapi_auth_mode is None  # inherits global at registration
    assert by_id["b"].tedapi_auth_mode == "bearer"
    assert by_id["b"].tedapi_api_version == "V2026_06"


# ---------------------------------------------------------------------------
# Registration: resolution and coercion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_resolves_per_gateway_over_global(monkeypatch):
    """Per-gateway value wins over the PW_TEDAPI_* default, and both are
    normalised (case / whitespace) on the way through pypowerwall's coerce."""
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_auth_mode", "basic")
    monkeypatch.setattr(settings, "tedapi_api_version", " v2026_06 ")  # raw env value
    configs = [
        GatewayConfig(id="inherit", host="192.168.1.50", gw_pwd="pw"),
        GatewayConfig(
            id="override",
            host="192.168.1.60",
            gw_pwd="pw",
            tedapi_auth_mode=" Bearer ",
            tedapi_api_version="V2024_06",
        ),
    ]
    await gateway_manager.initialize(configs, poll_interval=5)
    try:
        assert gateway_manager.gateways["inherit"].tedapi_auth_mode == "basic"
        assert gateway_manager.gateways["inherit"].tedapi_api_version == "V2026_06"
        assert gateway_manager.gateways["override"].tedapi_auth_mode == "bearer"
        assert gateway_manager.gateways["override"].tedapi_api_version == "V2024_06"
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_invalid_transport_values_fall_back_with_warning(caplog):
    """pypowerwall.Powerwall() raises ValueError on 'bearr', which would turn
    the gateway into a permanently failing poll; registration must fall back
    (via pypowerwall's own coerce helpers, which log the bad value)."""
    configs = [
        GatewayConfig(
            id="typo",
            host="192.168.1.50",
            gw_pwd="pw",
            tedapi_auth_mode="bearr",
            tedapi_api_version="V2030_01",
        )
    ]
    with caplog.at_level(logging.WARNING):
        await gateway_manager.initialize(configs, poll_interval=5)
    try:
        gw = gateway_manager.gateways["typo"]
        assert gw.tedapi_auth_mode == "basic"
        assert gw.tedapi_api_version == "V2024_06"
        messages = [r.getMessage() for r in caplog.records]
        assert any("bearr" in m for m in messages), messages
        assert any("V2030_01" in m for m in messages), messages
    finally:
        await gateway_manager.shutdown()


# ---------------------------------------------------------------------------
# Constructor wiring + active transport capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_passed_to_powerwall_constructor(monkeypatch, mock_pypowerwall):
    import pypowerwall

    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)
    _pending_gateway("bearer-connect", tedapi_auth_mode="bearer", tedapi_api_version="V2026_06")

    await gateway_manager._poll_gateway("bearer-connect")

    assert powerwall_spy.called, "pypowerwall.Powerwall() was never called"
    call_kwargs = powerwall_spy.call_args.kwargs
    assert call_kwargs.get("tedapi_auth_mode") == "bearer"
    assert call_kwargs.get("tedapi_api_version") == "V2026_06"
    assert call_kwargs.get("gw_pwd") == "pw"


@pytest.mark.asyncio
async def test_default_transport_passed_to_powerwall_constructor(monkeypatch, mock_pypowerwall):
    import pypowerwall

    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)
    _pending_gateway("plain")

    await gateway_manager._poll_gateway("plain")

    call_kwargs = powerwall_spy.call_args.kwargs
    assert call_kwargs.get("tedapi_auth_mode") == "basic"
    assert call_kwargs.get("tedapi_api_version") == "V2024_06"


@pytest.mark.asyncio
async def test_active_transport_recorded_from_live_client(mock_pypowerwall):
    """The transport the live client actually uses is captured for /stats
    (e.g. hybrid mode speaks basic even when bearer was requested)."""
    mock_pypowerwall.tedapi.auth_mode = "basic"
    mock_pypowerwall.tedapi_api_version = "V2024_06"
    _pending_gateway("live", tedapi_auth_mode="bearer", tedapi_api_version="V2026_06")

    await gateway_manager._poll_gateway("live")

    data = gateway_manager.cache["live"].data
    assert data.tedapi_auth_mode == "basic"
    assert data.tedapi_api_version == "V2024_06"


@pytest.mark.asyncio
async def test_active_transport_ignores_non_string_attributes(mock_pypowerwall):
    """A Mock client (or one without the concept) must not leak repr() strings."""
    _pending_gateway("mocked")  # mock.tedapi.auth_mode is an auto-created Mock

    await gateway_manager._poll_gateway("mocked")

    data = gateway_manager.cache["mocked"].data
    assert data.tedapi_auth_mode is None
    assert data.tedapi_api_version is None


# ---------------------------------------------------------------------------
# /stats, /health, /api/gateways
# ---------------------------------------------------------------------------


def test_stats_reports_tedapi_transport(client, connected_gateway, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_auth_mode", "basic")
    monkeypatch.setattr(settings, "tedapi_api_version", "V2024_06")

    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["config"]["PW_TEDAPI_AUTH_MODE"] == "basic"
    assert data["config"]["PW_TEDAPI_API_VERSION"] == "V2024_06"
    assert data["config"]["PW_NEG_SOLAR"] is False
    assert data["tedapi_auth_mode"] == "basic"
    assert data["tedapi_api_version"] == "V2024_06"
    gw_status = data["gateway_statuses"][0]
    assert gw_status["tedapi_auth_mode"] == "basic"
    assert gw_status["tedapi_auth_mode_active"] is None
    assert gw_status["tedapi_api_version"] == "V2024_06"


def test_stats_reports_active_bearer_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"
    gw.tedapi_api_version = "V2026_06"
    status = gateway_manager.cache["test-gateway"]
    status.data.tedapi_auth_mode = "bearer"
    status.data.tedapi_api_version = "V2026_06"

    data = client.get("/stats").json()
    assert data["tedapi_auth_mode"] == "bearer"
    assert data["tedapi_api_version"] == "V2026_06"
    gw_status = data["gateway_statuses"][0]
    assert gw_status["tedapi_auth_mode_active"] == "bearer"
    assert gw_status["tedapi_api_version_active"] == "V2026_06"


def test_health_reports_tedapi_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"

    data = client.get("/health").json()
    detail = data["gateway_details"][0]
    assert detail["id"] == "test-gateway"
    assert detail["auth_mode"] == "bearer"
    assert detail["tedapi_api_version"] == "V2024_06"


def test_api_gateways_includes_transport(client, connected_gateway):
    gw = gateway_manager.gateways["test-gateway"]
    gw.tedapi_auth_mode = "bearer"
    gw.tedapi_api_version = "V2026_06"

    data = client.get("/api/gateways/test-gateway").json()
    gateway = data.get("gateway", data)
    assert gateway["tedapi_auth_mode"] == "bearer"
    assert gateway["tedapi_api_version"] == "V2026_06"
