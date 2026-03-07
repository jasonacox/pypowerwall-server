"""Tests for legacy proxy API endpoints."""
import pytest
from unittest.mock import Mock


def test_aggregates_endpoint(client, connected_gateway):
    """Test /aggregates endpoint."""
    response = client.get("/aggregates")
    assert response.status_code == 200
    data = response.json()
    assert "site" in data
    assert "solar" in data
    assert "battery" in data
    assert "load" in data


def test_soe_endpoint(client, connected_gateway):
    """Test /soe endpoint."""
    response = client.get("/soe")
    assert response.status_code == 200
    data = response.json()
    assert data["percentage"] == 85.5


def test_csv_endpoint(client, connected_gateway):
    """Test /csv endpoint without headers."""
    response = client.get("/csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    
    lines = response.text.strip().split("\n")
    assert len(lines) == 1  # No header, just data
    
    values = lines[0].split(",")
    assert len(values) == 5  # Grid,Home,Solar,Battery,Level


def test_csv_endpoint_with_headers(client, connected_gateway):
    """Test /csv endpoint with headers."""
    response = client.get("/csv?headers=yes")
    assert response.status_code == 200
    
    lines = response.text.strip().split("\n")
    assert len(lines) == 2  # Header + data
    assert lines[0] == "Grid,Home,Solar,Battery,BatteryLevel"


def test_csv_v2_endpoint(client, connected_gateway):
    """Test /csv/v2 endpoint."""
    response = client.get("/csv/v2")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    
    lines = response.text.strip().split("\n")
    assert len(lines) == 1
    
    values = lines[0].split(",")
    assert len(values) == 7  # Grid,Home,Solar,Battery,Level,GridStatus,Reserve


def test_temps_endpoint(client, connected_gateway):
    """Test /temps endpoint."""
    response = client.get("/temps")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_temps_pw_endpoint(client, connected_gateway):
    """Test /temps/pw endpoint."""
    response = client.get("/temps/pw")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    # Should have PW1_temp, PW2_temp, etc. keys


def test_alerts_endpoint(client, connected_gateway):
    """Test /alerts endpoint."""
    response = client.get("/alerts")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_alerts_pw_endpoint(client, connected_gateway):
    """Test /alerts/pw endpoint."""
    response = client.get("/alerts/pw")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_strings_endpoint(client, connected_gateway):
    """Test /strings endpoint."""
    response = client.get("/strings")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_vitals_endpoint(client, connected_gateway):
    """Test /vitals endpoint."""
    response = client.get("/vitals")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_freq_endpoint(client, connected_gateway):
    """Test /freq endpoint."""
    response = client.get("/freq")
    assert response.status_code == 200
    data = response.json()
    
    # Check that we get a comprehensive dictionary, not just a single freq value
    assert "PW1_name" in data
    assert "PW1_PINV_Fout" in data
    assert "PW1_PackagePartNumber" in data
    assert "PW1_f_out" in data
    assert "grid_status" in data
    
    # Verify values from system_status battery_blocks
    assert data["PW1_f_out"] == 60.0
    assert data["PW1_PackagePartNumber"] == "1234567-00-A"
    assert data["PW1_PackageSerialNumber"] == "TG1234567890AB"
    
    # Verify values from vitals TEPINV
    assert data["PW1_name"] == "TEPINV--1234"
    assert data["PW1_PINV_Fout"] == 60.0
    assert data["PW1_PINV_VSplit1"] == 120.0
    assert data["PW1_PINV_VSplit2"] == 120.0
    
    # Verify ISLAND/METER metrics from TESYNC
    assert "ISLAND_FreqL1_Load" in data
    assert data["ISLAND_FreqL1_Load"] == 60.0
    
    # Verify grid status (numeric: 1 = UP, 0 = DOWN)
    assert data["grid_status"] == 1


def test_pod_endpoint(client, connected_gateway):
    """Test /pod endpoint."""
    response = client.get("/pod")
    assert response.status_code == 200
    data = response.json()
    
    # Check that we get pod data - can be from vitals or system_status
    # POD fields come from vitals, other fields from system_status battery_blocks
    assert len(data) > 0
    # Should have at least some POD fields from vitals
    assert any(key.startswith("PW1_POD_") for key in data.keys())


def test_battery_endpoint(client, connected_gateway):
    """Test /battery endpoint."""
    response = client.get("/battery")
    assert response.status_code == 200
    data = response.json()
    assert "power" in data
    assert isinstance(data["power"], (int, float))


def test_tedapi_config_endpoint(client, connected_gateway):
    """Test /tedapi/config endpoint."""
    response = client.get("/tedapi/config")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_tedapi_status_endpoint(client, connected_gateway):
    """Test /tedapi/status endpoint."""
    response = client.get("/tedapi/status")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_tedapi_battery_endpoint(client, connected_gateway):
    """Test /tedapi/battery endpoint."""
    response = client.get("/tedapi/battery")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_endpoint_without_gateway(client, mock_gateway_manager):
    """Test endpoints return 503 when no gateway available."""
    response = client.get("/aggregates")
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /control/<path> endpoint tests (cloud control routing)
# ---------------------------------------------------------------------------

_CONTROL_TOKEN = "test-secret-token"


@pytest.fixture
def control_client(monkeypatch):
    """Test client with control features enabled via monkeypatched settings."""
    from app.config import settings
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "control_secret", _CONTROL_TOKEN)
    return TestClient(app)


def test_control_reserve_routes_to_cloud(
    control_client, connected_gateway, monkeypatch
):
    """Test that POST /control/reserve uses cloud_control when _cloud_control is set."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_reserve.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_reserve.assert_called_once_with(20)


def test_control_mode_routes_to_cloud(
    control_client, connected_gateway, monkeypatch
):
    """Test that POST /control/mode uses cloud_control when _cloud_control is set."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_mode.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/mode",
        json={"value": "self_consumption"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_mode.assert_called_once_with("self_consumption")


def test_control_cloud_returns_none_gives_503(
    control_client, connected_gateway, monkeypatch
):
    """Test that cloud_control returning None raises HTTP 503."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_reserve.return_value = None
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 503


def test_control_reserve_fallback_without_cloud(
    control_client, connected_gateway, mock_pypowerwall
):
    """Test that POST /control/reserve falls back to call_api when no _cloud_control."""
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    mock_pypowerwall.post.return_value = {"result": "Updated"}

    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_pypowerwall.post.assert_called_once()


def test_control_unmapped_path_uses_call_api(
    control_client, connected_gateway, mock_pypowerwall, monkeypatch
):
    """Test that unmapped paths always fall back to call_api even if _cloud_control is set."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud
    mock_pypowerwall.post.return_value = {"result": "OK"}

    response = control_client.post(
        "/control/some/other/path",
        json={"key": "value"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    # cloud mock should NOT have been called
    mock_cloud.set_reserve.assert_not_called()
    mock_cloud.set_mode.assert_not_called()
    mock_pypowerwall.post.assert_called_once()


def test_control_requires_auth_header(control_client, connected_gateway):
    """Test that /control/* returns 401 without Authorization header."""
    response = control_client.post("/control/reserve", json={"value": 20})
    assert response.status_code == 401


def test_control_rejects_wrong_token(control_client, connected_gateway):
    """Test that /control/* returns 401 with an invalid token."""
    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": "wrong-token"},
    )
    assert response.status_code == 401
