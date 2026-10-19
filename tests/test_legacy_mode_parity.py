"""Legacy single-gateway settings pick the same mode as the proxy.

The proxy builds one pypowerwall connection with auto_select=True: with no
PW_HOST it uses FleetAPI when .pypowerwall.fleetapi exists, else Cloud. With
PW_HOST + PW_PASSWORD (no gateway password or RSA key) it uses the local API,
which a Powerwall 2 serves in full.
"""
import json
from unittest.mock import Mock

import pytest

from app.config import GatewayConfig, Settings, settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus


def _legacy_settings(monkeypatch, tmp_path, **env):
    for name in ("PW_HOST", "PW_EMAIL", "PW_PASSWORD", "PW_GW_PWD", "PW_GATEWAYS", "PW_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PW_AUTH_PATH", str(tmp_path))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings().gateways


def test_no_host_with_fleetapi_file_selects_fleetapi(monkeypatch, tmp_path):
    (tmp_path / ".pypowerwall.fleetapi").write_text("{}")
    (tmp_path / ".pypowerwall.auth").write_text(json.dumps({"me@example.com": {}}))
    [gw] = _legacy_settings(monkeypatch, tmp_path, PW_EMAIL="me@example.com")
    assert gw.fleetapi is True and gw.cloud_mode is True


def test_no_host_no_email_with_fleetapi_file_still_configures_a_gateway(monkeypatch, tmp_path):
    (tmp_path / ".pypowerwall.fleetapi").write_text("{}")
    [gw] = _legacy_settings(monkeypatch, tmp_path)
    assert gw.fleetapi is True and gw.cloud_mode is True and not gw.email


def test_no_host_with_auth_file_selects_cloud_and_reads_email(monkeypatch, tmp_path):
    (tmp_path / ".pypowerwall.auth").write_text(json.dumps({"me@example.com": {"token": "x"}}))
    [gw] = _legacy_settings(monkeypatch, tmp_path)
    assert gw.cloud_mode is True and gw.fleetapi is False
    assert gw.email == "me@example.com"


def test_no_host_email_without_auth_files_is_cloud_as_before(monkeypatch, tmp_path):
    [gw] = _legacy_settings(monkeypatch, tmp_path, PW_EMAIL="me@example.com")
    assert gw.cloud_mode is True and gw.fleetapi is False


def test_host_wins_over_fleetapi_file(monkeypatch, tmp_path):
    (tmp_path / ".pypowerwall.fleetapi").write_text("{}")
    [gw] = _legacy_settings(monkeypatch, tmp_path, PW_HOST="192.168.91.1", PW_GW_PWD="x")
    assert gw.cloud_mode is False and gw.fleetapi is False


def test_nothing_configured_has_no_gateway(monkeypatch, tmp_path):
    assert _legacy_settings(monkeypatch, tmp_path) == []


def _pending(gateway_id, gateway, config):
    gateway_manager.gateways[gateway_id] = gateway
    gateway_manager._pending_configs[gateway_id] = config
    gateway_manager.cache[gateway_id] = GatewayStatus(gateway=gateway, online=False)
    gateway_manager._consecutive_failures[gateway_id] = 0
    gateway_manager._next_poll_time[gateway_id] = 0


@pytest.mark.asyncio
async def test_fleetapi_without_email_connects_through_fleetapi(monkeypatch, mock_gateway_manager, mock_pypowerwall):
    import pypowerwall

    spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", spy)
    kwargs = dict(id="fa", name="FleetAPI", cloud_mode=True, fleetapi=True, authpath="/app/.auth")
    _pending("fa", Gateway(**{k: v for k, v in kwargs.items() if k != "authpath"}), GatewayConfig(**kwargs))

    await gateway_manager._poll_gateway("fa")

    call = spy.call_args.kwargs
    assert call["cloudmode"] is True and call["fleetapi"] is True
    assert "email" not in call
    assert "fa" in gateway_manager.connections


@pytest.mark.asyncio
@pytest.mark.parametrize("answers_api_status, expected_basic_lan", [
    (True, False),   # Powerwall 2: full local API
    (False, True),   # Powerwall 3 wired LAN: Basic LAN
])
async def test_password_only_local_detects_basic_lan_on_connect(
    mock_gateway_manager, mock_pypowerwall, answers_api_status, expected_basic_lan
):
    mock_pypowerwall.is_connected.return_value = answers_api_status
    mock_pypowerwall.grid_status.return_value = "UP"
    mock_pypowerwall.tedapi = None
    gw = Gateway(id="pw", name="PW", host="10.0.1.2", basic_lan=True)  # candidate
    _pending("pw", gw, GatewayConfig(id="pw", name="PW", host="10.0.1.2", password="pw", email="me@example.com"))

    await gateway_manager._poll_gateway("pw")

    assert "pw" in gateway_manager.connections
    assert gateway_manager.gateways["pw"].basic_lan is expected_basic_lan
    # A Powerwall 2 gets the endpoints Basic LAN skips (vitals, alerts, system status)
    assert mock_pypowerwall.vitals.called is (not expected_basic_lan)
    assert mock_pypowerwall.alerts.called is (not expected_basic_lan)


@pytest.mark.asyncio
async def test_tedapi_probe_only_for_tedapi_gateways(monkeypatch):
    """A password-only local gateway has no TEDAPI to probe or recover."""
    import pypowerwall
    from tests.test_gateway_manager import _make_realistic_pw_mock

    monkeypatch.setattr(settings, "tedapi_recovery", True)
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: _make_realistic_pw_mock())
    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    configs = [
        GatewayConfig(id="pw2", name="PW2", host="10.0.1.2", password="pw"),
        GatewayConfig(id="pw3", name="PW3", host="192.168.91.1", gw_pwd="secret"),
    ]
    await gm.initialize(configs, poll_interval=5)
    try:
        assert "pw2" not in gm._probe_tasks
        assert "pw3" in gm._probe_tasks
    finally:
        await gm.shutdown()


def test_stats_tedapi_false_for_password_only_local(client, mock_gateway_manager):
    gateway_manager.gateways["pw2"] = Gateway(id="pw2", name="PW2", host="10.0.1.2", basic_lan=False)
    stats = client.get("/stats").json()
    assert stats["tedapi"] is False
