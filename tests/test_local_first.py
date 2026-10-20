"""Tests for PW_LOCAL_FIRST write priority (issue #149).

Default (PW_LOCAL_FIRST=no): the helper declines every write and the
historical dispatch runs verbatim. With PW_LOCAL_FIRST=yes, a confirmed
v1r transport whose LAN is up and which has a local connection is tried
first; in every other case (non-v1r, LAN failed, no local connection)
the helper declines too and the historical dispatch runs unchanged. Any
dispatched attempt is final and never followed by a second write.
"""
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.main import app
from app.models.gateway import Gateway, GatewayStatus, PowerwallData

_TOKEN = "local-first-test-secret"


def _gateway(gid="hybrid", **kwargs):
    base = {"id": gid, "name": gid, "online": True, "host": "10.0.0.2"}
    base.update(kwargs)
    return Gateway(**base)


def _status(gateway, tedapi_mode):
    data = PowerwallData(
        tedapi_mode=tedapi_mode, mode="self_consumption", reserve=20.0,
        grid_charging=True, grid_export="pv_only", grid_status="UP",
    )
    return GatewayStatus(
        gateway=gateway, data=data, online=True, last_updated=time.time()
    )


@pytest.fixture
def gm(monkeypatch):
    """Hybrid gateway (cloud bound) plus a v1r-flavored twin, recording mocks."""
    monkeypatch.setattr(gateway_manager, "gateways", {})
    monkeypatch.setattr(gateway_manager, "cache", {})
    hybrid = _gateway("hybrid")
    gateway_manager.gateways["hybrid"] = hybrid
    gateway_manager.cache["hybrid"] = _status(hybrid, "full")
    v1r = _gateway("v1r", rsa_key_configured=True)
    gateway_manager.gateways["v1r"] = v1r
    gateway_manager.cache["v1r"] = _status(v1r, "v1r")
    monkeypatch.setattr(gateway_manager, "_cloud_control", object())
    # Local connection objects exist for both gateways (local_control
    # itself is mocked; the helper only checks presence).
    monkeypatch.setattr(
        gateway_manager, "connections", {"hybrid": object(), "v1r": object()}
    )
    monkeypatch.setattr(
        gateway_manager, "local_control", AsyncMock(return_value={"ok": True})
    )
    monkeypatch.setattr(
        gateway_manager, "cloud_control", AsyncMock(return_value={"ok": True})
    )
    monkeypatch.setattr(settings, "local_first", False)
    return gateway_manager


def test_local_first_defaults_to_off():
    """PW_LOCAL_FIRST defaults to no: existing users see zero behavior change."""
    assert settings.local_first is False


@pytest.mark.asyncio
async def test_helper_declines_when_flag_off(gm):
    """Flag off: the helper declines and the historical dispatch runs.

    It makes no calls itself — neither local nor cloud — on any gateway.
    """
    for gid in ("hybrid", "v1r"):
        label, result = await gateway_manager.control_write(
            gid, "set_reserve", 20, timeout=10.0
        )
        assert (label, result) == (None, None)
    gateway_manager.cloud_control.assert_not_awaited()
    gateway_manager.local_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_helper_declines_for_non_v1r_when_flag_on(gm, monkeypatch):
    """Flag on but no confirmed v1r transport: historical dispatch runs."""
    monkeypatch.setattr(settings, "local_first", True)
    label, result = await gateway_manager.control_write(
        "hybrid", "set_reserve", 20, timeout=10.0
    )
    assert (label, result) == (None, None)
    gateway_manager.cloud_control.assert_not_awaited()
    gateway_manager.local_control.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", ["offline", "missing"])
async def test_local_first_declines_when_last_poll_failed(gm, monkeypatch, cached):
    """A dead LAN without a WiFi host never trips lan_failed: a failed last
    poll (or no poll yet) declines too, so the historical dispatch runs."""
    monkeypatch.setattr(settings, "local_first", True)
    if cached == "offline":
        gateway_manager.cache["v1r"].online = False
    else:
        del gateway_manager.cache["v1r"]
    monkeypatch.setattr(gateway_manager, "_is_confirmed_v1r", lambda gid: True)
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert (label, result) == (None, None)
    gateway_manager.local_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_no_connection_declines(gm, monkeypatch):
    """No local connection: nothing could have dispatched, the helper
    declines and the historical dispatch runs unchanged."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(gateway_manager, "connections", {"hybrid": object()})
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert (label, result) == (None, None)
    gateway_manager.local_control.assert_not_awaited()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_lan_failed_declines(gm, monkeypatch):
    """Library LAN failover tripped: local v1r writes are LAN-only, so the
    helper declines — no doomed LAN attempt, and the historical dispatch
    runs unchanged (issue #149 ESP32-bridge case)."""
    from unittest.mock import Mock

    monkeypatch.setattr(settings, "local_first", True)
    conn = Mock()
    conn.tedapi.lan_failed = True
    monkeypatch.setattr(gateway_manager, "connections", {"v1r": conn})
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert (label, result) == (None, None)
    gateway_manager.local_control.assert_not_awaited()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_v1r_success_never_touches_cloud(gm, monkeypatch):
    """Local-first: a successful local write is never followed by a cloud one."""
    monkeypatch.setattr(settings, "local_first", True)
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert label == "local v1r"
    assert result == {"ok": True}
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_timeout_never_fails_over(gm, monkeypatch):
    """A dispatched attempt is final: None (e.g. timeout) never fails over.

    A timed-out write may still have been applied — failing over would
    risk double-applying (issue #149, maintainer review).
    """
    monkeypatch.setattr(settings, "local_first", True)
    gateway_manager.local_control.return_value = None
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert label == "local v1r"
    assert result is None
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_error_dict_never_fails_over(gm, monkeypatch):
    """A dispatched attempt is final: a library error dict never fails over.

    The gateway answered, so the transport worked — a second write on
    another path could race it.
    """
    monkeypatch.setattr(settings, "local_first", True)
    gateway_manager.local_control.return_value = {"error": "boom"}
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert label == "local v1r"
    assert result == {"error": "boom"}
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_first_without_cloud_reports_local_failure(gm, monkeypatch):
    """Local-first without cloud: a failed local write has nowhere to go."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    gateway_manager.local_control.return_value = None
    label, result = await gateway_manager.control_write(
        "v1r", "set_reserve", 20, timeout=10.0
    )
    assert label == "local v1r"
    assert result is None
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()


@pytest.fixture
def control_client(monkeypatch):
    monkeypatch.setattr(settings, "control_secret", _TOKEN)
    return TestClient(app)


def test_http_reserve_local_first_prefers_v1r(control_client, gm, monkeypatch):
    """POST /control/reserve with PW_LOCAL_FIRST=yes writes local first."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(
        gateway_manager, "gateways", {"v1r": gateway_manager.gateways["v1r"]}
    )
    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 200
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()


def test_http_companion_reserve_local_first_takes_v1r(
    control_client, gm, monkeypatch
):
    """POST /control/reserve with a mode companion and PW_LOCAL_FIRST=yes
    writes local first (the set_operation path)."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(
        gateway_manager, "gateways", {"v1r": gateway_manager.gateways["v1r"]}
    )
    response = control_client.post(
        "/control/reserve",
        json={"value": 20, "mode": "self_consumption"},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 200
    gateway_manager.local_control.assert_awaited_once()
    assert gateway_manager.local_control.await_args.args == (
        "v1r", "set_operation", 20, "self_consumption",
    )
    gateway_manager.cloud_control.assert_not_awaited()


def test_http_companion_mode_local_first_takes_v1r(
    control_client, gm, monkeypatch
):
    """POST /control/mode with a level companion and PW_LOCAL_FIRST=yes
    writes local first (the set_operation path)."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(
        gateway_manager, "gateways", {"v1r": gateway_manager.gateways["v1r"]}
    )
    response = control_client.post(
        "/control/mode",
        json={"value": "self_consumption", "level": 20},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 200
    gateway_manager.local_control.assert_awaited_once()
    assert gateway_manager.local_control.await_args.args == (
        "v1r", "set_operation", 20, "self_consumption",
    )
    gateway_manager.cloud_control.assert_not_awaited()


def test_http_reserve_lan_failed_uses_historical_dispatch(
    control_client, gm, monkeypatch
):
    """PW_LOCAL_FIRST=yes, LAN failed: the helper declines and the hybrid
    gateway's historical dispatch runs unchanged (cloud write, no local
    attempt)."""
    from unittest.mock import Mock

    monkeypatch.setattr(settings, "local_first", True)
    conn = Mock()
    conn.tedapi.lan_failed = True
    monkeypatch.setattr(gateway_manager, "connections", {"v1r": conn})
    monkeypatch.setattr(
        gateway_manager, "gateways", {"v1r": gateway_manager.gateways["v1r"]}
    )
    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 200
    gateway_manager.cloud_control.assert_awaited_once()
    gateway_manager.local_control.assert_not_awaited()


def test_http_reserve_default_still_prefers_cloud(control_client, gm):
    """POST /control/reserve default: hybrid gateway writes go cloud."""
    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 200
    gateway_manager.cloud_control.assert_awaited_once()
    gateway_manager.local_control.assert_not_awaited()


def test_http_companion_cloud_failure_keeps_old_503_detail(
    control_client, gm, monkeypatch
):
    """Default: a failed cloud write in the companion branch keeps its message."""
    gateway_manager.cloud_control.return_value = None
    response = control_client.post(
        "/control/reserve",
        json={"value": 20, "mode": "self_consumption"},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Control operation failed via cloud"


def test_http_companion_local_failure_keeps_old_503_detail(
    control_client, gm, monkeypatch
):
    """Default: a failed local write in the companion branch keeps its message."""
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    gateway_manager.local_control.return_value = None
    response = control_client.post(
        "/control/reserve",
        json={"value": 20, "mode": "self_consumption"},
        headers={"Authorization": _TOKEN},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Control operation failed or gateway not available"
    )


@pytest.mark.parametrize("error", [{"error": "boom"}, {"ERROR": "boom"}])
@pytest.mark.parametrize("path, body", [
    ("/control/reserve", {"value": 20}),
    ("/control/reserve", {"value": 20, "mode": "self_consumption"}),
    ("/control/mode", {"value": "self_consumption", "level": 20}),
])
def test_http_local_first_error_dict_is_503_without_cloud_write(
    control_client, gm, monkeypatch, error, path, body
):
    """A dispatched local-first write that returns a library error dict is a
    failed write: 503, and no second write through the cloud."""
    monkeypatch.setattr(settings, "local_first", True)
    monkeypatch.setattr(
        gateway_manager, "gateways", {"v1r": gateway_manager.gateways["v1r"]}
    )
    gateway_manager.local_control.return_value = error
    response = control_client.post(
        path, json=body, headers={"Authorization": _TOKEN}
    )
    assert response.status_code == 503
    gateway_manager.local_control.assert_awaited_once()
    gateway_manager.cloud_control.assert_not_awaited()
