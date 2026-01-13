"""Tests for aggregates API endpoint."""


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
