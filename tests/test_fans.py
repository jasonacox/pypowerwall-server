"""Tests for /fans and /fans/pw: Powerwall 2/+ PVAC fans (unchanged shapes) and
Powerwall 3 inverter fans (TEPINV blocks, two fans per inverter).

Mirrors pypowerwall proxy t104 (jasonacox/pypowerwall#398, proxy/tests/test_fans.py).
Hardware basis (2026-09-26, two PW3s on firmware 26.18.1): each PW3 inverter
reports PCH_FanSpeed_A/B (measured RPM) and PCH_FanDuty_A/B (duty cycle, %);
there is no target-RPM signal.
"""

import pytest
from pypowerwall.tedapi import TEDAPI

PW2_FANS = {
    "PVAC--1538000-45-C--TG2": {
        "PVAC_Fan_Speed_Actual_RPM": 1175,
        "PVAC_Fan_Speed_Target_RPM": 1200,
    },
    "PVAC--1538000-45-C--TG1": {
        "PVAC_Fan_Speed_Actual_RPM": 1180,
        "PVAC_Fan_Speed_Target_RPM": 1200,
    },
}

# get_fan_speeds() order: leader first (get_pw3_vitals order), not sorted - the
# follower's serial sorts before the leader's here, as on the validation hardware
PW3_FANS = {
    "TEPINV--1707000-11-M--TG1253370033TB": {
        "PCH_FanSpeed_A": 1395,
        "PCH_FanSpeed_B": 1397,
        "PCH_FanDuty_A": 19.1,
        "PCH_FanDuty_B": 19.1,
    },
    "TEPINV--1707000-11-M--TG125337002LNY": {
        "PCH_FanSpeed_A": 1000,
        "PCH_FanSpeed_B": 991,
        "PCH_FanDuty_A": 5.1,
        "PCH_FanDuty_B": 6.6,
    },
}


def _get(client, connected_gateway, path, fan_speeds):
    connected_gateway.data.fan_speeds = fan_speeds
    response = client.get(path)
    assert response.status_code == 200
    return response


def test_fans_pw_pw2_unchanged(client, connected_gateway):
    """PW2: FANn_actual/FANn_target per PVAC in sorted key order, no duty key.

    Compared as raw bytes so key order and serialization stay byte-identical to
    the pre-PW3 output.
    """
    response = _get(client, connected_gateway, "/fans/pw", dict(PW2_FANS))
    assert response.content == (
        b'{"FAN1_actual":1180,"FAN1_target":1200,'
        b'"FAN2_actual":1175,"FAN2_target":1200}'
    )


def test_fans_pw_pw3_two_fans_per_inverter_leader_first(client, connected_gateway):
    data = _get(client, connected_gateway, "/fans/pw", dict(PW3_FANS)).json()
    assert list(data.items()) == [
        ("FAN1_actual", 1395),
        ("FAN1_target", None),
        ("FAN1_duty", 19.1),
        ("FAN2_actual", 1397),
        ("FAN2_target", None),
        ("FAN2_duty", 19.1),
        ("FAN3_actual", 1000),
        ("FAN3_target", None),
        ("FAN3_duty", 5.1),
        ("FAN4_actual", 991),
        ("FAN4_target", None),
        ("FAN4_duty", 6.6),
    ]


def test_fans_pw_pw3_signals_none_keep_numbering(client, connected_gateway):
    """A fan signal the gateway didn't deliver stays in its FANn slot as null."""
    fans = {
        "TEPINV--1707000-11-M--TG1": {
            "PCH_FanSpeed_A": None,
            "PCH_FanSpeed_B": 1397,
            "PCH_FanDuty_A": None,
            "PCH_FanDuty_B": 19.1,
        },
        # Block with no fan signals at all (e.g. a partial TEDAPI response)
        "TEPINV--1707000-11-M--TG2": {},
    }
    data = _get(client, connected_gateway, "/fans/pw", fans).json()
    assert data == {
        "FAN1_actual": None,
        "FAN1_target": None,
        "FAN1_duty": None,
        "FAN2_actual": 1397,
        "FAN2_target": None,
        "FAN2_duty": 19.1,
        "FAN3_actual": None,
        "FAN3_target": None,
        "FAN3_duty": None,
        "FAN4_actual": None,
        "FAN4_target": None,
        "FAN4_duty": None,
    }


def test_fans_pw_pw3_numbered_after_pvac_fans(client, connected_gateway):
    fans = {**PW3_FANS, **PW2_FANS}
    data = _get(client, connected_gateway, "/fans/pw", fans).json()
    assert (data["FAN1_actual"], data["FAN2_actual"]) == (1180, 1175)
    assert (data["FAN3_actual"], data["FAN6_actual"]) == (1395, 991)
    assert "FAN1_duty" not in data
    assert "FAN2_duty" not in data
    assert data["FAN3_duty"] == 19.1


def test_fans_pw_no_fans_is_empty_object(client, connected_gateway):
    assert _get(client, connected_gateway, "/fans/pw", {}).json() == {}
    assert _get(client, connected_gateway, "/fans/pw", None).json() == {}


def test_fans_raw_passthrough(client, connected_gateway):
    """/fans returns get_fan_speeds() as cached, PW2 and PW3 alike."""
    assert _get(client, connected_gateway, "/fans", dict(PW3_FANS)).json() == PW3_FANS
    assert _get(client, connected_gateway, "/fans", dict(PW2_FANS)).json() == PW2_FANS


@pytest.mark.skipif(
    not hasattr(TEDAPI, "extract_pw3_fan_speeds"),
    reason="needs pypowerwall >= 0.18.2 (PW3 fans in get_fan_speeds)",
)
def test_fans_pw_from_pinned_library_get_fan_speeds(client, connected_gateway):
    """Contract with the pinned library: real TEDAPI.get_fan_speeds() output for
    two PW3s (from get_pw3_vitals() blocks) maps to leader-first FANn keys."""
    tedapi = TEDAPI.__new__(TEDAPI)  # no network: stub the two data sources
    tedapi.pw3 = True
    tedapi.get_device_controller = lambda force=False: {}
    tedapi.get_pw3_vitals = lambda force=False: {
        name: {**signals, "PCH_Temp": 40.0} for name, signals in PW3_FANS.items()
    }
    fan_speeds = tedapi.get_fan_speeds()
    assert list(fan_speeds) == list(PW3_FANS)

    data = _get(client, connected_gateway, "/fans/pw", fan_speeds).json()
    assert data == _get(client, connected_gateway, "/fans/pw", PW3_FANS).json()
    assert (data["FAN1_actual"], data["FAN4_duty"]) == (1395, 6.6)
