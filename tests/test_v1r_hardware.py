"""pw3 is derived from the battery hardware, not the TEDAPI transport (#112).

pypowerwall sets tedapi.pw3 for every v1r connection, Powerwall 2 included, so
the server resolves pw3 from tedapi_config battery blocks, remembers the
answer per gateway, and reports null in /stats only while a v1r gateway's
hardware is still unknown.
"""

from typing import Any, Optional
from unittest.mock import Mock

import pytest

from app.core.gateway_manager import _is_pw3_hardware, gateway_manager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData

PW3_CONFIG = {"battery_blocks": [{"type": "Powerwall3", "vin": "1707000-11-J--TG1"}]}
PW2_CONFIG = {"battery_blocks": [{"type": "ACPW", "PackagePartNumber": "1114932-00-A"}]}


def _connection(tedapi_mode: str, transport_pw3: bool, config: Any) -> Mock:
    """A pypowerwall connection mock with just enough data for one poll."""
    pw = Mock()
    pw.tedapi_mode = tedapi_mode
    pw.tedapi = Mock()
    pw.tedapi.pw3 = transport_pw3
    pw.tedapi.get_config.return_value = config
    pw.tedapi.get_fan_speeds.return_value = {}
    pw.poll.return_value = {
        "site": {"instant_power": 0},
        "solar": {"instant_power": 0},
        "battery": {"instant_power": 0},
        "load": {"instant_power": 0},
    }
    pw.level.return_value = 50.0
    pw.grid_status.return_value = "UP"
    for getter in ("get_mode", "get_reserve", "get_grid_charging", "get_grid_export"):
        getattr(pw, getter).return_value = None
    pw.system_status.return_value = {}
    pw.vitals.return_value = {}
    pw.strings.return_value = {}
    pw.temps.return_value = {}
    pw.alerts.return_value = []
    pw.freq.return_value = 60.0
    pw.status.return_value = "Running"
    pw.version.return_value = "1.0"
    pw.din.return_value = "D"
    pw.uptime.return_value = "0h"
    pw.site_name.return_value = "S"
    return pw


async def _poll(gateway_id: str, pw: Mock) -> Optional[bool]:
    """Run one fetch and record it as the last good data, like _poll_gateway."""
    if gateway_id not in gateway_manager.gateways:
        gateway_manager.gateways[gateway_id] = Gateway(
            id=gateway_id, name=gateway_id, host="10.0.0.1", gw_pwd="x"
        )
    data = await gateway_manager._fetch_gateway_data(gateway_id, pw)
    gateway_manager._last_successful_data[gateway_id] = data
    return data.pw3


@pytest.mark.parametrize(
    "config, expected",
    [
        (None, None),
        ({}, None),
        ({"battery_blocks": []}, None),
        ({"battery_blocks": ["junk"]}, None),
        ({"battery_blocks": [{"type": "Powerwall3Follower"}]}, True),
        ({"battery_blocks": [{"type": "LFPV"}]}, True),
        ({"battery_blocks": [{"type": "ACPW", "partNumber": "1707000-00-A"}]}, True),
        ({"battery_blocks": [{"type": "ACPW", "vin": "1707000-11-J--TG12"}]}, True),
        (PW2_CONFIG, False),
        ({"battery_blocks": [{"type": "ACPW", "vin": "1114932-00-A--TG1"}]}, False),
    ],
)
def test_is_pw3_hardware(config: Any, expected: Optional[bool]) -> None:
    assert _is_pw3_hardware(config) is expected


@pytest.mark.asyncio
async def test_v1r_cold_start_is_unknown() -> None:
    """v1r with no config yet: the transport flag says nothing, pw3 stays None."""
    assert await _poll("v1r", _connection("v1r", True, None)) is None


@pytest.mark.asyncio
async def test_v1r_pw2_hardware_overrides_transport() -> None:
    assert await _poll("v1r", _connection("v1r", True, PW2_CONFIG)) is False


@pytest.mark.asyncio
async def test_v1r_pw3_hardware() -> None:
    assert await _poll("v1r", _connection("v1r", True, PW3_CONFIG)) is True


@pytest.mark.asyncio
async def test_known_hardware_survives_consecutive_config_misses() -> None:
    """Hardware is learned once; failed config reads don't reset it to unknown."""
    assert await _poll("v1r", _connection("v1r", True, PW3_CONFIG)) is True
    assert await _poll("v1r", _connection("v1r", True, None)) is True
    assert await _poll("v1r", _connection("v1r", True, None)) is True


@pytest.mark.asyncio
async def test_non_v1r_keeps_transport_flag_until_hardware_known() -> None:
    assert await _poll("full", _connection("full", True, None)) is True


@pytest.mark.asyncio
async def test_non_v1r_transport_flag_overridden_by_pw2_hardware() -> None:
    """Behavior change pinned: full/hybrid TEDAPI also report the hardware."""
    assert await _poll("full", _connection("full", True, PW2_CONFIG)) is False


def _cache(gateway_id: str, tedapi_mode: Optional[str], pw3: Optional[bool]) -> None:
    gateway = Gateway(id=gateway_id, name=gateway_id, host="10.0.0.1", gw_pwd="x")
    gateway_manager.gateways[gateway_id] = gateway
    gateway_manager.cache[gateway_id] = GatewayStatus(
        gateway=gateway,
        data=PowerwallData(tedapi_mode=tedapi_mode, pw3=pw3, timestamp=1.0),
        online=True,
        last_updated=1.0,
    )


@pytest.mark.parametrize(
    "gateways, expected",
    [
        ([("a", "v1r", None)], None),  # v1r, hardware not known yet
        ([("a", "v1r", None), ("b", "full", False)], None),
        ([("a", "v1r", True), ("b", "full", False)], True),
        ([("a", "v1r", False)], False),
        ([("a", None, None)], False),  # FleetAPI / cloud / Basic LAN
        ([("a", "full", None)], False),  # non-v1r unknown stays a bool
    ],
)
def test_stats_pw3_aggregation(client, gateways, expected) -> None:
    """/stats pw3 stays a bool except while a v1r gateway's hardware is unknown."""
    for gateway_id, tedapi_mode, pw3 in gateways:
        _cache(gateway_id, tedapi_mode, pw3)
    assert client.get("/stats").json()["pw3"] is expected
