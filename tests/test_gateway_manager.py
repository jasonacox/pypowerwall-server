"""Tests for gateway manager."""
import pytest
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
