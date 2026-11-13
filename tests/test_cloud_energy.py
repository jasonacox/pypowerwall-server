"""Tests for the opt-in Tesla Cloud lifetime energy overlay."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core.cloud_energy import (
    apply_cloud_energy,
    fetch_cloud_energy,
    summarize_cloud_energy,
)
from app.core.gateway_manager import GatewayManager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


def _series():
    return [
        {
            "grid_energy_imported": 1000,
            "grid_energy_exported_from_solar": 400,
            "grid_energy_exported_from_battery": 10,
            "solar_energy_exported": 800,
            "battery_energy_imported_from_grid": 100,
            "battery_energy_imported_from_solar": 200,
            "battery_energy_imported_from_generator": 0,
            "battery_energy_exported": 50,
            "total_home_usage": 700,
        },
        {
            "grid_energy_imported": 2000,
            "grid_energy_exported_from_solar": 600,
            "grid_energy_exported_from_battery": 20,
            "solar_energy_exported": 1200,
            "battery_energy_imported_from_grid": 150,
            "battery_energy_imported_from_solar": 250,
            "battery_energy_imported_from_generator": 0,
            "battery_energy_exported": 75,
            "total_home_usage": 900,
        },
    ]


def test_summarize_cloud_energy():
    totals = summarize_cloud_energy({"response": {"time_series": _series()}})

    assert totals == {
        "grid_import": 3000.0,
        "grid_export": 1030.0,
        "solar_generation": 2000.0,
        "battery_charge": 700.0,
        "battery_discharge": 125.0,
        "home_usage": 1600.0,
    }


def test_summarize_falls_back_to_total_export_field():
    totals = summarize_cloud_energy(
        {
            "time_series": [
                {
                    "grid_energy_imported": 1,
                    "total_grid_energy_exported": 2.5,
                    "solar_energy_exported": 3,
                    "total_battery_charge": 4,
                    "total_battery_discharge": 5,
                    "total_home_usage": 6,
                }
            ]
        }
    )

    assert totals["grid_export"] == 2.5
    assert totals["battery_charge"] == 4.0
    assert totals["battery_discharge"] == 5.0


def test_summarize_rejects_all_zero_series():
    with pytest.raises(RuntimeError):
        summarize_cloud_energy(
            {"time_series": [{"grid_energy_imported": 0, "solar_energy_exported": 0}]}
        )


def test_summarize_ignores_non_finite_values():
    totals = summarize_cloud_energy(
        {
            "time_series": [
                {
                    "grid_energy_imported": float("nan"),
                    "solar_energy_exported": 1,
                }
            ]
        }
    )

    assert totals["grid_import"] == 0.0
    assert totals["solar_generation"] == 1.0


def test_summarize_rejects_missing_series():
    with pytest.raises(RuntimeError):
        summarize_cloud_energy({"response": {}})


def test_fetch_cloud_energy_uses_site_api():
    calls = []

    class Client:
        def _site_api(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"response": {"time_series": _series()}}, False

    totals = fetch_cloud_energy(SimpleNamespace(client=Client()))

    assert totals["grid_import"] == 3000.0
    assert calls[0][0][0] == "CALENDAR_HISTORY_DATA"
    assert calls[0][1] == {"kind": "energy", "period": "lifetime"}


def test_fetch_cloud_energy_uses_fleetapi_history():
    calls = []

    class Fleet:
        def get_calendar_history(self, **kwargs):
            calls.append(kwargs)
            return {"time_series": _series()}

    cloud = SimpleNamespace(client=SimpleNamespace(fleet=Fleet()))

    totals = fetch_cloud_energy(cloud)

    assert totals["grid_import"] == 3000.0
    assert calls == [{"kind": "energy", "duration": "lifetime"}]


def test_apply_cloud_energy_overlays_only_present_sections():
    aggregates = {
        "site": {"energy_imported": 1, "energy_exported": 2, "instant_power": 3},
        "solar": {"energy_exported": 4, "instant_power": 5},
        "battery": {"energy_imported": 6, "energy_exported": 7},
        "load": {"energy_imported": 8},
    }
    totals = {
        "grid_import": 100.0,
        "grid_export": 200.0,
        "solar_generation": 300.0,
        "battery_charge": 400.0,
        "battery_discharge": 500.0,
        "home_usage": 600.0,
    }

    apply_cloud_energy(aggregates, totals)

    assert aggregates["site"]["energy_imported"] == 100.0
    assert aggregates["site"]["energy_exported"] == 200.0
    assert aggregates["site"]["instant_power"] == 3
    assert aggregates["solar"]["energy_exported"] == 300.0
    assert aggregates["battery"]["energy_imported"] == 400.0
    assert aggregates["battery"]["energy_exported"] == 500.0
    assert aggregates["load"]["energy_imported"] == 600.0


@pytest.mark.asyncio
async def test_refresh_cloud_energy_caches_totals(monkeypatch):
    gm = GatewayManager()
    gm._executor = ThreadPoolExecutor(max_workers=1)
    totals = {
        "grid_import": 1.0,
        "grid_export": 2.0,
        "solar_generation": 3.0,
        "battery_charge": 4.0,
        "battery_discharge": 5.0,
        "home_usage": 6.0,
    }
    gm_module = importlib.import_module("app.core.gateway_manager")
    monkeypatch.setattr(gm_module, "fetch_cloud_energy", lambda cloud: totals)

    try:
        assert await gm._refresh_cloud_energy("gw", Mock()) is True
    finally:
        gm._executor.shutdown(wait=True)

    assert gm._cloud_energy_totals["gw"] == totals
    assert gm._cloud_energy_updated["gw"] > 0
    assert gm._cloud_energy_failures["gw"] == 0


@pytest.mark.asyncio
async def test_refresh_cloud_energy_records_failure(monkeypatch):
    gm = GatewayManager()
    gm._executor = ThreadPoolExecutor(max_workers=1)

    def fail(cloud):
        raise RuntimeError("cloud unavailable")

    gm_module = importlib.import_module("app.core.gateway_manager")
    monkeypatch.setattr(gm_module, "fetch_cloud_energy", fail)

    try:
        assert await gm._refresh_cloud_energy("gw", Mock()) is False
    finally:
        gm._executor.shutdown(wait=True)

    assert gm._cloud_energy_failures["gw"] == 1
    assert gm._cloud_energy_last_error["gw"] == "cloud unavailable"
    assert "gw" not in gm._cloud_energy_totals


@pytest.mark.asyncio
async def test_poll_gateway_applies_cloud_energy(monkeypatch):
    gm = GatewayManager()
    gateway = Gateway(id="home", name="home", host="10.0.0.2")
    gm.gateways["home"] = gateway
    gm.cache["home"] = Mock()
    gm.connections["home"] = Mock()
    gm._cloud_energy_totals["home"] = {"grid_import": 123.0}
    data = PowerwallData(
        aggregates={"site": {"energy_imported": 1, "instant_power": 500}},
        timestamp=1.0,
    )

    async def fake_fetch(gateway_id, pw, *, cloud_fallback=False):
        return data

    gm._fetch_gateway_data = fake_fetch
    gm._record_timeseries_sample = Mock()
    gm._record_signal_sample = Mock()
    gm._schedule_mqtt_publish = Mock()
    monkeypatch.setattr(settings, "cloud_energy", True)

    await gm._poll_gateway("home")

    assert gm.cache["home"].data.aggregates["site"]["energy_imported"] == 123.0
    assert gm.cache["home"].data.aggregates["site"]["instant_power"] == 500
    assert gm.cache["home"].data.aggregates["cloud_energy"]["active"] is True


@pytest.mark.asyncio
async def test_health_reports_cloud_energy_state(monkeypatch):
    import time

    import app.main as main

    gm = GatewayManager()
    gateway = Gateway(id="home", name="home", host="10.0.0.2")
    gm.gateways["home"] = gateway
    gm.cache["home"] = GatewayStatus(
        gateway=gateway,
        online=True,
        data=PowerwallData(
            aggregates={"site": {"energy_imported": 1}},
            timestamp=time.time(),
        ),
    )
    gm._cloud_energy_totals["home"] = {
        "grid_import": 123.0,
        "grid_export": 45.0,
    }
    gm._cloud_energy_updated["home"] = time.time()
    monkeypatch.setattr(settings, "cloud_energy", True)
    monkeypatch.setattr(main, "gateway_manager", gm)

    result = await main.health_check()

    state = result["cloud_energy"]["home"]
    assert state["enabled"] is True
    assert state["refresh_seconds"] == settings.cloud_energy_refresh
    assert state["active"] is True
    assert "totals_wh" not in state
    assert state["failures"] == 0
    assert state["last_error"] is None


def test_cloud_energy_status_includes_totals_for_stats(monkeypatch):
    gm = GatewayManager()
    gm.gateways["home"] = Gateway(id="home", name="home", host="10.0.0.2")
    gm._cloud_energy_totals["home"] = {"grid_import": 123.0}
    monkeypatch.setattr(settings, "cloud_energy", True)

    state = gm.get_all_cloud_energy_states()["home"]

    assert state["active"] is True
    assert state["totals_wh"]["grid_import"] == 123.0


@pytest.mark.asyncio
async def test_health_reports_cloud_energy_disabled(monkeypatch):
    import app.main as main

    gm = GatewayManager()
    gm.gateways["home"] = Gateway(id="home", name="home", host="10.0.0.2")
    monkeypatch.setattr(settings, "cloud_energy", False)
    monkeypatch.setattr(main, "gateway_manager", gm)

    result = await main.health_check()

    assert result["cloud_energy"] == {}


def test_apply_cloud_energy_respects_flag(monkeypatch):
    gm = GatewayManager()
    gm._cloud_energy_totals["gw"] = {"grid_import": 99.0}
    data = PowerwallData(aggregates={"site": {"energy_imported": 1}})
    monkeypatch.setattr(settings, "cloud_energy", False)

    gm._apply_cloud_energy("gw", data)
    assert data.aggregates["site"]["energy_imported"] == 1
    assert "cloud_energy" not in data.aggregates

    monkeypatch.setattr(settings, "cloud_energy", True)
    gm._apply_cloud_energy("gw", data)
    assert data.aggregates["site"]["energy_imported"] == 99.0
    assert data.aggregates["cloud_energy"]["active"] is True


@pytest.mark.asyncio
async def test_initialize_starts_cloud_energy_for_local_gateway(monkeypatch):
    from unittest.mock import AsyncMock

    from app.config import GatewayConfig

    gm = GatewayManager()
    config = GatewayConfig(
        id="v1r",
        name="v1r",
        host="10.0.0.2",
        gw_pwd="pw",
        email="user@example.com",
        authpath="/auth",
    )
    monkeypatch.setattr(settings, "cloud_energy", True)
    monkeypatch.setattr(settings, "cloud_hybrid", False)
    monkeypatch.setattr(settings, "cloud_read_fallback", False)
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "tedapi_recovery", False)
    monkeypatch.setattr(gm, "_poll_gateway_loop", AsyncMock())
    monkeypatch.setattr(gm, "_init_cloud_control", AsyncMock())
    monkeypatch.setattr(gm, "_cloud_energy_loop", AsyncMock())

    try:
        await gm.initialize([config], poll_interval=4)
        assert gm._cloud_control_configured is True
        assert gm._cloud_control_task is not None
        assert "v1r" in gm._cloud_energy_tasks
        assert gm._cloud_for_energy_only is True
    finally:
        await gm.shutdown()


@pytest.mark.asyncio
async def test_cloud_energy_binds_site_without_mqtt(monkeypatch, tmp_path):
    gm = GatewayManager()
    site = tmp_path / ".pypowerwall.site"
    site.write_text("123")
    gm._cloud_control = SimpleNamespace(
        client=SimpleNamespace(sitefile=str(site)),
        cloudmode=True,
        fleetapi=False,
        mode="cloud",
    )
    monkeypatch.setattr(settings, "cloud_energy", True)
    monkeypatch.setattr(settings, "cloud_read_fallback", False)
    monkeypatch.setattr(settings, "mqtt_controls", 0)
    monkeypatch.setattr(settings, "mqtt_host", None)
    monkeypatch.setattr(settings, "mqtt_username", None)
    monkeypatch.setattr(settings, "mqtt_password", None)
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "siteid", None)

    assert await gm._cloud_site_unambiguous() is True


def test_energy_only_cloud_connection_skips_hybrid_reads():
    gm = GatewayManager()
    gm._cloud_control = Mock()
    gm._cloud_for_energy_only = True

    assert gm._hybrid_reads_enabled() is False


def test_cloud_energy_client_uses_bound_shared_connection():
    gm = GatewayManager()
    shared = Mock()
    gm._cloud_control = shared
    gm._cloud_control_gateway_id = "home"

    assert gm._cloud_energy_client("home") is shared


def test_cloud_energy_client_uses_cloud_gateway_connection():
    gm = GatewayManager()
    own = Mock()
    gm.gateways["remote"] = Gateway(
        id="remote", name="remote", email="test@example.com", cloud_mode=True
    )
    gm.connections["remote"] = own

    assert gm._cloud_energy_client("remote") is own
