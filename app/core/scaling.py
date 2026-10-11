"""Helpers for Tesla-style battery percentage scaling."""

import math
from typing import Any, Optional


TESLA_BATTERY_RESERVE_FLOOR = 5.0
TESLA_BATTERY_USABLE_RANGE = 100.0 - TESLA_BATTERY_RESERVE_FLOOR


def raw_to_tesla_battery_percent(raw_percent: Optional[float]) -> Optional[float]:
    """Convert raw 5-100% SOE to the Tesla app's 0-100% display scale."""
    if raw_percent is None:
        return None
    scaled = (
        (raw_percent - TESLA_BATTERY_RESERVE_FLOOR)
        / TESLA_BATTERY_USABLE_RANGE
        * 100.0
    )
    return max(0.0, min(100.0, scaled))


def tesla_to_raw_battery_percent(display_percent: Optional[float]) -> Optional[float]:
    """Convert a Tesla app 0-100% display value back to raw SOE."""
    if display_percent is None:
        return None
    raw = (
        display_percent / 100.0 * TESLA_BATTERY_USABLE_RANGE
        + TESLA_BATTERY_RESERVE_FLOOR
    )
    return max(TESLA_BATTERY_RESERVE_FLOOR, min(100.0, raw))


def resolve_hold_level(status: Any) -> Optional[int]:
    """Resolve a hold level (backup reserve = SoC) from a gateway status.

    Returns the cached state of charge rounded *down* to a whole percent,
    or None when there is no usable reading. Rounding down never sets a
    reserve above the actual charge. Only a live (online) poll counts:
    stale graceful-degradation data must never freeze a reserve.
    """
    if not (
        status
        and getattr(status, "online", False)
        and status.data
        and status.data.soe is not None
    ):
        return None
    try:
        level = int(math.floor(float(status.data.soe)))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(100, level))
