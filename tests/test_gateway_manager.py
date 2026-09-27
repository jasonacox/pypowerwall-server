"""Tests for gateway manager."""
import asyncio
import pytest
from unittest.mock import Mock
from app.core.gateway_manager import GatewayManager, gateway_manager
from app.core.scaling import raw_to_tesla_battery_percent


def _make_realistic_pw_mock() -> Mock:
    """Build a pypowerwall connection mock with real numeric/dict return values.

    A bare Mock() causes real code paths (e.g. the neg_solar correction's
    `aggregates.get("solar", {}).get("instant_power", 0) < 0` comparison, or
    battery percent scaling) to raise a TypeError comparing a Mock to an int.
    That TypeError is treated as a genuine poll failure, driving the poll
    loop into an endless exponential-backoff retry cycle for tests that
    never actually care about polling behavior.
    """
    mock = Mock()
    mock.poll.return_value = {
        "site": {"instant_power": 100, "instant_reactive_power": 0},
        "solar": {"instant_power": 500, "instant_reactive_power": 0},
        "battery": {"instant_power": -200, "instant_reactive_power": 0},
        "load": {"instant_power": 400, "instant_reactive_power": 0},
    }
    mock.level.return_value = 85.5
    mock.freq.return_value = 60.0
    mock.status.return_value = "Running"
    mock.version.return_value = "23.44.0"
    mock.vitals.return_value = {}
    mock.strings.return_value = {}
    mock.temps.return_value = {}
    mock.alerts.return_value = []
    mock.din.return_value = "1232100-00-E--T14111AB1234567"
    mock.uptime.return_value = "1d 0h 0m"
    mock.site_name.return_value = "Home"
    mock.grid_status.return_value = "UP"
    mock.is_connected.return_value = True
    mock.tedapi = None
    return mock


def test_get_gateway(connected_gateway):
    """Test getting a gateway by ID."""
    status = gateway_manager.get_gateway("test-gateway")
    assert status is not None
    assert status.gateway.id == "test-gateway"
    assert status.gateway.name == "Test Gateway"
    assert status.online is True


def test_get_nonexistent_gateway(mock_gateway_manager):
    """Test getting a gateway that doesn't exist."""
    status = mock_gateway_manager.get_gateway("nonexistent")
    assert status is None


def test_get_all_gateways(connected_gateway):
    """Test getting all gateways."""
    gateways = gateway_manager.get_all_gateways()
    assert len(gateways) >= 1
    assert "test-gateway" in gateways
    assert gateways["test-gateway"].online is True


def test_get_connection(connected_gateway):
    """Test getting a pypowerwall connection."""
    pw = gateway_manager.get_connection("test-gateway")
    assert pw is not None
    assert hasattr(pw, "poll")
    assert hasattr(pw, "level")


def test_get_nonexistent_connection(mock_gateway_manager):
    """Test getting a connection that doesn't exist."""
    pw = mock_gateway_manager.get_connection("nonexistent")
    assert pw is None


@pytest.mark.asyncio
async def test_polling_updates_gateway_data(mock_gateway_manager, mock_pypowerwall):
    """Test that polling updates gateway data."""
    from app.models.gateway import Gateway, GatewayStatus
    
    # Set up a gateway
    gateway = Gateway(
        id="poll-test",
        name="Poll Test",
        host="192.168.1.100",
        gw_pwd="password123"
    )
    
    mock_gateway_manager.gateways["poll-test"] = gateway
    mock_gateway_manager.connections["poll-test"] = mock_pypowerwall
    mock_gateway_manager.cache["poll-test"] = GatewayStatus(gateway=gateway, online=False)
    
    # Manually trigger poll
    await mock_gateway_manager._poll_gateway("poll-test")
    
    # Check that data was updated
    status = mock_gateway_manager.get_gateway("poll-test")
    assert status.online is True
    assert status.data.aggregates is not None
    assert status.data.soe_raw == 85.5
    assert status.data.soe == pytest.approx(raw_to_tesla_battery_percent(85.5))


@pytest.mark.asyncio
async def test_polling_logs_site_name_on_connect(caplog, mock_gateway_manager, mock_pypowerwall):
    """Test that the connection-success log includes the Tesla site name."""
    import logging
    from app.models.gateway import Gateway, GatewayStatus

    gateway = Gateway(
        id="site-name-test",
        name="Site Name Test",
        host="192.168.1.101",
        gw_pwd="password123",
    )

    mock_gateway_manager.gateways["site-name-test"] = gateway
    mock_gateway_manager.connections["site-name-test"] = mock_pypowerwall
    mock_gateway_manager.cache["site-name-test"] = GatewayStatus(gateway=gateway, online=False)

    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway("site-name-test")

    assert "Successfully connected to gateway site-name-test" in caplog.text
    assert "Site: Test Site" in caplog.text


@pytest.mark.asyncio
async def test_polling_handles_timeout(mock_gateway_manager, mock_pypowerwall):
    """Test that polling handles timeouts gracefully."""
    from app.models.gateway import Gateway, GatewayStatus
    
    gateway = Gateway(
        id="timeout-test",
        name="Timeout Test",
        host="192.168.1.100",
        gw_pwd="password123"
    )
    
    mock_gateway_manager.gateways["timeout-test"] = gateway
    mock_gateway_manager.connections["timeout-test"] = mock_pypowerwall
    mock_gateway_manager.cache["timeout-test"] = GatewayStatus(gateway=gateway, online=False)
    
    # Mock poll to raise exception
    mock_pypowerwall.poll.side_effect = Exception("Connection timeout")
    
    # Should not raise exception
    await mock_gateway_manager._poll_gateway("timeout-test")
    
    # Gateway should be marked offline
    status = mock_gateway_manager.get_gateway("timeout-test")
    assert status.online is False


@pytest.mark.asyncio
async def test_polling_with_missing_optional_data(mock_gateway_manager, mock_pypowerwall):
    """Test polling when vitals/strings are unavailable."""
    from app.models.gateway import Gateway, GatewayStatus
    
    gateway = Gateway(
        id="partial-test",
        name="Partial Test",
        host="192.168.1.100",
        gw_pwd="password123"
    )
    
    mock_gateway_manager.gateways["partial-test"] = gateway
    mock_gateway_manager.connections["partial-test"] = mock_pypowerwall
    mock_gateway_manager.cache["partial-test"] = GatewayStatus(gateway=gateway, online=False)
    
    # Make vitals and strings raise exceptions
    mock_pypowerwall.vitals.side_effect = Exception("Not available")
    mock_pypowerwall.strings.side_effect = Exception("Not available")
    
    await mock_gateway_manager._poll_gateway("partial-test")
    
    # Should still be online with aggregates data
    status = mock_gateway_manager.get_gateway("partial-test")
    assert status.online is True
    assert status.data.aggregates is not None
    assert status.data.vitals is None
    assert status.data.strings is None


@pytest.mark.asyncio
async def test_polling_preserves_complete_multi_pw_snapshot_on_partial_tedapi_drop(
    mock_gateway_manager, mock_pypowerwall
):
    """Keep the richer multi-PW TEDAPI snapshot when one poll transiently drops a follower."""
    from app.models.gateway import Gateway, GatewayStatus, PowerwallData

    gateway = Gateway(
        id="multi-pw-test",
        name="Multi PW Test",
        host="192.168.1.100",
        gw_pwd="password123",
    )

    previous_vitals = {
        "TEPINV--leader": {"PINV_Fout": 60.0},
        "TEPINV--follower": {"PINV_Fout": 60.1},
    }
    previous_system_status = {
        "battery_blocks": [
            {"PackageSerialNumber": "PW1", "f_out": 60.0},
            {"PackageSerialNumber": "PW2", "f_out": 60.1},
        ]
    }
    previous_tedapi_config = {
        "battery_blocks": [
            {"type": "Powerwall3"},
            {"type": "Powerwall3Follower"},
        ]
    }

    mock_gateway_manager.gateways["multi-pw-test"] = gateway
    mock_gateway_manager.connections["multi-pw-test"] = mock_pypowerwall
    mock_gateway_manager.cache["multi-pw-test"] = GatewayStatus(
        gateway=gateway, online=False
    )
    mock_gateway_manager._last_successful_data["multi-pw-test"] = PowerwallData(
        aggregates=mock_pypowerwall.poll.return_value,
        soe_raw=85.5,
        soe=raw_to_tesla_battery_percent(85.5),
        vitals=previous_vitals,
        system_status=previous_system_status,
        tedapi_config=previous_tedapi_config,
        timestamp=1111.0,
    )

    mock_pypowerwall.vitals.return_value = {
        "TEPINV--leader": {"PINV_Fout": 60.0},
    }
    mock_pypowerwall.system_status.return_value = {
        "battery_blocks": [
            {"PackageSerialNumber": "PW1", "f_out": 60.0},
        ]
    }
    mock_pypowerwall.tedapi.get_config.return_value = previous_tedapi_config

    await mock_gateway_manager._poll_gateway("multi-pw-test")

    status = mock_gateway_manager.get_gateway("multi-pw-test")
    assert status.online is True
    assert len(status.data.tedapi_config["battery_blocks"]) == 2
    assert list(status.data.vitals.keys()) == list(previous_vitals.keys())
    assert (
        len(status.data.system_status["battery_blocks"])
        == len(previous_system_status["battery_blocks"])
        == 2
    )
    assert (
        status.data.system_status["battery_blocks"][1]["PackageSerialNumber"] == "PW2"
    )


def test_preserve_complete_multi_pw_snapshot_ignores_single_pw_system(
    mock_gateway_manager,
):
    """Single-PW systems should not trigger the multi-PW preservation guard."""
    from app.models.gateway import PowerwallData

    gateway_id = "single-pw-test"
    previous = PowerwallData(
        vitals={"TEPINV--leader": {"PINV_Fout": 60.0}},
        system_status={"battery_blocks": [{"PackageSerialNumber": "PW1"}]},
        tedapi_config={"battery_blocks": [{"type": "Powerwall3"}]},
    )
    current = PowerwallData(
        vitals={"TEPINV--leader": {"PINV_Fout": 59.9}},
        system_status={"battery_blocks": [{"PackageSerialNumber": "PW1-new"}]},
        tedapi_config={"battery_blocks": [{"type": "Powerwall3"}]},
    )
    mock_gateway_manager._last_successful_data[gateway_id] = previous

    result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
        gateway_id, current
    )

    assert result.vitals["TEPINV--leader"]["PINV_Fout"] == 59.9
    assert result.system_status["battery_blocks"][0]["PackageSerialNumber"] == "PW1-new"


def test_gateway_rsa_key_configured():
    """Test rsa_key_configured flag and path disclosure prevention."""
    from app.models.gateway import Gateway

    gw = Gateway(
        id="v1r",
        name="TEDAPI v1r",
        host="192.168.91.1",
        rsa_key_path="/keys/tedapi_rsa_private.pem",
        rsa_key_configured=True,
    )
    assert gw.rsa_key_configured is True
    # rsa_key_path must NOT appear in serialized output (path disclosure prevention)
    data = gw.model_dump()
    assert "rsa_key_path" not in data
    assert data["rsa_key_configured"] is True


def test_gateway_rsa_key_not_configured():
    """Test rsa_key_configured defaults to False when no key is set."""
    from app.models.gateway import Gateway

    gw = Gateway(
        id="tedapi",
        name="TEDAPI Gateway",
        host="192.168.91.1",
        gw_pwd="wifi-password",
    )
    assert gw.rsa_key_configured is False
    data = gw.model_dump()
    assert "rsa_key_path" not in data
    assert data["rsa_key_configured"] is False


@pytest.mark.asyncio
async def test_rsa_key_path_passed_to_powerwall_constructor(monkeypatch, mock_pypowerwall):
    """Test that rsa_key_path is passed to pypowerwall.Powerwall() when configured.

    Covers the plumbing in gateway_manager._poll_gateway() lines 292-293:
        if config.rsa_key_path:
            tedapi_kwargs["rsa_key_path"] = config.rsa_key_path
    """
    from unittest.mock import Mock
    from app.config import GatewayConfig
    from app.models.gateway import Gateway, GatewayStatus
    import pypowerwall

    # Replace Powerwall constructor with a spy that returns the standard mock instance
    powerwall_spy = Mock(return_value=mock_pypowerwall)
    monkeypatch.setattr(pypowerwall, "Powerwall", powerwall_spy)

    gw = Gateway(
        id="v1r-connect",
        name="TEDAPI v1r",
        host="192.168.91.1",
        rsa_key_path="/keys/tedapi_rsa_private.pem",
        rsa_key_configured=True,
    )
    config = GatewayConfig(
        id="v1r-connect",
        name="TEDAPI v1r",
        host="192.168.91.1",
        rsa_key_path="/keys/tedapi_rsa_private.pem",
    )

    gateway_manager.gateways["v1r-connect"] = gw
    gateway_manager._pending_configs["v1r-connect"] = config
    gateway_manager.cache["v1r-connect"] = GatewayStatus(gateway=gw, online=False)
    gateway_manager._consecutive_failures["v1r-connect"] = 0
    gateway_manager._next_poll_time["v1r-connect"] = 0

    await gateway_manager._poll_gateway("v1r-connect")

    # Powerwall must have been constructed exactly once with the correct kwargs
    assert powerwall_spy.called, "pypowerwall.Powerwall() was never called"
    call_kwargs = powerwall_spy.call_args.kwargs
    assert call_kwargs.get("rsa_key_path") == "/keys/tedapi_rsa_private.pem"
    assert call_kwargs.get("host") == "192.168.91.1"
# ---------------------------------------------------------------------------
# Multi-PW snapshot guard — config convergence & staleness cap
# ---------------------------------------------------------------------------


def test_preserve_guard_trusts_current_config_over_stale_previous(
    mock_gateway_manager,
):
    """When current TEDAPI config says 1 block but previous says 2, trust current.

    This prevents the max() latch problem: a gateway that legitimately transitions
    from multi-PW to single-PW should converge immediately, not be stuck preserving
    old multi-PW snapshots.
    """
    from app.models.gateway import PowerwallData

    gateway_id = "config-convergence-test"
    previous = PowerwallData(
        vitals={
            "TEPINV--leader": {"PINV_Fout": 60.0},
            "TEPINV--follower": {"PINV_Fout": 60.1},
        },
        system_status={
            "battery_blocks": [
                {"PackageSerialNumber": "PW1"},
                {"PackageSerialNumber": "PW2"},
            ]
        },
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )
    current = PowerwallData(
        vitals={"TEPINV--leader": {"PINV_Fout": 59.9}},
        system_status={"battery_blocks": [{"PackageSerialNumber": "PW1-new"}]},
        tedapi_config={"battery_blocks": [{"type": "Powerwall3"}]},  # now single-PW
    )
    mock_gateway_manager._last_successful_data[gateway_id] = previous

    result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
        gateway_id, current
    )

    # Current config says 1 block — no preservation should occur
    assert result.vitals["TEPINV--leader"]["PINV_Fout"] == 59.9
    assert len(result.system_status["battery_blocks"]) == 1
    assert result.system_status["battery_blocks"][0]["PackageSerialNumber"] == "PW1-new"


def test_preserve_guard_uses_previous_config_when_current_is_missing(
    mock_gateway_manager,
):
    """When current TEDAPI config is empty, fall back to previous count."""
    from app.models.gateway import PowerwallData

    gateway_id = "missing-config-test"
    previous = PowerwallData(
        vitals={
            "TEPINV--leader": {"PINV_Fout": 60.0},
            "TEPINV--follower": {"PINV_Fout": 60.1},
        },
        system_status={
            "battery_blocks": [
                {"PackageSerialNumber": "PW1"},
                {"PackageSerialNumber": "PW2"},
            ]
        },
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )
    current = PowerwallData(
        vitals={"TEPINV--leader": {"PINV_Fout": 59.9}},
        system_status={"battery_blocks": [{"PackageSerialNumber": "PW1-new"}]},
        tedapi_config=None,  # transient read failure
    )
    mock_gateway_manager._last_successful_data[gateway_id] = previous

    result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
        gateway_id, current
    )

    # Should preserve previous multi-PW snapshot (previous config used as fallback)
    assert len(result.vitals) == 2
    assert len(result.system_status["battery_blocks"]) == 2


def test_preserve_guard_staleness_cap_lets_partial_data_through(
    mock_gateway_manager,
):
    """After _PRESERVE_STALENESS_CAP consecutive preserved polls, partial data passes."""
    from app.models.gateway import PowerwallData

    gateway_id = "stale-cap-test"
    cap = mock_gateway_manager._PRESERVE_STALENESS_CAP

    previous = PowerwallData(
        vitals={
            "TEPINV--leader": {"PINV_Fout": 60.0},
            "TEPINV--follower": {"PINV_Fout": 60.1},
        },
        system_status={
            "battery_blocks": [
                {"PackageSerialNumber": "PW1"},
                {"PackageSerialNumber": "PW2"},
            ]
        },
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )

    # Simulate (cap) preserved polls to fill the staleness counter
    for i in range(cap):
        mock_gateway_manager._preserve_stale_count[gateway_id] = i
        mock_gateway_manager._last_successful_data[gateway_id] = previous
        current = PowerwallData(
            vitals={"TEPINV--leader": {"PINV_Fout": float(59 + i)}},
            system_status={"battery_blocks": [{"PackageSerialNumber": "PW1"}]},
            tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
        )
        result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
            gateway_id, current
        )
        # Should still be preserved
        assert len(result.vitals) == 2, f"Failed at iteration {i}"

    # Now the cap-th poll — staleness counter should be at cap-1, this push hits cap
    mock_gateway_manager._last_successful_data[gateway_id] = previous
    current = PowerwallData(
        vitals={"TEPINV--leader": {"PINV_Fout": 55.0}},
        system_status={"battery_blocks": [{"PackageSerialNumber": "PW1"}]},
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )
    result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
        gateway_id, current
    )

    # Cap reached — partial data should pass through
    assert len(result.vitals) == 1
    assert result.vitals["TEPINV--leader"]["PINV_Fout"] == 55.0
    assert len(result.system_status["battery_blocks"]) == 1


def test_preserve_guard_resets_staleness_on_complete_poll(
    mock_gateway_manager,
):
    """A complete poll resets the staleness counter to zero."""
    from app.models.gateway import PowerwallData

    gateway_id = "reset-stale-test"
    mock_gateway_manager._preserve_stale_count[gateway_id] = 2

    previous = PowerwallData(
        vitals={
            "TEPINV--leader": {"PINV_Fout": 60.0},
            "TEPINV--follower": {"PINV_Fout": 60.1},
        },
        system_status={
            "battery_blocks": [
                {"PackageSerialNumber": "PW1"},
                {"PackageSerialNumber": "PW2"},
            ]
        },
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )
    # Current poll has complete data (2 blocks)
    current = PowerwallData(
        vitals={
            "TEPINV--leader": {"PINV_Fout": 59.9},
            "TEPINV--follower": {"PINV_Fout": 60.0},
        },
        system_status={
            "battery_blocks": [
                {"PackageSerialNumber": "PW1-new"},
                {"PackageSerialNumber": "PW2-new"},
            ]
        },
        tedapi_config={"battery_blocks": [{"type": "PW3"}, {"type": "PW3Follower"}]},
    )
    mock_gateway_manager._last_successful_data[gateway_id] = previous

    result = mock_gateway_manager._preserve_complete_multi_pw_snapshot(
        gateway_id, current
    )

    # No preservation needed — current data is complete
    assert result.vitals["TEPINV--leader"]["PINV_Fout"] == 59.9
    assert gateway_id not in mock_gateway_manager._preserve_stale_count


# ---------------------------------------------------------------------------
# cloud_control() method tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cloud_control_success(mock_gateway_manager):
    """Test cloud_control dispatches to the _cloud_control connection."""
    mock_cloud = Mock()
    mock_cloud.set_reserve.return_value = True
    mock_gateway_manager._cloud_control = mock_cloud

    result = await mock_gateway_manager.cloud_control("set_reserve", 20)

    assert result is True
    mock_cloud.set_reserve.assert_called_once_with(20)


@pytest.mark.asyncio
async def test_cloud_control_serializes_concurrent_write_calls(mock_gateway_manager):
    """Test cloud_control serializes concurrent write calls through the shared lock."""
    events = []

    def make_writer(name):
        def writer(*args, **kwargs):
            events.append(f"{name}-start")
            if name == "reserve":
                events.append("reserve-before-sleep")
                import time

                time.sleep(0.05)
            events.append(f"{name}-end")
            return True

        return writer

    mock_cloud = Mock()
    mock_cloud.set_reserve.side_effect = make_writer("reserve")
    mock_cloud.set_mode.side_effect = make_writer("mode")
    mock_gateway_manager._cloud_control = mock_cloud

    await asyncio.gather(
        mock_gateway_manager.cloud_control("set_reserve", 20),
        mock_gateway_manager.cloud_control("set_mode", "self_consumption"),
    )

    assert events == [
        "reserve-start",
        "reserve-before-sleep",
        "reserve-end",
        "mode-start",
        "mode-end",
    ]
    mock_cloud.set_reserve.assert_called_once_with(20)
    mock_cloud.set_mode.assert_called_once_with("self_consumption")


@pytest.mark.asyncio
async def test_cloud_control_no_connection(mock_gateway_manager):
    """Test cloud_control returns None immediately when _cloud_control is not set."""
    mock_gateway_manager._cloud_control = None

    result = await mock_gateway_manager.cloud_control("set_reserve", 20)

    assert result is None


@pytest.mark.asyncio
async def test_cloud_control_method_not_found(mock_gateway_manager):
    """Test cloud_control returns None when the method doesn't exist on the connection."""
    mock_cloud = Mock()
    del mock_cloud.nonexistent_method  # accessing it will raise AttributeError
    mock_gateway_manager._cloud_control = mock_cloud

    result = await mock_gateway_manager.cloud_control("nonexistent_method")

    assert result is None


@pytest.mark.asyncio
async def test_cloud_control_generic_error(mock_gateway_manager):
    """Test cloud_control returns None on unexpected errors."""
    mock_cloud = Mock()
    mock_cloud.set_reserve.side_effect = RuntimeError("connection lost")
    mock_gateway_manager._cloud_control = mock_cloud

    result = await mock_gateway_manager.cloud_control("set_reserve", 20)

    assert result is None


@pytest.mark.asyncio
async def test_cloud_control_timeout(mock_gateway_manager):
    """Test cloud_control returns None on timeout."""
    import asyncio

    mock_cloud = Mock()
    # Simulate timeout by raising asyncio.TimeoutError inside the executor thread
    mock_cloud.set_reserve.side_effect = asyncio.TimeoutError()
    mock_gateway_manager._cloud_control = mock_cloud

    result = await mock_gateway_manager.cloud_control("set_reserve", 20)

    assert result is None


# ---------------------------------------------------------------------------
# initialize() cloud control setup tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_creates_cloud_control(monkeypatch):
    """Test that initialize() creates a _cloud_control connection for TEDAPI+cloud config."""
    from app.config import GatewayConfig

    mock_cloud = _make_realistic_pw_mock()
    call_count = 0

    def mock_powerwall_factory(**kwargs):
        nonlocal call_count
        call_count += 1
        return mock_cloud

    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", mock_powerwall_factory)

    configs = [
        GatewayConfig(
            id="home",
            name="Home Gateway",
            host="192.168.91.1",
            gw_pwd="secret",
            email="user@example.com",
        )
    ]

    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    gm._cloud_control = None

    await gm.initialize(configs, poll_interval=5)

    # Cloud control connects in the background so startup isn't blocked
    assert gm._cloud_control_task is not None
    await gm._cloud_control_task

    # _cloud_control should be set
    assert gm._cloud_control is not None

    # Cleanup
    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_creates_cloud_control_for_basic_lan(monkeypatch):
    """Test that initialize() creates _cloud_control for Basic LAN+cloud config.

    Hybrid Basic LAN: local reads via PW_HOST+PW_PASSWORD, control writes via
    the cloud connection (discussion #79).
    """
    from app.config import GatewayConfig

    mock_cloud = _make_realistic_pw_mock()

    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_cloud)

    configs = [
        GatewayConfig(
            id="home",
            name="Home Gateway",
            host="10.42.1.51",
            password="12345",
            email="user@example.com",
        )
    ]

    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    gm._cloud_control = None

    await gm.initialize(configs, poll_interval=5)

    assert gm._cloud_control_task is not None
    await gm._cloud_control_task
    assert gm._cloud_control is not None

    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_no_cloud_control_for_cloud_mode(monkeypatch):
    """Test that initialize() does NOT create _cloud_control for pure cloud-mode gateways."""
    from app.config import GatewayConfig

    mock_cloud = _make_realistic_pw_mock()

    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", lambda **kw: mock_cloud)

    configs = [
        GatewayConfig(
            id="remote",
            name="Remote Gateway",
            email="user@example.com",
            cloud_mode=True,
        )
    ]

    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    gm._cloud_control = None

    await gm.initialize(configs, poll_interval=5)

    # pure cloud mode — no hybrid _cloud_control needed, no background task
    assert gm._cloud_control_task is None
    assert gm._cloud_control is None

    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_cloud_control_uses_pw_authpath_fallback(monkeypatch):
    """Test that initialize() uses settings.pw_authpath when config.authpath is None."""
    from app.config import GatewayConfig, settings

    captured_kwargs = {}

    def mock_powerwall_factory(**kwargs):
        captured_kwargs.update(kwargs)
        return _make_realistic_pw_mock()

    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", mock_powerwall_factory)
    monkeypatch.setattr(settings, "pw_authpath", "/global/auth/path")

    configs = [
        GatewayConfig(
            id="home",
            name="Home Gateway",
            host="192.168.91.1",
            gw_pwd="secret",
            email="user@example.com",
            # no authpath set on config — should fall back to settings.pw_authpath
        )
    ]

    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    gm._cloud_control = None

    await gm.initialize(configs, poll_interval=5)
    await gm._cloud_control_task

    # The cloud control connection should have been created with the global authpath
    # (captured_kwargs reflects the LAST Powerwall() call, which is the cloud control one
    # since the gateway connection is deferred to first poll via lazy init)
    assert captured_kwargs.get("authpath") == "/global/auth/path"

    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_cloud_control_exception_is_handled(monkeypatch):
    """Test that initialize() logs a warning and continues when cloud control setup fails."""
    from app.config import GatewayConfig

    call_count = 0

    def mock_powerwall_factory(**kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("cloud auth failed")

    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", mock_powerwall_factory)

    configs = [
        GatewayConfig(
            id="home",
            name="Home Gateway",
            host="192.168.91.1",
            gw_pwd="secret",
            email="user@example.com",
        )
    ]

    gm = gateway_manager
    gm.gateways.clear()
    gm.connections.clear()
    gm.cache.clear()
    gm._cloud_control = None

    # Should not raise — exception is swallowed with a warning
    await gm.initialize(configs, poll_interval=5)
    await gm._cloud_control_task

    assert gm._cloud_control is None

    await gm.shutdown()


# --- Firmware change logging (Powerwall-Dashboard #854) ---

def _add_firmware_gateway(mock_gateway_manager, mock_pypowerwall, gw_id="fw-test"):
    """Register a connected gateway whose firmware we can drive via mock version()."""
    from app.models.gateway import Gateway, GatewayStatus

    gateway = Gateway(
        id=gw_id,
        name="Firmware Test",
        host="192.168.1.104",
        gw_pwd="password123",
    )
    mock_gateway_manager.gateways[gw_id] = gateway
    mock_gateway_manager.connections[gw_id] = mock_pypowerwall
    mock_gateway_manager.cache[gw_id] = GatewayStatus(gateway=gateway, online=False)
    return gw_id


def _count(log_text, needle):
    return log_text.count(needle)


@pytest.mark.asyncio
async def test_firmware_logged_once_on_first_poll(caplog, mock_gateway_manager, mock_pypowerwall):
    """First successful poll logs the firmware exactly once."""
    import logging

    gw_id = _add_firmware_gateway(mock_gateway_manager, mock_pypowerwall)
    mock_pypowerwall.version.return_value = "23.44.0"

    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)
        await mock_gateway_manager._poll_gateway(gw_id)

    assert _count(caplog.text, "Gateway fw-test firmware: 23.44.0") == 1


@pytest.mark.asyncio
async def test_firmware_change_is_logged(caplog, mock_gateway_manager, mock_pypowerwall):
    """A version change between polls logs a dated change line."""
    import logging

    gw_id = _add_firmware_gateway(mock_gateway_manager, mock_pypowerwall)
    mock_pypowerwall.version.return_value = "23.44.0"
    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)

    caplog.clear()
    mock_pypowerwall.version.return_value = "24.12.1"
    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)

    assert "Gateway fw-test firmware changed: 23.44.0 -> 24.12.1" in caplog.text


@pytest.mark.asyncio
async def test_transient_version_miss_does_not_relog(caplog, mock_gateway_manager, mock_pypowerwall):
    """A transient version-fetch miss (None) followed by the same version logs nothing new."""
    import logging

    gw_id = _add_firmware_gateway(mock_gateway_manager, mock_pypowerwall)
    mock_pypowerwall.version.return_value = "23.44.0"
    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)

    caplog.clear()
    # Transient miss: version fetch times out -> None
    mock_pypowerwall.version.return_value = None
    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)
    # Version returns to the same value
    mock_pypowerwall.version.return_value = "23.44.0"
    with caplog.at_level(logging.INFO):
        await mock_gateway_manager._poll_gateway(gw_id)

    assert _count(caplog.text, "firmware") == 0


# ---------------------------------------------------------------------------
# Grid charging/export getter availability (issue #114)
# ---------------------------------------------------------------------------


def _add_grid_gateway(
    mock_gateway_manager: GatewayManager,
    mock_pypowerwall: Mock,
    gw_id: str = "grid-test",
) -> str:
    """Register a connected gateway for grid-control polling tests."""
    from app.models.gateway import Gateway, GatewayStatus

    gateway = Gateway(
        id=gw_id,
        name="Grid Test",
        host="192.168.1.105",
        gw_pwd="password123",
    )
    mock_gateway_manager.gateways[gw_id] = gateway
    mock_gateway_manager.connections[gw_id] = mock_pypowerwall
    mock_gateway_manager.cache[gw_id] = GatewayStatus(gateway=gateway, online=False)
    return gw_id


def test_grid_controls_supported_predicate():
    """The availability predicate follows pypowerwall client routing."""
    from app.core.gateway_manager import _grid_controls_supported

    # Plain local clients (hybrid TEDAPI / password-only) are NOT supported
    for mode in ("hybrid", "off"):
        pw = Mock(tedapi_mode=mode, cloudmode=False, fleetapi=False)
        assert _grid_controls_supported(pw) is False

    # TEDAPI-backed and cloud/FleetAPI clients ARE supported
    for mode in ("v1r", "full"):
        pw = Mock(tedapi_mode=mode, cloudmode=False, fleetapi=False)
        assert _grid_controls_supported(pw) is True
    assert (
        _grid_controls_supported(
            Mock(tedapi_mode="off", cloudmode=True, fleetapi=False)
        )
        is True
    )
    assert (
        _grid_controls_supported(
            Mock(tedapi_mode="hybrid", cloudmode=False, fleetapi=True)
        )
        is True
    )

    # Defensive: missing attributes (unexpected client) -> not supported
    assert _grid_controls_supported(object()) is False


@pytest.mark.parametrize(
    "kwargs, local_stub",
    [
        pytest.param({"host": "1.2.3.4", "password": "abcde"}, True, id="local"),
        pytest.param(
            {"host": "1.2.3.4", "password": "abcde", "gw_pwd": "XYZ12345"},
            True,
            id="hybrid",
        ),
        pytest.param({"host": "192.168.91.1", "gw_pwd": "XYZ12345"}, False, id="full"),
        pytest.param(
            {"host": "1.2.3.4", "gw_pwd": "XYZ12345", "rsa_key_path": "/k.pem"},
            False,
            id="v1r",
        ),
        pytest.param(
            {"host": "", "email": "a@b.c", "cloudmode": True}, False, id="cloud"
        ),
        pytest.param(
            {"host": "", "email": "a@b.c", "cloudmode": True, "fleetapi": True},
            False,
            id="fleetapi",
        ),
    ],
)
def test_grid_controls_supported_matches_library_routing(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict, local_stub: bool
) -> None:
    """Contract with the pinned pypowerwall: the predicate is False exactly when
    the library routes the connection to PyPowerwallLocal, whose grid getters
    are ERROR-logging stubs (#114). Builds a real Powerwall with the backend
    classes replaced by spec'd mocks, so connect() runs the library's own
    routing without network access."""
    from unittest.mock import MagicMock

    import pypowerwall
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud
    from pypowerwall.fleetapi.pypowerwall_fleetapi import PyPowerwallFleetAPI
    from pypowerwall.local.pypowerwall_local import PyPowerwallLocal
    from pypowerwall.tedapi.pypowerwall_tedapi import PyPowerwallTEDAPI

    from app.core.gateway_manager import _grid_controls_supported

    def backend(real):
        def ctor(*args, **kw):
            client = MagicMock(spec=real)
            client.tedapi = real is PyPowerwallTEDAPI or "gw_pwd" in kwargs
            client.siteid = 1
            return client

        return ctor

    for real in (
        PyPowerwallLocal,
        PyPowerwallTEDAPI,
        PyPowerwallCloud,
        PyPowerwallFleetAPI,
    ):
        monkeypatch.setattr(pypowerwall, real.__name__, backend(real))
    monkeypatch.setattr(
        pypowerwall.Powerwall, "_validate_init_configuration", lambda self: None
    )

    pw = pypowerwall.Powerwall(**kwargs)

    # The library routes this config where we expect (so a routing change in
    # a future pin fails here), and the predicate agrees with the routing
    assert isinstance(pw.client, PyPowerwallLocal) is local_stub
    assert _grid_controls_supported(pw) is (not local_stub)


@pytest.mark.asyncio
@pytest.mark.parametrize("tedapi_mode", ["hybrid", "off"])
async def test_grid_getters_not_called_on_local_client(
    mock_gateway_manager, mock_pypowerwall, tedapi_mode
):
    """Polling must not invoke the stub local getters that ERROR-log (#114)."""
    mock_pypowerwall.tedapi_mode = tedapi_mode
    gw_id = _add_grid_gateway(mock_gateway_manager, mock_pypowerwall)

    await mock_gateway_manager._poll_gateway(gw_id)
    await mock_gateway_manager._poll_gateway(gw_id)

    mock_pypowerwall.get_grid_charging.assert_not_called()
    mock_pypowerwall.get_grid_export.assert_not_called()
    status = mock_gateway_manager.get_gateway(gw_id)
    assert status.data.grid_charging is None
    assert status.data.grid_export is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tedapi_mode", ["v1r", "full"])
async def test_grid_getters_polled_when_locally_supported(
    mock_gateway_manager, mock_pypowerwall, tedapi_mode
):
    """TEDAPI v1r/full clients implement the getters and are still polled."""
    mock_pypowerwall.tedapi_mode = tedapi_mode
    mock_pypowerwall.get_grid_charging.return_value = True
    mock_pypowerwall.get_grid_export.return_value = "battery_ok"
    gw_id = _add_grid_gateway(mock_gateway_manager, mock_pypowerwall)

    await mock_gateway_manager._poll_gateway(gw_id)

    mock_pypowerwall.get_grid_charging.assert_called_once()
    mock_pypowerwall.get_grid_export.assert_called_once()
    status = mock_gateway_manager.get_gateway(gw_id)
    assert status.data.grid_charging is True
    assert status.data.grid_export == "battery_ok"


@pytest.mark.asyncio
async def test_grid_getters_polled_for_cloud_gateway(
    mock_gateway_manager, mock_pypowerwall
):
    """Cloud-mode gateway connections implement the getters and are polled."""
    mock_pypowerwall.cloudmode = True
    mock_pypowerwall.get_grid_charging.return_value = False
    mock_pypowerwall.get_grid_export.return_value = "pv_only"
    gw_id = _add_grid_gateway(mock_gateway_manager, mock_pypowerwall)

    await mock_gateway_manager._poll_gateway(gw_id)

    mock_pypowerwall.get_grid_charging.assert_called_once()
    mock_pypowerwall.get_grid_export.assert_called_once()
    status = mock_gateway_manager.get_gateway(gw_id)
    assert status.data.grid_charging is False
    assert status.data.grid_export == "pv_only"


@pytest.mark.asyncio
async def test_hybrid_local_skips_stub_and_uses_cloud_fallback(
    mock_gateway_manager, mock_pypowerwall
):
    """Hybrid setups get real values from cloud control without the local stubs."""
    mock_pypowerwall.tedapi_mode = "hybrid"
    cloud = Mock()
    cloud.get_grid_charging.return_value = True
    cloud.get_grid_export.return_value = "battery_ok"
    mock_gateway_manager._cloud_control = cloud
    gw_id = _add_grid_gateway(mock_gateway_manager, mock_pypowerwall)

    try:
        await mock_gateway_manager._poll_gateway(gw_id)

        # Local stubs never invoked (no per-cycle ERROR logs), cloud queried
        mock_pypowerwall.get_grid_charging.assert_not_called()
        mock_pypowerwall.get_grid_export.assert_not_called()
        cloud.get_grid_charging.assert_called_once()
        cloud.get_grid_export.assert_called_once()
        status = mock_gateway_manager.get_gateway(gw_id)
        assert status.data.grid_charging is True
        assert status.data.grid_export == "battery_ok"
    finally:
        mock_gateway_manager._cloud_control = None
