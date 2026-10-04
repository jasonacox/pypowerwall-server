"""Canonical Powerwall temperature/fan signal registry.

One catalogue, in #130's shape and with #130's metric ids, shared by every
feature that reads these signals (MQTT publishing + Home Assistant
discovery, the history time-series store). Metric ids are permanent once
released: they become Home Assistant unique IDs and stored history series,
so renaming one later would orphan entities and data.

Adding a metric: one entry in ``SIGNAL_METRICS`` (plus ``SIGNAL_GROUPS`` for
a new group), no schema change - and each consumer's presentation for it.
Today that is MQTT's topic suffix and icon in
``app.mqtt.ha_discovery.DEVICE_METRIC_TOPICS`` (``test_map_covers_registry``
fails until it is added); the history page needs nothing more.

This module must stay dependency-free (standard library only, no MQTT or
web imports) so the history store can import it without pulling in MQTT.
"""

import math
from typing import Any, Dict, Optional

# Chart/label groups, ordered by ``order``. ``zero_based`` starts the y-axis
# at 0 (speeds, duty cycles) instead of fitting the data; ``decimals`` is the
# precision consumers show.
SIGNAL_GROUPS: Dict[str, Dict[str, Any]] = {
    "temperature": {
        "label": "Powerwall temperatures",
        "order": 10,
        "zero_based": False,
        "decimals": 1,
    },
    "fan_speed": {"label": "Fan speed", "order": 20, "zero_based": True, "decimals": 0},
    "fan_duty": {
        "label": "Fan duty cycle",
        "order": 30,
        "zero_based": True,
        "decimals": 1,
    },
}

# One entry per metric: the pypowerwall vitals / fan_speeds signal names it
# records (per device block: TEPOD--, TEPINV--, TETHC--, PVAC--, so each
# Powerwall unit gets its own series), its label, unit, group and order
# within the group. PCH_heatsinkTemp is deliberately absent: it reads a
# constant 45.45 °C on current PW3 firmware.
SIGNAL_METRICS: Dict[str, Dict[str, Any]] = {
    # Powerwall 3 battery (TEPOD blocks)
    "pack_temp_max": {
        "signals": ["HVP_PackTempMax"],
        "label": "Pack temp (max)",
        "unit": "°C",
        "group": "temperature",
        "order": 10,
    },
    "pack_temp_min": {
        "signals": ["HVP_PackTempMin"],
        "label": "Pack temp (min)",
        "unit": "°C",
        "group": "temperature",
        "order": 20,
    },
    "shunt_temp": {
        "signals": ["HVP_ShuntTemperature"],
        "label": "Shunt temp",
        "unit": "°C",
        "group": "temperature",
        "order": 30,
    },
    # Powerwall 3 inverter (TEPINV blocks, vitals and fan_speeds)
    "inverter_ambient": {
        "signals": ["PCH_AmbientTemp"],
        "label": "Inverter ambient",
        "unit": "°C",
        "group": "temperature",
        "order": 40,
    },
    # Powerwall 2/+ thermal controller (TETHC blocks)
    "controller_ambient": {
        "signals": ["THC_AmbientTemp"],
        "label": "Thermal controller ambient",
        "unit": "°C",
        "group": "temperature",
        "order": 50,
    },
    "fan_a_rpm": {
        "signals": ["PCH_FanSpeed_A"],
        "label": "Fan A speed",
        "unit": "rpm",
        "group": "fan_speed",
        "order": 10,
    },
    "fan_b_rpm": {
        "signals": ["PCH_FanSpeed_B"],
        "label": "Fan B speed",
        "unit": "rpm",
        "group": "fan_speed",
        "order": 20,
    },
    # Powerwall+ inverter fan (PVAC blocks)
    "fan_rpm": {
        "signals": ["PVAC_Fan_Speed_Actual_RPM"],
        "label": "Fan speed",
        "unit": "rpm",
        "group": "fan_speed",
        "order": 30,
    },
    # Powerwall+ inverter fan target (PVAC blocks)
    "fan_target_rpm": {
        "signals": ["PVAC_Fan_Speed_Target_RPM"],
        "label": "Fan target speed",
        "unit": "rpm",
        "group": "fan_speed",
        "order": 40,
    },
    "fan_a_duty": {
        "signals": ["PCH_FanDuty_A"],
        "label": "Fan A duty",
        "unit": "%",
        "group": "fan_duty",
        "order": 10,
    },
    "fan_b_duty": {
        "signals": ["PCH_FanDuty_B"],
        "label": "Fan B duty",
        "unit": "%",
        "group": "fan_duty",
        "order": 20,
    },
}

# Derived lookup used when recording: signal name -> metric id.
SIGNAL_TO_METRIC: Dict[str, str] = {
    signal: metric
    for metric, entry in SIGNAL_METRICS.items()
    for signal in entry["signals"]
}


def signal_value(value: Any) -> Optional[float]:
    """Return ``value`` as a finite float, or None.

    Rejects booleans (bool is an int subclass), non-numbers and NaN/inf:
    a ``"nan"`` or ``inf`` string must never reach MQTT topics or JSON.
    Never raises - a single malformed value (e.g. an int too large for a
    float, raising OverflowError) must not cost the gateway its MQTT.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _valid_serial(serial: Any) -> Optional[str]:
    """Pass through a usable serial; None for empty/topic-hostile values."""
    if not (isinstance(serial, str) and serial):
        return None
    if any(ch in serial for ch in "/+#"):
        return None
    return serial


def _block_serial(key: str, block: Dict[str, Any]) -> Optional[str]:
    """Resolve the unit serial for a device block.

    Prefers the block's own serialNumber field (as the web console does);
    falls back to the last "--" segment of the device key.
    """
    serial = _valid_serial(block.get("serialNumber"))
    if serial is not None:
        return serial
    parts = key.split("--")
    if len(parts) < 2:
        return None
    return _valid_serial(parts[-1])


def _apply_block(signals: Dict[str, float], block: Dict[str, Any]) -> None:
    """Copy known catalogue signals out of a device block (first writer wins)."""
    for field, metric in SIGNAL_TO_METRIC.items():
        if metric in signals:
            continue
        value = signal_value(block.get(field))
        if value is not None:
            signals[metric] = value


_FAN_SPEEDS_PREFIXES = ("PVAC", "TEPINV")


def tepinv_serials(
    vitals: Optional[Dict[str, Any]], fan_speeds: Optional[Dict[str, Any]]
) -> set:
    """Serials that have a TEPINV (PW3) block in either source.

    get_fan_speeds() returns PVAC entries before TEPINV, and vitals blocks
    arrive in gateway order, so neither source alone can be trusted to put
    TEPINV first. Collecting the serials up front (from both sources) means
    a same-serial PVAC block never contributes its PW2-style fan readings
    to a PW3 unit, whatever the block order - that would create duplicate
    fan entities for one unit.
    """
    serials: set = set()
    for source, from_key_only in ((vitals, False), (fan_speeds, True)):
        if not isinstance(source, dict):
            continue
        for key, block in source.items():
            if not (isinstance(key, str) and key.startswith("TEPINV--")):
                continue
            if from_key_only:
                parts = key.split("--")
                if len(parts) >= 3:
                    serial = _valid_serial(parts[-1])
                    if serial is not None:
                        serials.add(serial)
            elif isinstance(block, dict):
                serial = _block_serial(key, block)
                if serial is not None:
                    serials.add(serial)
    return serials


def _unit_signals(
    blocks: Any,
    serial_from_key_only: bool = False,
    prefixes: tuple = ("TEPOD", "TEPINV", "TETHC", "PVAC"),
    pw3_serials: Optional[set] = None,
) -> Dict[str, Dict[str, float]]:
    """Fold an ordered sequence of (key, block) device blocks per unit serial.

    PVAC blocks for a serial in ``pw3_serials`` are skipped entirely: that
    unit already has PW3 (TEPINV) fan reporting.
    """
    devices: Dict[str, Dict[str, float]] = {}
    for key, block in blocks:
        if not isinstance(key, str) or not isinstance(block, dict):
            continue
        prefix = key.split("--", 1)[0]
        if prefix not in prefixes:
            continue
        if serial_from_key_only:
            parts = key.split("--")
            if len(parts) < 3:
                continue
            serial = _valid_serial(parts[-1])
        else:
            serial = _block_serial(key, block)
        if serial is None:
            continue
        if prefix == "PVAC" and pw3_serials and serial in pw3_serials:
            continue  # a PW3 unit's PVAC block adds nothing
        signals = devices.setdefault(serial, {})
        _apply_block(signals, block)
    return devices


def extract_unit_signals(
    vitals: Optional[Dict[str, Any]],
    fan_speeds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, float]]:
    """Extract per-Powerwall-unit temperature and fan readings.

    Combines a ``pw.vitals()`` payload with the ``get_fan_speeds()`` payload
    cached by the poll loop, normalized to canonical metric ids (see
    ``SIGNAL_METRICS``) and keyed by unit serial - the same keying as the
    web console's Powerwall Status table:

        {"TG123456789H1234": {"pack_temp_max": 23.5, "fan_a_rpm": 1200.0, ...}}

    Vitals is the primary source (PW3: TEPOD pack temps, TEPINV ambient +
    fans; PW2/2+: TETHC controller temp, PVAC fan).  The fan_speeds payload
    only fills signals vitals did not report this poll (its keys carry the
    serial as the last "--" segment).  A serial with a TEPINV block in
    either source never takes fan readings from its PVAC block, whatever
    the block order (see ``tepinv_serials``).

    Returns {} for missing/malformed input - never raises.  Units that end
    up with no readings are dropped.
    """
    pw3_serials = tepinv_serials(vitals, fan_speeds)
    devices: Dict[str, Dict[str, float]] = {}
    if isinstance(vitals, dict):
        devices = _unit_signals(vitals.items(), pw3_serials=pw3_serials)
    if isinstance(fan_speeds, dict):
        # Only fills gaps: vitals-reported signals are never overwritten.
        for serial, signals in _unit_signals(
            fan_speeds.items(), True, _FAN_SPEEDS_PREFIXES, pw3_serials
        ).items():
            existing = devices.setdefault(serial, {})
            for metric, value in signals.items():
                existing.setdefault(metric, value)
    return {serial: signals for serial, signals in devices.items() if signals}
