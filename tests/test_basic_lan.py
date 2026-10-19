"""Tests for Powerwall 3 Basic LAN mode (PW_HOST + PW_PASSWORD).

Covers discussion #79: PW3 units expose a limited local API over their wired
LAN interface (vendor subnet). PW_HOST + PW_PASSWORD must be accepted as a
valid local configuration, the connectivity probe must not rely on
/api/status (404 in this mode), and the poller must skip the endpoints this
mode does not serve.
"""
import pytest
from unittest.mock import Mock

from app.core.gateway_manager import gateway_manager
from app.core.scaling import raw_to_tesla_battery_percent
from app.config import GatewayConfig
from app.models.gateway import Gateway, GatewayStatus


def _make_basic_lan_pw_mock() -> Mock:
    """Mock pypowerwall connection shaped like PW3 Basic LAN behavior.

    - /api/status is NOT served (is_connected() would be a false negative)
    - /api/meters/aggregates, /api/system_status/soe and
      /api/system_status/grid_status ARE served
    - no TEDAPI client attached
    """
    mock = Mock()
    mock.poll.side_effect = lambda api, **kw: {
        "/api/meters/aggregates": {
            "site": {"instant_power": -6648, "instant_reactive_power": 0},
            "solar": {"instant_power": 1497, "instant_reactive_power": 0},
            "battery": {"instant_power": -881, "instant_reactive_power": 0},
            "load": {"instant_power": 621, "instant_reactive_power": 0},
        },
        "/api/system_status/grid_status": {"grid_status": "SystemGridConnected"},
    }.get(api)
    mock.level.return_value = 90.27777777777779
    mock.grid_status.return_value = "UP"
    mock.is_connected.return_value = False  # /api/status is 404 in Basic LAN
    mock.tedapi = None
    return mock


@pytest.mark.asyncio
async def test_initialize_accepts_host_plus_password(monkeypatch):
    """host + password (Basic LAN) registers as a valid local gateway."""
    import pypowerwall

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)

    configs = [
        GatewayConfig(id="pw3", name="PW3", host="10.42.1.44", password="12345")
    ]

    await gateway_manager.initialize(configs, poll_interval=5)

    assert "pw3" in gateway_manager.gateways, (
        "host+password config was rejected - Basic LAN must be a valid local mode"
    )
    assert gateway_manager.gateways["pw3"].basic_lan is True

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_initialize_accepts_legacy_pw_password_env_setting(monkeypatch):
    """host + settings.pw_password (PW_PASSWORD env var) is also accepted."""
    from app.config import settings

    import pypowerwall

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)
    monkeypatch.setattr(settings, "pw_password", "12345")

    configs = [
        GatewayConfig(id="pw3-env", name="PW3", host="10.42.1.44"),
    ]

    await gateway_manager.initialize(configs, poll_interval=5)

    assert "pw3-env" in gateway_manager.gateways
    assert gateway_manager.gateways["pw3-env"].basic_lan is True

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_initialize_still_rejects_host_only(monkeypatch):
    """host with no credentials of any kind is still invalid."""
    configs = [GatewayConfig(id="bad", name="Bad", host="10.0.0.5")]

    await gateway_manager.initialize(configs, poll_interval=5)

    assert "bad" not in gateway_manager.gateways

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_basic_lan_probe_falls_back_to_grid_status(
    mock_gateway_manager, mock_pypowerwall
):
    """is_connected() false negative must not reject a reachable Basic LAN gateway.

    PW3 Basic LAN returns 404 for /api/status, so pypowerwall's is_connected()
    reports False even though the mode's endpoints answer. The poller must
    fall back to pw.grid_status() for the connectivity probe.
    """
    mock_pypowerwall.is_connected.return_value = False
    mock_pypowerwall.grid_status.return_value = "UP"

    gw = Gateway(id="pw3-probe", name="PW3", host="10.42.1.44", basic_lan=True)
    config = GatewayConfig(
        id="pw3-probe", name="PW3", host="10.42.1.44", password="12345"
    )

    gateway_manager.gateways["pw3-probe"] = gw
    gateway_manager._pending_configs["pw3-probe"] = config
    gateway_manager.cache["pw3-probe"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["pw3-probe"] = 0
    gateway_manager._next_poll_time["pw3-probe"] = 0

    await gateway_manager._poll_gateway("pw3-probe")

    # Connection established despite is_connected() == False
    assert "pw3-probe" in gateway_manager.connections
    mock_pypowerwall.grid_status.assert_called()
    assert gateway_manager.cache["pw3-probe"].online is True


@pytest.mark.asyncio
async def test_basic_lan_probe_fails_when_grid_status_unavailable(
    mock_gateway_manager, mock_pypowerwall
):
    """A genuinely unreachable Basic LAN gateway must still fail cleanly."""
    mock_pypowerwall.is_connected.return_value = False
    mock_pypowerwall.grid_status.return_value = None

    gw = Gateway(id="pw3-dead", name="PW3", host="10.42.1.44", basic_lan=True)
    config = GatewayConfig(
        id="pw3-dead", name="PW3", host="10.42.1.44", password="12345"
    )

    gateway_manager.gateways["pw3-dead"] = gw
    gateway_manager._pending_configs["pw3-dead"] = config
    gateway_manager.cache["pw3-dead"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["pw3-dead"] = 0
    gateway_manager._next_poll_time["pw3-dead"] = 0

    await gateway_manager._poll_gateway("pw3-dead")

    assert "pw3-dead" not in gateway_manager.connections
    assert "pw3-dead" in gateway_manager._pending_configs  # retained for retry
    assert gateway_manager.cache["pw3-dead"].online is False


@pytest.mark.asyncio
async def test_basic_lan_skips_unavailable_endpoint_fetches(
    mock_gateway_manager, mock_pypowerwall
):
    """Basic LAN gateways only poll the endpoints the mode serves."""
    mock_pypowerwall.tedapi = None

    gw = Gateway(id="pw3-fetch", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-fetch"] = gw
    gateway_manager.connections["pw3-fetch"] = mock_pypowerwall

    data = await gateway_manager._fetch_gateway_data("pw3-fetch", mock_pypowerwall)

    # Core data is collected
    assert data.aggregates
    assert data.soe_raw == 85.5
    assert data.soe == pytest.approx(raw_to_tesla_battery_percent(85.5))
    assert data.grid_status == "UP"

    # Endpoints Basic LAN does not serve are never requested
    mock_pypowerwall.vitals.assert_not_called()
    mock_pypowerwall.strings.assert_not_called()
    mock_pypowerwall.status.assert_not_called()
    mock_pypowerwall.version.assert_not_called()
    mock_pypowerwall.din.assert_not_called()
    mock_pypowerwall.uptime.assert_not_called()
    mock_pypowerwall.alerts.assert_not_called()
    mock_pypowerwall.temps.assert_not_called()
    mock_pypowerwall.site_name.assert_not_called()
    mock_pypowerwall.get_mode.assert_not_called()
    mock_pypowerwall.get_reserve.assert_not_called()
    mock_pypowerwall.system_status.assert_not_called()

    # Direct polls limited to the two endpoints this mode serves
    polled = {c.args[0] for c in mock_pypowerwall.poll.call_args_list}
    assert polled == {"/api/meters/aggregates", "/api/system_status/grid_status"}


@pytest.mark.asyncio
async def test_basic_lan_hybrid_reads_mode_from_cloud(
    mock_gateway_manager, mock_pypowerwall
):
    """Hybrid Basic LAN: mode/reserve refresh from the cloud control connection.

    Regression (nesys, PR #85): the local Basic LAN API has no operation
    mode/reserve endpoint, so the Console showed a stale mode from the last
    cache write instead of the real system state. With a cloud control
    connection present, mode/reserve must be read from the cloud each poll.
    """
    mock_pypowerwall.tedapi = None

    mock_cloud = Mock()
    mock_cloud.get_mode.return_value = "autonomous"
    mock_cloud.get_reserve.return_value = 12.0
    gateway_manager._cloud_control = mock_cloud

    gw = Gateway(id="pw3-hybrid", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-hybrid"] = gw
    gateway_manager.connections["pw3-hybrid"] = mock_pypowerwall

    data = await gateway_manager._fetch_gateway_data(
        "pw3-hybrid", mock_pypowerwall
    )

    # Mode/reserve come from the cloud control connection
    mock_cloud.get_mode.assert_called_once()
    mock_cloud.get_reserve.assert_called_once()
    assert mock_cloud.get_reserve.call_args.kwargs.get("scale") is True
    assert data.mode == "autonomous"
    assert data.reserve == 12.0

    # The local connection is still never asked for mode/reserve
    mock_pypowerwall.get_mode.assert_not_called()
    mock_pypowerwall.get_reserve.assert_not_called()


@pytest.mark.asyncio
async def test_hybrid_poll_reads_do_not_flood_info_logs(
    mock_gateway_manager, mock_pypowerwall, caplog
):
    """Hybrid poll-loop reads (get_mode/get_reserve) must log at DEBUG, not INFO.

    Regression (nesys, PR #85): the cloud refresh added with hybrid mode
    made cloud_control() succeed twice per poll cycle (~5s), and its INFO
    "completed successfully" lines flooded the logs. Reads log at DEBUG;
    user-initiated writes keep the INFO line.
    """
    import logging

    mock_pypowerwall.tedapi = None

    mock_cloud = Mock()
    mock_cloud.get_mode.return_value = "autonomous"
    mock_cloud.get_reserve.return_value = 12.0
    mock_cloud.set_reserve.return_value = True
    gateway_manager._cloud_control = mock_cloud

    gw = Gateway(id="pw3-lognoise", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-lognoise"] = gw
    gateway_manager.connections["pw3-lognoise"] = mock_pypowerwall

    with caplog.at_level(logging.INFO):
        await gateway_manager._fetch_gateway_data("pw3-lognoise", mock_pypowerwall)
        assert "cloud_control(get_mode) completed" not in caplog.text
        assert "cloud_control(get_reserve) completed" not in caplog.text

    # Writes are user-initiated and rare - they stay visible at INFO
    with caplog.at_level(logging.INFO):
        await gateway_manager.cloud_control("set_reserve", 20)
        assert "cloud_control(set_reserve) completed successfully" in caplog.text


@pytest.mark.asyncio
async def test_basic_lan_without_cloud_does_not_show_stale_mode(
    mock_gateway_manager, mock_pypowerwall
):
    """Plain Basic LAN (no cloud): a cached mode must not be re-served stale.

    There is no source of truth for mode in this mode, so the previous
    value should be dropped rather than shown forever.
    """
    mock_pypowerwall.tedapi = None

    gw = Gateway(id="pw3-stale", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-stale"] = gw
    gateway_manager.connections["pw3-stale"] = mock_pypowerwall

    stale = Mock()
    stale.mode = "self_consumption"
    gateway_manager._last_successful_data["pw3-stale"] = stale

    data = await gateway_manager._fetch_gateway_data(
        "pw3-stale", mock_pypowerwall
    )

    assert data.mode is None


@pytest.mark.asyncio
async def test_tedapi_gateway_still_fetches_optional_data(
    mock_gateway_manager, mock_pypowerwall
):
    """Non-Basic-LAN gateways keep fetching optional data (no behavior change)."""
    gw = Gateway(id="tedapi-fetch", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.gateways["tedapi-fetch"] = gw
    gateway_manager.connections["tedapi-fetch"] = mock_pypowerwall

    data = await gateway_manager._fetch_gateway_data("tedapi-fetch", mock_pypowerwall)

    mock_pypowerwall.vitals.assert_called()
    mock_pypowerwall.status.assert_called()
    mock_pypowerwall.get_mode.assert_called()
    assert data.vitals is not None
    assert data.mode == "self_consumption"


@pytest.mark.asyncio
async def test_stats_reports_basic_lan_mode(monkeypatch):
    """/stats must expose basiclan=True (and tedapi=False) for Basic LAN gateways.

    Regression: the Console connect-mode card showed "TEDAPI" for Basic LAN
    because /stats set tedapi=True for any gateway with a host (discussion #79).
    """
    import pypowerwall
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)

    configs = [
        GatewayConfig(id="pw3", name="PW3", host="10.42.1.44", password="12345")
    ]
    await gateway_manager.initialize(configs, poll_interval=5)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["basiclan"] is True
        assert data["tedapi"] is False

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_stats_reports_hybrid_when_cloud_control_active(monkeypatch):
    """When the cloud-control connection is live, /stats must report cloudcontrol=True.

    Regression (nesys, PR #85): a Basic LAN gateway with cloud credentials is
    a *hybrid* setup, but the Console connect-mode card said only "Basic LAN" —
    hiding the cloud side entirely and masking degradation when WAN drops.
    """
    import pypowerwall
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)

    configs = [
        GatewayConfig(id="pw3", name="PW3", host="10.42.1.44", password="12345")
    ]
    await gateway_manager.initialize(configs, poll_interval=5)

    # Simulate the background cloud-control init having completed
    from unittest.mock import Mock

    gateway_manager._cloud_control = Mock()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["basiclan"] is True
        assert data["cloudcontrol"] is True  # Console renders "Hybrid"

    await gateway_manager.shutdown()


# ---------------------------------------------------------------------------
# Issue #87 — per-link hybrid health (Local/Cloud) and stale-marked mode/reserve
# ---------------------------------------------------------------------------


def test_cloud_link_status_none_when_hybrid_not_configured():
    """Non-hybrid setups: cloud_link_status() is None, /stats shape unchanged."""
    gateway_manager._cloud_control_configured = False
    assert gateway_manager.cloud_link_status() is None


@pytest.mark.asyncio
async def test_hybrid_poll_records_last_known_cloud_values(
    mock_gateway_manager, mock_pypowerwall
):
    """A successful hybrid poll records the last known cloud mode/reserve (#87).

    These power the stale-marked /api/operation fallback when the cloud link
    later drops: consumers must see the last known value *marked stale*, not a
    silently frozen reading.
    """
    import time

    mock_pypowerwall.tedapi = None

    mock_cloud = Mock()
    mock_cloud.get_mode.return_value = "autonomous"
    mock_cloud.get_reserve.return_value = 12.0
    gateway_manager._cloud_control = mock_cloud
    gateway_manager._cloud_control_configured = True

    gw = Gateway(id="pw3-lastknown", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-lastknown"] = gw
    gateway_manager.connections["pw3-lastknown"] = mock_pypowerwall

    before = time.time()
    await gateway_manager._fetch_gateway_data("pw3-lastknown", mock_pypowerwall)

    assert gateway_manager._cloud_mode == "autonomous"
    assert gateway_manager._cloud_reserve == 12.0
    assert gateway_manager._cloud_mode_time >= before
    assert gateway_manager._cloud_reserve_time >= before
    assert gateway_manager._cloud_failures == 0

    link = gateway_manager.cloud_link_status()
    assert link["state"] == "healthy"
    assert link["healthy"] is True
    assert link["last_known_mode"] == "autonomous"
    assert link["last_known_reserve"] == 12.0


@pytest.mark.asyncio
async def test_hybrid_poll_records_last_known_grid_values(
    mock_gateway_manager, mock_pypowerwall
):
    """A successful hybrid poll records last known grid charging/export.

    Same stale-marked /api/operation contract as mode/reserve: the reads
    must not disturb the cloud-link health counters, and only real
    library types (bool / non-empty str) are cached.
    """
    import time

    mock_pypowerwall.tedapi = None

    mock_cloud = Mock()
    mock_cloud.get_mode.return_value = "autonomous"
    mock_cloud.get_reserve.return_value = 12.0
    mock_cloud.get_grid_charging.return_value = True
    mock_cloud.get_grid_export.return_value = "pv_only"
    gateway_manager._cloud_control = mock_cloud
    gateway_manager._cloud_control_configured = True

    gw = Gateway(id="pw3-lastknown-grid", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-lastknown-grid"] = gw
    gateway_manager.connections["pw3-lastknown-grid"] = mock_pypowerwall

    before = time.time()
    data = await gateway_manager._fetch_gateway_data(
        "pw3-lastknown-grid", mock_pypowerwall
    )

    assert data.grid_charging is True
    assert data.grid_export == "pv_only"
    assert gateway_manager._cloud_grid_charging is True
    assert gateway_manager._cloud_grid_export == "pv_only"
    assert gateway_manager._cloud_grid_charging_time >= before
    assert gateway_manager._cloud_grid_export_time >= before
    assert gateway_manager._cloud_failures == 0

    link = gateway_manager.cloud_link_status()
    assert link["last_known_grid_charging"] is True
    assert link["last_known_grid_export"] == "pv_only"


@pytest.mark.asyncio
async def test_tedapi_local_none_grid_falls_back_to_cloud_no_prefill(
    mock_gateway_manager, mock_pypowerwall
):
    """TEDAPI gateway (basic_lan=False): local grid getters return None.

    The cloud fallback supplies both values (data + last-known caches with
    timestamps). A later cloud outage must leave data.grid_* unset — no
    pre-fill from last_successful_data — so /api/operation serves the
    timestamped _cloud_grid_* fallback stale-marked instead of presenting
    an old cloud value as fresh.
    """
    import time

    mock_pypowerwall.tedapi = None
    mock_pypowerwall.get_mode.return_value = "self_consumption"
    mock_pypowerwall.get_grid_charging.return_value = None
    mock_pypowerwall.get_grid_export.return_value = None

    mock_cloud = Mock()
    mock_cloud.get_grid_charging.return_value = False
    mock_cloud.get_grid_export.return_value = "battery_ok"
    gateway_manager._cloud_control = mock_cloud
    gateway_manager._cloud_control_configured = True

    gw = Gateway(id="pw3-tedapi-grid", name="PW3", host="10.42.1.46", basic_lan=False)
    gateway_manager.gateways["pw3-tedapi-grid"] = gw
    gateway_manager.connections["pw3-tedapi-grid"] = mock_pypowerwall

    before = time.time()
    data = await gateway_manager._fetch_gateway_data(
        "pw3-tedapi-grid", mock_pypowerwall
    )

    # Cloud fallback supplied both values; False must not read as "missing".
    assert data.grid_charging is False
    assert data.grid_export == "battery_ok"
    assert gateway_manager._cloud_grid_charging is False
    assert gateway_manager._cloud_grid_export == "battery_ok"
    assert gateway_manager._cloud_grid_charging_time >= before
    assert gateway_manager._cloud_grid_export_time >= before

    # Simulate a cloud outage: local still unavailable, cloud now raises.
    gateway_manager._last_successful_data["pw3-tedapi-grid"] = data
    mock_cloud.get_grid_charging.side_effect = Exception("cloud down")
    mock_cloud.get_grid_export.side_effect = Exception("cloud down")

    after = await gateway_manager._fetch_gateway_data(
        "pw3-tedapi-grid", mock_pypowerwall
    )
    assert after.grid_charging is None, "stale cloud value must not be pre-filled as fresh"
    assert after.grid_export is None, "stale cloud value must not be pre-filled as fresh"
    # Last-known caches retained for the stale-marked /api/operation fallback.
    assert gateway_manager._cloud_grid_charging is False
    assert gateway_manager._cloud_grid_export == "battery_ok"


@pytest.mark.asyncio
async def test_cloud_link_health_degrades_then_recovers(
    mock_gateway_manager, mock_pypowerwall
):
    """Cloud link health: degraded on failures, unavailable past threshold (#87).

    A WAN outage with local monitoring healthy is exactly the case the Console
    needs to surface: Local: Healthy / Cloud: Unavailable. The link must also
    recover to healthy when the cloud comes back.
    """
    mock_pypowerwall.tedapi = None

    mock_cloud = Mock()
    mock_cloud.get_mode.side_effect = Exception("WAN blocked")
    mock_cloud.get_reserve.side_effect = Exception("WAN blocked")
    gateway_manager._cloud_control = mock_cloud
    gateway_manager._cloud_control_configured = True

    gw = Gateway(id="pw3-wan", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-wan"] = gw
    gateway_manager.connections["pw3-wan"] = mock_pypowerwall

    # Each poll attempts get_mode + get_reserve: 2 failures per cycle.
    await gateway_manager._fetch_gateway_data("pw3-wan", mock_pypowerwall)
    link = gateway_manager.cloud_link_status()
    assert link["state"] == "degraded"
    assert link["connected"] is True

    await gateway_manager._fetch_gateway_data("pw3-wan", mock_pypowerwall)
    link = gateway_manager.cloud_link_status()
    assert link["state"] == "unavailable"  # failures >= threshold

    # Recovery: WAN returns, values refresh, link flips back to healthy.
    mock_cloud.get_mode.side_effect = None
    mock_cloud.get_mode.return_value = "self_consumption"
    mock_cloud.get_reserve.side_effect = None
    mock_cloud.get_reserve.return_value = 20.0
    data = await gateway_manager._fetch_gateway_data("pw3-wan", mock_pypowerwall)
    link = gateway_manager.cloud_link_status()
    assert link["state"] == "healthy"
    assert gateway_manager._cloud_failures == 0
    assert data.mode == "self_consumption"


@pytest.mark.asyncio
async def test_stats_reports_per_link_hybrid_health(monkeypatch):
    """/stats exposes cloud_control link state + hybrid-aware is_degraded (#87)."""
    import pypowerwall
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)

    configs = [
        GatewayConfig(
            id="pw3",
            name="PW3",
            host="10.42.1.44",
            password="12345",
            email="tesla@example.com",
            authpath="/auth",
        )
    ]
    from app.config import settings as _cfg
    monkeypatch.setattr(_cfg, "control_secret", "test-secret")  # hybrid needs it (or PW_CLOUD_HYBRID)
    await gateway_manager.initialize(configs, poll_interval=5)

    # Simulate the background cloud-control init having completed
    gateway_manager._cloud_control = Mock()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cloud_control"]["configured"] is True
        assert data["cloud_control"]["state"] == "healthy"
        assert data["connection_health"]["local_healthy"] is True
        assert data["connection_health"]["cloud_healthy"] is True
        assert data["connection_health"]["is_degraded"] is False

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_stats_degraded_when_cloud_down_but_local_healthy(monkeypatch):
    """Local healthy + cloud unavailable => overall Degraded, Local row Healthy (#87).

    The old card read Healthy in this state because Connection tracked the
    local link only, hiding the degraded control path entirely.
    """
    import pypowerwall
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    mock_pw = _make_basic_lan_pw_mock()
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_pw)

    configs = [
        GatewayConfig(
            id="pw3",
            name="PW3",
            host="10.42.1.44",
            password="12345",
            email="tesla@example.com",
            authpath="/auth",
        )
    ]
    from app.config import settings as _cfg
    monkeypatch.setattr(_cfg, "control_secret", "test-secret")  # hybrid needs it (or PW_CLOUD_HYBRID)
    await gateway_manager.initialize(configs, poll_interval=5)

    # Cloud link up at some point (values recorded), then WAN dropped.
    gateway_manager._cloud_control = Mock()
    gateway_manager._cloud_failures = 7
    gateway_manager._cloud_mode = "autonomous"
    gateway_manager._cloud_mode_time = 1759100000.0

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cloud_control"]["state"] == "unavailable"
        assert data["cloud_control"]["last_known_mode"] == "autonomous"
        assert data["connection_health"]["local_healthy"] is True
        assert data["connection_health"]["cloud_healthy"] is False
        assert data["connection_health"]["is_degraded"] is True  # hybrid-aware

    await gateway_manager.shutdown()


@pytest.mark.asyncio
async def test_api_operation_serves_stale_cloud_mode_when_link_down(
    mock_gateway_manager, mock_pypowerwall
):
    """/api/operation: last known cloud value, marked stale, when link is down (#87).

    Not a silent freeze: consumers get stale=True plus the fetch time, and the
    Console renders "Self-Consumption (stale)".
    """
    from app.models.gateway import PowerwallData, GatewayStatus
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    gw = Gateway(id="pw3-stale-api", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-stale-api"] = gw
    gateway_manager.connections["pw3-stale-api"] = mock_pypowerwall
    gateway_manager.cache["pw3-stale-api"] = GatewayStatus(
        gateway=gw, data=PowerwallData(), online=True, last_updated=1.0
    )

    # Hybrid configured; cloud was up (mode/reserve seen), now down.
    gateway_manager._cloud_control = Mock()
    gateway_manager._cloud_control_configured = True
    gateway_manager._cloud_failures = 7
    gateway_manager._cloud_mode = "self_consumption"
    gateway_manager._cloud_mode_time = 1759100000.0
    gateway_manager._cloud_reserve = 20.0
    gateway_manager._cloud_reserve_time = 1759100001.0

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/operation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["real_mode"] == "self_consumption"
        assert data["backup_reserve_percent"] == 20.0
        assert data["stale"] is True
        assert data["last_updated"] == 1759100001.0  # later of the two fetches


@pytest.mark.asyncio
async def test_api_operation_null_when_cloud_never_seen(
    mock_gateway_manager, mock_pypowerwall
):
    """Hybrid but cloud never connected: mode/reserve are null, not fabricated (#87)."""
    from app.models.gateway import PowerwallData, GatewayStatus
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    gw = Gateway(id="pw3-nun-api", name="PW3", host="10.42.1.44", basic_lan=True)
    gateway_manager.gateways["pw3-nun-api"] = gw
    gateway_manager.connections["pw3-nun-api"] = mock_pypowerwall
    gateway_manager.cache["pw3-nun-api"] = GatewayStatus(
        gateway=gw, data=PowerwallData(), online=True, last_updated=1.0
    )

    # Hybrid credentials present but init never succeeded (WAN blocked at boot).
    gateway_manager._cloud_control = None
    gateway_manager._cloud_control_configured = True

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/operation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["real_mode"] is None
        assert data["backup_reserve_percent"] is None
        assert data["stale"] is False


@pytest.mark.asyncio
async def test_basic_lan_degraded_cloud_control_skips_stub_grid_getters(
    mock_gateway_manager, mock_pypowerwall
):
    """Basic LAN + degraded cloud-control client must not poll grid getters.

    The hybrid cloud-control connection (auto_select=True) can fall back to
    a local client when FleetAPI and cloud auth both fail. On that client
    get_grid_charging()/get_grid_export() are ERROR-logging stubs (issue
    #114), so the Basic LAN poll path must gate its supplementary reads on
    _grid_controls_supported() instead of calling them.

    The mock uses explicit cloudmode/fleetapi/tedapi_mode attributes — an
    unconstrained Mock would make the dynamically created cloudmode
    attribute truthy and mask exactly this regression.
    """
    mock_pypowerwall.tedapi = None

    cloud = Mock(
        spec=["get_mode", "get_reserve", "get_grid_charging", "get_grid_export",
              "cloudmode", "fleetapi", "tedapi_mode"]
    )
    cloud.cloudmode = False
    cloud.fleetapi = False
    cloud.tedapi_mode = None  # degraded to local: unsupported stubs
    cloud.get_mode.return_value = "autonomous"
    cloud.get_reserve.return_value = 12.0
    gateway_manager._cloud_control = cloud
    gateway_manager._cloud_control_configured = True

    gw = Gateway(id="pw3-degraded-cloud", name="PW3", host="10.42.1.47", basic_lan=True)
    gateway_manager.gateways["pw3-degraded-cloud"] = gw
    gateway_manager.connections["pw3-degraded-cloud"] = mock_pypowerwall

    data = await gateway_manager._fetch_gateway_data(
        "pw3-degraded-cloud", mock_pypowerwall
    )

    cloud.get_grid_charging.assert_not_called()
    cloud.get_grid_export.assert_not_called()
    assert data.grid_charging is None
    assert data.grid_export is None
