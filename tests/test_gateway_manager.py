"""Tests for gateway manager."""
import asyncio
import pytest
from unittest.mock import Mock
from app.core.gateway_manager import gateway_manager


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
    assert status.data.soe == 85.5


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

    mock_cloud = Mock()
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

    # _cloud_control should be set
    assert gm._cloud_control is not None

    # Cleanup
    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_no_cloud_control_for_cloud_mode(monkeypatch):
    """Test that initialize() does NOT create _cloud_control for pure cloud-mode gateways."""
    from app.config import GatewayConfig

    mock_cloud = Mock()

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

    # pure cloud mode — no hybrid _cloud_control needed
    assert gm._cloud_control is None

    await gm.shutdown()


@pytest.mark.asyncio
async def test_initialize_cloud_control_uses_pw_authpath_fallback(monkeypatch):
    """Test that initialize() uses settings.pw_authpath when config.authpath is None."""
    from app.config import GatewayConfig, settings

    captured_kwargs = {}

    def mock_powerwall_factory(**kwargs):
        captured_kwargs.update(kwargs)
        return Mock()

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
        if kwargs.get("cloudmode"):
            raise RuntimeError("cloud auth failed")
        return Mock()

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

    assert gm._cloud_control is None

    await gm.shutdown()
