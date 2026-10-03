"""
Tests for Phase 2 — Home Assistant MQTT auto-discovery payloads.

These tests validate:
  - build_discovery_payloads() returns the expected number of entries
  - Every entry has a valid discovery topic path
  - Key sensor payloads carry correct HA fields (unique_id, device_class, unit, etc.)
  - The shared device block appears on every payload and contains gateway info
  - Availability config references the correct availability topic
  - The MqttPublisher._publish_ha_discovery() integration path works end-to-end
  - Discovery is sent exactly once per gateway per connection (not on every poll)
  - Discovery is re-sent after a reconnect (_discovery_sent cleared)
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.mqtt.ha_discovery import (
    build_discovery_payloads,
    discovery_signature,
    extract_device_signals,
    extract_remote_meters,
)
from app.mqtt.publisher import MqttPublisher
from app.models.gateway import Gateway, GatewayStatus, PowerwallData

# Base entities every gateway announces: 20 sensors + 3 binary sensors.
# Solar-string and remote-meter entities are added on top when reported.
BASE_ENTITY_COUNT = 23


# ---------------------------------------------------------------------------
# Helpers (shared with test_mqtt_publisher.py pattern)
# ---------------------------------------------------------------------------

def make_status(
    gateway_id: str = "test-gw",
    gateway_name: str = "Test Gateway",
    version: str = "23.44.0",
    online: bool = True,
    soe: float = 73.6842105263,
    soe_raw: float = 75.0,
) -> GatewayStatus:
    gateway = Gateway(id=gateway_id, name=gateway_name, host="192.168.91.1", online=online)
    data = PowerwallData(
        soe_raw=soe_raw,
        soe=soe,
        aggregates={
            "solar": {"instant_power": 3000.0},
            "site": {"instant_power": -500.0},
            "load": {"instant_power": 2500.0},
            "battery": {"instant_power": 0.0},
        },
        grid_status="UP",
        mode="self_consumption",
        reserve=20.0,
        version=version,
        timestamp=1_000_000.0,
    )
    return GatewayStatus(gateway=gateway, data=data, online=online, last_updated=1_000_000.0)


# ---------------------------------------------------------------------------
# Unit tests for build_discovery_payloads()
# ---------------------------------------------------------------------------

class TestBuildDiscoveryPayloads:
    def _payloads(self, gateway_id="home", gateway_name="Home Powerwall",
                  prefix="pypowerwall", ha_prefix="homeassistant",
                  version="23.44.0") -> list[tuple[str, dict]]:
        raw = build_discovery_payloads(
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            topic_prefix=prefix,
            ha_prefix=ha_prefix,
            version=version,
        )
        return [(topic, json.loads(payload)) for topic, payload in raw]

    def test_returns_expected_count(self):
        results = self._payloads()
        assert len(results) == BASE_ENTITY_COUNT

    def test_all_topics_start_with_ha_prefix(self):
        results = self._payloads(ha_prefix="homeassistant")
        for topic, _ in results:
            assert topic.startswith("homeassistant/"), f"Bad topic: {topic}"

    def test_sensor_topics_contain_gateway_id(self):
        results = self._payloads(gateway_id="home")
        for topic, _ in results:
            assert "pypowerwall_home_" in topic

    def test_binary_sensor_topic_present(self):
        results = self._payloads()
        binary_topics = [t for t, _ in results if "/binary_sensor/" in t]
        assert len(binary_topics) == 3
        assert any("online" in t for t in binary_topics)
        assert any("grid_connected" in t for t in binary_topics)
        assert any("grid_charging" in t for t in binary_topics)

    def test_sensor_topics_end_with_config(self):
        results = self._payloads()
        for topic, _ in results:
            assert topic.endswith("/config"), f"Topic should end with /config: {topic}"

    def test_device_block_on_every_payload(self):
        results = self._payloads(gateway_id="cabin", gateway_name="Cabin Powerwall", version="24.0.1")
        for topic, payload in results:
            assert "device" in payload, f"Missing device block: {topic}"
            device = payload["device"]
            assert device["manufacturer"] == "Tesla"
            assert device["model"] == "Powerwall"
            assert device["sw_version"] == "24.0.1"
            assert "pypowerwall_cabin" in device["identifiers"][0]
            assert device["name"] == "Cabin Powerwall"

    def test_battery_sensor_fields(self):
        results = dict(self._payloads())
        battery_topic = "homeassistant/sensor/pypowerwall_home_battery/config"
        assert battery_topic in results
        p = results[battery_topic]
        assert p["unit_of_measurement"] == "%"
        assert p["device_class"] == "battery"
        assert p["state_class"] == "measurement"
        assert p["state_topic"] == "pypowerwall/home/battery"
        assert p["unique_id"] == "pypowerwall_home_battery"

    def test_solar_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/sensor/pypowerwall_home_solar/config"
        assert topic in results
        p = results[topic]
        assert p["unit_of_measurement"] == "W"
        assert p["device_class"] == "power"
        assert p["state_topic"] == "pypowerwall/home/solar"

    def test_grid_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/sensor/pypowerwall_home_grid/config"
        assert topic in results
        p = results[topic]
        assert p["unit_of_measurement"] == "W"
        assert p["device_class"] == "power"

    def test_battery_energy_sensor_fields(self):
        results = dict(self._payloads())
        checks = {
            "total_capacity": "pypowerwall/home/total_capacity",
            "current_charge": "pypowerwall/home/current_charge",
        }
        for uid, state_topic in checks.items():
            p = results[f"homeassistant/sensor/pypowerwall_home_{uid}/config"]
            assert p["unit_of_measurement"] == "Wh"
            assert p["device_class"] == "energy_storage"
            assert p["state_class"] == "measurement"
            assert p["state_topic"] == state_topic
            assert p["unique_id"] == f"pypowerwall_home_{uid}"

    def test_lifetime_energy_sensors_present(self):
        """All six lifetime energy sensors are discovered."""
        results = dict(self._payloads())
        for uid in (
            "grid_energy_imported", "grid_energy_exported",
            "home_energy_imported", "solar_energy_exported",
            "battery_energy_imported", "battery_energy_exported",
        ):
            assert f"homeassistant/sensor/pypowerwall_home_{uid}/config" in results

    def test_energy_sensor_fields(self):
        """Energy sensors carry Wh / energy / total_increasing for the HA Energy dashboard."""
        results = dict(self._payloads())
        checks = {
            "grid_energy_imported": "pypowerwall/home/grid_energy_imported",
            "grid_energy_exported": "pypowerwall/home/grid_energy_exported",
            "home_energy_imported": "pypowerwall/home/home_energy_imported",
            "solar_energy_exported": "pypowerwall/home/solar_energy_exported",
            "battery_energy_imported": "pypowerwall/home/battery_energy_imported",
            "battery_energy_exported": "pypowerwall/home/battery_energy_exported",
        }
        for uid, state_topic in checks.items():
            p = results[f"homeassistant/sensor/pypowerwall_home_{uid}/config"]
            assert p["unit_of_measurement"] == "Wh", uid
            assert p["device_class"] == "energy", uid
            assert p["state_class"] == "total_increasing", uid
            assert p["state_topic"] == state_topic, uid
            assert p["unique_id"] == f"pypowerwall_home_{uid}", uid

    def test_version_sensor_is_diagnostic(self):
        results = dict(self._payloads())
        topic = "homeassistant/sensor/pypowerwall_home_version/config"
        assert topic in results
        p = results[topic]
        assert p.get("entity_category") == "diagnostic"

    def test_online_binary_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/binary_sensor/pypowerwall_home_online/config"
        assert topic in results
        p = results[topic]
        assert p["device_class"] == "connectivity"
        assert p["payload_on"] == "true"
        assert p["payload_off"] == "false"
        assert p["state_topic"] == "pypowerwall/home/online"

    def test_grid_connected_binary_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/binary_sensor/pypowerwall_home_grid_connected/config"
        p = results[topic]
        assert p["unique_id"] == "pypowerwall_home_grid_connected"
        assert p["state_topic"] == "pypowerwall/home/grid_connected"
        assert p["payload_on"] == "true"
        assert p["payload_off"] == "false"
        assert p["device_class"] == "connectivity"

    def test_grid_charging_binary_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/binary_sensor/pypowerwall_home_grid_charging/config"
        p = results[topic]
        assert p["unique_id"] == "pypowerwall_home_grid_charging"
        assert p["state_topic"] == "pypowerwall/home/grid_charging"
        assert p["payload_on"] == "true"
        assert p["payload_off"] == "false"
        assert "device_class" not in p  # generic On/Off

    def test_grid_export_text_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/sensor/pypowerwall_home_grid_export/config"
        p = results[topic]
        assert p["unique_id"] == "pypowerwall_home_grid_export"
        assert p["state_topic"] == "pypowerwall/home/grid_export"
        # Text sensor: HA rejects a unit/device_class/state_class on text states
        for key in ("unit_of_measurement", "device_class", "state_class"):
            assert key not in p

    def test_time_remaining_sensor_fields(self):
        results = dict(self._payloads())
        topic = "homeassistant/sensor/pypowerwall_home_time_remaining/config"
        p = results[topic]
        assert p["unique_id"] == "pypowerwall_home_time_remaining"
        assert p["state_topic"] == "pypowerwall/home/time_remaining"
        assert p["unit_of_measurement"] == "h"
        assert p["device_class"] == "duration"
        assert p["state_class"] == "measurement"

    def test_availability_references_correct_topic(self):
        results = self._payloads(gateway_id="main", prefix="pw")
        for topic, payload in results:
            avail_list = payload.get("availability", [])
            # Two entries: per-gateway topic and global LWT topic
            assert len(avail_list) == 2
            topics = {a["topic"] for a in avail_list}
            assert "pw/main/availability" in topics
            assert "pw/availability" in topics
            for entry in avail_list:
                assert entry["payload_available"] == "online"
                assert entry["payload_not_available"] == "offline"
            assert payload.get("availability_mode") == "all"

    def test_custom_prefix_and_ha_prefix(self):
        results = build_discovery_payloads(
            gateway_id="site2",
            gateway_name="Site 2",
            topic_prefix="mypw",
            ha_prefix="ha",
            version="22.1.0",
        )
        for topic, _ in results:
            assert topic.startswith("ha/")
        payloads = {t: json.loads(p) for t, p in results}
        battery = payloads.get("ha/sensor/pypowerwall_site2_battery/config")
        assert battery is not None
        assert battery["state_topic"] == "mypw/site2/battery"

        battery_raw = payloads.get("ha/sensor/pypowerwall_site2_battery_raw/config")
        assert battery_raw is not None
        assert battery_raw["state_topic"] == "mypw/site2/battery_raw"

    def test_version_none_handled(self):
        """build_discovery_payloads() must not crash when version is None."""
        results = build_discovery_payloads(
            gateway_id="gw",
            gateway_name="GW",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            version=None,
        )
        assert len(results) == BASE_ENTITY_COUNT
        for _, payload_str in results:
            p = json.loads(payload_str)
            assert p["device"]["sw_version"] == "unknown"

    def test_no_string_sensors_when_string_ids_absent(self):
        """When string_ids is not supplied, no string sensors are added."""
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
        )
        string_topics = [t for t, _ in results if "_string_" in t]
        assert string_topics == []
        assert len(results) == BASE_ENTITY_COUNT

    def test_string_sensors_single_pw3(self):
        """Six strings A–F → 6×3 per-string + 3×3 paired rollup = 27 extra entries."""
        string_ids = ["A", "B", "C", "D", "E", "F"]
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            string_ids=string_ids,
        )
        payloads = {t: json.loads(p) for t, p in results}
        # base + 6 strings × 3 metrics + 3 pairs × 3 metrics
        assert len(results) == BASE_ENTITY_COUNT + 18 + 9

        # Spot-check string A voltage
        topic = "homeassistant/sensor/pypowerwall_home_string_a_voltage/config"
        assert topic in payloads
        p = payloads[topic]
        assert p["unit_of_measurement"] == "V"
        assert p["device_class"] == "voltage"
        assert p["state_topic"] == "pypowerwall/home/strings/A/voltage"
        assert p["entity_category"] == "diagnostic"

        # Spot-check paired rollup AB power
        topic = "homeassistant/sensor/pypowerwall_home_string_ab_power/config"
        assert topic in payloads
        p = payloads[topic]
        assert p["unit_of_measurement"] == "W"
        assert p["device_class"] == "power"
        assert p["state_topic"] == "pypowerwall/home/strings/AB/power"

    def test_string_sensors_partial_strings(self):
        """Only strings present are discovered — no entities for missing strings."""
        string_ids = ["A", "B"]
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            string_ids=string_ids,
        )
        payloads = {t: json.loads(p) for t, p in results}
        # base + 2×3 per-string + 1 pair (AB) × 3
        assert len(results) == BASE_ENTITY_COUNT + 6 + 3
        # AB pair present
        assert "homeassistant/sensor/pypowerwall_home_string_ab_voltage/config" in payloads
        # CD and EF pairs must NOT be present (C/D/E/F not in string_ids)
        assert "homeassistant/sensor/pypowerwall_home_string_cd_power/config" not in payloads

    def test_string_sensors_multi_pw3(self):
        """Multi-PW3 numbered strings (A1–F2) generate correct paired rollups."""
        string_ids = ["A1", "B1", "C1", "D1", "E1", "F1",
                      "A2", "B2", "C2", "D2", "E2", "F2"]
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            string_ids=string_ids,
        )
        payloads = {t: json.loads(p) for t, p in results}
        # base + 12×3 per-string + 6 pairs × 3
        assert len(results) == BASE_ENTITY_COUNT + 36 + 18
        # Spot-check numbered pair AB1
        assert "homeassistant/sensor/pypowerwall_home_string_ab1_voltage/config" in payloads
        assert "homeassistant/sensor/pypowerwall_home_string_ab2_power/config" in payloads

    def test_no_remote_meter_sensors_when_absent(self):
        """When remote_meters is not supplied, no remote-meter sensors are added."""
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
        )
        assert [t for t, _ in results if "remote_meter" in t] == []
        assert len(results) == BASE_ENTITY_COUNT

    def test_remote_meter_sensors_single_ct(self):
        """One meter, one CT -> 5 extra entries (voltage/current/power/energy x2)."""
        remote_meters = {
            "2002069-00-E--EM4260230B10BC": {
                "0": {
                    "InstVoltage": 122.7,
                    "InstCurrent": 0.95,
                    "InstRealPower": 158.3,
                    "Location": "solar",
                },
            },
        }
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            remote_meters=remote_meters,
        )
        payloads = {t: json.loads(p) for t, p in results}
        assert len(results) == BASE_ENTITY_COUNT + 1 * 5  # 1 CT x 5 metrics

        topic = "homeassistant/sensor/pypowerwall_home_remote_meter_2002069_00_e_em4260230b10bc_ct0_voltage/config"
        assert topic in payloads
        p = payloads[topic]
        assert p["unit_of_measurement"] == "V"
        assert p["device_class"] == "voltage"
        assert p["state_class"] == "measurement"
        assert (
            p["state_topic"]
            == "pypowerwall/home/meters/remote/2002069-00-E--EM4260230B10BC/ct0/voltage"
        )
        assert p["entity_category"] == "diagnostic"
        assert "solar" in p["name"].lower()

        energy_topic = "homeassistant/sensor/pypowerwall_home_remote_meter_2002069_00_e_em4260230b10bc_ct0_energy_imported/config"
        assert energy_topic in payloads
        ep = payloads[energy_topic]
        assert ep["unit_of_measurement"] == "Wh"
        assert ep["device_class"] == "energy"
        assert ep["state_class"] == "total_increasing"

    def test_remote_meter_sensors_multiple_cts_and_meters(self):
        """Two CTs on one meter plus a second meter -> 3 CTs x 5 metrics = 15 extra."""
        remote_meters = {
            "DIN0000000000000000000001": {
                "0": {"InstVoltage": 120.0},
                "1": {"InstVoltage": 121.0},
            },
            "DIN0000000000000000000002": {
                "0": {"InstVoltage": 240.0},
            },
        }
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            remote_meters=remote_meters,
        )
        payloads = {t: json.loads(p) for t, p in results}
        assert len(results) == BASE_ENTITY_COUNT + 3 * 5  # 3 CTs x 5 metrics

        assert (
            "homeassistant/sensor/pypowerwall_home_remote_meter_din0000000000000000000001_ct0_voltage/config"
            in payloads
        )
        assert (
            "homeassistant/sensor/pypowerwall_home_remote_meter_din0000000000000000000001_ct1_voltage/config"
            in payloads
        )
        assert (
            "homeassistant/sensor/pypowerwall_home_remote_meter_din0000000000000000000002_ct0_voltage/config"
            in payloads
        )


class TestExtractRemoteMeters:
    """Unit tests for extract_remote_meters() - regroups the flat
    TRM--<din>/TRM_CT{n}_<Metric> vitals shape into {din: {ct: {metric: value}}}."""

    def test_single_meter_single_ct(self):
        vitals = {
            "TRM--2002069-00-E--EM4260230B10BC": {
                "TRM_CT0_InstVoltage": 122.7,
                "TRM_CT0_InstCurrent": 0.95,
                "TRM_CT0_Location": "solar",
                "serialNumber": "2002069-00-E--EM4260230B10BC",  # non-CT field, ignored
            },
        }
        result = extract_remote_meters(vitals)
        assert result == {
            "2002069-00-E--EM4260230B10BC": {
                "0": {"InstVoltage": 122.7, "InstCurrent": 0.95, "Location": "solar"},
            },
        }

    def test_multiple_cts_same_meter(self):
        vitals = {
            "TRM--DIN1": {
                "TRM_CT0_InstVoltage": 120.0,
                "TRM_CT1_InstVoltage": 121.0,
            },
        }
        result = extract_remote_meters(vitals)
        assert set(result["DIN1"].keys()) == {"0", "1"}
        assert result["DIN1"]["0"]["InstVoltage"] == 120.0
        assert result["DIN1"]["1"]["InstVoltage"] == 121.0

    def test_multiple_meters(self):
        vitals = {
            "TRM--DIN1": {"TRM_CT0_InstVoltage": 120.0},
            "TRM--DIN2": {"TRM_CT0_InstVoltage": 240.0},
        }
        result = extract_remote_meters(vitals)
        assert set(result.keys()) == {"DIN1", "DIN2"}

    def test_non_trm_blocks_ignored(self):
        vitals = {
            "TEPINV--1707000-21-M--TG126233000WMD": {
                "PINV_State": "PINV_GridFollowing"
            },
            "NEURIO--VAH1234AB1234": {"NEURIO_CT0_InstVoltage": 120.0},
        }
        assert extract_remote_meters(vitals) == {}

    def test_meter_with_no_ct_fields_omitted(self):
        """A TRM-- block with only device-identity fields (no TRM_CT{n}_*
        metrics) contributes nothing - there's no reading to publish."""
        vitals = {"TRM--DIN1": {"serialNumber": "DIN1", "manufacturer": "TESLA"}}
        assert extract_remote_meters(vitals) == {}

    def test_none_or_malformed_input(self):
        assert extract_remote_meters(None) == {}
        assert extract_remote_meters({}) == {}
        assert extract_remote_meters({"TRM--DIN1": "not a dict"}) == {}

    def test_empty_din_suffix_skipped(self):
        """A bare 'TRM--' key (no DIN suffix) is malformed and contributes
        nothing, rather than being registered under an empty-string key."""
        vitals = {"TRM--": {"TRM_CT0_InstVoltage": 120.0}}
        assert extract_remote_meters(vitals) == {}

    def test_non_string_field_keys_ignored(self):
        """A non-string field key must not raise (the helper never raises)."""
        vitals = {"TRM--DIN1": {1: 2, None: 3, "TRM_CT0_InstVoltage": 1.0}}
        assert extract_remote_meters(vitals) == {"DIN1": {"0": {"InstVoltage": 1.0}}}

    @pytest.mark.parametrize("din", ["A/B", "A+B", "A#B"])
    def test_din_with_topic_wildcards_skipped(self, din):
        """The DIN is an MQTT topic level: '/', '+' and '#' would break the
        publish, so such a block is skipped."""
        vitals = {f"TRM--{din}": {"TRM_CT0_InstVoltage": 1.0}}
        assert extract_remote_meters(vitals) == {}

    def test_realistic_pypowerwall_block(self):
        """The 0.18.2 aggregate_remote_meter_data() shape, None energy included."""
        vitals = {
            "TRM--2002069-00-E--EM4260230B10BC": {
                "TRM_CT0_InstRealPower": 158.3,
                "TRM_CT0_InstReactivePower": -12.0,
                "TRM_CT0_InstVoltage": 122.7,
                "TRM_CT0_InstCurrent": 0.95,
                "TRM_CT0_EnergyExportedWs": None,
                "TRM_CT0_EnergyImportedWs": 43466036,
                "TRM_CT0_Location": "solar",
            }
        }
        ct0 = extract_remote_meters(vitals)["2002069-00-E--EM4260230B10BC"]["0"]
        assert ct0["EnergyExportedWs"] is None
        assert ct0["Location"] == "solar"


# ---------------------------------------------------------------------------
# Integration tests for MqttPublisher._publish_ha_discovery()
# ---------------------------------------------------------------------------

class TestPublisherHaDiscovery:
    def _make_publisher(self, monkeypatch) -> MqttPublisher:
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "mqtt_host", "localhost")
        monkeypatch.setattr(_settings, "mqtt_topic_prefix", "pypowerwall")
        monkeypatch.setattr(_settings, "mqtt_ha_prefix", "homeassistant")
        monkeypatch.setattr(_settings, "mqtt_ha_discovery", True)
        monkeypatch.setattr(_settings, "mqtt_qos", 1)
        monkeypatch.setattr(_settings, "mqtt_retain", True)
        return MqttPublisher()

    @pytest.mark.asyncio
    async def test_discovery_published_on_first_poll(self, monkeypatch):
        """Discovery payloads are sent on the first publish_gateway() call."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        await pub.publish_gateway("test-gw", status)

        topics = [c.args[0] for c in mock_client.publish.call_args_list]
        disc_topics = [t for t in topics if "homeassistant" in t]
        assert len(disc_topics) == BASE_ENTITY_COUNT  # one per sensor/binary_sensor

    @pytest.mark.asyncio
    async def test_discovery_sent_only_once_per_connection(self, monkeypatch):
        """Calling publish_gateway() twice must not send discovery twice."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        await pub.publish_gateway("test-gw", status)
        first_call_count = mock_client.publish.call_count

        # Second poll for same gateway - discovery must NOT be repeated
        await pub.publish_gateway("test-gw", status)
        second_call_count = mock_client.publish.call_count

        # Only sensor topic publishes added in second call; no new discovery
        disc_in_second = [
            c.args[0]
            for c in mock_client.publish.call_args_list[first_call_count:]
            if "homeassistant" in c.args[0]
        ]
        assert disc_in_second == []

    @pytest.mark.asyncio
    async def test_discovery_resent_after_reconnect(self, monkeypatch):
        """After _discovery_sent is cleared (reconnect), discovery fires again."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        await pub.publish_gateway("test-gw", status)

        # Simulate reconnect - connection loop clears this set
        pub._discovery_sent.clear()
        mock_client.publish.reset_mock()

        await pub.publish_gateway("test-gw", status)
        disc_topics = [
            c.args[0]
            for c in mock_client.publish.call_args_list
            if "homeassistant" in c.args[0]
        ]
        assert len(disc_topics) == BASE_ENTITY_COUNT

    @pytest.mark.asyncio
    async def test_discovery_skipped_when_ha_discovery_false(self, monkeypatch):
        """No discovery payloads when MQTT_HA_DISCOVERY=false."""
        pub = self._make_publisher(monkeypatch)
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "mqtt_ha_discovery", False)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        await pub.publish_gateway("test-gw", status)

        topics = [c.args[0] for c in mock_client.publish.call_args_list]
        disc_topics = [t for t in topics if "homeassistant" in t]
        assert disc_topics == []

    @pytest.mark.asyncio
    async def test_discovery_includes_correct_device_name(self, monkeypatch):
        """Discovery device name matches the gateway name from GatewayStatus."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status(gateway_name="My Beach House")
        await pub.publish_gateway("beach", status)

        # Find any discovery payload and check device name
        for call in mock_client.publish.call_args_list:
            topic = call.args[0]
            payload_str = call.args[1]
            if "homeassistant" in topic:
                payload = json.loads(payload_str)
                assert payload["device"]["name"] == "My Beach House"
                break
        else:
            pytest.fail("No discovery payload published")

    @pytest.mark.asyncio
    async def test_discovery_includes_remote_meter_from_vitals(self, monkeypatch):
        """A TRM--<din> block in status.data.vitals produces remote-meter
        discovery sensors, end to end through _publish_ha_discovery()."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        gateway = Gateway(
            id="test-gw", name="Test Gateway", host="192.168.91.1", online=True
        )
        data = PowerwallData(
            soe=75.0,
            soe_raw=75.0,
            aggregates={
                "solar": {"instant_power": 3000.0},
                "site": {"instant_power": -500.0},
                "load": {"instant_power": 2500.0},
                "battery": {"instant_power": 0.0},
            },
            grid_status="UP",
            mode="self_consumption",
            reserve=20.0,
            version="26.26.11",
            vitals={
                "TRM--2002069-00-E--EM4260230B10BC": {
                    "TRM_CT0_InstVoltage": 122.7,
                    "TRM_CT0_InstCurrent": 0.95,
                    "TRM_CT0_InstRealPower": 158.3,
                    "TRM_CT0_Location": "solar",
                },
            },
            timestamp=1_000_000.0,
        )
        status = GatewayStatus(
            gateway=gateway, data=data, online=True, last_updated=1_000_000.0
        )

        await pub.publish_gateway("test-gw", status)

        disc_topics = [
            c.args[0]
            for c in mock_client.publish.call_args_list
            if "homeassistant" in c.args[0] and "remote_meter" in c.args[0]
        ]
        assert (
            len(disc_topics) == 5
        )  # voltage/current/power/energy_imported/energy_exported
        assert (
            "homeassistant/sensor/pypowerwall_test-gw_remote_meter_"
            "2002069_00_e_em4260230b10bc_ct0_voltage/config" in disc_topics
        )


def test_extract_remote_meters_matches_pinned_library_output():
    """Contract with the pinned pypowerwall: TEDAPI.aggregate_remote_meter_data()
    output (what vitals() merges in) parses into the per-CT shape, keeps each
    CT's original slot (a skipped slot stays skipped) and yields 5 sensors per
    CT."""
    from pypowerwall.tedapi import TEDAPI

    if not hasattr(TEDAPI, "aggregate_remote_meter_data"):
        pytest.skip("pypowerwall without remote meter support")
    din = "2002069-00-E--EM4260230B10BC"
    status_data = {
        "teslaRemoteMeter": {
            "meters": [
                {
                    "din": din,
                    "reading": {
                        "ctReadings": [
                            {
                                "realPowerW": 158.3,
                                "voltageV": 122.7,
                                "currentA": 0.95,
                                "energyImportedWs": 43466036,
                                "energyExportedWs": None,
                            },
                            {"realPowerW": 5.0, "voltageV": 122.7, "currentA": 0.1},
                            {
                                "realPowerW": -20.0,
                                "voltageV": 122.6,
                                "currentA": 0.2,
                                "energyImportedWs": 10,
                                "energyExportedWs": 7200,
                            },
                        ]
                    },
                }
            ]
        }
    }
    meter_config = {
        din: {
            "cts": [True, False, True],
            "location": ["solar", "", "site"],
            "type": "trm_mb",
        }
    }
    tedapi = TEDAPI.__new__(TEDAPI)  # pure function of its arguments
    flat, _ = tedapi.aggregate_remote_meter_data(
        {"vin": "GW"}, status_data, meter_config
    )

    meters = extract_remote_meters(flat)
    assert list(meters) == [din]
    assert sorted(meters[din]) == ["0", "2"]  # CT slot 1 is disabled in config
    assert meters[din]["0"]["InstVoltage"] == 122.7
    assert meters[din]["0"]["EnergyExportedWs"] is None
    assert meters[din]["2"]["Location"] == "site"

    results = build_discovery_payloads(
        gateway_id="home",
        gateway_name="Home",
        topic_prefix="pypowerwall",
        ha_prefix="homeassistant",
        remote_meters=meters,
    )
    assert len(results) == BASE_ENTITY_COUNT + 2 * 5


def _status_with(strings=None, vitals=None) -> GatewayStatus:
    status = make_status()
    status.data.strings = strings
    status.data.vitals = vitals
    return status


_TRM_VITALS = {"TRM--DIN1": {"TRM_CT0_InstVoltage": 120.0, "TRM_CT0_Location": "site"}}
_STRINGS = {"A": {"Voltage": 300.0, "Current": 2.0, "Power": 600.0, "Connected": True}}


class TestDiscoveryForLateEntities:
    """Strings and remote-meter CTs first reported on a later poll (e.g. the
    first poll's vitals timed out) must still be discovered - once."""

    def _make_publisher(self, monkeypatch) -> MqttPublisher:
        from app.config import settings

        monkeypatch.setattr(settings, "mqtt_host", "localhost")
        monkeypatch.setattr(settings, "mqtt_port", 1883)
        monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
        monkeypatch.setattr(settings, "mqtt_qos", 1)
        monkeypatch.setattr(settings, "mqtt_retain", True)
        monkeypatch.setattr(settings, "mqtt_ha_discovery", True)
        monkeypatch.setattr(settings, "mqtt_ha_prefix", "homeassistant")
        pub = MqttPublisher()
        pub._client = AsyncMock()
        pub._connected = True
        return pub

    async def _discovery_topics(self, pub: MqttPublisher, status) -> list:
        pub._client.publish.reset_mock()
        await pub.publish_gateway("test-gw", status)
        return [
            c.args[0]
            for c in pub._client.publish.call_args_list
            if c.args[0].startswith("homeassistant/")
        ]

    @pytest.mark.asyncio
    async def test_remote_meter_discovered_when_it_appears_later(self, monkeypatch):
        pub = self._make_publisher(monkeypatch)
        first = await self._discovery_topics(pub, _status_with(vitals=None))
        assert len(first) == BASE_ENTITY_COUNT
        assert not [t for t in first if "remote_meter" in t]

        second = await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS))
        assert len([t for t in second if "remote_meter" in t]) == 5

        # A later snapshot without vitals doesn't re-send anything
        assert await self._discovery_topics(pub, _status_with(vitals=None)) == []
        # Nor does the same data again
        assert await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS)) == []

    @pytest.mark.asyncio
    async def test_strings_discovered_when_they_appear_later(self, monkeypatch):
        pub = self._make_publisher(monkeypatch)
        first = await self._discovery_topics(pub, _status_with(strings=None))
        assert not [t for t in first if "_string_" in t]

        second = await self._discovery_topics(pub, _status_with(strings=_STRINGS))
        assert [t for t in second if "_string_" in t]

        assert await self._discovery_topics(pub, _status_with(strings=None)) == []

    @pytest.mark.asyncio
    async def test_alternating_partial_snapshots_do_not_flap(self, monkeypatch):
        """Strings-only and meters-only snapshots alternating (e.g. strings and
        vitals timing out on different polls) re-send nothing once both are
        announced."""
        pub = self._make_publisher(monkeypatch)
        await self._discovery_topics(pub, _status_with(strings=_STRINGS))
        assert await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS))
        assert await self._discovery_topics(pub, _status_with(strings=_STRINGS)) == []
        assert await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS)) == []

    @pytest.mark.asyncio
    async def test_reconnect_resends_everything_known(self, monkeypatch):
        pub = self._make_publisher(monkeypatch)
        await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS))
        pub._discovery_sent.clear()  # the connection loop does this on reconnect
        again = await self._discovery_topics(pub, _status_with(vitals=_TRM_VITALS))
        assert len(again) == BASE_ENTITY_COUNT + 5


# ---------------------------------------------------------------------------
# Per-unit device signals (temperatures, fans)
# ---------------------------------------------------------------------------

# Realistic PW3 site: one main unit (TEPOD pack temps + TEPINV fans) plus a
# PW2 unit (TETHC controller temp + PVAC fan). serialNumber is preferred when
# present; the key suffix otherwise.
_DEVICE_VITALS = {
    "TEPOD--1081100-38-F--TG2312H0001": {
        "serialNumber": "TG2312H0001",
        "HVP_PackTempMax": 23.5,
        "HVP_PackTempMin": 22.1,
        "HVP_ShuntTemperature": 24.0,
    },
    "TEPINV--1707000-21-M--TG2312H0001": {
        "serialNumber": "TG2312H0001",
        "PCH_AmbientTemp": 31.2,
        "PCH_FanSpeed_A": 1200,
        "PCH_FanDuty_A": 35.5,
        "PCH_FanSpeed_B": 1180.0,
        "PCH_FanDuty_B": "33.2",
    },
    "TETHC--1081100-08-C--TG123456789": {
        "serialNumber": "TG123456789",
        "THC_AmbientTemp": 25.0,
    },
    "PVAC--1081100-08-C--TG123456789": {
        "PVAC_Fan_Speed_Actual_RPM": 810,
        "PVAC_Fan_Speed_Target_RPM": 900,
    },
}


class TestExtractDeviceSignals:
    """Unit tests for extract_device_signals() - normalizes vitals +
    get_fan_speeds() into per-unit temperature/fan signals keyed by serial."""

    def test_pw3_unit(self):
        """A PW3 unit yields pack temps, shunt, inverter ambient and both fans."""
        result = extract_device_signals(_DEVICE_VITALS, None)
        pw3 = result["TG2312H0001"]
        assert pw3["temp_pack_max"] == 23.5
        assert pw3["temp_pack_min"] == 22.1
        assert pw3["temp_shunt"] == 24.0
        assert pw3["temp_ambient"] == 31.2
        assert pw3["fan_a_rpm"] == 1200.0
        assert pw3["fan_a_duty"] == 35.5
        assert pw3["fan_b_rpm"] == 1180.0
        assert pw3["fan_b_duty"] == 33.2  # string value coerced to float

    def test_pw2_unit(self):
        """A PW2 unit yields the controller temp and a single fan (no duty)."""
        result = extract_device_signals(_DEVICE_VITALS, None)
        pw2 = result["TG123456789"]
        assert pw2["temp_controller"] == 25.0
        assert pw2["fan_rpm"] == 810.0
        assert pw2["fan_target_rpm"] == 900.0
        assert "fan_a_rpm" not in pw2
        assert "fan_a_duty" not in pw2

    def test_serial_falls_back_to_key_suffix(self):
        """Without a serialNumber field, the last '--' segment of the device
        key identifies the unit."""
        vitals = {
            "TETHC--1081100-08-C--TG999999999": {"THC_AmbientTemp": 21.0},
        }
        result = extract_device_signals(vitals, None)
        assert result == {"TG999999999": {"temp_controller": 21.0}}

    def test_fan_speeds_payload_fills_missing_signals(self):
        """The get_fan_speeds() payload only fills signals vitals did not
        report - vitals values win on conflict."""
        vitals = {
            "TETHC--1081100-08-C--TG123456789": {"THC_AmbientTemp": 25.0},
        }
        fan_speeds = {
            "PVAC--1081100-08-C--TG123456789": {
                "PVAC_Fan_Speed_Actual_RPM": 812,
                "PVAC_Fan_Speed_Target_RPM": 905,
            }
        }
        result = extract_device_signals(vitals, fan_speeds)
        assert result["TG123456789"]["fan_rpm"] == 812.0
        assert result["TG123456789"]["fan_target_rpm"] == 905.0

        # With vitals fan readings present, the fan_speeds value loses
        fan_speeds["PVAC--1081100-08-C--TG123456789"]["PVAC_Fan_Speed_Actual_RPM"] = 1
        vitals["PVAC--1081100-08-C--TG123456789"] = {"PVAC_Fan_Speed_Actual_RPM": 810}
        result = extract_device_signals(vitals, fan_speeds)
        assert result["TG123456789"]["fan_rpm"] == 810.0

    def test_tepinv_wins_over_same_serial_pvac(self):
        """A PW3 unit also has a PVAC block (no fan readings); processing
        order means TEPINV fans always win, whatever the key order."""
        vitals = {
            "PVAC--1081100-38-F--TG2312H0001": {
                "PVAC_Fan_Speed_Actual_RPM": 700,  # must not win
            },
            "TEPINV--1707000-21-M--TG2312H0001": {
                "serialNumber": "TG2312H0001",
                "PCH_FanSpeed_A": 1200,
            },
        }
        result = extract_device_signals(vitals, None)
        assert result["TG2312H0001"]["fan_a_rpm"] == 1200.0
        assert "fan_rpm" not in result["TG2312H0001"]

    def test_none_or_malformed_input(self):
        assert extract_device_signals(None, None) == {}
        assert extract_device_signals({}, {}) == {}
        assert extract_device_signals({"TEPOD--x": "not a dict"}, None) == {}

    def test_none_and_non_numeric_values_dropped(self):
        vitals = {
            "TEPINV--1707000-21-M--TG2312H0001": {
                "serialNumber": "TG2312H0001",
                "PCH_FanSpeed_A": None,
                "PCH_FanDuty_A": "not-a-number",
                "PCH_FanSpeed_B": 5.0,
            },
        }
        result = extract_device_signals(vitals, None)
        assert result == {"TG2312H0001": {"fan_b_rpm": 5.0}}

    @pytest.mark.parametrize("serial", ["TG1/23", "TG1+23", "TG1#23", ""])
    def test_serial_with_topic_wildcards_skipped(self, serial):
        """The serial is an MQTT topic level: '/', '+', '#' and '' would break
        the publish, so such blocks are skipped."""
        vitals = {f"TETHC--part--{serial}": {"THC_AmbientTemp": 21.0}}
        assert extract_device_signals(vitals, None) == {}

    def test_unrelated_blocks_ignored(self):
        vitals = {
            "STSTSM--1081100-08-C--GW123456789": {"GatewayLoad": 5.0},
            "NEURIO--VAH1234AB1234": {"NEURIO_CT0_InstVoltage": 120.0},
        }
        assert extract_device_signals(vitals, None) == {}

    def test_malformed_fan_speeds_keys_ignored(self):
        """fan_speeds keys need at least '<type>--<part>--<serial>'."""
        assert extract_device_signals(None, {"PVAC--TG123456789": {"PVAC_Fan_Speed_Actual_RPM": 1}}) == {}
        assert extract_device_signals(None, {"TETHC--a--b--TG1": {"THC_AmbientTemp": 1}}) == {}


class TestDeviceSignalSensors:
    """Discovery payloads for per-unit temperature/fan sensors."""

    def test_no_device_sensors_when_absent(self):
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
        )
        assert [t for t, _ in results if "_device_" in t] == []
        assert len(results) == BASE_ENTITY_COUNT

    def test_pw3_unit_sensors(self):
        """A PW3 unit: 4 temperature + 4 fan sensors."""
        signals = extract_device_signals(_DEVICE_VITALS, None)
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            device_signals={"TG2312H0001": signals["TG2312H0001"]},
        )
        payloads = {t: json.loads(p) for t, p in results}
        device_entries = {
            t: p for t, p in payloads.items() if "_device_tg2312h0001_" in t
        }
        assert len(device_entries) == 8
        assert len(results) == BASE_ENTITY_COUNT + 8

        topic = (
            "homeassistant/sensor/pypowerwall_home_device_tg2312h0001_temp_pack_max/config"
        )
        assert topic in payloads
        p = payloads[topic]
        assert p["unit_of_measurement"] == "°C"
        assert p["device_class"] == "temperature"
        assert p["state_class"] == "measurement"
        assert (
            p["state_topic"] == "pypowerwall/home/devices/TG2312H0001/temperature/pack_max"
        )
        assert p["entity_category"] == "diagnostic"
        assert "TG2312H0001" in p["name"]
        assert "Pack Temp Max" in p["name"]

        fan = payloads[
            "homeassistant/sensor/pypowerwall_home_device_tg2312h0001_fan_a_rpm/config"
        ]
        assert fan["unit_of_measurement"] == "rpm"
        assert "device_class" not in fan
        assert fan["state_topic"] == "pypowerwall/home/devices/TG2312H0001/fan/a/rpm"

    def test_pw2_unit_sensors(self):
        """A PW2 unit: controller temp + fan speed/target, no duty sensors."""
        signals = extract_device_signals(_DEVICE_VITALS, None)
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            device_signals={"TG123456789": signals["TG123456789"]},
        )
        payloads = {t: json.loads(p) for t, p in results}
        device_entries = {
            t: p for t, p in payloads.items() if "_device_tg123456789_" in t
        }
        assert len(device_entries) == 3
        assert len(results) == BASE_ENTITY_COUNT + 3
        # No PW3 fan duty sensors for a PW2 unit
        assert not [t for t in device_entries if "duty" in t]

    def test_partial_signals_only_discover_what_exists(self):
        """An expansion pack (pack temps, no inverter) gets only its temps."""
        results = build_discovery_payloads(
            gateway_id="home",
            gateway_name="Home",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            device_signals={"TG2312H0002": {"temp_pack_max": 22.0, "temp_pack_min": 21.0}},
        )
        device_topics = [t for t, _ in results if "_device_" in t]
        assert len(device_topics) == 2
        state_topics = [json.loads(p)["state_topic"] for t, p in results if "_device_" in t]
        assert all("temperature" in t for t in state_topics)

    def test_device_sensors_have_availability(self):
        """Device sensors share the gateway availability topics like all others."""
        signals = extract_device_signals(_DEVICE_VITALS, None)
        results = build_discovery_payloads(
            gateway_id="main",
            gateway_name="Main",
            topic_prefix="pw",
            ha_prefix="homeassistant",
            device_signals=signals,
        )
        for topic, payload_str in results:
            if "_device_" not in topic:
                continue
            p = json.loads(payload_str)
            topics = {a["topic"] for a in p["availability"]}
            assert "pw/main/availability" in topics
            assert "pw/availability" in topics


class TestDiscoverySignatureDevices:
    """discovery_signature() includes per-unit device signals so late-seen
    units are discovered exactly once."""

    def test_device_signals_in_signature(self):
        sig = discovery_signature(None, _DEVICE_VITALS, None)
        assert ("device", "TG2312H0001", "temp_pack_max") in sig
        assert ("device", "TG123456789", "fan_rpm") in sig
        assert len(sig) == 11  # 8 PW3 signals + 3 PW2 signals

    def test_empty_inputs(self):
        assert discovery_signature(None, None, None) == frozenset()

    def test_fan_speeds_only_contributes(self):
        """fan_speeds alone (no vitals) still yields device entities."""
        sig = discovery_signature(
            None,
            None,
            {"PVAC--1081100-08-C--TG123456789": {"PVAC_Fan_Speed_Actual_RPM": 810}},
        )
        assert ("device", "TG123456789", "fan_rpm") in sig


class TestDeviceSignalsLateDiscovery:
    """Device temperature/fan signals first reported on a later poll (e.g.
    after the first poll's vitals timed out) must still be discovered - once."""

    def _make_publisher(self, monkeypatch) -> MqttPublisher:
        from app.config import settings

        monkeypatch.setattr(settings, "mqtt_host", "localhost")
        monkeypatch.setattr(settings, "mqtt_port", 1883)
        monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
        monkeypatch.setattr(settings, "mqtt_qos", 1)
        monkeypatch.setattr(settings, "mqtt_retain", True)
        monkeypatch.setattr(settings, "mqtt_ha_discovery", True)
        monkeypatch.setattr(settings, "mqtt_ha_prefix", "homeassistant")
        pub = MqttPublisher()
        pub._client = AsyncMock()
        pub._connected = True
        return pub

    async def _discovery_topics(self, pub: MqttPublisher, status) -> list:
        pub._client.publish.reset_mock()
        await pub.publish_gateway("test-gw", status)
        return [
            c.args[0]
            for c in pub._client.publish.call_args_list
            if c.args[0].startswith("homeassistant/")
        ]

    @pytest.mark.asyncio
    async def test_devices_discovered_when_vitals_appear_later(self, monkeypatch):
        pub = self._make_publisher(monkeypatch)
        first = await self._discovery_topics(pub, make_status())
        assert len(first) == BASE_ENTITY_COUNT
        assert not [t for t in first if "_device_" in t]

        status = make_status()
        status.data.vitals = _DEVICE_VITALS
        second = await self._discovery_topics(pub, status)
        # 8 PW3-unit signals + 3 PW2-unit signals
        assert len([t for t in second if "_device_" in t]) == 11

        # A later snapshot without vitals doesn't re-send anything
        assert await self._discovery_topics(pub, make_status()) == []
