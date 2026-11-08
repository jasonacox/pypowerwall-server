"""Tests for the opt-in Cloud-Read-Fallback after local poll failures."""
import asyncio
import time
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core.gateway_manager import GatewayManager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


def _manager(monkeypatch, *, bound=True, cloud=True, failover=True, flag=True):
    gm = GatewayManager()
    gm._poll_interval = 4
    gw = Gateway(id="v1r", name="v1r", host="10.0.0.2")
    gm.gateways["v1r"] = gw
    gm.cache["v1r"] = GatewayStatus(gateway=gw, online=False)
    gm.connections["v1r"] = Mock()
    gm._cloud_control = Mock(cloudmode=cloud, fleetapi=False, mode="cloud") if cloud else Mock(
        cloudmode=False, fleetapi=False, mode="local"
    )
    gm._cloud_control_gateway_id = "v1r" if bound else None
    gm._next_poll_time["v1r"] = 0
    gm._consecutive_failures["v1r"] = 0
    gm._record_timeseries_sample = Mock()
    gm._record_signal_sample = Mock()
    gm._schedule_mqtt_publish = Mock()
    monkeypatch.setattr(settings, "cloud_read_fallback", flag)
    monkeypatch.setattr(settings, "failover", failover)
    return gm


def _data(source="cloud"):
    return PowerwallData(
        aggregates={
            "solar": {"instant_power": 1000},
            "site": {"instant_power": 0},
            "battery": {"instant_power": 0},
            "load": {"instant_power": 1000},
        },
        soe=50.0,
        timestamp=time.time(),
    )


def _install_fetch(gm, *, local_data=None, cloud_data=None, calls=None):
    async def fake_fetch(gateway_id, pw, *, cloud_fallback=False):
        calls.append((pw is gm._cloud_control, cloud_fallback))
        if pw is gm._cloud_control:
            return cloud_data
        if local_data is None:
            raise RuntimeError("local down")
        return local_data

    gm._fetch_gateway_data = fake_fetch

async def _enter_fallback(gm, gateway_id="v1r"):
    """Three failed local polls, then the cloud-read loop's next tick."""
    for _ in range(3):
        gm._next_poll_time[gateway_id] = 0
        await gm._poll_gateway(gateway_id)
    return await gm._maybe_cloud_read(gateway_id)



@pytest.mark.asyncio
async def test_cloud_fallback_triggers_on_third_local_failure(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)

    await gm._poll_gateway("v1r")
    assert calls == [(False, False)]
    assert gm._cloud_read_state.get("v1r") is None

    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    assert calls[-1] == (False, False)
    assert gm._cloud_read_state.get("v1r") is None
    assert await gm._maybe_cloud_read("v1r") is None   # two failures: not yet

    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    assert calls[-1] == (False, False)   # the poll itself never reads the cloud
    assert await gm._maybe_cloud_read("v1r") is True   # the cloud-read loop's tick
    assert calls[-1] == (True, True)
    status = gm.cache["v1r"]
    assert status.online is True
    assert status.data_source == "cloud"
    assert status.local_online is False
    assert status.data is not None
    assert gm._cloud_read_state["v1r"]["active"] is True
    # Third local failure uses the 30 s backoff; cloud fallback must preserve it.
    assert gm._next_poll_time["v1r"] >= time.time() + 29
    gm._schedule_mqtt_publish.assert_called_with("v1r")


def test_apply_cloud_read_preserves_local_backoff(monkeypatch):
    gm = _manager(monkeypatch)
    gm._next_poll_time["v1r"] = 12345.0
    gm._apply_cloud_read("v1r", _data())
    assert gm._next_poll_time["v1r"] == 12345.0


@pytest.mark.asyncio
async def test_cloud_fallback_off_never_uses_cloud(monkeypatch):
    gm = _manager(monkeypatch, flag=False)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)

    await _enter_fallback(gm)

    assert gm.cache["v1r"].online is False
    assert all(not is_cloud for is_cloud, _ in calls)


@pytest.mark.asyncio
async def test_cloud_fallback_does_not_trigger_on_first_failure(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await gm._poll_gateway("v1r")
    assert not any(is_cloud for is_cloud, _ in calls)
    assert gm.cache["v1r"].online is False


@pytest.mark.asyncio
async def test_cloud_fallback_failure_keeps_offline_and_records_error(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=None, calls=calls)

    await _enter_fallback(gm)

    assert gm.cache["v1r"].online is False
    state = gm._cloud_read_state["v1r"]
    assert state["active"] is True
    assert state["failures"] == 1
    assert "aggregates" in state["last_error"]


@pytest.mark.asyncio
async def test_local_recovery_clears_cloud_state(monkeypatch):
    gm = _manager(monkeypatch)
    gm._cloud_read_state["v1r"] = gm._new_cloud_read_state()
    gm._cloud_read_state["v1r"]["active"] = True
    calls = []
    _install_fetch(gm, local_data=_data(), calls=calls)

    await gm._poll_gateway("v1r")

    assert gm.cache["v1r"].data_source == "local"
    assert gm.cache["v1r"].local_online is True
    assert "v1r" not in gm._cloud_read_state
    assert all(not is_cloud for is_cloud, _ in calls)


@pytest.mark.asyncio
async def test_cloud_fallback_requires_bound_site(monkeypatch):
    gm = _manager(monkeypatch, bound=False)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)

    await _enter_fallback(gm)

    assert gm.cache["v1r"].online is False
    assert all(not is_cloud for is_cloud, _ in calls)


@pytest.mark.asyncio
async def test_cloud_fallback_requires_cloud_client_and_failover(monkeypatch):
    gm = _manager(monkeypatch, cloud=False)
    assert gm._cloud_read_fallback_allowed("v1r") is False

    gm = _manager(monkeypatch, failover=False)
    assert gm._cloud_read_fallback_allowed("v1r") is False


class _FakeCloudPowerwall:
    cloudmode = True
    fleetapi = False
    tedapi_mode = "Cloud"
    tedapi = None

    def poll(self, path):
        if path == "/api/meters/aggregates":
            return {
                "solar": {"instant_power": 1000},
                "site": {"instant_power": 0},
                "battery": {"instant_power": 0},
                "load": {"instant_power": 1000},
            }
        return None

    def level(self):
        return 50.0

    def grid_status(self):
        return "UP"

    def __getattr__(self, name):
        return lambda *args, **kwargs: None



@pytest.mark.asyncio
async def test_cloud_fallback_serves_local_site_info_without_touching_it(monkeypatch):
    """The cloud's site_info must not replace the local copy, which would
    outlive local recovery until the next refresh (300 s)."""
    gm = _manager(monkeypatch)
    gm._site_info["v1r"] = {"source": "local"}
    gm._site_info_attempt["v1r"] = (0.0, True)   # refresh long overdue
    polled = []

    class Cloud(_FakeCloudPowerwall):
        def poll(self, path):
            polled.append(path)
            if path == "/api/site_info":
                return {"source": "cloud"}
            return super().poll(path)

    data = await gm._fetch_gateway_data("v1r", Cloud(), cloud_fallback=True)

    assert data.site_info == {"source": "local"}
    assert "/api/site_info" not in polled
    assert gm._site_info["v1r"] == {"source": "local"}
    assert gm._site_info_attempt["v1r"] == (0.0, True)

@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_only, cloud_calls", [(True, False), (False, True)])
async def test_fallback_only_connection_adds_no_per_poll_cloud_reads(
    monkeypatch, fallback_only, cloud_calls
):
    """A connection started only for PW_CLOUD_READ_FALLBACK must not query
    Tesla on every healthy local poll (Basic LAN mode/reserve and the like);
    with PW_CONTROL_SECRET or PW_CLOUD_HYBRID it still does."""
    from unittest.mock import AsyncMock

    gm = _manager(monkeypatch)
    gm.gateways["v1r"].basic_lan = True
    gm._cloud_for_fallback_only = fallback_only
    gm.cloud_control = AsyncMock(return_value=None)

    await gm._fetch_gateway_data("v1r", _FakeCloudPowerwall())

    assert gm.cloud_control.await_count > 0 if cloud_calls else gm.cloud_control.await_count == 0


@pytest.mark.asyncio
async def test_cloud_fallback_uses_same_fields_as_cloud_mode(monkeypatch):
    normal = _manager(monkeypatch)
    normal.gateways["v1r"].cloud_mode = True
    normal.gateways["v1r"].basic_lan = False
    fallback = _manager(monkeypatch)
    fallback.gateways["v1r"].cloud_mode = False
    fallback.gateways["v1r"].basic_lan = True
    fake = _FakeCloudPowerwall()

    cloud_mode_data = await normal._fetch_gateway_data("v1r", fake)
    fallback_data = await fallback._fetch_gateway_data(
        "v1r", fake, cloud_fallback=True
    )

    left = cloud_mode_data.model_dump()
    right = fallback_data.model_dump()
    left.pop("timestamp", None)
    right.pop("timestamp", None)
    assert left == right


@pytest.mark.asyncio
async def test_health_reports_degraded_for_cloud_fallback(monkeypatch):
    import app.main as main

    gm = _manager(monkeypatch)
    gm.cache["v1r"] = GatewayStatus(
        gateway=gm.gateways["v1r"],
        data=_data(),
        online=True,
        data_source="cloud",
        local_online=False,
    )
    gm._cloud_read_state["v1r"] = {
        "active": True,
        "since": time.time() - 5,
        "last_success": time.time(),
        "failures": 0,
        "last_error": None,
    }
    monkeypatch.setattr(main, "gateway_manager", gm)

    result = await main.health_check()

    assert result["status"] == "degraded"
    assert result["gateway_details"][0]["data_source"] == "cloud"
    assert result["gateway_details"][0]["local_online"] is False
    assert result["cloud_read_fallback"]["v1r"]["active"] is True


@pytest.mark.asyncio
async def test_cloud_read_fallback_binds_cloud_without_mqtt(
    monkeypatch, tmp_path
):
    """The fallback needs a site-bound cloud link even without MQTT controls."""
    import types
    import sys

    from app.config import GatewayConfig

    gm = GatewayManager()
    site = tmp_path / ".pypowerwall.site"
    site.write_text("123")
    cloud = types.SimpleNamespace(
        client=types.SimpleNamespace(sitefile=str(site)),
        cloudmode=True,
        fleetapi=False,
        mode="cloud",
        is_connected=lambda: True,
    )
    gm_module = sys.modules["app.core.gateway_manager"]
    monkeypatch.setattr(
        gm_module.pypowerwall, "Powerwall", lambda **kwargs: cloud
    )
    for name, value in {
        "mqtt_controls": 0,
        "mqtt_host": None,
        "mqtt_username": None,
        "mqtt_password": None,
        "control_secret": None,
        "siteid": None,
        "cloud_read_fallback": True,
        "failover": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    config = GatewayConfig(
        id="v1r",
        name="v1r",
        host="10.0.0.2",
        email="user@example.com",
        authpath=str(tmp_path),
    )
    await gm._init_cloud_control([config])
    assert gm._cloud_control is cloud
    assert gm._cloud_control_gateway_id == "v1r"


@pytest.mark.asyncio
async def test_cloud_read_flag_starts_cloud_without_control_secret(monkeypatch):
    from app.config import GatewayConfig, settings
    from unittest.mock import AsyncMock

    gm = GatewayManager()
    cfg = GatewayConfig(
        id="v1r",
        name="v1r",
        host="10.0.0.2",
        gw_pwd="pw",
        rsa_key_path="key.pem",
        wifi_host="192.168.91.1",
        email="user@example.com",
        authpath="/data",
    )
    monkeypatch.setattr(settings, "cloud_read_fallback", True)
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "cloud_hybrid", False)
    monkeypatch.setattr(settings, "failover", True)
    monkeypatch.setattr(settings, "tedapi_recovery", False)
    monkeypatch.setattr(gm, "_poll_gateway_loop", AsyncMock())
    monkeypatch.setattr(gm, "_init_cloud_control", AsyncMock())
    try:
        await gm.initialize([cfg], poll_interval=4)
        assert gm._cloud_control_configured is True
        assert gm._cloud_control_task is not None
    finally:
        await gm.shutdown()


def test_cloud_read_fallback_defaults_off():
    assert settings.cloud_read_fallback is False


def test_cloud_fallback_is_limited_to_bound_gateway(monkeypatch):
    gm = _manager(monkeypatch)
    gm.gateways["other"] = Gateway(id="other", name="other", host="10.0.0.3")
    gm.cache["other"] = GatewayStatus(gateway=gm.gateways["other"], online=False)
    assert gm._cloud_read_fallback_allowed("v1r") is True
    assert gm._cloud_read_fallback_allowed("other") is False


def test_cloud_read_state_snapshot_is_serializable(monkeypatch):
    gm = _manager(monkeypatch)
    gm._cloud_read_state["v1r"] = {
        "active": True,
        "since": time.time() - 5,
        "last_success": time.time(),
        "failures": 0,
        "last_error": None,
    }
    snapshot = gm.get_cloud_read_state("v1r")
    assert snapshot["active"] is True
    assert snapshot["duration_seconds"] >= 4
    assert snapshot["enabled"] is True


@pytest.mark.asyncio
async def test_cloud_fallback_mqtt_stays_online_and_publishes_cloud_data(monkeypatch):
    """Cloud data keeps HA availability online; values come from the cloud read."""
    import json
    from unittest.mock import AsyncMock

    from app.mqtt.publisher import MqttPublisher

    gm = _manager(monkeypatch)
    gm._apply_cloud_read("v1r", _data())
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True

    await pub.publish_gateway("v1r", gm.cache["v1r"])

    published = {c.args[0]: c.args[1] for c in pub._client.publish.call_args_list}
    assert published["pypowerwall/v1r/online"] == "true"
    assert published["pypowerwall/v1r/availability"] == "online"
    assert json.loads(published["pypowerwall/v1r/status"])["soe"] == 50.0


def test_cloud_fallback_does_not_mix_multi_powerwall_snapshot(monkeypatch):
    """A cloud fallback is a complete cloud view, not a partial local merge."""
    gm = _manager(monkeypatch)
    monkeypatch.setattr(
        gm,
        "_preserve_complete_multi_pw_snapshot",
        Mock(side_effect=AssertionError("must not merge local snapshot")),
    )
    # Only cloud_fallback=True skips the merge; the call must complete normally.
    import asyncio
    data = asyncio.run(
        gm._fetch_gateway_data("v1r", _FakeCloudPowerwall(), cloud_fallback=True)
    )
    assert data.powerwalls is None


@pytest.mark.asyncio
async def test_slow_local_retry_does_not_pause_cloud_reads(monkeypatch):
    """The cloud-read loop keeps its cadence while a local retry hangs (a
    timing-out gateway can hold one for its whole poll budget)."""
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"

    release = asyncio.Event()

    async def hanging_local(gateway_id, pw, *, cloud_fallback=False):
        calls.append((pw is gm._cloud_control, cloud_fallback))
        if pw is gm._cloud_control:
            return _data()
        await release.wait()
        raise RuntimeError("local down")

    gm._fetch_gateway_data = hanging_local
    gm._poll_interval = 0.05
    monkeypatch.setattr(gm, "_cloud_read_interval", lambda: 0.05)
    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = 0
    start = len(calls)
    local = asyncio.create_task(gm._poll_gateway("v1r"))
    cloud = asyncio.create_task(gm._cloud_read_loop("v1r"))
    try:
        await asyncio.sleep(0.4)
        assert not local.done()   # the local retry is still hanging
        assert sum(is_cloud for is_cloud, _ in calls[start:]) >= 3
        assert gm.cache["v1r"].data_source == "cloud"
    finally:
        release.set()
        cloud.cancel()
        await asyncio.gather(local, cloud, return_exceptions=True)


@pytest.mark.asyncio
async def test_no_second_cloud_read_while_one_is_in_flight(monkeypatch):
    """A read that outlasts the interval must not let another caller queue a
    second one behind it: reads would start closer than the interval (the
    FleetAPI 30 s minimum)."""
    gm = _manager(monkeypatch)
    _install_fetch(gm, cloud_data=_data(), calls=[])
    await _enter_fallback(gm)

    gate = asyncio.Event()
    cloud_fetches = []

    async def slow_cloud(gateway_id, pw, *, cloud_fallback=False):
        if pw is gm._cloud_control:
            cloud_fetches.append(1)
            await gate.wait()
            return _data()
        raise RuntimeError("local down")

    gm._fetch_gateway_data = slow_cloud
    gm._next_cloud_read_time["v1r"] = 0
    first = asyncio.create_task(gm._maybe_cloud_read("v1r"))
    await asyncio.sleep(0)
    gm._next_cloud_read_time["v1r"] = 0   # the interval passed mid-read
    assert await gm._maybe_cloud_read("v1r") is None
    gate.set()
    assert await first is True
    assert len(cloud_fetches) == 1
    assert "v1r" not in gm._cloud_read_inflight


@pytest.mark.asyncio
async def test_cloud_read_is_dropped_when_local_recovers_mid_read(monkeypatch):
    """A cloud read still in flight when local reads recover must not
    overwrite the fresh local status."""
    gm = _manager(monkeypatch)
    _install_fetch(gm, cloud_data=_data(), calls=[])
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"

    gate = asyncio.Event()

    async def fetch(gateway_id, pw, *, cloud_fallback=False):
        if pw is gm._cloud_control:
            await gate.wait()
            return _data()
        return _data()

    gm._fetch_gateway_data = fetch
    gm._next_cloud_read_time["v1r"] = 0
    cloud = asyncio.create_task(gm._maybe_cloud_read("v1r"))
    await asyncio.sleep(0)   # the cloud read is now in flight
    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")   # local reads recover
    assert gm.cache["v1r"].data_source == "local"

    gate.set()
    assert await cloud is None
    assert gm.cache["v1r"].data_source == "local"
    assert gm.cache["v1r"].local_online is True
    assert "v1r" not in gm._cloud_read_state


@pytest.mark.asyncio
async def test_cloud_read_spanning_recovery_and_a_new_outage_is_dropped(monkeypatch):
    """Recovery, then three new failures while the same cloud read is still
    pending: its pre-recovery result must not be published."""
    gm = _manager(monkeypatch)
    _install_fetch(gm, cloud_data=_data(), calls=[])
    await _enter_fallback(gm)

    gate = asyncio.Event()
    local_ok = True
    stale = _data()

    async def fetch(gateway_id, pw, *, cloud_fallback=False):
        if pw is gm._cloud_control:
            await gate.wait()
            return stale
        if local_ok:
            return _data()
        raise RuntimeError("local down")

    gm._fetch_gateway_data = fetch
    gm._next_cloud_read_time["v1r"] = 0
    cloud = asyncio.create_task(gm._maybe_cloud_read("v1r"))
    await asyncio.sleep(0)
    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")              # local recovers
    local_ok = False
    for _ in range(3):                          # and fails again
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")
    assert gm._consecutive_failures["v1r"] >= 3

    gate.set()
    assert await cloud is None
    assert gm.cache["v1r"].data is not stale
    assert gm.cache["v1r"].data_source != "cloud"


@pytest.mark.asyncio
async def test_cloud_read_task_runs_only_for_local_gateways_and_stops_on_shutdown(monkeypatch):
    import pypowerwall
    from app.config import GatewayConfig
    from app.core.gateway_manager import gateway_manager as manager

    monkeypatch.setattr(settings, "cloud_read_fallback", True)
    monkeypatch.setattr(settings, "failover", True)
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "cloud_hybrid", False)
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: Mock(
        mode="local", cloudmode=False, fleetapi=False, tedapi=None, is_connected=lambda: True))
    configs = [
        GatewayConfig(id="local", name="Local", host="10.0.0.2", password="12345"),
        GatewayConfig(id="cloud", name="Cloud", email="a@b.c", cloud_mode=True),
    ]
    await manager.initialize(configs, poll_interval=5)
    try:
        assert "local" in manager.gateways
        assert set(manager._cloud_read_tasks) == {"local"}   # never a Cloud gateway
        assert manager._cloud_for_fallback_only is True   # no control secret, no PW_CLOUD_HYBRID
        tasks = list(manager._cloud_read_tasks.values())
    finally:
        await manager.shutdown()
    assert manager._cloud_read_tasks == {}
    assert all(t.done() for t in tasks)


def test_cloud_fallback_cadence_is_cache_expire_with_fleetapi_floor(monkeypatch):
    gm = _manager(monkeypatch)
    gm._poll_interval = 5
    assert gm._cloud_read_interval() == 5
    gm._cloud_control.fleetapi = True
    assert gm._cloud_read_interval() == 30
    gm._poll_interval = 60
    assert gm._cloud_read_interval() == 60


@pytest.mark.asyncio
async def test_failed_local_retry_keeps_cloud_cache_until_next_cloud_read(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"

    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = time.time() + 100
    await gm._poll_gateway("v1r")

    assert gm.cache["v1r"].online is True
    assert gm.cache["v1r"].data_source == "cloud"


@pytest.mark.asyncio
async def test_cloud_failure_marks_cloud_cache_offline(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"

    _install_fetch(gm, cloud_data=None, calls=calls)
    gm._next_poll_time["v1r"] = time.time() + 100   # local retry in backoff
    gm._next_cloud_read_time["v1r"] = 0
    assert await gm._maybe_cloud_read("v1r") is False   # what _cloud_read_loop runs

    status = gm.cache["v1r"]
    assert status.online is False
    assert status.data_source == "cache"
    assert status.data is None
    assert status.local_online is False
    assert gm._cloud_read_state["v1r"]["failures"] == 1

    # The public getter may serve last-known data only within the TTL.
    within_ttl = gm.get_gateway("v1r")
    assert within_ttl.online is False
    assert within_ttl.data_source == "cache"
    assert within_ttl.data is not None
    assert within_ttl.local_online is False

    # Once the TTL expires, the same offline status must not retain data,
    # nor call itself cached data.
    gm._last_cloud_data["v1r"].timestamp = time.time() - settings.cache_ttl - 1
    expired = gm.get_gateway("v1r")
    assert expired.online is False
    assert expired.data is None
    assert expired.data_source is None
    assert expired.local_online is False

    # Disabling graceful degradation must not expose stale data either.
    monkeypatch.setattr(settings, "graceful_degradation", False)
    assert gm.get_gateway("v1r").data is None
    assert gm.get_gateway("v1r").data_source is None


@pytest.mark.asyncio
async def test_cloud_reads_leave_the_local_snapshot_alone(monkeypatch):
    """History numbers Powerwalls from the last local snapshot
    (get_last_data) and the multi-Powerwall guard compares against it; cloud
    data has neither the battery blocks nor tedapi_config."""
    gm = _manager(monkeypatch)
    local = _data()
    local.timestamp = time.time() - 10
    gm._last_successful_data["v1r"] = local
    _install_fetch(gm, cloud_data=_data(), calls=[])
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"
    assert gm.get_last_data("v1r") is local
    assert gm._last_cloud_data["v1r"] is gm.cache["v1r"].data

    # Offline after a failed cloud refresh: the newer (cloud) copy is served
    _install_fetch(gm, cloud_data=None, calls=[])
    gm._next_cloud_read_time["v1r"] = 0
    assert await gm._maybe_cloud_read("v1r") is False
    assert gm.get_gateway("v1r").data is gm._last_cloud_data["v1r"]

    # Local recovery drops the cloud copy
    _install_fetch(gm, local_data=_data(source="local"), calls=[])
    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    assert "v1r" not in gm._last_cloud_data


@pytest.mark.asyncio
async def test_failed_local_poll_reports_local_link_down(monkeypatch):
    """With no cloud snapshot to keep, the generic failure status still says
    the local link is down; a Cloud gateway has no local link (None)."""
    gm = _manager(monkeypatch, flag=False)
    _install_fetch(gm, calls=[])
    await gm._poll_gateway("v1r")
    assert gm.cache["v1r"].online is False
    assert gm.cache["v1r"].data_source != "cache"
    assert gm.cache["v1r"].local_online is False
    assert gm.get_gateway("v1r").local_online is False

    gm.gateways["v1r"].cloud_mode = True
    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    assert gm.cache["v1r"].local_online is None


@pytest.mark.asyncio
async def test_failed_cloud_fallback_read_counts_as_cloud_link_failure(monkeypatch):
    """The fallback reads over the shared cloud connection, so a failed read
    counts against its link health like a failed cloud_control() (#87)."""
    gm = _manager(monkeypatch)

    async def fake_fetch(gateway_id, pw, *, cloud_fallback=False):
        raise RuntimeError("cloud down" if pw is gm._cloud_control else "local down")

    gm._fetch_gateway_data = fake_fetch
    await _enter_fallback(gm)

    assert gm._cloud_read_state_for("v1r")["failures"] == 1
    assert gm._cloud_failures == 1

    # A read that returns no aggregates is a failed attempt too
    _install_fetch(gm, cloud_data=PowerwallData(timestamp=time.time()), calls=[])
    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    await gm._maybe_cloud_read("v1r")   # the cloud-read loop's tick
    assert gm._cloud_read_state_for("v1r")["failures"] == 2
    assert gm._cloud_failures == 2


def test_cloud_fallback_allows_local_gateway_with_fleetapi_hybrid(monkeypatch):
    gm = _manager(monkeypatch)
    gm.gateways["v1r"].cloud_mode = False
    gm.gateways["v1r"].fleetapi = True
    assert gm._cloud_read_fallback_allowed("v1r") is True

    gm.gateways["v1r"].host = None
    gm.gateways["v1r"].cloud_mode = True
    assert gm._cloud_read_fallback_allowed("v1r") is False


@pytest.mark.asyncio
async def test_local_gateway_internal_cloud_failover_is_reported_as_cloud(monkeypatch):
    gm = _manager(monkeypatch)
    data = _data()
    data.connection_mode = "Cloud"
    _install_fetch(gm, local_data=data, calls=[])
    gm._next_poll_time["v1r"] = 0

    await gm._poll_gateway("v1r")

    status = gm.cache["v1r"]
    assert status.online is True
    assert status.data_source == "cloud"
    assert status.local_online is False


@pytest.mark.asyncio
@pytest.mark.parametrize("flag, health", [(False, "healthy"), (True, "degraded")])
async def test_internal_cloud_failover_reported_without_the_flag(monkeypatch, flag, health):
    """data_source/local_online follow the live connection either way; /health
    only reports the new degraded state with PW_CLOUD_READ_FALLBACK on, so it
    is unchanged for everyone else."""
    import app.main as main

    gm = _manager(monkeypatch, flag=flag)
    data = _data()
    data.connection_mode = "Cloud"
    _install_fetch(gm, local_data=data, calls=[])
    gm._next_poll_time["v1r"] = 0
    monkeypatch.setattr(main, "gateway_manager", gm)

    await gm._poll_gateway("v1r")
    result = await main.health_check()

    assert gm.cache["v1r"].data_source == "cloud"
    assert gm.cache["v1r"].local_online is False
    assert result["status"] == health


@pytest.mark.asyncio
async def test_any_successful_poll_ends_the_fallback(monkeypatch):
    """Even through the library's own failover to Cloud: polls succeed, so no
    fallback reads run and /health and /stats must not keep reporting one."""
    gm = _manager(monkeypatch)
    gm._cloud_read_state["v1r"] = gm._new_cloud_read_state()
    gm._cloud_read_state["v1r"]["active"] = True
    data = _data()
    data.connection_mode = "Cloud"
    _install_fetch(gm, local_data=data, calls=[])
    gm._next_poll_time["v1r"] = 0

    await gm._poll_gateway("v1r")

    assert "v1r" not in gm._cloud_read_state
    assert gm.cache["v1r"].data_source == "cloud"
    assert gm.cache["v1r"].local_online is False


@pytest.mark.asyncio
async def test_cloud_fallback_clears_preserved_vitals_marker(monkeypatch):
    gm = _manager(monkeypatch)
    gm._vitals_preserved.add("v1r")
    data = await gm._fetch_gateway_data(
        "v1r", _FakeCloudPowerwall(), cloud_fallback=True
    )
    assert data.aggregates
    assert "v1r" not in gm._vitals_preserved


@pytest.mark.asyncio
async def test_cloud_read_deadline_starts_before_fetch(monkeypatch):
    gm = _manager(monkeypatch)
    gm._poll_interval = 1
    gm._consecutive_failures["v1r"] = 3
    gm._next_cloud_read_time["v1r"] = 0

    async def slow_fetch(gateway_id):
        await asyncio.sleep(0.2)
        return _data()

    gm._fetch_cloud_read = slow_fetch
    started = time.time()
    assert await gm._maybe_cloud_read("v1r") is True
    # Old code reset this after completion (started + 0.2 + 1).
    assert gm._next_cloud_read_time["v1r"] <= started + 1.15


def test_successful_cloud_fallback_restores_shared_cloud_health(monkeypatch):
    gm = _manager(monkeypatch)
    gm._cloud_control_configured = True
    gm._cloud_failures = 3
    gm._cloud_last_success = None

    gm._apply_cloud_read("v1r", _data())

    assert gm._cloud_failures == 0
    assert gm._cloud_last_success is not None


@pytest.mark.asyncio
async def test_local_and_cloud_failure_preserves_offline_snapshot(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await _enter_fallback(gm)
    assert gm.cache["v1r"].data_source == "cloud"

    _install_fetch(gm, cloud_data=None, calls=calls)
    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    await gm._maybe_cloud_read("v1r")   # the cloud-read loop's tick

    status = gm.cache["v1r"]
    assert status.online is False
    assert status.data_source == "cache"
    assert status.local_online is False
    assert status.data is None


@pytest.mark.asyncio
async def test_local_retry_between_cloud_refreshes_keeps_offline_snapshot(monkeypatch):
    """A failed local retry before the next cloud read keeps the snapshot the
    failed cloud refresh left (data_source, local_online)."""
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    await _enter_fallback(gm)
    _install_fetch(gm, cloud_data=None, calls=calls)
    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")
    await gm._maybe_cloud_read("v1r")   # the cloud-read loop's tick
    assert gm.cache["v1r"].data_source == "cache"

    gm._next_poll_time["v1r"] = 0   # local retry due, cloud read not due
    await gm._poll_gateway("v1r")
    status = gm.cache["v1r"]
    assert status.online is False
    assert status.data_source == "cache"
    assert status.local_online is False


@pytest.mark.asyncio
async def test_cloud_fallback_does_not_reuse_stale_local_mode(monkeypatch):
    gm = _manager(monkeypatch)
    gm._last_successful_data["v1r"] = PowerwallData(
        soe=50.0, mode="backup", timestamp=time.time()
    )
    data = await gm._fetch_gateway_data(
        "v1r", _FakeCloudPowerwall(), cloud_fallback=True
    )
    assert data.mode is None


@pytest.mark.asyncio
async def test_cloud_fallback_is_per_gateway_on_multi_powerwall_site(monkeypatch):
    gm = _manager(monkeypatch)
    other = Gateway(id="other", name="other", host="10.0.0.3")
    gm.gateways["other"] = other
    gm.cache["other"] = GatewayStatus(gateway=other, online=False)
    gm._consecutive_failures["other"] = 3

    assert gm._cloud_read_fallback_allowed("v1r") is True
    assert gm._cloud_read_fallback_allowed("other") is False
    assert await gm._fetch_cloud_read("other") is None
    assert gm.cache["other"].data_source is None
