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

Remote meter sensors (when remote_meters provided — Tesla wireless CT meters,
config.json type "trm_mb", surfaced by pypowerwall as TRM--<din> vitals blocks):
    meters/remote/{din}/ct{n}/voltage         — CT voltage (V)
    meters/remote/{din}/ct{n}/current         — CT current (A)
    meters/remote/{din}/ct{n}/power           — CT real power (W)
    meters/remote/{din}/ct{n}/energy_imported — CT lifetime energy imported (Wh, total_increasing)
    meters/remote/{din}/ct{n}/energy_exported — CT lifetime energy exported (Wh, total_increasing)

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
    grid_export — Grid export policy (battery_ok | pv_only | never)

Binary sensor:
    online      — Gateway connection status
    grid_connected — Grid connected (true when grid_status=="UP", device_class=connectivity)
    grid_charging — Grid charging allowed (true/false, generic On/Off, no device class)

Numeric sensors:
    time_remaining — Backup time remaining (h, device_class=duration)

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
import re
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

# Matches the per-CT fields pypowerwall flattens onto each TRM--<din> vitals
# block, e.g. "TRM_CT0_InstVoltage" -> ct index "0", metric "InstVoltage".
_TRM_CT_FIELD_RE = re.compile(r"^TRM_CT(\d+)_(.+)$")


def extract_remote_meters(
    vitals: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Parse Tesla Remote Meter data out of a pw.vitals() payload.

    pypowerwall surfaces each wireless CT remote meter (config.json type
    "trm_mb") as a flat "TRM--<din>" block with "TRM_CT{n}_<Metric>" fields
    per active CT (there can be more than one CT per meter, and more than
    one remote meter per gateway). This regroups that flat shape into
    {din: {ct_index: {metric: value}}}, e.g.:

        {"2002069-00-E--EM4260230B10BC": {"0": {"InstVoltage": 122.7,
                                                  "InstCurrent": 0.95,
                                                  "InstRealPower": 158.3,
                                                  "Location": "solar", ...}}}

    Shared by ha_discovery (to build sensor definitions) and the publisher
    (to publish the actual values) so the TRM-key parsing lives in one place.
    Returns {} for missing/malformed input - never raises.
    """
    meters: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not isinstance(vitals, dict):
        return meters
    for key, block in vitals.items():
        if (
            not isinstance(key, str)
            or not key.startswith("TRM--")
            or not isinstance(block, dict)
        ):
            continue
        din = key[len("TRM--") :]
        # The DIN becomes an MQTT topic level: skip empty ones and any with
        # topic separators/wildcards, which a broker would reject on publish
        if not din or any(ch in din for ch in "/+#"):
            continue
        cts: Dict[str, Dict[str, Any]] = {}
        for field, value in block.items():
            if not isinstance(field, str):
                continue
            match = _TRM_CT_FIELD_RE.match(field)
            if not match:
                continue
            ct_index, metric = match.group(1), match.group(2)
            cts.setdefault(ct_index, {})[metric] = value
        if cts:
            meters[din] = cts
    return meters


def discovery_signature(
    strings: Optional[Dict[str, Any]], vitals: Optional[Dict[str, Any]]
) -> frozenset:
    """The optional (data-dependent) entities a snapshot would announce.

    Solar strings and remote-meter CTs are only discovered when a poll
    reports them. The publisher compares this signature with what it has
    already announced, so a family first seen on a later poll (e.g. after the
    first poll's vitals timed out) still gets discovered.
    """
    signature = set()
    if isinstance(strings, dict):
        signature.update(("string", sid) for sid in strings)
    for din, cts in extract_remote_meters(vitals).items():
        signature.update(("remote_meter", din, ct) for ct in cts)
    return frozenset(signature)


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
    remote_meters: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
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
        remote_meters: Tesla Remote Meter data as returned by
                       extract_remote_meters(pw.vitals()) - {din: {ct_index:
                       {metric: value}}}.  When provided, per-CT sensors are
                       added so HA auto-discovers each wireless CT meter.

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
        binary_sensor(
            "grid_connected", "Grid Connected",
            f"{data_prefix}/grid_connected",
            payload_on="true",
            payload_off="false",
            device_class="connectivity",
            icon="mdi:transmission-tower",
        ),
        # --- Grid charging (bool) ---
        binary_sensor(
            "grid_charging", "Grid Charging",
            f"{data_prefix}/grid_charging",
            payload_on="true",
            payload_off="false",
            device_class=None,
            icon="mdi:battery-charging-outline",
        ),
        # --- Text sensor: grid export policy ---
        sensor(
            "grid_export", "Grid Export",
            f"{data_prefix}/grid_export",
            unit=None,
            device_class=None,
            state_class=None,  # type: ignore[arg-type]
            icon="mdi:transmission-tower-export",
        ),
        # --- Time remaining (h) ---
        sensor(
            "time_remaining", "Time Remaining",
            f"{data_prefix}/time_remaining",
            unit="h",
            device_class="duration",
            state_class="measurement",
            icon="mdi:timer-outline",
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

    # --- Remote meter sensors (Tesla wireless CT meters, one or more CTs
    # per meter, one or more meters per gateway) ---
    if remote_meters:
        meters_prefix = f"{data_prefix}/meters/remote"
        _REMOTE_METER_METRICS = [
            ("voltage", "Voltage", "V", "voltage", "measurement", "mdi:lightning-bolt"),
            ("current", "Current", "A", "current", "measurement", "mdi:current-ac"),
            ("power", "Power", "W", "power", "measurement", "mdi:flash"),
            (
                "energy_imported",
                "Energy Imported",
                "Wh",
                "energy",
                "total_increasing",
                "mdi:transmission-tower-import",
            ),
            (
                "energy_exported",
                "Energy Exported",
                "Wh",
                "energy",
                "total_increasing",
                "mdi:transmission-tower-export",
            ),
        ]
        for din, cts in remote_meters.items():
            # din looks like "2002069-00-E--EM4260230B10BC" - use the serial
            # suffix after the last "--" for a shorter, still-unique label.
            short_id = din.rsplit("--", 1)[-1] or din
            din_slug = re.sub(r"[^a-z0-9]+", "_", din.lower()).strip("_")
            for ct_index, fields in cts.items():
                location = fields.get("Location")
                label = f"Remote Meter {short_id} CT{ct_index}"
                if location:
                    label = f"{label} ({location})"
                m_prefix = f"{meters_prefix}/{din}/ct{ct_index}"
                for (
                    metric,
                    metric_label,
                    unit,
                    dc,
                    state_class,
                    icon,
                ) in _REMOTE_METER_METRICS:
                    results.append(
                        sensor(
                            f"remote_meter_{din_slug}_ct{ct_index}_{metric}",
                            f"{label} {metric_label}",
                            f"{m_prefix}/{metric}",
                            unit=unit,
                            device_class=dc,
                            state_class=state_class,
                            icon=icon,
                            entity_category="diagnostic",
                        )
                    )

    return results
