"""Legacy endpoints return what the pypowerwall proxy returns, so proxy clients
(Powerwall-Dashboard's Telegraf, scripts) can switch to the server unchanged."""
import pytest

from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway


def test_soe_returns_raw_gateway_level(client, connected_gateway):
    """/soe percentage is the raw gateway level, like the proxy."""
    data = client.get("/soe").json()
    assert data == {"percentage": 85.5, "raw_percentage": 85.5}


def test_json_soe_is_raw_gateway_level(client, connected_gateway):
    data = client.get("/json").json()
    assert data["soe"] == 85.5
    assert data["soe_raw"] == 85.5


@pytest.mark.parametrize("grid_status, expected", [
    ("UP", 1),
    ("DOWN", 0),
    ("SYNCING", -1),
    (None, None),
])
def test_freq_grid_status_matches_proxy_numeric(client, connected_gateway, grid_status, expected):
    """/freq grid_status uses the proxy's numeric map: -1 SYNCING, null unknown."""
    connected_gateway.data.grid_status = grid_status
    assert client.get("/freq").json()["grid_status"] == expected


def _gap_aggregates(connected_gateway):
    """Meters that report instant_power as None, or are missing entirely."""
    connected_gateway.data.aggregates = {
        "site": {"instant_power": None},
        "solar": None,
        "load": {"instant_power": 1234.5},
    }


def test_csv_treats_power_gaps_as_zero(client, connected_gateway):
    _gap_aggregates(connected_gateway)
    response = client.get("/csv")
    assert response.status_code == 200
    assert response.text == "0.00,1234.50,0.00,0.00,85.50\n"


def test_csv_v2_treats_power_gaps_as_zero_and_truncates_reserve(client, connected_gateway):
    _gap_aggregates(connected_gateway)
    connected_gateway.data.grid_status = "UP"
    connected_gateway.data.reserve = 19.9
    response = client.get("/csv/v2")
    assert response.status_code == 200
    assert response.text == "0.00,1234.50,0.00,0.00,85.50,1,19\n"


def test_json_treats_power_gaps_as_zero(client, connected_gateway):
    _gap_aggregates(connected_gateway)
    data = client.get("/json").json()
    assert (data["grid"], data["home"], data["solar"], data["battery"]) == (0, 1234.5, 0, 0)


def _poll_returns(mock_pypowerwall, aggregates):
    mock_pypowerwall.poll.side_effect = lambda api, *a, **k: aggregates if "aggregates" in api else None


@pytest.mark.asyncio
async def test_neg_solar_correction_tolerates_none_readings(mock_gateway_manager, mock_pypowerwall, monkeypatch):
    """PW_NEG_SOLAR=no must not crash when solar or load power is None (#72)."""
    monkeypatch.setattr(settings, "neg_solar", False)
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    aggregates = {
        "site": {"instant_power": 100},
        "solar": {"instant_power": None},
        "battery": {"instant_power": 0},
        "load": {"instant_power": None},
    }
    _poll_returns(mock_pypowerwall, aggregates)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.aggregates["solar"]["instant_power"] is None


@pytest.mark.asyncio
async def test_neg_solar_correction_moves_negative_solar_to_load(mock_gateway_manager, mock_pypowerwall, monkeypatch):
    monkeypatch.setattr(settings, "neg_solar", False)
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    aggregates = {
        "site": {"instant_power": 100},
        "solar": {"instant_power": -20},
        "battery": {"instant_power": 0},
        "load": {"instant_power": 500},
    }
    _poll_returns(mock_pypowerwall, aggregates)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.aggregates["solar"]["instant_power"] == 0
    assert data.aggregates["load"]["instant_power"] == 520


@pytest.mark.asyncio
async def test_neg_solar_correction_with_null_load(mock_gateway_manager, mock_pypowerwall, monkeypatch):
    """Negative solar with a None load reading: solar is clamped, load stays None."""
    monkeypatch.setattr(settings, "neg_solar", False)
    gateway_manager.gateways["gw"] = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret")
    gateway_manager.connections["gw"] = mock_pypowerwall
    aggregates = {
        "site": {"instant_power": 100},
        "solar": {"instant_power": -20},
        "battery": {"instant_power": 0},
        "load": {"instant_power": None},
    }
    _poll_returns(mock_pypowerwall, aggregates)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    assert data.aggregates["solar"]["instant_power"] == 0
    assert data.aggregates["load"]["instant_power"] is None


@pytest.mark.asyncio
async def test_null_readings_from_poll_reach_aggregate_routes(
    client, mock_gateway_manager, mock_pypowerwall, monkeypatch
):
    """A polled data gap (None reading, None meter block) doesn't break the
    multi-gateway totals (they count the gap as 0) or the routes that read a
    single meter, and the cached snapshot keeps the None."""
    from app.models.gateway import GatewayStatus

    monkeypatch.setattr(settings, "neg_solar", False)
    gateway = Gateway(id="gw", name="GW", host="192.168.91.1", gw_pwd="secret", online=True)
    gateway_manager.gateways["gw"] = gateway
    gateway_manager.connections["gw"] = mock_pypowerwall
    aggregates = {
        "site": {"instant_power": 100},
        "solar": {"instant_power": None},
        "battery": None,
        "load": {"instant_power": 400},
    }
    _poll_returns(mock_pypowerwall, aggregates)
    data = await gateway_manager._fetch_gateway_data("gw", mock_pypowerwall)
    gateway_manager.cache["gw"] = GatewayStatus(gateway=gateway, data=data, online=True, last_updated=data.timestamp)

    totals = gateway_manager.get_aggregate_data()
    assert totals.total_solar_power == 0
    assert totals.total_battery_power == 0
    assert totals.total_load_power == 400
    assert client.get("/api/aggregate/power").status_code == 200
    assert gateway_manager.cache["gw"].data.aggregates["solar"]["instant_power"] is None

    # Routes that read one meter: a None block answers like a missing one (200)
    monkeypatch.setattr("app.api.legacy.get_default_gateway", lambda: "gw")
    assert client.get("/battery").json() == {"power": 0}
    assert client.get("/pw/power").json() == {"site": 100, "solar": None, "battery": 0, "load": 400}
    assert client.get("/pw/battery").json() == {}
    assert client.get("/pw/solar").json() == {"instant_power": None}
    for path in ("/csv", "/csv/v2", "/json", "/aggregates", "/api/meters/solar"):
        assert client.get(path).status_code == 200, path


def test_neg_solar_defaults_to_yes_like_the_proxy(monkeypatch):
    """PW_NEG_SOLAR defaults to yes (proxy default); "no" still turns the correction on."""
    from app.config import Settings

    monkeypatch.delenv("PW_NEG_SOLAR", raising=False)
    assert Settings().neg_solar is True
    monkeypatch.setenv("PW_NEG_SOLAR", "no")
    assert Settings().neg_solar is False
