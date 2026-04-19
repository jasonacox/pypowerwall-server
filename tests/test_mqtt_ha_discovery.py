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

from app.mqtt.ha_discovery import build_discovery_payloads
from app.mqtt.publisher import MqttPublisher
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


# ---------------------------------------------------------------------------
# Helpers (shared with test_mqtt_publisher.py pattern)
# ---------------------------------------------------------------------------

def make_status(
    gateway_id: str = "test-gw",
    gateway_name: str = "Test Gateway",
    version: str = "23.44.0",
    online: bool = True,
    soe: float = 75.0,
) -> GatewayStatus:
    gateway = Gateway(id=gateway_id, name=gateway_name, host="192.168.91.1", online=online)
    data = PowerwallData(
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
        # 9 sensors + 1 binary sensor = 10
        assert len(results) == 10

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
        assert len(binary_topics) == 1
        assert "online" in binary_topics[0]

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

    def test_availability_references_correct_topic(self):
        results = self._payloads(gateway_id="main", prefix="pw")
        for topic, payload in results:
            avail_list = payload.get("availability", [])
            assert len(avail_list) == 1
            assert avail_list[0]["topic"] == "pw/main/availability"
            assert avail_list[0]["payload_available"] == "online"
            assert avail_list[0]["payload_not_available"] == "offline"

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

    def test_version_none_handled(self):
        """build_discovery_payloads() must not crash when version is None."""
        results = build_discovery_payloads(
            gateway_id="gw",
            gateway_name="GW",
            topic_prefix="pypowerwall",
            ha_prefix="homeassistant",
            version=None,
        )
        assert len(results) == 10
        for _, payload_str in results:
            p = json.loads(payload_str)
            assert p["device"]["sw_version"] == "unknown"


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
        assert len(disc_topics) == 10  # one per sensor/binary_sensor

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
        assert len(disc_topics) == 10

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
