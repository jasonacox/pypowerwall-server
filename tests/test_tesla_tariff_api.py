"""Tests for the Tesla tariff and Time-of-Use cloud API routes.

GET /api/tesla/tariff_rate and POST /api/tesla/time_of_use_settings go to the
Tesla cloud: through the hybrid cloud-control connection, or the default
gateway's own connection in cloud/FleetAPI mode. TEDAPI/local gateways get 503.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api import legacy
from app.config import settings
from app.core.gateway_manager import _WRITE_METHODS, gateway_manager
from app.main import app
from app.models.gateway import Gateway

_CONTROL_TOKEN = "test-secret-token"
_AUTH = {"Authorization": f"Bearer {_CONTROL_TOKEN}"}
TARIFF = {"code": "GME-DYNAMIC", "name": "Dynamic", "energy_charges": {}}
TOU = {
    "optimization_strategy": "economics",
    "tariff_content_v2": {"code": "GME-DYNAMIC", "seasons": {}},
}
UPDATED = {"Message": "Updated", "Code": 201}  # pypowerwall 0.18.2 set_tariff()


@pytest.fixture(autouse=True)
def reset_tariff_cache():
    legacy._tariff_cache.update(value=None, time=0.0)
    yield
    legacy._tariff_cache.update(value=None, time=0.0)


@pytest.fixture
def tesla_client(monkeypatch):
    """Client with control authentication enabled for the write route."""
    monkeypatch.setattr(settings, "control_secret", _CONTROL_TOKEN)
    return TestClient(app)


def _hybrid(monkeypatch, result) -> AsyncMock:
    """Hybrid setup: a dedicated cloud-control connection answers."""
    cloud_control = AsyncMock(return_value=result)
    monkeypatch.setattr(gateway_manager, "_cloud_control", object())
    monkeypatch.setattr(gateway_manager, "cloud_control", cloud_control)
    return cloud_control


def _gateway(monkeypatch, result, **mode) -> AsyncMock:
    """No cloud-control connection; one default gateway in the given mode."""
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    gateway_manager.gateways["default"] = Gateway(id="default", name="Home", **mode)
    local_control = AsyncMock(return_value=result)
    monkeypatch.setattr(gateway_manager, "local_control", local_control)
    return local_control


# --- GET /api/tesla/tariff_rate ---------------------------------------------


def test_get_uses_hybrid_cloud_control(tesla_client, monkeypatch):
    cloud_control = _hybrid(monkeypatch, TARIFF)
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 200
    assert response.json() == TARIFF
    cloud_control.assert_awaited_once_with("get_tariff", timeout=15.0)


@pytest.mark.parametrize(
    "mode",
    [
        {"email": "a@b.c", "cloud_mode": True},
        {"email": "a@b.c", "fleetapi": True},
    ],
    ids=["cloud", "fleetapi"],
)
def test_get_uses_cloud_or_fleetapi_gateway(tesla_client, monkeypatch, mode):
    """Pure cloud/FleetAPI servers have no hybrid link: use the gateway itself."""
    local_control = _gateway(monkeypatch, TARIFF, **mode)
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 200
    assert response.json() == TARIFF
    local_control.assert_awaited_once_with("default", "get_tariff", timeout=15.0)


def test_get_tedapi_only_is_503_without_calling(tesla_client, monkeypatch):
    """TEDAPI answers get_tariff() with an empty mock; never serve that as 200."""
    local_control = _gateway(monkeypatch, {}, host="192.168.91.1", gw_pwd="x")
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 503
    local_control.assert_not_awaited()


def test_get_none_is_503(tesla_client, monkeypatch):
    _hybrid(monkeypatch, None)
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to retrieve Tesla tariff rate"


def test_get_error_is_502(tesla_client, monkeypatch):
    _hybrid(monkeypatch, {"ERROR": "Tesla API failed"})
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 502
    assert response.json()["detail"] == "Tesla API failed"


def test_get_is_cached(tesla_client, monkeypatch):
    """Repeated GETs inside the TTL make one Tesla call."""
    cloud_control = _hybrid(monkeypatch, TARIFF)
    for _ in range(3):
        assert tesla_client.get("/api/tesla/tariff_rate").json() == TARIFF
    assert cloud_control.await_count == 1


def test_get_refreshes_after_ttl(tesla_client, monkeypatch):
    cloud_control = _hybrid(monkeypatch, TARIFF)
    tesla_client.get("/api/tesla/tariff_rate")
    legacy._tariff_cache["time"] -= legacy._TARIFF_CACHE_TTL + 1
    tesla_client.get("/api/tesla/tariff_rate")
    assert cloud_control.await_count == 2


@pytest.mark.parametrize("failure", [None, {"ERROR": "Tesla API failed"}])
def test_get_serves_last_good_tariff_when_refresh_fails(
    tesla_client, monkeypatch, failure
):
    _hybrid(monkeypatch, TARIFF)
    tesla_client.get("/api/tesla/tariff_rate")
    legacy._tariff_cache["time"] -= legacy._TARIFF_CACHE_TTL + 1
    _hybrid(monkeypatch, failure)
    response = tesla_client.get("/api/tesla/tariff_rate")
    assert response.status_code == 200
    assert response.json() == TARIFF


# --- POST /api/tesla/time_of_use_settings -----------------------------------


def test_post_requires_auth(tesla_client, monkeypatch):
    cloud_control = _hybrid(monkeypatch, UPDATED)
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}
    )
    assert response.status_code == 401
    cloud_control.assert_not_awaited()


def test_post_forwards_only_tou_settings(tesla_client, monkeypatch):
    """Unknown body keys never reach Tesla; the write goes through set_tariff()."""
    cloud_control = _hybrid(monkeypatch, UPDATED)
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings",
        json={"tou_settings": TOU, "extra": "x"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert response.json() == UPDATED
    cloud_control.assert_awaited_once_with("set_tariff", TOU, timeout=20.0)


def test_set_tariff_is_serialized_with_other_writes():
    assert "set_tariff" in _WRITE_METHODS


def test_post_uses_cloud_gateway(tesla_client, monkeypatch):
    local_control = _gateway(monkeypatch, UPDATED, email="a@b.c", cloud_mode=True)
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    assert response.status_code == 200
    local_control.assert_awaited_once_with("default", "set_tariff", TOU, timeout=20.0)


def test_post_tedapi_only_is_503(tesla_client, monkeypatch):
    local_control = _gateway(monkeypatch, UPDATED, host="192.168.91.1", gw_pwd="x")
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    assert response.status_code == 503
    local_control.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tou_settings": None},
        {"tou_settings": []},
        {"tou_settings": {}},
        {"tou_settings": {"optimization_strategy": "economics"}},
        {"tou_settings": {"tariff_content_v2": "not an object"}},
    ],
)
def test_post_rejects_invalid_body(tesla_client, monkeypatch, body):
    cloud_control = _hybrid(monkeypatch, UPDATED)
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json=body, headers=_AUTH
    )
    assert response.status_code == 400
    cloud_control.assert_not_awaited()


def test_post_none_is_503(tesla_client, monkeypatch):
    _hybrid(monkeypatch, None)
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to update Tesla time-of-use settings"


def test_post_error_is_502(tesla_client, monkeypatch):
    _hybrid(monkeypatch, {"ERROR": "Tesla rejected the tariff"})
    response = tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Tesla rejected the tariff"


def test_successful_post_clears_the_tariff_cache(tesla_client, monkeypatch):
    """The next GET after a write reads the new tariff instead of the cache."""
    cloud_control = _hybrid(monkeypatch, TARIFF)
    tesla_client.get("/api/tesla/tariff_rate")
    cloud_control.return_value = UPDATED
    tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    cloud_control.return_value = TARIFF
    tesla_client.get("/api/tesla/tariff_rate")
    assert [c.args[0] for c in cloud_control.await_args_list] == [
        "get_tariff",
        "set_tariff",
        "get_tariff",
    ]


def test_failed_post_keeps_the_tariff_cache(tesla_client, monkeypatch):
    cloud_control = _hybrid(monkeypatch, TARIFF)
    tesla_client.get("/api/tesla/tariff_rate")
    cloud_control.return_value = {"ERROR": "rejected"}
    tesla_client.post(
        "/api/tesla/time_of_use_settings", json={"tou_settings": TOU}, headers=_AUTH
    )
    cloud_control.return_value = TARIFF
    tesla_client.get("/api/tesla/tariff_rate")
    assert [c.args[0] for c in cloud_control.await_args_list] == [
        "get_tariff",
        "set_tariff",
    ]


def test_library_tariff_api_contract():
    """Contract with the pinned pypowerwall: get_tariff() reads and set_tariff()
    writes the Tesla tariff endpoints, wrapping only tou_settings."""
    from unittest.mock import Mock

    import pypowerwall

    pw = Mock()
    pypowerwall.Powerwall.get_tariff(pw)
    pw.poll.assert_called_once_with("/api/tesla/tariff_rate", force=False)
    pypowerwall.Powerwall.set_tariff(pw, TOU)
    pw.post.assert_called_once_with(
        "/api/tesla/time_of_use_settings", {"tou_settings": TOU}, jsonformat=False
    )
