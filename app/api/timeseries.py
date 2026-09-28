"""
Time-Series API Endpoints

REST API for daily energy statistics and raw sample access backed by the
TimeSeriesStore (SQLite, see app/core/timeseries.py).
All routes are prefixed with /api/timeseries (configured in main.py).

Routes:
    - GET /api/timeseries/daily   -> Daily kWh totals per gateway/category
    - GET /api/timeseries/today   -> Today's running totals for all gateways
    - GET /api/timeseries/trend   -> Bucketed kW + battery level (charting)
    - GET /api/timeseries/samples -> Raw samples (troubleshooting)
    - GET /api/timeseries/status  -> Subsystem status and DB sizing
    - GET /api/timeseries/signals -> Recorded Powerwall temperature/fan series
    - GET /api/timeseries/signal_trend -> Temperature/fan history for charts

Query Parameters:
    daily:   days (int, default 7)  — number of days back from today
             gateway (str, optional) — restrict to a single gateway ID
             start / end (YYYY-MM-DD, optional) — inclusive local-day range;
             either one replaces ``days`` and returns every stored day in
             the range (the History page's lookup)
    today:   none — returns every configured gateway's running totals
    trend:   hours (int, default 24, max 168) — window length; gateway
             (str, optional) restricts to one gateway. start (unix ts,
             optional) overrides hours with an explicit window start;
             end (unix ts, optional) explicit window end (default now);
             fit (bool, optional, default false) uses all retained raw
             data. Returns ~360 bucket-averaged points: solar/home/
             battery/grid kW plus mean battery level (%) per bucket.
    samples: gateway (str, optional), start (unix ts), end (unix ts),
             limit (int, default 500, max 10000)
    signals: gateway (str, optional)
    signal_trend:
             metrics (comma list, e.g. pack_temp_max,fan_a_rpm; default
             all), gateway (str), devices (comma list of device blocks),
             start / end (unix ts), hours (int, default 24, used without
             start), resolution ("auto" | "raw" | "daily")

All endpoints respond with {"enabled": false, ...} when the subsystem is
disabled (PW_TIMESERIES_RETENTION=-1) so clients can hide the UI panel
instead of erroring.

Design Notes:
    - All reads go through the store's single worker thread — they never
      block or contend with the poll loop's writes (WAL mode).
    - Endpoints return immediately; missing data yields empty lists, not
      errors, matching the degraded-gracefully style of the other APIs.
"""

import re
import time
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.exceptions import RequestValidationError

from app.api.legacy import powerwall_unit_labels
from app.core.gateway_manager import gateway_manager
from app.core.timeseries import get_timeseries_store

router = APIRouter()

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _query_error(name: str, msg: str) -> RequestValidationError:
    """A 422 in FastAPI's standard validation-error shape for a query param.

    Args:
        name: Query parameter name, reported in ``loc``.
        msg: Human-readable reason.

    Returns:
        The exception to raise.
    """
    return RequestValidationError(
        [{"loc": ("query", name), "msg": msg, "type": "value_error"}]
    )


def _check_day(value: Optional[str], name: str) -> Optional[str]:
    """Validate a YYYY-MM-DD query parameter as a real calendar date.

    Raises 422 on bad input, including well-formed but impossible dates
    such as 2026-02-30 (they would otherwise silently match nothing).

    Args:
        value: The raw query value (None when absent).
        name: Query parameter name, for the error.

    Returns:
        The value unchanged when valid.
    """
    if value is None:
        return value
    try:
        if not _DAY_RE.match(value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise _query_error(name, "must be a valid YYYY-MM-DD date")
    return value


# Latest epoch accepted for /signal_trend start/end (2100-01-01); rejects inf,
# NaN and absurd values that would otherwise raise deep in the store (HTTP
# 500). Only the new /signal_trend route is bounded this way: /trend and
# /samples were already released and keep accepting open-ended values such as
# end=9999999999 (/trend rejects only NaN/inf, which used to raise a 500).
MAX_EPOCH = 4102444800


def _epoch_query(description: str) -> Any:
    """A bounded, finite optional epoch-seconds query parameter."""
    return Query(
        default=None,
        ge=0,
        le=MAX_EPOCH,
        allow_inf_nan=False,
        description=description,
    )


def _powerwall_labels() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Powerwall numbering per gateway, from each gateway's cached status."""
    labels: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for gateway_id in gateway_manager.gateways:
        status = gateway_manager.get_gateway(gateway_id)
        data = status.data if status else None
        labels[gateway_id] = powerwall_unit_labels(
            getattr(data, "system_status", None),
            getattr(data, "tedapi_config", None),
        )
    return labels


def _annotate_powerwalls(result: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``powerwall`` (label) and ``powerwall_order`` to every series.

    Labels match the Console and ``/pod`` (``PW1``, ``PW2``, ``PW1 Exp 1``),
    so every API client numbers units the same way. A series whose serial
    isn't in the gateway's battery list (e.g. before the first full poll)
    is numbered after every known unit, in serial order.

    Args:
        result: A get_signal_series() / get_signal_trend() response; its
            ``series`` entries are updated in place.

    Returns:
        The same ``result``.
    """
    series = result.get("series") or []
    if not series:
        return result
    labels = _powerwall_labels()
    unknown: Dict[str, set] = {}
    for entry in series:
        serial = str(entry.get("device", "")).rsplit("--", 1)[-1]
        if serial not in labels.get(entry.get("gateway"), {}):
            unknown.setdefault(entry.get("gateway"), set()).add(serial)
    for gateway_id, serials in unknown.items():
        known = labels.setdefault(gateway_id, {})
        # Next free unit number after every known one (expansions included),
        # so an unknown serial never shares an order with a known unit
        number = max((v["order"] // 100 for v in known.values()), default=0)
        for serial in sorted(serials):
            number += 1
            known[serial] = {"label": f"PW{number}", "order": number * 100}
    for entry in series:
        serial = str(entry.get("device", "")).rsplit("--", 1)[-1]
        unit = labels[entry.get("gateway")][serial]
        entry["powerwall"] = unit["label"]
        entry["powerwall_order"] = unit["order"]
    return result


def _gateway_names() -> Dict[str, str]:
    """Display name per configured gateway (for the History page selectors)."""
    return {
        gateway_id: getattr(gateway, "name", None) or gateway_id
        for gateway_id, gateway in gateway_manager.gateways.items()
    }


def _gateway_timezones() -> Dict[str, str]:
    """Configured timezone per gateway (daily rollups use local days)."""
    return {
        gateway_id: gateway.timezone
        for gateway_id, gateway in gateway_manager.gateways.items()
        if getattr(gateway, "timezone", None)
    }


def _split(value: Optional[str]) -> Optional[List[str]]:
    """Split a comma-separated query parameter, dropping blanks."""
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


@router.get("/daily")
async def get_daily_energy(
    days: int = Query(default=7, ge=1, le=366),
    gateway: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None, description="First day, YYYY-MM-DD"),
    end: Optional[str] = Query(default=None, description="Last day, YYYY-MM-DD"),
):
    """Daily energy totals (kWh) per gateway, most recent day first.

    Each day entry contains one row per gateway that reported samples that
    day, with directional kWh categories (solar, home, battery charge/
    discharge, grid import/export). ``start``/``end`` select an explicit
    range of gateway-local days instead of the last ``days``.
    """
    start_day = _check_day(start, "start")
    end_day = _check_day(end, "end")
    if start_day and end_day and start_day > end_day:
        raise _query_error("start", "must not be after end")
    return await get_timeseries_store().get_daily_energy(
        days=days,
        gateway=gateway,
        start_day=start_day,
        end_day=end_day,
    )


@router.get("/today")
async def get_today():
    """Today's running kWh totals for every configured gateway.

    Uses each gateway's own timezone to determine 'today'. Gateways without
    samples yet are omitted. Handy for the UI panel and quick checks:

        {"enabled": true, "gateways": {"gw1": {...kWh...}}}
    """
    store = get_timeseries_store()
    if not store.enabled:
        return {"enabled": False, "gateways": {}}
    out = {}
    for gateway_id, gateway in gateway_manager.gateways.items():
        row = await store.get_today(gateway_id, timezone=gateway.timezone)
        if row is not None:
            out[gateway_id] = row
    return {
        "enabled": True,
        "gateways": out,
        "server_time": time.time(),
    }


@router.get("/trend")
async def get_trend(
    hours: int = Query(default=24, ge=1, le=168),
    gateway: Optional[str] = Query(default=None),
    start: Optional[float] = Query(default=None, allow_inf_nan=False),
    end: Optional[float] = Query(default=None, allow_inf_nan=False),
    fit: bool = Query(default=False),
):
    """Bucketed time series of power (kW) and battery level (%) for charts.

    Averages raw samples into ~4-minute buckets over the requested window
    so a 24h view is ~360 points rather than ~17k rows. Battery kW is
    positive = discharging, grid kW positive = importing; ``battery_level``
    is mean state of charge per bucket (raw-sample only, never persisted
    into daily aggregates).
    """
    return await get_timeseries_store().get_trend(
        hours=hours, gateway=gateway, start=start, end=end, fit=fit
    )


@router.get("/samples")
async def get_samples(
    gateway: Optional[str] = Query(default=None),
    start: Optional[float] = Query(default=None),
    end: Optional[float] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
):
    """Raw power samples, ascending by time (troubleshooting)."""
    return await get_timeseries_store().get_samples(
        gateway=gateway, start=start, end=end, limit=limit
    )


@router.get("/status")
async def get_status():
    """Time-series subsystem status: retention settings, DB size, row counts."""
    result = await get_timeseries_store().status()
    result["gateway_names"] = _gateway_names()
    return result


@router.get("/signals")
async def get_signals(
    gateway: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Recorded Powerwall temperature and fan series.

    One entry per (gateway, device block, metric) with the time range of
    retained raw samples (``first_ts``/``last_ts``) and daily rollups
    (``first_day``/``last_day``), plus a ``metrics`` catalog of labels,
    units and chart groups (temperature, fan_speed, fan_duty).
    """
    result = await get_timeseries_store().get_signal_series(gateway=gateway)
    return _annotate_powerwalls(result)


@router.get("/signal_trend")
async def get_signal_trend(
    metrics: Optional[str] = Query(
        default=None, description="Comma-separated metric ids (default all)"
    ),
    gateway: Optional[str] = Query(default=None),
    devices: Optional[str] = Query(
        default=None, description="Comma-separated device blocks"
    ),
    start: Optional[float] = _epoch_query("Window start, epoch seconds"),
    end: Optional[float] = _epoch_query("Window end, epoch seconds"),
    hours: int = Query(default=24, ge=1, le=24 * 3660),
    resolution: str = Query(default="auto", pattern="^(auto|raw|daily)$"),
) -> Dict[str, Any]:
    """Powerwall temperature / fan history, one point list per series.

    Each point has ``avg``, ``min``, ``max`` and sample count ``n``. ``raw``
    resolution buckets the stored samples into ~360 points; ``daily`` returns
    one point per stored local day (with ``day``). ``auto`` picks raw for
    windows up to 14 days still covered by raw retention, else daily; raw
    reads are served as daily beyond 14 days or ~500k rows.

    Args:
        metrics: Comma-separated metric ids (default all).
        gateway: Restrict to one gateway ID.
        devices: Comma-separated device blocks.
        start: Window start, epoch seconds (0..2100-01-01).
        end: Window end, epoch seconds (0..2100-01-01).
        hours: Window length when no start is given.
        resolution: "auto", "raw" or "daily".

    Returns:
        Series with ``powerwall`` labels and their points.
    """
    result = await get_timeseries_store().get_signal_trend(
        metrics=_split(metrics),
        gateway=gateway,
        devices=_split(devices),
        start=start,
        end=end,
        hours=hours,
        resolution=resolution,
        timezones=_gateway_timezones(),
    )
    return _annotate_powerwalls(result)
