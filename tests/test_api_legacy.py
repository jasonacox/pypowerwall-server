"""Tests for legacy proxy API endpoints."""
import pytest
from unittest.mock import Mock
from app.core.scaling import raw_to_tesla_battery_percent
from app.api.legacy import _expansion_parent_indexes


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
    assert data["percentage"] == pytest.approx(
        raw_to_tesla_battery_percent(85.5)
    )
    assert data["raw_percentage"] == 85.5


def test_api_system_status_soe_endpoint(client, connected_gateway):
    """Test /api/system_status/soe returns scaled and raw values."""
    response = client.get("/api/system_status/soe")
    assert response.status_code == 200
    data = response.json()
    assert data["percentage"] == pytest.approx(
        raw_to_tesla_battery_percent(85.5)
    )
    assert data["raw_percentage"] == 85.5


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
    assert data["PW1_name"] == "TEPINV--1234567-00-A--TG1234567890AB"
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


def test_pod_marks_powerwall_expansion_as_attached_to_leader(
    client, connected_gateway
):
    """Expose the TEDAPI expansion-to-leader relationship in the pod data."""
    connected_gateway.data.system_status = {
        "battery_blocks": [
            {"PackageSerialNumber": "LEADER"},
            {"PackageSerialNumber": "FOLLOWER"},
            {"PackageSerialNumber": "EXPANSION", "Type": "BatteryExpansion"},
        ]
    }
    connected_gateway.data.tedapi_config = {
        "battery_blocks": [
            {
                "vin": "1707000-30-L--LEADER",
                "battery_expansions": [{"din": "1807000-20-B--EXPANSION"}],
            },
            {"vin": "1707000-30-L--FOLLOWER"},
        ]
    }

    response = client.get("/pod")

    assert response.status_code == 200
    assert response.json()["PW3_attached_to"] == "PW1"


def test_expansion_parent_indexes_ignore_unknown_expansions():
    """Only expansions present in the current system-status snapshot are mapped."""
    system_status = {
        "battery_blocks": [{"PackageSerialNumber": "LEADER"}]
    }
    tedapi_config = {
        "battery_blocks": [
            {
                "vin": "1707000-30-L--LEADER",
                "battery_expansions": [{"din": "1807000-20-B--MISSING"}],
            }
        ]
    }

    assert _expansion_parent_indexes(system_status, tedapi_config) == {}


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


def test_control_grid_charging_routes_to_cloud(
    control_client, connected_gateway
):
    """Test that POST /control/grid_charging uses cloud_control when _cloud_control is set."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_grid_charging.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/grid_charging",
        json={"value": True},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_grid_charging.assert_called_once_with(True)


def test_control_grid_charging_disable(
    control_client, connected_gateway
):
    """Test that POST /control/grid_charging with False disables grid charging."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_grid_charging.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/grid_charging",
        json={"value": False},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_grid_charging.assert_called_once_with(False)


def test_control_grid_charging_missing_value_returns_400(
    control_client, connected_gateway
):
    """Test that POST /control/grid_charging without 'value' returns 400."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/grid_charging",
        json={},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400


def test_control_grid_charging_non_boolean_value_returns_400(
    control_client, connected_gateway
):
    """Test that POST /control/grid_charging with a non-boolean 'value' returns 400."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/grid_charging",
        json={"value": "yes"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400


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
    """POST /control/reserve without cloud control calls set_reserve() on the local connection."""
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    mock_pypowerwall.set_reserve.return_value = {"result": "Updated"}

    response = control_client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_pypowerwall.set_reserve.assert_called_once_with(20)
    mock_pypowerwall.post.assert_not_called()


def test_control_mode_fallback_without_cloud_uses_local_set_mode(
    control_client, connected_gateway, mock_pypowerwall
):
    """POST /control/mode without cloud control calls set_mode() locally (issue #82)."""
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    mock_pypowerwall.set_mode.return_value = {"result": "Updated"}

    response = control_client.post(
        "/control/mode",
        json={"value": "self_consumption"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_pypowerwall.set_mode.assert_called_once_with("self_consumption")
    # Must NOT raw-POST /api/mode — that is the bug from issue #82
    mock_pypowerwall.post.assert_not_called()


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


# ---------------------------------------------------------------------------
# /control companion parameter tests (ported from pypowerwall PR #308)
# ---------------------------------------------------------------------------

def test_control_reserve_with_valid_mode_calls_set_operation(
    control_client, connected_gateway
):
    """POST /control/reserve with mode= companion should call set_operation."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_operation.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/reserve",
        json={"value": 5, "mode": "self_consumption"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_operation.assert_called_once_with(5, "self_consumption")
    mock_cloud.set_reserve.assert_not_called()
    mock_cloud.set_mode.assert_not_called()


def test_control_mode_with_valid_level_calls_set_operation(
    control_client, connected_gateway
):
    """POST /control/mode with level= companion should call set_operation."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    mock_cloud.set_operation.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/mode",
        json={"value": "backup", "level": 80},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_cloud.set_operation.assert_called_once_with(80, "backup")
    mock_cloud.set_mode.assert_not_called()
    mock_cloud.set_reserve.assert_not_called()


def test_control_reserve_with_invalid_mode_returns_400(
    control_client, connected_gateway
):
    """POST /control/reserve with invalid mode= should return 400, no cloud call."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/reserve",
        json={"value": 5, "mode": "garbage"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400
    mock_cloud.set_reserve.assert_not_called()
    mock_cloud.set_operation.assert_not_called()


def test_control_mode_with_invalid_level_returns_400(
    control_client, connected_gateway
):
    """POST /control/mode with non-integer level= should return 400, no cloud call."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/mode",
        json={"value": "backup", "level": "not-a-number"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400
    mock_cloud.set_mode.assert_not_called()
    mock_cloud.set_operation.assert_not_called()


def test_control_reserve_with_boolean_value_returns_400(
    control_client, connected_gateway
):
    """POST /control/reserve with boolean value should return 400, no cloud call."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/reserve",
        json={"value": True, "mode": "backup"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400
    mock_cloud.set_reserve.assert_not_called()
    mock_cloud.set_operation.assert_not_called()


def test_control_mode_with_boolean_level_returns_400(
    control_client, connected_gateway
):
    """POST /control/mode with boolean level= should return 400, no cloud call."""
    from app.core.gateway_manager import gateway_manager

    mock_cloud = Mock()
    gateway_manager._cloud_control = mock_cloud

    response = control_client.post(
        "/control/mode",
        json={"value": "backup", "level": False},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 400
    mock_cloud.set_mode.assert_not_called()
    mock_cloud.set_operation.assert_not_called()


def test_control_reserve_companion_fallback_without_cloud(
    control_client, connected_gateway, mock_pypowerwall
):
    """POST /control/reserve with mode= calls set_operation() locally when no cloud control."""
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    mock_pypowerwall.set_operation.return_value = {"result": "Updated"}

    response = control_client.post(
        "/control/reserve",
        json={"value": 5, "mode": "self_consumption"},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_pypowerwall.set_operation.assert_called_once_with(5, "self_consumption")
    mock_pypowerwall.post.assert_not_called()


def test_control_mode_companion_fallback_without_cloud(
    control_client, connected_gateway, mock_pypowerwall
):
    """POST /control/mode with level= calls set_operation() locally when no cloud control."""
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    mock_pypowerwall.set_operation.return_value = {"result": "Updated"}

    response = control_client.post(
        "/control/mode",
        json={"value": "backup", "level": 80},
        headers={"Authorization": _CONTROL_TOKEN},
    )

    assert response.status_code == 200
    mock_pypowerwall.set_operation.assert_called_once_with(80, "backup")
    mock_pypowerwall.post.assert_not_called()


# ---------------------------------------------------------------------------
# /api/operation tests (issue #14 — mode caching)
# ---------------------------------------------------------------------------

def test_api_operation_returns_cached_mode(client, connected_gateway):
    """Test /api/operation returns the polled cached mode when data.mode is present."""
    # Set mode directly on the cached data (simulates a completed poll cycle)
    connected_gateway.data.mode = "backup"
    connected_gateway.data.reserve = 30.0

    response = client.get("/api/operation")
    assert response.status_code == 200
    data = response.json()
    assert data["real_mode"] == "backup"
    assert data["backup_reserve_percent"] == 30.0


def test_api_operation_prefers_cached_mode_over_system_status(client, connected_gateway):
    """Test /api/operation prefers data.mode over system_status.default_real_mode."""
    # Set both cached mode and system_status fallback — cached mode must win.
    connected_gateway.data.mode = "autonomous"
    connected_gateway.data.system_status = {"default_real_mode": "self_consumption"}

    response = client.get("/api/operation")
    assert response.status_code == 200
    data = response.json()
    assert data["real_mode"] == "autonomous"


def test_api_operation_falls_back_to_system_status_when_mode_not_cached(client, connected_gateway):
    """Test /api/operation falls back to system_status.default_real_mode when mode is None."""
    # Ensure mode hasn't been cached yet (None)
    connected_gateway.data.mode = None
    connected_gateway.data.system_status = {"default_real_mode": "backup"}

    response = client.get("/api/operation")
    assert response.status_code == 200
    data = response.json()
    assert data["real_mode"] == "backup"


def test_api_operation_defaults_when_neither_mode_available(client, connected_gateway):
    """When no mode source exists (plain Basic LAN, hybrid cloud down), /api/operation
    must report null — not a fabricated "self_consumption".

    Regression (nesys, PR #85): the Console displayed "Self-Consumption" for a
    system that was actually in autonomous, because a hard-coded default was
    served whenever mode was unavailable.
    """
    connected_gateway.data.mode = None
    connected_gateway.data.system_status = {}  # No default_real_mode key

    response = client.get("/api/operation")
    assert response.status_code == 200
    data = response.json()
    assert data["real_mode"] is None  # unavailable, not fabricated


# ---------------------------------------------------------------------------
# /pw/* convenience endpoint tests
# ---------------------------------------------------------------------------


def test_pw_level(client, connected_gateway):
    """Test /pw/level returns raw battery percentage (old proxy shape)."""
    response = client.get("/pw/level")
    assert response.status_code == 200
    data = response.json()
    assert data == {"level": 85.5}


def test_pw_power(client, connected_gateway):
    """Test /pw/power returns site, solar, battery, load power."""
    response = client.get("/pw/power")
    assert response.status_code == 200
    data = response.json()
    assert data["site"] == 100
    assert data["solar"] == 5000
    assert data["battery"] == -2000
    assert data["load"] == 3100


def test_pw_site(client, connected_gateway):
    """Test /pw/site returns site power data."""
    response = client.get("/pw/site")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == 100


def test_pw_solar(client, connected_gateway):
    """Test /pw/solar returns solar power data."""
    response = client.get("/pw/solar")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == 5000


def test_pw_battery(client, connected_gateway):
    """Test /pw/battery returns the full battery meter block (old proxy shape)."""
    response = client.get("/pw/battery")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == -2000


def test_pw_battery_blocks(client, connected_gateway):
    """Test /pw/battery_blocks returns dict keyed by serial (old proxy shape)."""
    response = client.get("/pw/battery_blocks")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "TG1234567890AB" in data
    # Serial is the key, not a field in the block body
    assert "PackageSerialNumber" not in data["TG1234567890AB"]
    assert data["TG1234567890AB"]["PackagePartNumber"] == "1234567-00-A"


def test_pw_load(client, connected_gateway):
    """Test /pw/load returns load power data."""
    response = client.get("/pw/load")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == 3100


def test_pw_grid(client, connected_gateway):
    """Test /pw/grid returns grid (site) power data."""
    response = client.get("/pw/grid")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == 100


def test_pw_home(client, connected_gateway):
    """Test /pw/home returns home consumption data (same as load)."""
    response = client.get("/pw/home")
    assert response.status_code == 200
    data = response.json()
    assert data["instant_power"] == 3100


def test_pw_vitals(client, connected_gateway):
    """Test /pw/vitals returns device vitals."""
    response = client.get("/pw/vitals")
    assert response.status_code == 200
    data = response.json()
    assert "TEPOD--1234" in data
    assert "TEPINV--1234567-00-A--TG1234567890AB" in data


def test_pw_temps(client, connected_gateway):
    """Test /pw/temps returns temperature metrics."""
    response = client.get("/pw/temps")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_pw_strings(client, connected_gateway):
    """Test /pw/strings returns solar string data."""
    response = client.get("/pw/strings")
    assert response.status_code == 200
    data = response.json()
    assert "A" in data


def test_pw_din(client, connected_gateway):
    """Test /pw/din returns device identifier."""
    response = client.get("/pw/din")
    assert response.status_code == 200
    data = response.json()
    assert data["din"] == "1232100-00-E--T14111AB1234567"


def test_pw_uptime(client, connected_gateway):
    """Test /pw/uptime returns uptime string."""
    response = client.get("/pw/uptime")
    assert response.status_code == 200
    data = response.json()
    assert data["uptime"] == "5d 3h 42m"


def test_pw_version(client, connected_gateway):
    """Test /pw/version returns firmware version."""
    response = client.get("/pw/version")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "23.44.0"
    assert data["vint"] == 2344


def test_pw_status(client, connected_gateway):
    """Test /pw/status returns the full /api/status payload (old proxy shape)."""
    response = client.get("/pw/status")
    assert response.status_code == 200
    data = response.json()
    assert "din" in data
    assert "version" in data
    assert "start_time" in data
    assert "can_reboot" in data


def test_pw_system_status(client, connected_gateway):
    """Test /pw/system_status returns system status."""
    response = client.get("/pw/system_status")
    assert response.status_code == 200
    data = response.json()
    assert "battery_blocks" in data
    assert "nominal_full_pack_energy" in data


def test_pw_grid_status(client, connected_gateway):
    """Test /pw/grid_status returns the raw API payload (old proxy shape)."""
    response = client.get("/pw/grid_status")
    assert response.status_code == 200
    data = response.json()
    assert "grid_status" in data
    assert "grid_services_active" in data
    # UP maps to the API-level SystemGridConnected value
    assert data["grid_status"] == "SystemGridConnected"


def test_pw_aggregates(client, connected_gateway):
    """Test /pw/aggregates returns aggregated meter data."""
    response = client.get("/pw/aggregates")
    assert response.status_code == 200
    data = response.json()
    assert "site" in data
    assert "solar" in data
    assert "battery" in data
    assert "load" in data


def test_pw_site_name(client, connected_gateway):
    """Test /pw/site_name returns site name."""
    response = client.get("/pw/site_name")
    assert response.status_code == 200
    data = response.json()
    assert "site_name" in data


def test_pw_alerts(client, connected_gateway):
    """Test /pw/alerts returns alerts wrapped in a dict (old proxy shape)."""
    response = client.get("/pw/alerts")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert isinstance(data["alerts"], list)


def test_pw_is_connected(client, connected_gateway):
    """Test /pw/is_connected returns connection boolean."""
    response = client.get("/pw/is_connected")
    assert response.status_code == 200
    data = response.json()
    assert data["is_connected"] is True


def test_pw_get_reserve(client, connected_gateway):
    """Test /pw/get_reserve returns reserve setting."""
    response = client.get("/pw/get_reserve")
    assert response.status_code == 200
    data = response.json()
    assert data["reserve"] == 20


def test_pw_get_mode(client, connected_gateway):
    """Test /pw/get_mode returns operating mode."""
    # Set mode on cached data (simulates a completed poll cycle)
    connected_gateway.data.mode = "self_consumption"
    response = client.get("/pw/get_mode")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "self_consumption"


def test_pw_get_time_remaining(client, connected_gateway):
    """Test /pw/get_time_remaining returns time_remaining (old proxy shape)."""
    response = client.get("/pw/get_time_remaining")
    assert response.status_code == 200
    data = response.json()
    assert data["time_remaining"] == 8.5


def test_pw_endpoints_no_gateway(client, mock_gateway_manager):
    """Test /pw/* endpoints return 503 when no gateway available."""
    for path in ["/pw/level", "/pw/power", "/pw/vitals", "/pw/version"]:
        response = client.get(path)
        assert response.status_code == 503


def test_api_operation_all_mode_values(client, connected_gateway):
    """Test /api/operation correctly returns each valid mode string."""
    for mode in ("self_consumption", "backup", "autonomous"):
        connected_gateway.data.mode = mode
        response = client.get("/api/operation")
        assert response.status_code == 200
        assert response.json()["real_mode"] == mode
