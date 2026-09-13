"""Pytest configuration and fixtures."""
import asyncio
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import main as main_module
from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.core.scaling import raw_to_tesla_battery_percent
from app.core.timeseries import reset_timeseries_store


def _reset_singleton_state():
    """Clear ALL mutable state on the gateway_manager singleton.

    Task references from a previous test belong to that test's (closed)
    event loop, and backoff/poll bookkeeping bleeds between tests unless
    cleared here.
    """
    gateway_manager.gateways.clear()
    gateway_manager.connections.clear()
    gateway_manager.cache.clear()
    gateway_manager._cloud_control = None
    gateway_manager._cloud_control_task = None
    # Hybrid cloud-link health bookkeeping (issue #87)
    gateway_manager._cloud_control_configured = False
    gateway_manager._cloud_failures = 0
    gateway_manager._cloud_last_success = None
    gateway_manager._cloud_mode = None
    gateway_manager._cloud_mode_time = None
    gateway_manager._cloud_reserve = None
    gateway_manager._cloud_reserve_time = None
    gateway_manager._executor = None
    gateway_manager._write_lock = asyncio.Lock()
    gateway_manager._islanding_futures.clear()
    gateway_manager._islanding_last_dispatch.clear()
    gateway_manager._poll_tasks.clear()
    gateway_manager._mqtt_tasks.clear()
    gateway_manager._consecutive_failures.clear()
    gateway_manager._next_poll_time.clear()
    gateway_manager._pending_configs.clear()
    gateway_manager._last_successful_data.clear()
    gateway_manager._preserve_stale_count.clear()
    # Cancel any running probe tasks before clearing references
    for task in gateway_manager._probe_tasks.values():
        if hasattr(task, "cancel"):
            task.cancel()
    gateway_manager._fallback_state.clear()
    gateway_manager._tedapi_probe_failures.clear()
    gateway_manager._probe_tasks.clear()


@pytest.fixture(autouse=True)
def reset_gateway_manager():
    """Reset gateway manager before each test."""
    _reset_singleton_state()
    yield
    _reset_singleton_state()


@pytest.fixture(autouse=True)
def isolate_timeseries_store(tmp_path, monkeypatch):
    """Redirect the TimeSeriesStore to a per-test temp directory.

    Tests that exercise the poll loop record real samples; without this
    they would create data/timeseries.db in the repo checkout. Dedicated
    time-series tests construct TimeSeriesStore directly.
    """
    monkeypatch.setattr(
        settings, "timeseries_path", str(tmp_path / "timeseries.db")
    )
    reset_timeseries_store()
    yield
    reset_timeseries_store()


@pytest.fixture(autouse=True)
def reset_rate_limit_buckets():
    """Clear rate limiter bucket state before and after each test."""
    main_module._rate_limit_buckets.clear()
    yield
    main_module._rate_limit_buckets.clear()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def mock_pypowerwall():
    """Mock pypowerwall.Powerwall instance."""
    mock = Mock()
    mock.poll.return_value = {
        "site": {"instant_power": 100, "instant_reactive_power": 0},
        "solar": {"instant_power": 5000, "instant_reactive_power": 0},
        "battery": {"instant_power": -2000, "instant_reactive_power": 0},
        "load": {"instant_power": 3100, "instant_reactive_power": 0}
    }
    mock.level.return_value = 85.5
    mock.freq.return_value = 60.0
    mock.status.return_value = "Running"
    mock.version.return_value = "23.44.0"
    mock.vitals.return_value = {
        "TEPOD--1234": {
            "serialNumber": "TG1234567890AB",
            "POD_ActiveHeating": False,
            "POD_ChargeComplete": False,
            "POD_ChargeRequest": True,
            "POD_DischargeComplete": False,
            "POD_PermanentlyFaulted": False,
            "POD_PersistentlyFaulted": False,
            "POD_enable_line": True,
            "POD_available_charge_power": 5000,
            "POD_available_dischg_power": 5000,
            "POD_nom_energy_remaining": 12000,
            "POD_nom_energy_to_be_charged": 1500,
            "POD_nom_full_pack_energy": 13500
        },
        "TEPINV--1234567-00-A--TG1234567890AB": {
            "PINV_Fout": 60.0,
            "PINV_VSplit1": 120.0,
            "PINV_VSplit2": 120.0
        },
        "TESYNC--1234": {
            "ISLAND_FreqL1_Load": 60.0,
            "ISLAND_VL1N_Main": 120.0,
            "METER_X_CTA_I": 10.5
        }
    }
    mock.strings.return_value = {
        "A": {"Connected": True, "Current": 5.2, "Power": 2500, "Voltage": 480}
    }
    mock.temps.return_value = {
        "TEPOD--1234": 25.5
    }
    mock.alerts.return_value = []
    mock.din.return_value = "1232100-00-E--T14111AB1234567"
    mock.uptime.return_value = "5d 3h 42m"
    mock.site_name.return_value = "Test Site"
    mock.get_reserve.return_value = 20
    mock.get_time_remaining.return_value = 8.5
    mock.get_mode.return_value = "self_consumption"
    mock.grid_status.return_value = "UP"
    mock.system_status.return_value = {
        "nominal_full_pack_energy": 13500,
        "nominal_energy_remaining": 11547,
        "battery_blocks": [
            {
                "Type": "",
                "PackagePartNumber": "1234567-00-A",
                "PackageSerialNumber": "TG1234567890AB",
                "nominal_energy_remaining": 11547,
                "nominal_full_pack_energy": 13500,
                "pinv_state": "PINV_Active",
                "pinv_grid_state": "Grid_Compliant",
                "p_out": -2000,
                "q_out": 0,
                "v_out": 240,
                "f_out": 60.0,
                "i_out": 8.3,
                "energy_charged": 50000,
                "energy_discharged": 45000,
                "off_grid": False,
                "vf_mode": False,
                "wobble_detected": False,
                "charge_power_clamped": False,
                "backup_ready": True,
                "OpSeqState": "Active",
                "version": "23.44.0"
            }
        ]
    }
    
    # TEDAPI mock
    mock.tedapi = Mock()
    mock.tedapi.get_config.return_value = {"vin": "12345", "din": "1234567-00-A"}
    mock.tedapi.get_status.return_value = {"state": "ready"}
    mock.tedapi.get_components.return_value = {"components": []}
    mock.tedapi.get_battery_blocks.return_value = {"blocks": []}
    mock.tedapi.get_device_controller.return_value = {"controller": "active"}
    
    return mock


@pytest.fixture
def mock_gateway_manager(monkeypatch, mock_pypowerwall):
    """Mock gateway manager with a test gateway."""
    # Clear existing gateways
    gateway_manager.gateways.clear()
    gateway_manager.connections.clear()
    gateway_manager.cache.clear()
    gateway_manager._last_successful_data.clear()
    gateway_manager._preserve_stale_count.clear()
    gateway_manager._fallback_state.clear()
    gateway_manager._tedapi_probe_failures.clear()
    
    # Mock the Powerwall constructor
    def mock_powerwall_init(*args, **kwargs):
        return mock_pypowerwall
    
    import pypowerwall
    monkeypatch.setattr(pypowerwall, "Powerwall", mock_powerwall_init)
    
    return gateway_manager


@pytest.fixture
def connected_gateway(mock_gateway_manager, mock_pypowerwall):
    """Add a connected gateway to the manager."""
    from app.models.gateway import Gateway, GatewayStatus, PowerwallData
    
    # Create gateway and add to manager
    gateway = Gateway(
        id="test-gateway",
        name="Test Gateway",
        host="192.168.91.1",
        gw_pwd="TEST_PASSWORD",
        online=True
    )
    
    # Create data
    data = PowerwallData(
        aggregates=mock_pypowerwall.poll.return_value,
        soe_raw=mock_pypowerwall.level.return_value,
        soe=raw_to_tesla_battery_percent(mock_pypowerwall.level.return_value),
        freq=mock_pypowerwall.freq.return_value,
        status=mock_pypowerwall.status.return_value,
        version=mock_pypowerwall.version.return_value,
        din=mock_pypowerwall.din.return_value,
        uptime=mock_pypowerwall.uptime.return_value,
        vitals=mock_pypowerwall.vitals.return_value,
        strings=mock_pypowerwall.strings.return_value,
        system_status=mock_pypowerwall.system_status.return_value,
        grid_status=mock_pypowerwall.grid_status.return_value,
        reserve=mock_pypowerwall.get_reserve.return_value,
        time_remaining=mock_pypowerwall.get_time_remaining.return_value,
        timestamp=1234567890.0
    )
    
    status = GatewayStatus(
        gateway=gateway,
        data=data,
        online=True,
        last_updated=1234567890.0
    )
    
    mock_gateway_manager.gateways["test-gateway"] = gateway
    mock_gateway_manager.connections["test-gateway"] = mock_pypowerwall
    mock_gateway_manager.cache["test-gateway"] = status
    
    return status
