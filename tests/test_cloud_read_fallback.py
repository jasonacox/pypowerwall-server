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

    gm._next_poll_time["v1r"] = 0
    await gm._poll_gateway("v1r")
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

    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")

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

    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")

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

    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")

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
async def test_cloud_fallback_reads_on_its_own_cadence_during_local_backoff(monkeypatch):
    gm = _manager(monkeypatch)
    calls = []
    _install_fetch(gm, cloud_data=_data(), calls=calls)
    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")
    assert calls[-1] == (True, True)

    local_calls = sum(not is_cloud for is_cloud, _ in calls)
    gm._next_poll_time["v1r"] = time.time() + 100
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")

    assert calls[-1] == (True, True)
    assert sum(not is_cloud for is_cloud, _ in calls) == local_calls
    assert gm._next_poll_time["v1r"] > time.time() + 90


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
    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")
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
    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")
    assert gm.cache["v1r"].data_source == "cloud"

    _install_fetch(gm, cloud_data=None, calls=calls)
    gm._next_poll_time["v1r"] = time.time() + 100
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")

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

    # Once the TTL expires, the same offline status must not retain data.
    gm._last_successful_data["v1r"].timestamp = time.time() - settings.cache_ttl - 1
    expired = gm.get_gateway("v1r")
    assert expired.online is False
    assert expired.data is None

    # Disabling graceful degradation must not expose stale data either.
    monkeypatch.setattr(settings, "graceful_degradation", False)
    assert gm.get_gateway("v1r").data is None


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
async def test_internal_cloud_failover_does_not_clear_fallback_state(monkeypatch):
    gm = _manager(monkeypatch)
    gm._cloud_read_state["v1r"] = gm._new_cloud_read_state()
    gm._cloud_read_state["v1r"]["active"] = True
    data = _data()
    data.connection_mode = "Cloud"
    _install_fetch(gm, local_data=data, calls=[])
    gm._next_poll_time["v1r"] = 0

    await gm._poll_gateway("v1r")

    assert "v1r" in gm._cloud_read_state
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
    for _ in range(3):
        gm._next_poll_time["v1r"] = 0
        await gm._poll_gateway("v1r")
    assert gm.cache["v1r"].data_source == "cloud"

    _install_fetch(gm, cloud_data=None, calls=calls)
    gm._next_poll_time["v1r"] = 0
    gm._next_cloud_read_time["v1r"] = 0
    await gm._poll_gateway("v1r")

    status = gm.cache["v1r"]
    assert status.online is False
    assert status.data_source == "cache"
    assert status.local_online is False
    assert status.data is None


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
