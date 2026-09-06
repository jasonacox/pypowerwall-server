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
    battery     — Tesla-scaled battery charge (%, device_class=battery)
    battery_raw — Raw battery charge (%)
    solar       — Solar power (W, device_class=power)
    grid        — Grid power (W, device_class=power, positive=importing)
    home        — Home load (W, device_class=power)
    powerwall   — Powerwall power (W, device_class=power, positive=discharging)
    reserve     — Backup reserve target (%)
    total_capacity — Total battery capacity (Wh)
    current_charge — Current battery charge (Wh)

Solar string sensors (when string_ids provided):
    strings/{id}/voltage  — String voltage (V, per string A–F or multi-PW3 A1–F2…)
    strings/{id}/current  — String current (A)
    strings/{id}/power    — String power (W)
    strings/{AB}/voltage  — Paired-string voltage (V, from first string in pair)
    strings/{AB}/current  — Paired-string current (A, sum of pair)
    strings/{AB}/power    — Paired-string power (W, sum of pair)

Lifetime energy sensors (Wh, device_class=energy, state_class=total_increasing):
    grid_energy_imported     — Grid energy imported, lifetime (from aggregates site)
    grid_energy_exported     — Grid energy exported, lifetime (from aggregates site)
    home_energy_imported     — Home energy consumption, lifetime (from aggregates load)
    solar_energy_exported    — Solar energy production, lifetime (from aggregates solar)
    battery_energy_imported  — Battery energy charged, lifetime
    battery_energy_exported  — Battery energy discharged, lifetime

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
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


def _device_block(gateway_id: str, gateway_name: str, version: Optional[str]) -> dict:
    """Build the shared HA device block for all sensors on this gateway."""
    return {
        "identifiers": [f"pypowerwall_{gateway_id}"],
        "name": gateway_name,
        "manufacturer": "Tesla",
        "model": "Powerwall",
        "sw_version": version or "unknown",
    }


def build_discovery_payloads(
    gateway_id: str,
    gateway_name: str,
    topic_prefix: str,
    ha_prefix: str,
    version: Optional[str] = None,
    string_ids: Optional[Sequence[str]] = None,
) -> list[tuple[str, str]]:
    """Build all HA auto-discovery (topic, payload) pairs for a gateway.

    Args:
        gateway_id:    Gateway identifier (slug used in topics/unique IDs).
        gateway_name:  Human-readable name shown in HA device card.
        topic_prefix:  MQTT topic prefix (e.g. "pypowerwall").
        ha_prefix:     Home Assistant discovery prefix (e.g. "homeassistant").
        version:       Powerwall firmware version string (optional).
        string_ids:    Solar string identifiers present on this gateway (e.g.
                       ["A", "B", "C", "D", "E", "F"] for a single PW3, or
                       ["A1", "B1", …, "F2"] for a multi-PW3 setup).  When
                       provided, per-string and paired-rollup sensors are added
                       to the discovery payloads so HA auto-discovers them.

    Returns:
        List of (topic, json_payload_str) tuples, one per sensor/binary sensor.
    """
    device = _device_block(gateway_id, gateway_name, version)
    data_prefix = f"{topic_prefix}/{gateway_id}"
    avail_topic = f"{data_prefix}/availability"

    # Both the per-gateway topic and the global LWT topic are included so that
    # HA marks entities unavailable when either the gateway goes offline
    # (per-gateway) OR the server crashes/disconnects (global LWT).
    # "all" mode: entity is available only when EVERY topic says "online".
    global_avail_topic = f"{topic_prefix}/availability"

    def avail() -> list:
        return [
            {"topic": avail_topic, "payload_available": "online", "payload_not_available": "offline"},
            {"topic": global_avail_topic, "payload_available": "online", "payload_not_available": "offline"},
        ]

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
            "availability_mode": "all",
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
            "availability_mode": "all",
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
            "battery_raw", "Battery Raw",
            f"{data_prefix}/battery_raw",
            unit="%",
            state_class="measurement",
            icon="mdi:battery-medium",
            entity_category="diagnostic",
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
        sensor(
            "total_capacity", "Total Battery Capacity",
            f"{data_prefix}/total_capacity",
            unit="Wh",
            device_class="energy_storage",
            state_class="measurement",
            icon="mdi:battery-high",
        ),
        sensor(
            "current_charge", "Current Battery Charge",
            f"{data_prefix}/current_charge",
            unit="Wh",
            device_class="energy_storage",
            state_class="measurement",
            icon="mdi:battery-medium",
        ),
        # --- Lifetime energy sensors (Wh, total_increasing) ---
        # Lifetime accumulators from /api/meters/aggregates — on PW3/TEDAPI
        # these are overlaid by pypowerwall>=0.16.5 from the gateway's native
        # local API.  state_class=total_increasing lets the HA Energy dashboard
        # chart them directly (daily stats are derived by delta, same as PW2).
        sensor(
            "grid_energy_imported", "Grid Energy Imported",
            f"{data_prefix}/grid_energy_imported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:transmission-tower-import",
        ),
        sensor(
            "grid_energy_exported", "Grid Energy Exported",
            f"{data_prefix}/grid_energy_exported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:transmission-tower-export",
        ),
        sensor(
            "home_energy_imported", "Home Energy Consumption",
            f"{data_prefix}/home_energy_imported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:home-lightning-bolt",
        ),
        sensor(
            "solar_energy_exported", "Solar Energy Production",
            f"{data_prefix}/solar_energy_exported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:solar-power",
        ),
        sensor(
            "battery_energy_imported", "Battery Energy Charged",
            f"{data_prefix}/battery_energy_imported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:battery-charging",
        ),
        sensor(
            "battery_energy_exported", "Battery Energy Discharged",
            f"{data_prefix}/battery_energy_exported",
            unit="Wh",
            device_class="energy",
            state_class="total_increasing",
            icon="mdi:battery-minus",
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

    # --- Solar string sensors (per-string + paired rollups) ---
    if string_ids:
        strings_prefix = f"{data_prefix}/strings"
        _STRING_METRICS = [
            ("voltage", "Voltage", "V",  "voltage", "mdi:lightning-bolt"),
            ("current", "Current", "A",  "current", "mdi:current-ac"),
            ("power",   "Power",   "W",  "power",   "mdi:solar-power-variant"),
        ]
        _PAIR_BASES = [("A", "B", "AB"), ("C", "D", "CD"), ("E", "F", "EF")]

        # Per-string sensors (A–F, A1–F1, A2–F2, …)
        for sid in string_ids:
            s_prefix = f"{strings_prefix}/{sid}"
            sid_slug = sid.lower()
            for metric, label, unit, dc, icon in _STRING_METRICS:
                results.append(sensor(
                    f"string_{sid_slug}_{metric}",
                    f"String {sid} {label}",
                    f"{s_prefix}/{metric}",
                    unit=unit,
                    device_class=dc,
                    state_class="measurement",
                    icon=icon,
                    entity_category="diagnostic",
                ))

        # Paired-string rollup sensors (AB, CD, EF + numbered variants)
        sid_set = set(string_ids)
        suffixes: set[str] = set()
        for sid in string_ids:
            base = sid.rstrip("0123456789")
            if base in ("A", "B", "C", "D", "E", "F"):
                suffixes.add(sid[len(base):])

        for suffix in sorted(suffixes):
            for first, second, pair_base in _PAIR_BASES:
                a_key = first + suffix
                b_key = second + suffix
                if a_key not in sid_set or b_key not in sid_set:
                    continue
                pair_name = pair_base + suffix.upper()
                p_prefix = f"{strings_prefix}/{pair_name}"
                pair_slug = pair_name.lower()
                for metric, label, unit, dc, icon in _STRING_METRICS:
                    results.append(sensor(
                        f"string_{pair_slug}_{metric}",
                        f"String {pair_name} {label}",
                        f"{p_prefix}/{metric}",
                        unit=unit,
                        device_class=dc,
                        state_class="measurement",
                        icon=icon,
                        entity_category="diagnostic",
                    ))

    return results
