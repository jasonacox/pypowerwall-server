"""Tests for aggregates API endpoint."""
import pytest
from app.models.gateway import Gateway, GatewayStatus, PowerwallData
from app.core.gateway_manager import gateway_manager


# ---------------------------------------------------------------------------
# Fixture: two gateways — one powerwall-type, one inverter-type
# ---------------------------------------------------------------------------

@pytest.fixture
def two_gateways(mock_gateway_manager, mock_pypowerwall):
    """Add two gateways: a standard Powerwall and a solar inverter (no batteries)."""
    from app.models.gateway import Gateway, GatewayStatus, PowerwallData

    def _make_status(gw_id, gw_name, gw_type):
        gw = Gateway(
            id=gw_id,
            name=gw_name,
            host=f"192.168.1.{10 + len(mock_gateway_manager.gateways)}",
            gw_pwd="TEST_PASSWORD",
            online=True,
            type=gw_type,
        )
        data = PowerwallData(
            aggregates=mock_pypowerwall.poll.return_value,
            soe=mock_pypowerwall.level.return_value,
            freq=mock_pypowerwall.freq.return_value,
            status=mock_pypowerwall.status.return_value,
            version=mock_pypowerwall.version.return_value,
            vitals=mock_pypowerwall.vitals.return_value,
            strings=mock_pypowerwall.strings.return_value,
            system_status=mock_pypowerwall.system_status.return_value,
            grid_status=mock_pypowerwall.grid_status.return_value,
            reserve=mock_pypowerwall.get_reserve.return_value,
            time_remaining=mock_pypowerwall.get_time_remaining.return_value,
            timestamp=1234567890.0,
        )
        status = GatewayStatus(gateway=gw, data=data, online=True, last_updated=1234567890.0)
        mock_gateway_manager.gateways[gw_id] = gw
        mock_gateway_manager.connections[gw_id] = mock_pypowerwall
        mock_gateway_manager.cache[gw_id] = status
        return status

    home = _make_status("home", "Home Gateway", "powerwall")
    south = _make_status("south", "South Inverter", "inverter")
    return {"home": home, "south": south}


# ---------------------------------------------------------------------------
# Existing aggregate tests
# ---------------------------------------------------------------------------

def test_get_aggregates_success(client, connected_gateway):
    """Test getting aggregates data successfully."""
    response = client.get("/api/aggregate/")
    assert response.status_code == 200
    data = response.json()
    
    # Check for aggregate data structure
    assert "total_battery_percent" in data or "total_site_power" in data


def test_get_aggregates_with_gateway_id(client, connected_gateway):
    """Test getting aggregates for specific gateway."""
    # Note: aggregate endpoint doesn't support gateway_id param, it aggregates all
    response = client.get("/api/aggregate/")
    assert response.status_code == 200
    data = response.json()
    assert "total_site_power" in data or "num_online" in data


def test_get_aggregates_no_gateway(client, mock_gateway_manager):
    """Test getting aggregates when no gateway configured."""
    response = client.get("/api/aggregate/")
    # Should return 200 with zero values when no gateways
    assert response.status_code == 200


def test_get_aggregates_invalid_gateway_id(client, connected_gateway):
    """Test getting aggregates - always returns all gateway data."""
    # Aggregate endpoint doesn't filter by gateway, returns combined data
    response = client.get("/api/aggregate/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# /api/aggregate/strings
# ---------------------------------------------------------------------------

def test_aggregate_strings_single_gateway(client, connected_gateway):
    """GET /api/aggregate/strings returns per-gateway string dict for one gateway."""
    response = client.get("/api/aggregate/strings")
    assert response.status_code == 200
    data = response.json()
    # Must be keyed by gateway id
    assert "test-gateway" in data
    strings = data["test-gateway"]
    # Mock returns {"A": {Connected, Current, Power, Voltage}}
    assert "A" in strings
    assert strings["A"]["Connected"] is True


def test_aggregate_strings_no_gateways(client, mock_gateway_manager):
    """GET /api/aggregate/strings returns empty dict when no gateways."""
    response = client.get("/api/aggregate/strings")
    assert response.status_code == 200
    assert response.json() == {}


def test_aggregate_strings_two_gateways(client, two_gateways):
    """GET /api/aggregate/strings returns data for both gateways."""
    response = client.get("/api/aggregate/strings")
    assert response.status_code == 200
    data = response.json()
    assert "home" in data
    assert "south" in data


# ---------------------------------------------------------------------------
# /api/aggregate/alerts
# ---------------------------------------------------------------------------

def test_aggregate_alerts_single_gateway(client, connected_gateway):
    """GET /api/aggregate/alerts returns per-gateway alerts for one gateway."""
    response = client.get("/api/aggregate/alerts")
    assert response.status_code == 200
    data = response.json()
    assert "test-gateway" in data
    # Mock returns [] for alerts
    assert data["test-gateway"] == []


def test_aggregate_alerts_no_gateways(client, mock_gateway_manager):
    """GET /api/aggregate/alerts returns empty dict when no gateways."""
    response = client.get("/api/aggregate/alerts")
    assert response.status_code == 200
    assert response.json() == {}


def test_aggregate_alerts_two_gateways(client, two_gateways):
    """GET /api/aggregate/alerts returns data keyed by each gateway id."""
    response = client.get("/api/aggregate/alerts")
    assert response.status_code == 200
    data = response.json()
    assert "home" in data
    assert "south" in data
    assert isinstance(data["home"], list)


# ---------------------------------------------------------------------------
# /api/aggregate/vitals
# ---------------------------------------------------------------------------

def test_aggregate_vitals_single_gateway(client, connected_gateway):
    """GET /api/aggregate/vitals returns per-gateway vitals dict."""
    response = client.get("/api/aggregate/vitals")
    assert response.status_code == 200
    data = response.json()
    assert "test-gateway" in data
    vitals = data["test-gateway"]
    # Mock vitals include TEPOD, TEPINV, TESYNC keys
    assert any(k.startswith("TEPOD") or k.startswith("TEPINV") for k in vitals)


def test_aggregate_vitals_no_gateways(client, mock_gateway_manager):
    """GET /api/aggregate/vitals returns empty dict when no gateways."""
    response = client.get("/api/aggregate/vitals")
    assert response.status_code == 200
    assert response.json() == {}


def test_aggregate_vitals_two_gateways(client, two_gateways):
    """GET /api/aggregate/vitals returns data from both gateways."""
    response = client.get("/api/aggregate/vitals")
    assert response.status_code == 200
    data = response.json()
    assert "home" in data
    assert "south" in data


# ---------------------------------------------------------------------------
# Gateway type and port fields
# ---------------------------------------------------------------------------

def test_gateway_type_default_is_powerwall(connected_gateway):
    """Gateway type defaults to 'powerwall' when not specified."""
    assert connected_gateway.gateway.type == "powerwall"


def test_gateway_type_inverter(mock_gateway_manager, mock_pypowerwall):
    """Gateway type 'inverter' is stored and retrievable."""
    gw = Gateway(
        id="inv", name="Inverter", host="10.0.0.1", gw_pwd="pw", type="inverter"
    )
    data = PowerwallData(
        aggregates={}, soe=0.0, freq=60.0, status="Running", version="1.0",
        vitals={}, strings={}, system_status={}, grid_status="UP",
        reserve=0.0, time_remaining=0.0, timestamp=0.0,
    )
    status = GatewayStatus(gateway=gw, data=data, online=True, last_updated=0.0)
    mock_gateway_manager.gateways["inv"] = gw
    mock_gateway_manager.cache["inv"] = status
    assert mock_gateway_manager.get_gateway("inv").gateway.type == "inverter"


def test_gateway_port_field_stored(mock_gateway_manager, mock_pypowerwall):
    """Gateway port field is stored correctly."""
    gw = Gateway(
        id="gw-port", name="Travel Router", host="192.168.1.50",
        gw_pwd="pw", port=8443
    )
    assert gw.port == 8443
    assert gw.type == "powerwall"  # default unchanged


def test_gateway_port_none_by_default(connected_gateway):
    """Gateway port defaults to None."""
    assert connected_gateway.gateway.port is None
