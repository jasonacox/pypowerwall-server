"""Tesla Cloud lifetime energy aggregation and aggregate overlay.

The Tesla Cloud lifetime endpoint returns a time series of energy buckets, not
a ready-made lifetime total. This module sums the buckets into the Wh values
used by the existing meter aggregate sections, and overlays only the fields
that are actually present in the response. Power, voltage and current values
are deliberately left untouched.
"""

from __future__ import annotations

import math
from typing import Any, Dict

CLOUD_ENERGY_FIELDS = {
    "grid_import": "site.energy_imported",
    "grid_export": "site.energy_exported",
    "solar_generation": "solar.energy_exported",
    "battery_charge": "battery.energy_imported",
    "battery_discharge": "battery.energy_exported",
    "home_usage": "load.energy_imported",
}


class CloudEnergyError(RuntimeError):
    """Raised when Tesla Cloud lifetime energy cannot be read."""


def _first_number(row: Dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            value = float(value)
            if math.isfinite(value):
                return value
    return 0.0


def summarize_cloud_energy(payload: Any) -> Dict[str, float]:
    """Sum the lifetime time series into meter energy counters.

    Returns only counters that were present in at least one bucket. An empty,
    all-zero or non-finite response is rejected so a broken Cloud response can
    never reset an already valid lifetime counter.
    """
    if isinstance(payload, dict):
        payload = payload.get("response", payload)
    if not isinstance(payload, dict):
        raise CloudEnergyError("Tesla Cloud energy response is not an object")
    series = payload.get("time_series")
    if not isinstance(series, list) or not series:
        raise CloudEnergyError("Tesla Cloud energy response has no time series")

    totals = {key: 0.0 for key in CLOUD_ENERGY_FIELDS}
    present = set()
    for row in series:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("grid_energy_imported"), (int, float)):
            present.add("grid_import")
        totals["grid_import"] += _first_number(row, ("grid_energy_imported",))
        export_sources = (
            "grid_energy_exported_from_solar",
            "grid_energy_exported_from_battery",
            "grid_energy_exported_from_generator",
        )
        if any(isinstance(row.get(key), (int, float)) for key in export_sources):
            present.add("grid_export")
            totals["grid_export"] += sum(
                _first_number(row, (key,)) for key in export_sources
            )
        elif isinstance(row.get("total_grid_energy_exported"), (int, float)):
            present.add("grid_export")
            totals["grid_export"] += _first_number(row, ("total_grid_energy_exported",))
        if any(
            isinstance(row.get(key), (int, float))
            for key in ("solar_energy_exported", "total_solar_generation")
        ):
            present.add("solar_generation")
        totals["solar_generation"] += _first_number(
            row, ("solar_energy_exported", "total_solar_generation")
        )
        charge_sources = (
            "battery_energy_imported_from_grid",
            "battery_energy_imported_from_solar",
            "battery_energy_imported_from_generator",
        )
        if any(isinstance(row.get(key), (int, float)) for key in charge_sources):
            present.add("battery_charge")
            totals["battery_charge"] += sum(
                _first_number(row, (key,)) for key in charge_sources
            )
        elif isinstance(row.get("total_battery_charge"), (int, float)):
            present.add("battery_charge")
            totals["battery_charge"] += _first_number(row, ("total_battery_charge",))
        if any(
            isinstance(row.get(key), (int, float))
            for key in ("battery_energy_exported", "total_battery_discharge")
        ):
            present.add("battery_discharge")
        totals["battery_discharge"] += _first_number(
            row, ("battery_energy_exported", "total_battery_discharge")
        )
        if isinstance(row.get("total_home_usage"), (int, float)):
            present.add("home_usage")
        totals["home_usage"] += _first_number(row, ("total_home_usage",))
    if not present or not any(totals[key] > 0 for key in present):
        raise CloudEnergyError("Tesla Cloud energy response contains only zeros")
    return {key: totals[key] for key in present}


def fetch_cloud_energy(cloud: Any) -> Dict[str, float]:
    """Fetch and aggregate lifetime energy from a pypowerwall cloud client.

    Supports the Cloud owner client through its site API and FleetAPI through
    the FleetAPI history adapter. Callers must run this blocking helper in the
    executor, never directly on the event loop.
    """
    client = getattr(cloud, "client", None)
    site_api = getattr(client, "_site_api", None)
    if site_api is not None:
        response, _cached = site_api(
            "CALENDAR_HISTORY_DATA",
            0,
            True,
            kind="energy",
            period="lifetime",
        )
        return summarize_cloud_energy(response)
    fleet = getattr(client, "fleet", None)
    get_history = getattr(fleet, "get_calendar_history", None)
    if get_history is not None:
        return summarize_cloud_energy(get_history(kind="energy", duration="lifetime"))
    raise CloudEnergyError(
        "pypowerwall cloud client does not expose lifetime energy history"
    )


def apply_cloud_energy(aggregates: Dict[str, Any], totals: Dict[str, float]) -> None:
    """Overlay present cloud lifetime values onto existing meter aggregates.

    Missing totals and non-finite values are ignored. Existing power and other
    non-energy fields are never modified.
    """
    for total_key, target in CLOUD_ENERGY_FIELDS.items():
        value = totals.get(total_key)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        section_name, field_name = target.split(".", 1)
        section = aggregates.get(section_name)
        if isinstance(section, dict):
            section[field_name] = float(value)
