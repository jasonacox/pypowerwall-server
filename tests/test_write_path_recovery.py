"""Write serialization, connection lifecycle and setup wiring.

- A timed-out or cancelled write keeps the write lock until its library call
  finishes, on every write path (local, cloud, call_api), so two writes never
  overlap.
- initialize() keeps each gateway's configuration for recovery, and
  PW_FAILOVER reaches every pypowerwall connection the server builds.
- MQTT discovery remembers each battery's serial across snapshots that
  omit it, so its entities stay on their own Home Assistant device.
"""

import asyncio
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import GatewayConfig, settings
from app.core.gateway_manager import GatewayManager, gateway_manager
from app.models.gateway import Gateway, GatewayStatus
from app.mqtt.publisher import MqttPublisher


async def _wait_for(event: threading.Event) -> None:
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker never started")


def _blocking_call(started: threading.Event, release: threading.Event):
    def call(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return {"result": "done"}

    return call


def _gm_with_slow_write(path: str):
    """A manager whose first set_reserve blocks until released, and a
    coroutine factory that runs a write on ``path``."""
    gm = GatewayManager()
    started, release = threading.Event(), threading.Event()
    pw = Mock()
    pw.set_reserve.side_effect = _blocking_call(started, release)
    pw.set_mode.return_value = {"result": "mode"}
    if path == "cloud":
        gm._cloud_control = pw

        def write(method, *args, timeout):
            return gm.cloud_control(method, *args, timeout=timeout)

    else:
        gm.connections["gw"] = pw

        def write(method, *args, timeout):
            return gm.call_api("gw", method, *args, timeout=timeout)

    return gm, pw, started, release, write


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cloud", "call_api"])
async def test_timed_out_write_holds_lock_until_worker_finishes(path):
    gm, pw, started, release, write = _gm_with_slow_write(path)
    try:
        assert await write("set_reserve", 20, timeout=0.05) is None
        assert started.is_set()

        queued = asyncio.create_task(write("set_mode", "backup", timeout=5))
        await asyncio.sleep(0.05)
        pw.set_mode.assert_not_called()
        assert not queued.done()

        release.set()
        assert await asyncio.wait_for(queued, timeout=5) == {"result": "mode"}
        assert not gm._write_lock.locked()
    finally:
        release.set()
        await gm.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cloud", "call_api"])
async def test_cancelled_write_holds_lock_until_worker_finishes(path):
    """A client that disconnects mid-write cancels the request task; the
    library call keeps running, so the lock must stay held."""
    gm, pw, started, release, write = _gm_with_slow_write(path)
    try:
        first = asyncio.create_task(write("set_reserve", 20, timeout=5))
        await _wait_for(started)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert gm._write_lock.locked()

        release.set()
        assert await asyncio.wait_for(
            write("set_mode", "backup", timeout=5), timeout=5
        ) == {"result": "mode"}
    finally:
        release.set()
        await gm.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cloud", "call_api"])
async def test_write_that_expires_while_queued_never_runs(path):
    """The timeout covers waiting for the lock: a write still queued at its
    deadline is not dispatched later, after its caller gave up."""
    gm, pw, started, release, write = _gm_with_slow_write(path)
    try:
        first = asyncio.create_task(write("set_reserve", 20, timeout=5))
        await _wait_for(started)
        assert await write("set_mode", "backup", timeout=0.05) is None

        release.set()
        assert await asyncio.wait_for(first, timeout=5) == {"result": "done"}
        await asyncio.sleep(0.05)
        pw.set_mode.assert_not_called()
        assert not gm._write_lock.locked()
    finally:
        release.set()
        await gm.shutdown()


@pytest.mark.asyncio
async def test_lock_acquired_too_late_is_not_dispatched(monkeypatch):
    """Acquired with no time left (the wait used it all): released, not run."""
    import sys
    from types import SimpleNamespace

    gm_module = sys.modules["app.core.gateway_manager"]
    gm = GatewayManager()
    # The clock jumps past the deadline once the deadline is set
    clock = itertools.chain([0.0], itertools.repeat(10.0))
    monkeypatch.setattr(
        gm_module, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    func = Mock()
    with pytest.raises(asyncio.TimeoutError):
        await gm._locked_call(func, timeout=1)
    for _ in range(100):  # released when the worker gives the call up
        if not gm._write_lock.locked():
            break
        await asyncio.sleep(0.01)
    func.assert_not_called()
    assert not gm._write_lock.locked()


@pytest.mark.asyncio
async def test_write_queued_behind_busy_executor_never_runs():
    """Reads can fill the shared executor: a write whose deadline passes
    before a worker starts it is dropped, not sent late."""
    gm = GatewayManager()
    gm._executor = ThreadPoolExecutor(max_workers=1)
    started, release = threading.Event(), threading.Event()
    blocker = gm._executor.submit(_blocking_call(started, release))
    try:
        await _wait_for(started)
        write = Mock(return_value={"result": "late"})
        with pytest.raises(asyncio.TimeoutError):
            await gm._locked_call(write, timeout=0.05)

        release.set()
        blocker.result(timeout=5)
        for _ in range(100):
            if not gm._write_lock.locked():
                break
            await asyncio.sleep(0.01)
        write.assert_not_called()
        assert not gm._write_lock.locked()
    finally:
        release.set()
        gm._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_failed_dispatch_releases_lock():
    """If the executor refuses the call, the lock is not leaked."""
    gm = GatewayManager()
    gm._executor = ThreadPoolExecutor(max_workers=1)
    gm._executor.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        await gm._locked_call(lambda: None, timeout=1)
    assert not gm._write_lock.locked()


# ---------------------------------------------------------------------------
# Setup wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_keeps_config_for_recovery(monkeypatch, mock_pypowerwall):
    import pypowerwall

    monkeypatch.setattr(pypowerwall, "Powerwall", Mock(return_value=mock_pypowerwall))
    config = GatewayConfig(id="home", name="Home", host="192.168.91.1", gw_pwd="pw")
    try:
        await gateway_manager.initialize([config], poll_interval=5)
        assert gateway_manager._gateway_configs["home"] is config
    finally:
        await gateway_manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failover", [True, False])
@pytest.mark.parametrize("fleetapi", [False, True], ids=["cloud", "fleetapi"])
async def test_failover_reaches_cloud_gateway_connection(
    monkeypatch, mock_pypowerwall, failover, fleetapi
):
    import pypowerwall

    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)
    monkeypatch.setattr(settings, "failover", failover)
    kwargs = dict(
        id="cl", name="Cloud", email="a@b.c", cloud_mode=True, fleetapi=fleetapi
    )
    gw = Gateway(**kwargs)
    gateway_manager.gateways["cl"] = gw
    gateway_manager._pending_configs["cl"] = GatewayConfig(**kwargs)
    gateway_manager.cache["cl"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["cl"] = 0
    gateway_manager._next_poll_time["cl"] = 0

    await gateway_manager._poll_gateway("cl")

    assert powerwall_spy.call_args.kwargs.get("failover") is failover


@pytest.mark.asyncio
@pytest.mark.parametrize("failover", [True, False])
async def test_failover_reaches_cloud_control_connection(monkeypatch, failover):
    import pypowerwall

    pw = Mock(cloudmode=True)
    pw.is_connected.return_value = True
    powerwall_spy = Mock(return_value=pw)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)
    monkeypatch.setattr(settings, "failover", failover)
    monkeypatch.setattr(settings, "siteid", None)
    gm = GatewayManager()
    monkeypatch.setattr(gm, "_cloud_site_unambiguous", AsyncMock(return_value=True))
    config = GatewayConfig(id="gw", name="GW", host="192.168.91.1", email="a@b.c")
    try:
        await asyncio.wait_for(gm._init_cloud_control([config]), timeout=5)
        assert gm._cloud_control is pw
        assert powerwall_spy.call_args.kwargs.get("failover") is failover
    finally:
        await gm.shutdown()


# ---------------------------------------------------------------------------
# MQTT discovery battery serial memory (#153)
# ---------------------------------------------------------------------------


def _system_status(*serials):
    return {
        "battery_blocks": [
            {
                "nominal_full_pack_energy": 13500,
                "nominal_energy_remaining": 6000,
                **({"PackageSerialNumber": s} if s else {}),
            }
            for s in serials
        ]
    }


def test_discovery_remembers_battery_serials():
    publisher = MqttPublisher()
    first = publisher._battery_blocks("gw", _system_status("SN-A", "SN-B"))
    assert [b["serial"] for b in first] == ["SN-A", "SN-B"]

    later = publisher._battery_blocks("gw", _system_status(None, None))
    assert [b["serial"] for b in later] == ["SN-A", "SN-B"]


def test_discovery_battery_serial_memory_is_per_gateway_and_updates():
    publisher = MqttPublisher()
    publisher._battery_blocks("gw1", _system_status("SN-A"))
    assert publisher._battery_blocks("gw2", _system_status(None))[0]["serial"] is None

    publisher._battery_blocks("gw1", _system_status("SN-NEW"))
    assert (
        publisher._battery_blocks("gw1", _system_status(None))[0]["serial"] == "SN-NEW"
    )


def test_discovery_battery_serial_follows_reorder():
    """[A, B] then [B, missing]: the second battery must not inherit B."""
    publisher = MqttPublisher()
    publisher._battery_blocks("gw", _system_status("SN-A", "SN-B"))
    moved = publisher._battery_blocks("gw", _system_status("SN-B", None))
    assert [b["serial"] for b in moved] == ["SN-B", None]

    # B stays known at its new position only
    later = publisher._battery_blocks("gw", _system_status(None, None))
    assert [b["serial"] for b in later] == ["SN-B", None]


def test_discovery_battery_serial_follows_serial_only_block():
    """A block with a serial but no energy values isn't announced, but its
    serial still moves: [A, B] then [B (serial only), unlabeled] must not
    put B's identity on the second battery."""
    publisher = MqttPublisher()
    publisher._battery_blocks("gw", _system_status("SN-A", "SN-B"))
    status = {
        "battery_blocks": [
            {"PackageSerialNumber": "SN-B"},
            {"nominal_full_pack_energy": 13500, "nominal_energy_remaining": 6000},
        ]
    }
    batteries = publisher._battery_blocks("gw", status)
    assert [(b["index"], b["serial"]) for b in batteries] == [(2, None)]
