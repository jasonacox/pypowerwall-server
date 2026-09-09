"""Non-actuating tests for the authenticated local islanding endpoint."""

import asyncio
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core.gateway_manager import gateway_manager


@pytest.fixture
def islanding_client(client, connected_gateway, mock_pypowerwall, monkeypatch):
    """Use the real route/executor with a mocked Powerwall, never live hardware."""
    monkeypatch.setattr(settings, "control_secret", "islanding-test-token")
    mock_pypowerwall.go_off_grid.return_value = {
        "mode": 6,
        "force": True,
        "result": 1,
    }
    mock_pypowerwall.reconnect_grid.return_value = {
        "mode": 1,
        "force": False,
        "result": 1,
    }
    # Hybrid credentials must not redirect islanding to the cloud connection.
    cloud = Mock()
    monkeypatch.setattr(gateway_manager, "_cloud_control", cloud)
    yield client
    assert cloud.mock_calls == []
    mock_pypowerwall.post.assert_not_called()


_HEADERS = {"Authorization": "Bearer islanding-test-token"}
_OFF_GRID = {"value": "off_grid", "confirm": True}


@pytest.mark.parametrize(
    "payload, method, kwargs, expected",
    [
        (
            _OFF_GRID,
            "go_off_grid",
            {"confirm": True},
            {"mode": 6, "force": True, "result": 1},
        ),
        (
            {"value": "on_grid"},
            "reconnect_grid",
            {},
            {"mode": 1, "force": False, "result": 1},
        ),
    ],
)
@pytest.mark.parametrize("hybrid", [False, True])
def test_islanding_routes_locally(
    islanding_client, mock_pypowerwall, payload, method, kwargs, expected, hybrid
) -> None:
    """Both operations call exactly one library method, including in hybrid mode."""
    if not hybrid:
        gateway_manager._cloud_control = None
    response = islanding_client.post(
        "/control/islanding", json=payload, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json() == expected
    getattr(mock_pypowerwall, method).assert_called_once_with(**kwargs)
    other = "reconnect_grid" if method == "go_off_grid" else "go_off_grid"
    getattr(mock_pypowerwall, other).assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": "OFF_GRID", "confirm": True},
        {"value": True},
        {"value": 1},
        {"value": None},
        {"value": []},
        {"value": {}},
        {"value": "off_grid"},
        {"value": "off_grid", "confirm": False},
        {"value": "off_grid", "confirm": "true"},
        {"value": "off_grid", "confirm": 1},
        {"value": "off_grid", "confirm": None},
        {"value": "off_grid", "confirmed": True},
        {"value": "on_grid", "confirm": "false"},
    ],
)
def test_invalid_islanding_payload_never_actuates(
    islanding_client, mock_pypowerwall, payload
) -> None:
    response = islanding_client.post(
        "/control/islanding", json=payload, headers=_HEADERS
    )
    assert response.status_code == 400
    mock_pypowerwall.go_off_grid.assert_not_called()
    mock_pypowerwall.reconnect_grid.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
@pytest.mark.parametrize("payload", [_OFF_GRID, {"value": "on_grid"}])
def test_islanding_requires_auth(
    islanding_client, mock_pypowerwall, headers, payload
) -> None:
    response = islanding_client.post(
        "/control/islanding", json=payload, headers=headers
    )
    assert response.status_code == 401
    mock_pypowerwall.go_off_grid.assert_not_called()
    mock_pypowerwall.reconnect_grid.assert_not_called()


@pytest.mark.parametrize("payload", [_OFF_GRID, {"value": "on_grid"}])
def test_islanding_disabled(
    islanding_client, mock_pypowerwall, monkeypatch, payload
) -> None:
    monkeypatch.setattr(settings, "control_secret", None)
    response = islanding_client.post(
        "/control/islanding", json=payload, headers=_HEADERS
    )
    assert response.status_code == 403
    mock_pypowerwall.go_off_grid.assert_not_called()
    mock_pypowerwall.reconnect_grid.assert_not_called()


@pytest.mark.parametrize("body", ["{", "[]", "null", '"off_grid"'])
def test_islanding_requires_json_object(
    islanding_client, mock_pypowerwall, body
) -> None:
    response = islanding_client.post(
        "/control/islanding",
        content=body,
        headers={**_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    mock_pypowerwall.go_off_grid.assert_not_called()
    mock_pypowerwall.reconnect_grid.assert_not_called()


def test_islanding_missing_connection(islanding_client, mock_pypowerwall) -> None:
    gateway_manager.connections.clear()
    response = islanding_client.post(
        "/control/islanding", json=_OFF_GRID, headers=_HEADERS
    )
    assert response.status_code == 503
    mock_pypowerwall.go_off_grid.assert_not_called()


def test_islanding_no_gateway(islanding_client, mock_pypowerwall) -> None:
    gateway_manager.gateways.clear()
    response = islanding_client.post(
        "/control/islanding", json=_OFF_GRID, headers=_HEADERS
    )
    assert response.status_code == 503
    mock_pypowerwall.go_off_grid.assert_not_called()


@pytest.mark.parametrize(
    "method,payload",
    [("go_off_grid", _OFF_GRID), ("reconnect_grid", {"value": "on_grid"})],
)
@pytest.mark.parametrize(
    "failure", [None, False, RuntimeError("backend failed"), TimeoutError()]
)
def test_islanding_failures_are_http_errors(
    islanding_client, mock_pypowerwall, method, payload, failure
) -> None:
    control = getattr(mock_pypowerwall, method)
    if isinstance(failure, Exception):
        control.side_effect = failure
    else:
        control.return_value = failure
    response = islanding_client.post(
        "/control/islanding", json=payload, headers=_HEADERS
    )
    assert response.status_code == 503
    assert "Check grid status" in response.json()["detail"]
    assert control.call_count == 1  # No automatic retry or raw/cloud fallback.


@pytest.mark.parametrize("result", [{}, {"result": None}, {"result": 0}, {"result": 2}])
def test_unacknowledged_islanding_is_not_success(
    islanding_client, mock_pypowerwall, result
) -> None:
    mock_pypowerwall.go_off_grid.return_value = result
    response = islanding_client.post(
        "/control/islanding", json=_OFF_GRID, headers=_HEADERS
    )
    assert response.status_code == 502
    assert response.json()["detail"]["response"] == result


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["go_off_grid", "reconnect_grid"])
async def test_islanding_uses_existing_write_lock(
    connected_gateway, mock_pypowerwall, monkeypatch, method
) -> None:
    """Islanding waits for other control writes before entering the executor."""
    lock = asyncio.Lock()
    monkeypatch.setattr(gateway_manager, "_write_lock", lock)
    control = getattr(mock_pypowerwall, method)
    control.return_value = {"result": 1}
    async with lock:
        task = asyncio.create_task(
            gateway_manager.local_control(connected_gateway.gateway.id, method)
        )
        await asyncio.sleep(0)
        control.assert_not_called()
        assert not task.done()
    assert await asyncio.wait_for(task, timeout=2.0) == {"result": 1}
    control.assert_called_once()
