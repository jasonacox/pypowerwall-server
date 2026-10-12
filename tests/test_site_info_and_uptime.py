"""/api/site_info passes through pypowerwall's response (refreshed every few
minutes), and the Console shows gateway uptime when /api/status reports it."""
import re
import time

import pytest

from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway

SITE_INFO = {
    "max_system_energy_kWh": 27, "max_system_power_kW": 13.44, "site_name": "Test Site",
    "timezone": "America/Los_Angeles", "max_site_meter_power_kW": 1000000000,
    "min_site_meter_power_kW": -1000000000, "nominal_system_energy_kWh": 27,
    "nominal_system_power_kW": 13.44, "panel_max_current": 100,
    "grid_code": {"grid_code": "60Hz_240V_s_SMC_IEEE1547-2018_UL1741SB-CA-Rule21_2023",
                  "grid_voltage_setting": None, "grid_freq_setting": None, "grid_phase_setting": None,
                  "country": "United States", "state": "California", "utility": "Southern California Edison"},
}


def test_api_site_info_passes_through_library_response(client, connected_gateway):
    connected_gateway.data.site_info = SITE_INFO
    assert client.get("/api/site_info").json() == SITE_INFO


def test_api_site_info_falls_back_to_the_old_shape(client, connected_gateway):
    connected_gateway.data.site_info = None
    data = client.get("/api/site_info").json()
    assert set(data) == set(SITE_INFO)
    assert set(data["grid_code"]) == set(SITE_INFO["grid_code"])


def _route_poll(mock_pypowerwall, site_info):
    aggregates = mock_pypowerwall.poll.return_value
    calls = []

    def poll(api, *args, **kwargs):
        calls.append(api)
        return site_info() if api == "/api/site_info" else aggregates

    mock_pypowerwall.poll.side_effect = poll
    return calls


def _age_site_info(gateway_id, seconds):
    """Pretend the last /api/site_info fetch happened ``seconds`` ago."""
    _, ok = gateway_manager._site_info_attempt[gateway_id]
    gateway_manager._site_info_attempt[gateway_id] = (time.monotonic() - seconds, ok)


@pytest.mark.asyncio
async def test_poller_refreshes_site_info_every_few_minutes(mock_gateway_manager, mock_pypowerwall):
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    calls = _route_poll(mock_pypowerwall, lambda: SITE_INFO)

    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.site_info == SITE_INFO
    _age_site_info("gw", gateway_manager._SITE_INFO_REFRESH - 5)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.site_info == SITE_INFO  # cached copy, no new fetch
    assert calls.count("/api/site_info") == 1
    _age_site_info("gw", gateway_manager._SITE_INFO_REFRESH + 1)
    await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert calls.count("/api/site_info") == 2


@pytest.mark.asyncio
async def test_poller_keeps_last_good_site_info(mock_gateway_manager, mock_pypowerwall):
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    answers = [SITE_INFO, None]
    _route_poll(mock_pypowerwall, lambda: answers.pop(0))

    await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    _age_site_info("gw", gateway_manager._SITE_INFO_REFRESH + 1)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.site_info == SITE_INFO
    assert answers == []  # the refresh was attempted


@pytest.mark.asyncio
async def test_failed_fetch_is_throttled_too(mock_gateway_manager, mock_pypowerwall):
    # An outage must not turn into a /api/site_info request on every poll
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    calls = _route_poll(mock_pypowerwall, lambda: None)

    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.site_info is None
    await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert calls.count("/api/site_info") == 1
    _age_site_info("gw", gateway_manager._SITE_INFO_RETRY + 1)
    await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert calls.count("/api/site_info") == 2


def test_console_shows_three_dates_only_when_known(client):
    # Installed, Commissioned, Restarted: date only, hidden until /api/status
    # reports them; Restarted's hover shows the exact start time
    page = client.get("/console").text
    order = [page.index(f'id="{item}" style="display:none"')
             for item in ("installed-item", "commissioned-item", "restarted-item")]
    assert order == sorted(order)
    for label in ("Installed", "Commissioned", "Restarted"):
        assert f'<div class="health-label">{label}</div>' in page
    assert "['start_time', 'restarted']]" in page


def test_console_gateway_uptime_tile_hidden_until_known(client):
    page = client.get("/console").text
    assert re.search(r'<div class="health-item" id="gateway-uptime-item" style="display:none">', page)
    assert '<div class="health-label">Gateway Uptime</div>' in page
    # beside Server Uptime
    assert page.index('id="server-uptime"') < page.index('id="gateway-uptime-item"')
    assert "const upSecs = parseGoDuration(sData.up_time_seconds);" in page


def test_api_status_passes_through_the_date_key(client, connected_gateway):
    connected_gateway.data.status = {"din": "X", "start_time": "2026-09-24 21:28:16 -0700",
                                     "up_time_seconds": "403h55m9.184s",
                                     "battery_commission_date": "2025-12-15T15:48:57-08:00"}
    data = client.get("/api/status").json()
    assert data["battery_commission_date"] == "2025-12-15T15:48:57-08:00"
    assert data["up_time_seconds"] == "403h55m9.184s"
    assert "installation_date" not in data
    connected_gateway.data.status = {"din": "X", "installation_date": "2021-09-25T15:53:47-07:00"}
    assert client.get("/api/status").json()["installation_date"] == "2021-09-25T15:53:47-07:00"
