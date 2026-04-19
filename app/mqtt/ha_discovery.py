"""
Home Assistant MQTT Discovery — builds and publishes auto-discovery payloads.

When MQTT_HA_DISCOVERY=true (default), calling publish_discovery() for a gateway
causes Home Assistant to automatically create a "Powerwall" device with all sensors
grouped under it — no manual YAML configuration needed.

Discovery topics follow the HA convention:
    {ha_prefix}/sensor/pypowerwall_{gateway_id}_{sensor}/config

Each payload is retained so HA re-reads it after restarts.

Sensor catalogue
----------------
Sensors (numeric):
    battery     — Battery charge (%, device_class=battery)
    solar       — Solar power (W, device_class=power)
    grid        — Grid power (W, device_class=power, positive=importing)
    home        — Home load (W, device_class=power)
    powerwall   — Powerwall power (W, device_class=power, positive=discharging)
    reserve     — Backup reserve target (%)

Text sensors:
    grid_status — "UP" | "DOWN" | "unknown"
    mode        — Operation mode string (e.g. "self_consumption", "backup")
    version     — Firmware version string

Binary sensor:
    online      — Gateway connection status

All sensors share a single "Powerwall" device block so HA groups them together.
The device model is set from PowerwallData.version when available, otherwise
"Powerwall".

References
----------
    https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
    https://www.home-assistant.io/integrations/sensor.mqtt/
    https://www.home-assistant.io/integrations/binary_sensor.mqtt/
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _device_block(gateway_id: str, gateway_name: str, version: Optional[str]) -> dict:
    """Build the shared HA device block for all sensors on this gateway."""
    return {
        "identifiers": [f"pypowerwall_{gateway_id}"],
        "name": gateway_name,
        "manufacturer": "Tesla",
        "model": "Powerwall",
        "sw_version": version or "unknown",
        "via_device": "pypowerwall-server",
    }


def build_discovery_payloads(
    gateway_id: str,
    gateway_name: str,
    topic_prefix: str,
    ha_prefix: str,
    version: Optional[str] = None,
) -> list[tuple[str, str]]:
    """Build all HA auto-discovery (topic, payload) pairs for a gateway.

    Args:
        gateway_id:    Gateway identifier (slug used in topics/unique IDs).
        gateway_name:  Human-readable name shown in HA device card.
        topic_prefix:  MQTT topic prefix (e.g. "pypowerwall").
        ha_prefix:     Home Assistant discovery prefix (e.g. "homeassistant").
        version:       Powerwall firmware version string (optional).

    Returns:
        List of (topic, json_payload_str) tuples, one per sensor/binary sensor.
    """
    device = _device_block(gateway_id, gateway_name, version)
    data_prefix = f"{topic_prefix}/{gateway_id}"
    avail_topic = f"{data_prefix}/availability"

    def avail() -> list:
        return [{"topic": avail_topic, "payload_available": "online", "payload_not_available": "offline"}]

    def sensor(
        uid_suffix: str,
        name: str,
        state_topic: str,
        unit: Optional[str] = None,
        device_class: Optional[str] = None,
        state_class: str = "measurement",
        icon: Optional[str] = None,
        entity_category: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build a single numeric/text sensor discovery entry."""
        unique_id = f"pypowerwall_{gateway_id}_{uid_suffix}"
        disc_topic = f"{ha_prefix}/sensor/{unique_id}/config"
        payload: dict = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": state_topic,
            "device": device,
            "availability": avail(),
            "availability_mode": "any",
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if state_class:
            payload["state_class"] = state_class
        if icon:
            payload["icon"] = icon
        if entity_category:
            payload["entity_category"] = entity_category
        return disc_topic, json.dumps(payload)

    def binary_sensor(
        uid_suffix: str,
        name: str,
        state_topic: str,
        payload_on: str = "true",
        payload_off: str = "false",
        device_class: Optional[str] = None,
        icon: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build a single binary sensor discovery entry."""
        unique_id = f"pypowerwall_{gateway_id}_{uid_suffix}"
        disc_topic = f"{ha_prefix}/binary_sensor/{unique_id}/config"
        payload: dict = {
            "name": name,
            "unique_id": unique_id,
            "state_topic": state_topic,
            "payload_on": payload_on,
            "payload_off": payload_off,
            "device": device,
            "availability": avail(),
            "availability_mode": "any",
        }
        if device_class:
            payload["device_class"] = device_class
        if icon:
            payload["icon"] = icon
        return disc_topic, json.dumps(payload)

    results: list[tuple[str, str]] = [
        # --- Numeric sensors ---
        sensor(
            "battery", "Battery",
            f"{data_prefix}/battery",
            unit="%",
            device_class="battery",
            state_class="measurement",
        ),
        sensor(
            "solar", "Solar Power",
            f"{data_prefix}/solar",
            unit="W",
            device_class="power",
            state_class="measurement",
            icon="mdi:solar-power",
        ),
        sensor(
            "grid", "Grid Power",
            f"{data_prefix}/grid",
            unit="W",
            device_class="power",
            state_class="measurement",
            icon="mdi:transmission-tower",
        ),
        sensor(
            "home", "Home Load",
            f"{data_prefix}/home",
            unit="W",
            device_class="power",
            state_class="measurement",
            icon="mdi:home-lightning-bolt",
        ),
        sensor(
            "powerwall", "Powerwall Power",
            f"{data_prefix}/powerwall",
            unit="W",
            device_class="power",
            state_class="measurement",
            icon="mdi:battery-charging",
        ),
        sensor(
            "reserve", "Backup Reserve",
            f"{data_prefix}/reserve",
            unit="%",
            state_class="measurement",
            icon="mdi:battery-lock",
        ),
        # --- Text sensors ---
        sensor(
            "grid_status", "Grid Status",
            f"{data_prefix}/grid_status",
            unit=None,
            device_class=None,
            state_class=None,  # type: ignore[arg-type]
            icon="mdi:transmission-tower",
        ),
        sensor(
            "mode", "Operation Mode",
            f"{data_prefix}/mode",
            unit=None,
            device_class=None,
            state_class=None,  # type: ignore[arg-type]
            icon="mdi:cog",
        ),
        sensor(
            "version", "Firmware Version",
            f"{data_prefix}/version",
            unit=None,
            device_class=None,
            state_class=None,  # type: ignore[arg-type]
            icon="mdi:information",
            entity_category="diagnostic",
        ),
        # --- Binary sensor ---
        binary_sensor(
            "online", "Gateway Online",
            f"{data_prefix}/online",
            payload_on="true",
            payload_off="false",
            device_class="connectivity",
            icon="mdi:lan-connect",
        ),
    ]
    return results
