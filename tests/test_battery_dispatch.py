"""Local timed battery dispatch, form/JSON parity and failure handling."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

TOKEN = "dispatch-test-token"


@pytest.fixture(autouse=True)
def controls(monkeypatch, mock_pypowerwall):
    from app.api import legacy
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "control_secret", TOKEN)
    monkeypatch.setattr(settings, "battery_dispatch", True)
    legacy._dispatch_running.clear()
    legacy._dispatch_pending.clear()
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.dispatch_battery_power.return_value = {
        "power_watts": -500.0,
        "duration_seconds": 30,
    }
    mock_pypowerwall.resume_battery_operation.return_value = {
        "resume_battery_operation": True
    }
    gateway_manager._cloud_control = Mock()  # Hybrid cloud must never win.


def post(client, action, data, form=False, token=TOKEN):
    if form:
        return client.post(f"/control/{action}", data={**data, "token": token})
    return client.post(
        f"/control/{action}", json=data, headers={"Authorization": token}
    )


def _register_gateway(gateway_id, mode, cached_mode=None):
    """Register live and cached modes independently, without gateway I/O."""
    from app.core.gateway_manager import gateway_manager
    from app.models.gateway import Gateway, GatewayStatus, PowerwallData

    gateway = Gateway(id=gateway_id, name=gateway_id)
    connection = Mock(tedapi_mode=mode)
    connection.dispatch_battery_power.return_value = {
        "power_watts": 500,
        "duration_seconds": 30,
    }
    gateway_manager.gateways[gateway_id] = gateway
    gateway_manager.connections[gateway_id] = connection
    gateway_manager.cache[gateway_id] = GatewayStatus(
        gateway=gateway,
        data=PowerwallData(
            connection_mode="TEDAPI",
            tedapi_mode=cached_mode if cached_mode is not None else mode,
        ),
        online=True,
    )
    return connection


@pytest.mark.parametrize(
    "gateways,available",
    [
        ((("default", "v1r"), ("other", "full")), True),
        ((("default", "full"), ("other", "v1r")), False),
        ((("other", "full"), ("default", "v1r")), True),
        ((("other", "v1r"), ("default", "full")), False),
        ((("first", "v1r"), ("second", "full")), True),
        ((("first", "full"), ("second", "v1r")), False),
    ],
)
def test_dispatch_capability_matches_default_gateway_endpoint(
    client, gateways, available
):
    connections = {
        gateway_id: _register_gateway(gateway_id, mode) for gateway_id, mode in gateways
    }
    # Aggregate stats retain their established meaning: the last cached mode
    # can differ from the default gateway's live connection used for writes.
    assert client.get("/stats").json()["tedapi_mode"] == gateways[-1][1]
    response = client.get("/control/status")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "battery_dispatch": available,
        "dispatch_active": None,
        "dispatch_pending": False,
    }
    assert all(not connection.method_calls for connection in connections.values())

    result = post(client, "dispatch", {"power_watts": 500, "duration_seconds": 30})
    assert result.status_code == (200 if available else 503)
    default_id = "default" if "default" in connections else gateways[0][0]
    for gateway_id, connection in connections.items():
        if available and gateway_id == default_id:
            connection.dispatch_battery_power.assert_called_once_with(500, 30)
        else:
            connection.dispatch_battery_power.assert_not_called()


def test_dispatch_capability_tracks_live_connection_instead_of_stale_cache(client):
    connection = _register_gateway("default", "full", cached_mode="v1r")
    assert client.get("/control/status").json()["battery_dispatch"] is False
    connection.tedapi_mode = "v1r"
    assert client.get("/control/status").json()["battery_dispatch"] is True
    connection.tedapi_mode = "cloud"
    assert client.get("/control/status").json()["battery_dispatch"] is False
    assert not connection.method_calls


def test_dispatch_capability_is_false_without_a_gateway_or_live_connection(client):
    from app.core.gateway_manager import gateway_manager

    assert client.get("/control/status").json() == {
        "enabled": True,
        "battery_dispatch": False,
        "dispatch_active": None,
        "dispatch_pending": False,
    }
    _register_gateway("default", "v1r")
    gateway_manager.connections.clear()
    assert client.get("/control/status").json()["battery_dispatch"] is False


def test_dispatch_capability_is_false_when_control_is_disabled(client, monkeypatch):
    from app.config import settings

    connection = _register_gateway("default", "v1r")
    monkeypatch.setattr(settings, "control_secret", None)
    response = client.get("/control/status")
    assert response.json() == {
        "enabled": False,
        "battery_dispatch": False,
        "dispatch_active": None,
        "dispatch_pending": False,
    }
    assert not connection.method_calls


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("watts", [-500, 500, 0, 12.5])
def test_dispatch_and_resume_stay_local(
    client, connected_gateway, mock_pypowerwall, form, watts
):
    from app.core.gateway_manager import gateway_manager

    data = {"power_watts": watts, "duration_seconds": 30}
    mock_pypowerwall.dispatch_battery_power.return_value = data
    r = post(client, "dispatch", data, form)
    assert (r.status_code, r.json()) == (200, data)
    mock_pypowerwall.dispatch_battery_power.assert_called_once_with(watts, 30)
    r = post(client, "resume", {}, form)
    assert (r.status_code, r.json()) == (200, {"resume_battery_operation": True})
    mock_pypowerwall.resume_battery_operation.assert_called_once_with()
    assert not gateway_manager._cloud_control.method_calls


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"power_watts": "nan", "duration_seconds": 30},
        {"power_watts": "inf", "duration_seconds": 30},
        {"power_watts": True, "duration_seconds": 30},
        {"power_watts": 500, "duration_seconds": 0},
        {"power_watts": 500, "duration_seconds": -1},
        {"power_watts": 500, "duration_seconds": 86401},
        {"power_watts": 500, "duration_seconds": True},
        {"power_watts": 500, "duration_seconds": 1.5},
    ],
)
def test_invalid_input_never_sends(
    client, connected_gateway, mock_pypowerwall, form, data
):
    assert post(client, "dispatch", data, form).status_code == 400
    mock_pypowerwall.dispatch_battery_power.assert_not_called()
    mock_pypowerwall.resume_battery_operation.assert_not_called()


@pytest.mark.parametrize("action", ["dispatch", "resume"])
@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("token", ["", "wrong"])
def test_authentication_required(
    client, connected_gateway, mock_pypowerwall, action, form, token
):
    data = {"power_watts": 500, "duration_seconds": 30}
    assert post(client, action, data, form, token).status_code == 401
    mock_pypowerwall.dispatch_battery_power.assert_not_called()
    mock_pypowerwall.resume_battery_operation.assert_not_called()


@pytest.mark.parametrize("action", ["dispatch", "resume"])
@pytest.mark.parametrize("form", [False, True])
def test_disabled_controls_never_send(
    client, connected_gateway, mock_pypowerwall, monkeypatch, action, form
):
    from app.config import settings

    monkeypatch.setattr(settings, "control_secret", None)
    r = post(client, action, {"power_watts": -500, "duration_seconds": 30}, form)
    assert r.status_code == (400 if form else 403)
    mock_pypowerwall.dispatch_battery_power.assert_not_called()
    mock_pypowerwall.resume_battery_operation.assert_not_called()


@pytest.mark.parametrize("action", ["dispatch", "resume"])
@pytest.mark.parametrize("form", [False, True])
def test_non_v1r_never_falls_back_to_cloud(
    client, connected_gateway, mock_pypowerwall, action, form
):
    mock_pypowerwall.tedapi_mode = "full"
    r = post(client, action, {"power_watts": -500, "duration_seconds": 30}, form)
    assert r.status_code == (400 if form else 503)
    mock_pypowerwall.dispatch_battery_power.assert_not_called()
    mock_pypowerwall.resume_battery_operation.assert_not_called()


@pytest.mark.parametrize("action", ["dispatch", "resume"])
@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("result", [None, False, {}, {"ERROR": "unsupported"}])
def test_missing_ack_is_failure(
    client, connected_gateway, mock_pypowerwall, action, form, result
):
    method = (
        "dispatch_battery_power" if action == "dispatch" else "resume_battery_operation"
    )
    getattr(mock_pypowerwall, method).return_value = result
    r = post(client, action, {"power_watts": 500, "duration_seconds": 30}, form)
    assert r.status_code == (400 if form else 503)


@pytest.mark.asyncio
async def test_resume_waits_for_timed_out_dispatch(connected_gateway, mock_pypowerwall):
    from app.core.gateway_manager import gateway_manager, _WRITE_METHODS

    assert {"dispatch_battery_power", "resume_battery_operation"} <= _WRITE_METHODS
    started, release = threading.Event(), threading.Event()

    def slow_dispatch(*_args):
        started.set()
        release.wait(2)
        return {"power_watts": -500, "duration_seconds": 30}

    mock_pypowerwall.dispatch_battery_power.side_effect = slow_dispatch
    resume = None
    try:
        result = await gateway_manager.local_control(
            "test-gateway", "dispatch_battery_power", -500, 30, timeout=0.05
        )
        assert result is None
        assert started.is_set()
        resume = asyncio.create_task(
            gateway_manager.local_control(
                "test-gateway", "resume_battery_operation", timeout=1
            )
        )
        await asyncio.sleep(0.02)
        mock_pypowerwall.resume_battery_operation.assert_not_called()
        release.set()
        assert await resume == {"resume_battery_operation": True}
    finally:
        release.set()
        if resume:
            await resume


# --- PW_BATTERY_DISPATCH opt-in (use at your own risk) ---


@pytest.mark.parametrize("form", [False, True])
def test_dispatch_is_off_without_the_opt_in(
    client, connected_gateway, mock_pypowerwall, monkeypatch, form
):
    """Control alone doesn't enable dispatch: the panel hides and the API refuses."""
    from app.config import settings

    monkeypatch.setattr(settings, "battery_dispatch", False)
    assert client.get("/control/status").json() == {
        "enabled": True,
        "battery_dispatch": False,
        "dispatch_active": None,
        "dispatch_pending": False,
    }
    r = post(client, "dispatch", {"power_watts": 500, "duration_seconds": 30}, form)
    if form:
        assert "PW_BATTERY_DISPATCH" in r.json()["error"]
    else:
        assert r.status_code == 403
        assert "PW_BATTERY_DISPATCH" in r.json()["detail"]
    mock_pypowerwall.dispatch_battery_power.assert_not_called()


@pytest.mark.parametrize("form", [False, True])
def test_resume_works_without_the_opt_in(
    client, connected_gateway, mock_pypowerwall, monkeypatch, form
):
    """Resume only hands control back, so turning the opt-in off can't strand
    a running dispatch."""
    from app.config import settings

    monkeypatch.setattr(settings, "battery_dispatch", False)
    r = post(client, "resume", {}, form)
    assert (r.status_code, r.json()) == (200, {"resume_battery_operation": True})
    mock_pypowerwall.resume_battery_operation.assert_called_once_with()


def test_status_reports_the_running_dispatch_until_resume(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.dispatch_battery_power.return_value = {
        "power_watts": -2000,
        "duration_seconds": 600,
    }
    assert post(client, "dispatch", {"power_watts": -2000, "duration_seconds": 600}).status_code == 200
    status = client.get("/control/status").json()
    assert status["dispatch_active"]["power_watts"] == -2000
    # Remaining time, not a wall-clock end: immune to client clock skew
    assert 598 <= status["dispatch_active"]["remaining_seconds"] <= 600
    assert status["dispatch_active"]["confirmed"] is True
    assert status["dispatch_pending"] is False
    assert post(client, "resume", {}).status_code == 200
    assert client.get("/control/status").json()["dispatch_active"] is None


def test_status_drops_an_expired_dispatch(client, connected_gateway, mock_pypowerwall):
    import time
    from app.api import legacy

    mock_pypowerwall.dispatch_battery_power.return_value = {
        "power_watts": 500,
        "duration_seconds": 30,
    }
    post(client, "dispatch", {"power_watts": 500, "duration_seconds": 30})
    assert client.get("/control/status").json()["dispatch_active"] is not None
    gateway_id = next(iter(legacy._dispatch_running))
    legacy._dispatch_running[gateway_id] = (500, time.monotonic() - 0.01, True)
    assert client.get("/control/status").json()["dispatch_active"] is None
    assert gateway_id not in legacy._dispatch_running


@pytest.mark.parametrize(
    "result",
    [
        None,  # the library returns None for a lost response and a rejection alike
        {},
        {"ERROR": "unsupported"},
        {"power_watts": 400, "duration_seconds": 30},  # not this command's ack
        RuntimeError("connection reset"),
    ],
)
def test_unacknowledged_dispatch_warns_that_it_may_be_running(
    client, connected_gateway, mock_pypowerwall, result
):
    """No acknowledgement doesn't prove the gateway rejected it, so the
    warning stays up (unconfirmed) and the request still reports failure."""
    if isinstance(result, Exception):
        mock_pypowerwall.dispatch_battery_power.side_effect = result
    else:
        mock_pypowerwall.dispatch_battery_power.return_value = result
    r = post(client, "dispatch", {"power_watts": 500, "duration_seconds": 30})
    assert r.status_code in (500, 503)
    active = client.get("/control/status").json()["dispatch_active"]
    assert active["power_watts"] == 500 and active["confirmed"] is False


def test_running_dispatch_is_reported_while_v1r_is_down(
    client, connected_gateway, mock_pypowerwall
):
    """The gateway keeps running a timed dispatch after the client loses v1r,
    exactly when local resume isn't possible, so the warning stays."""
    mock_pypowerwall.dispatch_battery_power.return_value = {
        "power_watts": 500,
        "duration_seconds": 300,
    }
    post(client, "dispatch", {"power_watts": 500, "duration_seconds": 300})
    mock_pypowerwall.tedapi_mode = "cloud"
    status = client.get("/control/status").json()
    assert status["battery_dispatch"] is False
    assert status["dispatch_active"]["power_watts"] == 500


@pytest.mark.asyncio
async def test_timed_out_dispatch_is_recorded_when_its_worker_finishes(
    connected_gateway, mock_pypowerwall
):
    """A request can time out while the worker still delivers the command:
    status shows it pending, then records the real outcome."""
    import time
    from app.api import legacy
    from app.core.gateway_manager import gateway_manager

    started, release = threading.Event(), threading.Event()

    def slow_dispatch(watts, seconds):
        started.set()
        release.wait(2)
        return {"power_watts": watts, "duration_seconds": seconds}

    mock_pypowerwall.dispatch_battery_power.side_effect = slow_dispatch
    method, args = "dispatch_battery_power", (700, 120)
    submitted = time.monotonic()
    try:
        result = await gateway_manager.local_control(
            "test-gateway", method, *args, timeout=0.05,
            on_submit=legacy._track_dispatch("test-gateway", method, args),
        )
        assert result is None and started.is_set()
        status = legacy._dispatch_status("test-gateway")
        assert status == {"dispatch_active": None, "dispatch_pending": True}
        await asyncio.sleep(0.3)  # the gateway takes a while to answer
    finally:
        release.set()
    for _ in range(100):
        if not legacy._dispatch_status("test-gateway")["dispatch_pending"]:
            break
        await asyncio.sleep(0.01)
    status = legacy._dispatch_status("test-gateway")
    assert status["dispatch_pending"] is False
    assert status["dispatch_active"]["power_watts"] == 700
    assert status["dispatch_active"]["confirmed"] is True
    # Conservative: the gateway accepted it at or before completion, so the
    # warning counts from completion and never ends before the gateway stops
    _, deadline, _ = legacy._dispatch_running["test-gateway"]
    assert deadline >= submitted + 0.3 + 120


@pytest.mark.asyncio
async def test_unaccepted_or_failed_command_keeps_the_previous_state(
    connected_gateway, mock_pypowerwall
):
    from app.api import legacy
    from app.core.gateway_manager import gateway_manager

    legacy._dispatch_running["test-gateway"] = (300, 1e12, True)
    for outcome in (
        {"ERROR": "rejected"},
        {"resume_battery_operation": False},  # not an acknowledgement
        RuntimeError("boom"),
    ):
        if isinstance(outcome, Exception):
            mock_pypowerwall.resume_battery_operation.side_effect = outcome
        else:
            mock_pypowerwall.resume_battery_operation.return_value = outcome
        await gateway_manager.local_control(
            "test-gateway", "resume_battery_operation", timeout=1,
            on_submit=legacy._track_dispatch("test-gateway", "resume_battery_operation", ()),
        )
        await asyncio.sleep(0)
        assert legacy._dispatch_running["test-gateway"][0] == 300


@pytest.mark.parametrize("value", [True, False])
def test_stats_reports_the_opt_in(client, monkeypatch, value):
    from app.config import settings

    monkeypatch.setattr(settings, "battery_dispatch", value)
    assert client.get("/stats").json()["config"]["PW_BATTERY_DISPATCH"] is value


def test_opt_in_defaults_off(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("PW_BATTERY_DISPATCH", raising=False)
    assert Settings().battery_dispatch is False
    monkeypatch.setenv("PW_BATTERY_DISPATCH", "yes")
    assert Settings().battery_dispatch is True


@pytest.mark.parametrize(
    "opted_in, secret, expected",
    [
        (True, TOKEN, "USE AT YOUR OWN RISK"),
        (True, None, "no effect without PW_CONTROL_SECRET"),
        (False, TOKEN, None),
    ],
)
def test_startup_warns_when_dispatch_is_opted_in(
    monkeypatch, caplog, opted_in, secret, expected
):
    import logging
    from app import main
    from app.config import settings

    monkeypatch.setattr(settings, "battery_dispatch", opted_in)
    monkeypatch.setattr(settings, "control_secret", secret)
    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        main._log_battery_dispatch_opt_in()
    messages = [r.getMessage() for r in caplog.records]
    if expected:
        assert any(expected in m for m in messages)
    else:
        assert not messages


def test_lifespan_stays_an_async_context_manager():
    """The startup warning helper sits next to lifespan: keep the decorator on
    lifespan (FastAPI's contract), not on the helper."""
    from app import main

    manager = main.lifespan(main.app)  # creating it doesn't run startup
    assert hasattr(manager, "__aenter__") and hasattr(manager, "__aexit__")


@pytest.mark.asyncio
async def test_pending_until_the_outcome_is_recorded():
    """A future can be done before its callbacks run: status keeps reporting
    pending until the callback has recorded the outcome."""
    from app.api import legacy

    future = asyncio.get_running_loop().create_future()
    legacy._track_dispatch("gw", "dispatch_battery_power", (500, 60))(future)
    future.set_result({"power_watts": 500, "duration_seconds": 60})
    assert legacy._dispatch_status("gw") == {"dispatch_active": None, "dispatch_pending": True}
    await asyncio.sleep(0)
    status = legacy._dispatch_status("gw")
    assert status["dispatch_pending"] is False
    assert status["dispatch_active"]["confirmed"] is True


@pytest.mark.asyncio
async def test_a_command_that_expired_before_sending_changes_nothing():
    """_locked_call drops a write whose time ran out before a worker started
    it (asyncio.TimeoutError): it never reached the gateway."""
    from app.api import legacy

    legacy._dispatch_running["gw"] = (300, 1e12, True)
    for method, args in (("dispatch_battery_power", (900, 60)), ("resume_battery_operation", ())):
        future = asyncio.get_running_loop().create_future()
        legacy._track_dispatch("gw", method, args)(future)
        future.set_exception(asyncio.TimeoutError("expired before a worker started it"))
        await asyncio.sleep(0)
        assert legacy._dispatch_running["gw"] == (300, 1e12, True)
        assert "gw" not in legacy._dispatch_pending


def test_endpoint_requires_the_method_specific_acknowledgement(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.resume_battery_operation.return_value = {"resume_battery_operation": False}
    assert post(client, "resume", {}).status_code == 503
    mock_pypowerwall.dispatch_battery_power.return_value = {"power_watts": 400, "duration_seconds": 30}
    assert post(client, "dispatch", {"power_watts": 500, "duration_seconds": 30}).status_code == 503
