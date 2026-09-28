"""
TimeSeriesStore - Lightweight SQLite-backed time-series storage.

Persists raw 5-second power samples from the existing poll loop and derives
daily energy totals (kWh) per gateway via trapezoidal integration. Survives
restarts, adds no network calls, and can be disabled entirely for headless
proxy deployments (PW_TIMESERIES_RETENTION=-1).

Architecture:
    Two-layer storage keeps the on-disk footprint tiny by default:

    1. Raw samples (table ``samples``) - one row per gateway per poll cycle
       (~17k rows/day at the default 5s interval). Power readings are split
       into directional components at record time (battery charge/discharge,
       grid import/export) so no information is netted away. Pruned to the
       PW_TIMESERIES_RETENTION window by a background maintenance task.

    2. Daily aggregates (table ``daily_energy``) - one row per gateway per
       local day holding cumulative kWh per category. This is the layer that
       outlives raw-sample pruning: a full year of daily totals is ~1 KB per
       gateway. Retention governed by PW_TIMESERIES_DAILY_RETENTION.

    A third table (``integration_state``) stores the last integrated sample
    per gateway so energy accumulation resumes exactly where it left off
    after a restart, without double counting.

Device signals (Powerwall temperatures and fans):
    Per-device readings (battery pack max/min, shunt and inverter ambient
    temperatures; fan speed and duty cycle) are stored as generic series so
    a new signal needs only an entry in SIGNAL_METRICS, no schema change:

    - ``device_series``  one row per (gateway, device block, metric), e.g.
      ("default", "TEPOD--1707000-11-J--TG1...", "pack_temp_max", "°C").
    - ``device_samples`` (series_id, ts, value), recorded at most every
      PW_TIMESERIES_SIGNAL_INTERVAL (default 60s; 30s minimum for finer
      detail) and pruned to PW_TIMESERIES_SIGNAL_RETENTION (default 30d).
    - ``device_daily``   per series per gateway-local day: min, max, sum and
      count (so the mean), kept like daily_energy under
      PW_TIMESERIES_DAILY_RETENTION. Long-range history reads this table.

    At 60s a Powerwall 3 records 8 series (~11.5k rows/day, roughly
    0.4 MB/day on disk), so the 30-day default stays around 12 MB per unit.
    30s (the minimum) doubles that (~24 MB).

Energy integration:
    Trapezoidal integration between consecutive samples:
        kWh = (P0 + P1) / 2 * dt / 3_600_000   (P in watts, dt in seconds)

    - Intervals longer than the 1-hour gap threshold are NOT integrated
      (stale/outage data must not fabricate energy).
    - Intervals crossing local midnight (in the gateway's configured
      timezone) are split at the boundary so each day accrues only its own
      energy. DST transitions are handled naturally because integration is
      performed on real elapsed time; only day attribution uses local dates.

Thread safety:
    Two lanes, both off the event loop:
    - Writer lane: recording, daily rollups, integration state, pruning
      and the /today totals run on one worker thread
      (ThreadPoolExecutor(max_workers=1, thread_name_prefix="timeseries"))
      with the read-write connection, serialized by an RLock.
    - Read lane: the API queries (/daily, /trend, /samples, /signals,
      /signal_trend and the /status counts) run on a second worker thread
      ("timeseries-read") with a read-only connection
      (file:...?mode=ro), guarded by its own lock. They never take the
      writer's RLock, and WAL mode lets them read while the writer writes,
      so a long history query can't delay recording.
    For ":memory:" databases (tests), or if a read-only open fails, queries
    fall back to the writer lane. Query sizes stay bounded (raw signal
    reads switch to daily rollups), which keeps reads short and WAL
    checkpoints moving.

Environment Variables:
    PW_TIMESERIES_RETENTION       Raw sample retention (default "24h").
                                  Duration suffixes: s/m/h/d/w.
                                  "0" = unlimited, "-1" = disable subsystem
                                  entirely (no SQLite file, no UI panel).
    PW_TIMESERIES_DAILY_RETENTION Daily aggregate retention (default "0" =
                                  unlimited). One row/day/gateway is tiny,
                                  so unlimited is a sensible default.
    PW_TIMESERIES_SIGNAL_RETENTION
                                  Device signal (temperature/fan) sample
                                  retention (default "30d"). "0" = unlimited,
                                  "-1" = do not record device signals.
    PW_TIMESERIES_SIGNAL_INTERVAL Minimum seconds between device signal
                                  samples per gateway (default "60s").
    PW_TIMESERIES_PATH            SQLite file path (default "/data/timeseries.db"
                                  when /data exists — e.g. the Docker image —
                                  otherwise "data/timeseries.db" relative to
                                  the working directory). If set to a
                                  directory instead of a file (trailing "/"
                                  or an existing directory), "timeseries.db"
                                  is appended automatically.
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Categories tracked per sample. Sign conventions match PowerwallData:
# battery positive = discharging, site positive = importing.
CATEGORIES = (
    "solar",
    "home",
    "battery_charge",
    "battery_discharge",
    "grid_import",
    "grid_export",
)

# Do not integrate across gaps longer than this (seconds). A gateway that
# was unreachable for an hour must not come back and fabricate the energy
# it presumably did (or did not) move while gone.
GAP_THRESHOLD = 3600.0

# Raw samples are always kept for at least this long even when retention is
# configured shorter, preserving a troubleshooting window (seconds).
RAW_KEEP_FLOOR = 3600.0

# Maintenance (pruning) cadence in seconds.
MAINTENANCE_INTERVAL = 60.0

# Minimum seconds between repeated write-failure warnings.
_FAILURE_WARN_INTERVAL = 300.0

# Signal registry: what gets recorded and how it is shown. Adding a metric
# (or a whole new chart group) is one entry here, and in SIGNAL_GROUPS for a
# new group; the History page builds its cards from this catalog with no
# per-metric code. Metric ids are permanent once released (they are stored).
#
# Chart groups, ordered by ``order``. ``zero_based`` starts the y-axis at 0
# (speeds, duty cycles) instead of fitting the data; ``decimals`` is the
# precision the page shows.
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
_SIGNAL_TO_METRIC: Dict[str, str] = {
    signal: metric
    for metric, entry in SIGNAL_METRICS.items()
    for signal in entry["signals"]
}

# Default raw signal retention; also the pruning window when recording is
# turned off (PW_TIMESERIES_SIGNAL_RETENTION=-1), so old samples still age out.
SIGNAL_DEFAULT_RETENTION = "30d"

# Shortest temperature/fan sample interval. These signals change slowly and
# finer sampling costs real disk (and SD-card wear) for little insight: at
# 30s a Powerwall 3 uses ~24 MB per 30 days; 5s would be ~145 MB.
SIGNAL_MIN_INTERVAL = 30

# Longest window (seconds) served from raw device samples; longer ranges
# read the daily min/avg/max rollups instead.
SIGNAL_RAW_MAX_SPAN = 14 * 86400.0

# Most raw sample rows one signal-trend query may scan. Queries hold the
# store's lock and its single worker thread, so an unbounded raw read would
# queue power-sample writes behind it; larger requests read daily rollups.
SIGNAL_RAW_MAX_ROWS = 500_000


def extract_device_metrics(
    vitals: Optional[Dict[str, Any]],
    fan_speeds: Optional[Dict[str, Any]] = None,
) -> Dict[Tuple[str, str], float]:
    """Pull recordable device signals out of a poll's vitals and fan data.

    Args:
        vitals:     pypowerwall vitals() payload ({block: {signal: value}}).
        fan_speeds: pypowerwall tedapi.get_fan_speeds() payload (same shape).

    Returns:
        {(device block, metric id): value}. Missing, None, boolean and
        non-finite values are skipped, so an unavailable signal simply has
        no sample rather than a fabricated zero.
    """
    out: Dict[Tuple[str, str], float] = {}
    for payload in (vitals, fan_speeds):
        if not isinstance(payload, dict):
            continue
        for device, signals in payload.items():
            if not isinstance(signals, dict):
                continue
            for signal, metric in _SIGNAL_TO_METRIC.items():
                value = signals.get(signal)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    continue
                out[(str(device), metric)] = float(value)
    return out


# UTC fallback zoneinfo object for gateways with unresolvable timezones.
_UTC = ZoneInfo("UTC")

# zoneinfo cache: timezone name -> ZoneInfo (or None when unresolvable)
_zone_cache: Dict[str, Optional[ZoneInfo]] = {}


def parse_duration(value: str) -> int:
    """Parse a retention setting into seconds.

    Accepted forms:
        "-1"          -> -1  (disabled)
        "0"           -> 0   (unlimited)
        "24h", "7d"   -> suffixed durations (s, m, h, d, w)
        "3600"        -> bare number treated as seconds

    Raises:
        ValueError: on unparseable input.
    """
    text = str(value).strip().lower()
    if not text:
        raise ValueError(f"invalid duration: {value!r}")
    if text == "-1":
        return -1
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if text and (text[-1] in multipliers or text.isdigit()):
        if text[-1] in multipliers:
            number = text[:-1]
            suffix = text[-1]
        else:
            number = text
            suffix = "s"
        try:
            parsed = int(number)
        except ValueError:
            raise ValueError(f"invalid duration: {value!r}") from None
        if parsed < 0 or negative:
            return -1
        return parsed * multipliers[suffix]
    raise ValueError(f"invalid duration: {value!r}")


def _get_zone(name: Optional[str]) -> ZoneInfo:
    """Resolve a timezone name to ZoneInfo, cached, falling back to UTC."""
    if not name:
        return _UTC
    if name not in _zone_cache:
        try:
            _zone_cache[name] = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning(
                "Unknown timezone %r for time-series aggregation - using UTC", name
            )
            _zone_cache[name] = None
    return _zone_cache[name] or _UTC


def _local_date(ts: float, zone: ZoneInfo):
    """Local calendar date for a unix timestamp."""
    return datetime.fromtimestamp(ts, zone).date()


def _midnights_between(t0: float, t1: float, zone: ZoneInfo) -> List[float]:
    """Timestamps of local midnights strictly inside the interval (t0, t1)."""
    out: List[float] = []
    cursor = datetime.fromtimestamp(t0, zone).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    while True:
        midnight = cursor.timestamp()
        if midnight >= t1:
            break
        if midnight > t0:
            out.append(midnight)
        cursor += timedelta(days=1)
    return out


class TimeSeriesStore:
    """Thread-safe SQLite time-series store for daily energy statistics."""

    _DEFAULT_FILENAME = "timeseries.db"

    def __init__(
        self,
        db_path: str,
        retention: Any = "24h",
        daily_retention: Any = "0",
        signal_retention: Any = "30d",
        signal_interval: Any = "60s",
    ):
        """Create the store.

        Args:
            db_path:         SQLite database file path. If this points at a
                             directory (trailing slash, or an existing
                             directory on disk) rather than a file, the
                             default filename ("timeseries.db") is appended
                             automatically — a common misconfiguration when
                             PW_TIMESERIES_PATH is set to a mounted volume.
            retention:       Raw sample retention (duration string or seconds).
                             -1 disables the store, 0 means unlimited.
            daily_retention: Daily aggregate retention (duration string or
                             seconds). 0 means unlimited. Also applies to
                             the daily device-signal rollups.
            signal_retention: Raw signal (temperature/fan) sample
                             retention. -1 stops recording device signals,
                             0 means unlimited.
            signal_interval: Minimum seconds between signal samples
                             per gateway (floor SIGNAL_MIN_INTERVAL, 30s;
                             lower values are raised with a warning).
        """
        self._db_path = self._resolve_db_path(str(db_path))
        self._retention = self._coerce(retention, "24h", "PW_TIMESERIES_RETENTION")
        self._daily_retention = self._coerce(
            daily_retention, "0", "PW_TIMESERIES_DAILY_RETENTION"
        )
        self._signal_retention = self._coerce(
            signal_retention, SIGNAL_DEFAULT_RETENTION, "PW_TIMESERIES_SIGNAL_RETENTION"
        )
        interval = self._coerce(signal_interval, "60s", "PW_TIMESERIES_SIGNAL_INTERVAL")
        if interval < SIGNAL_MIN_INTERVAL:
            logger.warning(
                "PW_TIMESERIES_SIGNAL_INTERVAL=%ss is below the %ss minimum; "
                "using %ss",
                interval,
                SIGNAL_MIN_INTERVAL,
                SIGNAL_MIN_INTERVAL,
            )
            interval = SIGNAL_MIN_INTERVAL
        self._signal_interval = interval
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._maintenance_task: Optional[asyncio.Task] = None
        # Write-failure tracking: surfaced in status(), warned at most once
        # per _FAILURE_WARN_INTERVAL so a full disk is visible without spam.
        self._write_failures = 0
        self._last_failure_warn = 0.0
        # In-memory cache of the last integrated sample per gateway:
        # {gateway_id: {"ts": float, "values": {category: watts}}}
        self._state: Dict[str, Dict[str, Any]] = {}
        # Last recorded ts per series (gateway, device, metric), for interval
        # gating, and the (gateway, device, metric) -> series_id cache.
        self._signal_last: Dict[Tuple[str, str, str], float] = {}
        self._series_ids: Dict[Tuple[str, str, str], int] = {}
        # Read lane: a second, read-only connection on its own thread so
        # queries never wait behind (or block) recording. WAL mode lets it
        # read while the writer writes. Unavailable for ":memory:" or when a
        # read-only open fails; queries then share the writer lane.
        self._read_conn: Optional[sqlite3.Connection] = None
        self._read_executor: Optional[ThreadPoolExecutor] = None
        self._read_lock = threading.Lock()
        self._read_unavailable = self._db_path == ":memory:"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_db_path(cls, db_path: str) -> str:
        """Append the default filename when db_path looks like a directory.

        Guards against pointing PW_TIMESERIES_PATH at a directory (e.g. a
        mounted volume such as "/data") instead of a file — SQLite cannot
        open a directory as a database. The ":memory:" sentinel is passed
        through untouched.
        """
        if db_path == ":memory:":
            return db_path
        if db_path.endswith(("/", os.sep)) or os.path.isdir(db_path):
            return os.path.join(db_path, cls._DEFAULT_FILENAME)
        return db_path

    @staticmethod
    def _coerce(value: Any, default: str, setting: str) -> int:
        """Parse a retention value with a safe default on bad input."""
        if isinstance(value, bool):
            return parse_duration(default)
        if isinstance(value, (int, float)):
            result = int(value)
            return -1 if result < 0 else result
        try:
            return parse_duration(str(value))
        except ValueError:
            logger.warning(
                "Invalid %s value %r - using default %r", setting, value, default
            )
            return parse_duration(default)

    @property
    def enabled(self) -> bool:
        """True when the subsystem is active (retention != -1)."""
        return self._retention != -1

    @property
    def signals_enabled(self) -> bool:
        """True when device signals (temperatures, fans) are recorded."""
        return self.enabled and self._signal_retention != -1

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="timeseries"
            )
        return self._executor

    def _ensure_read_executor(self) -> ThreadPoolExecutor:
        """The read lane's single worker thread (separate from the writer)."""
        if self._read_executor is None:
            self._read_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="timeseries-read"
            )
        return self._read_executor

    def _query_executor(self) -> ThreadPoolExecutor:
        """Executor for API queries: the read lane when available."""
        if self._read_unavailable:
            return self._ensure_executor()
        return self._ensure_read_executor()

    def _ensure_read_conn(self) -> Optional[sqlite3.Connection]:
        """Open (lazily) the read-only connection. Caller holds _read_lock.

        The writer creates the database and tables first. Never raises:
        returns None (and stops trying) when a read-only open isn't
        possible, so callers fall back to the writer lane.

        Returns:
            The read-only connection, or None when unavailable.
        """
        if self._read_conn is None and not self._read_unavailable:
            try:
                with self._lock:
                    self._ensure_conn()  # database file and tables exist
            except sqlite3.Error:
                return None  # writer can't open either; retry next query
            try:
                uri = f"file:{quote(os.path.abspath(self._db_path))}?mode=ro"
                conn = sqlite3.connect(
                    uri, uri=True, check_same_thread=False, timeout=10.0
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("SELECT 1 FROM samples LIMIT 1")
                self._read_conn = conn
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore read-only open failed: %s", e)
                self._read_unavailable = True
        return self._read_conn

    @contextmanager
    def _reader(self) -> Iterator[Callable[[], sqlite3.Connection]]:
        """Hold a lane for one query and yield a function returning its connection.

        The read lane (read-only connection + its own lock) never takes the
        writer's RLock, so a long query can't delay recording. Without it
        (":memory:", or a failed read-only open) this falls back to the
        writer connection under the writer lock, as before. Yielding an
        opener keeps open errors inside each caller's ``except
        sqlite3.Error``.
        """
        if not self._read_unavailable:
            with self._read_lock:
                conn = self._ensure_read_conn()
                if conn is not None:
                    yield lambda: conn
                    return
        with self._lock:
            yield self._ensure_conn

    def _ensure_conn(self) -> sqlite3.Connection:
        """Open (lazily) and return the SQLite connection. Caller holds the lock."""
        if self._conn is None:
            directory = os.path.dirname(self._db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS samples (
                    gateway_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    solar_w REAL NOT NULL DEFAULT 0,
                    home_w REAL NOT NULL DEFAULT 0,
                    battery_charge_w REAL NOT NULL DEFAULT 0,
                    battery_discharge_w REAL NOT NULL DEFAULT 0,
                    grid_import_w REAL NOT NULL DEFAULT 0,
                    grid_export_w REAL NOT NULL DEFAULT 0,
                    soe REAL,
                    PRIMARY KEY (gateway_id, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
                CREATE TABLE IF NOT EXISTS daily_energy (
                    gateway_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    solar_kwh REAL NOT NULL DEFAULT 0,
                    home_kwh REAL NOT NULL DEFAULT 0,
                    battery_charge_kwh REAL NOT NULL DEFAULT 0,
                    battery_discharge_kwh REAL NOT NULL DEFAULT 0,
                    grid_import_kwh REAL NOT NULL DEFAULT 0,
                    grid_export_kwh REAL NOT NULL DEFAULT 0,
                    last_sample_ts REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (gateway_id, day)
                );
                CREATE TABLE IF NOT EXISTS device_series (
                    series_id INTEGER PRIMARY KEY,
                    gateway_id TEXT NOT NULL,
                    device TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    unit TEXT,
                    UNIQUE (gateway_id, device, metric)
                );
                CREATE TABLE IF NOT EXISTS device_samples (
                    series_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (series_id, ts)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_device_samples_ts
                    ON device_samples(ts);
                CREATE TABLE IF NOT EXISTS device_daily (
                    series_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    min_value REAL NOT NULL,
                    max_value REAL NOT NULL,
                    sum_value REAL NOT NULL,
                    count INTEGER NOT NULL,
                    last_ts INTEGER NOT NULL,
                    PRIMARY KEY (series_id, day)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS integration_state (
                    gateway_id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    solar_w REAL NOT NULL DEFAULT 0,
                    home_w REAL NOT NULL DEFAULT 0,
                    battery_charge_w REAL NOT NULL DEFAULT 0,
                    battery_discharge_w REAL NOT NULL DEFAULT 0,
                    grid_import_w REAL NOT NULL DEFAULT 0,
                    grid_export_w REAL NOT NULL DEFAULT 0
                );
                """)
            conn.commit()
            self._conn = conn
            logger.debug("TimeSeriesStore opened %s (WAL mode)", self._db_path)
        return self._conn

    # ------------------------------------------------------------------
    # Sample recording + integration
    # ------------------------------------------------------------------

    async def record_sample(
        self,
        gateway_id: str,
        ts: float,
        solar_w: float,
        home_w: float,
        battery_w: float,
        site_w: float,
        soe: Optional[float] = None,
        timezone: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record one power sample and integrate it into daily totals.

        Battery and grid power are split into directional components using
        the PowerwallData sign conventions (battery positive = discharging,
        site positive = importing) so charge/discharge and import/export are
        never netted together.

        Args:
            gateway_id: Gateway identifier.
            ts:         Unix timestamp of the sample.
            solar_w:    Solar production (W).
            home_w:     Home/load consumption (W).
            battery_w:  Battery power (W, positive = discharging).
            site_w:     Grid power (W, positive = importing).
            soe:        Battery state of energy (%) if known.
            timezone:   Gateway timezone name for local-midnight aggregation.

        Returns:
            The updated daily-energy row for the gateway's current local day
            (or None when the store is disabled / sample skipped).
        """
        if not self.enabled:
            return None
        values = {
            "solar": max(0.0, float(solar_w or 0.0)),
            "home": max(0.0, float(home_w or 0.0)),
            "battery_charge": max(0.0, -float(battery_w or 0.0)),
            "battery_discharge": max(0.0, float(battery_w or 0.0)),
            "grid_import": max(0.0, float(site_w or 0.0)),
            "grid_export": max(0.0, -float(site_w or 0.0)),
        }
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._ensure_executor(),
            partial(
                self._record_sample_sync, gateway_id, float(ts), values, soe, timezone
            ),
        )

    def _record_sample_sync(
        self,
        gateway_id: str,
        ts: float,
        values: Dict[str, float],
        soe: Optional[float],
        timezone: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                conn = self._ensure_conn()
                state = self._load_state(gateway_id, conn)

                # Integrate against the previous sample unless this one is
                # out-of-order/duplicate (dt <= 0) — those still get stored
                # as raw rows but never move integration state backwards.
                daily_deltas: Dict[str, Dict[str, float]] = {}
                if state is not None and ts > state["ts"]:
                    daily_deltas = self._integrate_interval(state, ts, values, timezone)

                conn.execute(
                    "INSERT OR REPLACE INTO samples "
                    "(gateway_id, ts, solar_w, home_w, battery_charge_w, "
                    "battery_discharge_w, grid_import_w, grid_export_w, soe) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        gateway_id,
                        ts,
                        values["solar"],
                        values["home"],
                        values["battery_charge"],
                        values["battery_discharge"],
                        values["grid_import"],
                        values["grid_export"],
                        soe,
                    ),
                )
                for day, deltas in daily_deltas.items():
                    conn.execute(
                        "INSERT INTO daily_energy "
                        "(gateway_id, day, solar_kwh, home_kwh, battery_charge_kwh, "
                        "battery_discharge_kwh, grid_import_kwh, grid_export_kwh, "
                        "last_sample_ts, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(gateway_id, day) DO UPDATE SET "
                        "solar_kwh=solar_kwh+excluded.solar_kwh, "
                        "home_kwh=home_kwh+excluded.home_kwh, "
                        "battery_charge_kwh=battery_charge_kwh"
                        "+excluded.battery_charge_kwh, "
                        "battery_discharge_kwh=battery_discharge_kwh"
                        "+excluded.battery_discharge_kwh, "
                        "grid_import_kwh=grid_import_kwh+excluded.grid_import_kwh, "
                        "grid_export_kwh=grid_export_kwh+excluded.grid_export_kwh, "
                        "last_sample_ts=excluded.last_sample_ts, "
                        "updated_at=excluded.updated_at",
                        (
                            gateway_id,
                            day,
                            deltas["solar"],
                            deltas["home"],
                            deltas["battery_charge"],
                            deltas["battery_discharge"],
                            deltas["grid_import"],
                            deltas["grid_export"],
                            ts,
                            time.time(),
                        ),
                    )
                if state is None or ts > state["ts"]:
                    conn.execute(
                        "INSERT OR REPLACE INTO integration_state "
                        "(gateway_id, ts, solar_w, home_w, battery_charge_w, "
                        "battery_discharge_w, grid_import_w, grid_export_w) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            gateway_id,
                            ts,
                            values["solar"],
                            values["home"],
                            values["battery_charge"],
                            values["battery_discharge"],
                            values["grid_import"],
                            values["grid_export"],
                        ),
                    )
                    self._state[gateway_id] = {"ts": ts, "values": dict(values)}
                conn.commit()

                if daily_deltas:
                    zone = _get_zone(timezone)
                    today = _local_date(ts, zone).isoformat()
                    if today in daily_deltas:
                        return self._get_day_row(gateway_id, today, conn)
                return None
            except Exception as e:  # storage must never break polling
                self._note_write_failure(e)
                return None

    def _note_write_failure(self, exc: Exception) -> None:
        """Count a failed write; warn at most once per interval."""
        self._write_failures += 1
        now = time.time()
        if now - self._last_failure_warn >= _FAILURE_WARN_INTERVAL:
            self._last_failure_warn = now
            logger.warning(
                "TimeSeriesStore sample write failed (%d total): %s",
                self._write_failures,
                exc,
            )

    def _load_state(
        self, gateway_id: str, conn: sqlite3.Connection
    ) -> Optional[Dict[str, Any]]:
        """Last integrated sample for a gateway (memory cache -> DB)."""
        if gateway_id in self._state:
            return self._state[gateway_id]
        row = conn.execute(
            "SELECT * FROM integration_state WHERE gateway_id=?", (gateway_id,)
        ).fetchone()
        if row is None:
            return None
        state = {
            "ts": row["ts"],
            "values": {
                "solar": row["solar_w"],
                "home": row["home_w"],
                "battery_charge": row["battery_charge_w"],
                "battery_discharge": row["battery_discharge_w"],
                "grid_import": row["grid_import_w"],
                "grid_export": row["grid_export_w"],
            },
        }
        self._state[gateway_id] = state
        return state

    @staticmethod
    def _integrate_interval(
        state: Dict[str, Any],
        ts: float,
        values: Dict[str, float],
        timezone: Optional[str],
    ) -> Dict[str, Dict[str, float]]:
        """Trapezoidal integration of one interval, split at local midnights.

        Returns {day: {category: kWh accrued on that local day}}.
        Skips integration entirely across gaps longer than GAP_THRESHOLD.
        """
        t0 = state["ts"]
        dt = ts - t0
        if dt <= 0 or dt > GAP_THRESHOLD:
            return {}

        zone = _get_zone(timezone)
        segments = [t0] + _midnights_between(t0, ts, zone) + [ts]
        result: Dict[str, Dict[str, float]] = {}
        for a, b in zip(segments, segments[1:]):
            # Segment [a, b] accrues to the local date of its start; a
            # segment ending exactly at midnight belongs to the day before.
            day = _local_date(a, zone).isoformat()
            f0 = (a - t0) / dt
            f1 = (b - t0) / dt
            day_deltas = result.setdefault(
                day, {category: 0.0 for category in CATEGORIES}
            )
            for category in CATEGORIES:
                v0 = state["values"][category]
                v1 = values[category]
                watts0 = v0 + (v1 - v0) * f0
                watts1 = v0 + (v1 - v0) * f1
                day_deltas[category] += (watts0 + watts1) / 2.0 * (b - a) / 3_600_000.0
        return result

    def _get_day_row(
        self, gateway_id: str, day: str, conn: sqlite3.Connection
    ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM daily_energy WHERE gateway_id=? AND day=?",
            (gateway_id, day),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Device signals (temperatures, fans)
    # ------------------------------------------------------------------

    async def record_signal_sample(
        self,
        gateway_id: str,
        ts: float,
        metrics: Dict[Tuple[str, str], float],
        timezone: Optional[str] = None,
    ) -> bool:
        """Record one snapshot of device signals for a gateway.

        Called every poll cycle. Each series is gated on its own: a value
        closer than the signal interval to that series' last sample is
        skipped, so the 60s default costs ~1 row per series per minute, and a
        poll that is missing some signals (e.g. vitals timed out but fans
        arrived) doesn't use up the interval for the others. A clock that
        steps backwards resets the gate for that series.

        Args:
            gateway_id: Gateway identifier.
            ts:         Unix timestamp of the poll.
            metrics:    {(device block, metric id): value}, as returned by
                        extract_device_metrics().
            timezone:   Gateway timezone name, for the daily rollup's day.

        Returns:
            True when the snapshot was stored.
        """
        if not self.signals_enabled or not metrics:
            return False
        # Poll timing jitters by a second or two; don't let a gap just short
        # of the interval (e.g. 59.9s at 60s) push the sample to the next poll.
        slack = min(2.5, self._signal_interval / 2.0)
        due: Dict[Tuple[str, str], float] = {}
        for (device, metric), value in metrics.items():
            last = self._signal_last.get((gateway_id, device, metric))
            if last is not None and 0 <= ts - last < self._signal_interval - slack:
                continue
            due[(device, metric)] = value
            self._signal_last[(gateway_id, device, metric)] = float(ts)
        if not due:
            return False
        metrics = due
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._ensure_executor(),
            partial(
                self._record_signal_sample_sync,
                gateway_id,
                float(ts),
                dict(metrics),
                timezone,
            ),
        )

    def _series_id(
        self,
        conn: sqlite3.Connection,
        gateway_id: str,
        device: str,
        metric: str,
    ) -> int:
        """Look up (creating if needed) the series id. Caller holds the lock.

        Args:
            conn: Writer connection.
            gateway_id: Gateway identifier.
            device: Device block key (e.g. ``TEPOD--<din>``).
            metric: Metric id from SIGNAL_METRICS.

        Returns:
            The ``device_series.series_id`` for (gateway, device, metric).
            Units are not stored per series; they come from SIGNAL_METRICS.
        """
        key = (gateway_id, device, metric)
        series_id = self._series_ids.get(key)
        if series_id is None:
            conn.execute(
                "INSERT OR IGNORE INTO device_series "
                "(gateway_id, device, metric) VALUES (?,?,?)",
                key,
            )
            series_id = conn.execute(
                "SELECT series_id FROM device_series "
                "WHERE gateway_id=? AND device=? AND metric=?",
                key,
            ).fetchone()[0]
            self._series_ids[key] = series_id
        return series_id

    def _record_signal_sample_sync(
        self,
        gateway_id: str,
        ts: float,
        metrics: Dict[Tuple[str, str], float],
        timezone: Optional[str],
    ) -> bool:
        """Write one gated signal snapshot and fold it into the daily rollups.

        Runs on the store's writer thread; storage errors are counted in
        ``write_failures`` and never raised into polling.

        Args:
            gateway_id: Gateway identifier.
            ts: Unix timestamp of the poll.
            metrics: {(device block, metric id): value} that passed gating.
            timezone: Gateway timezone name, for the rollup's local day.

        Returns:
            True when the snapshot was stored.
        """
        with self._lock:
            try:
                conn = self._ensure_conn()
                its = int(round(ts))
                day = _local_date(ts, _get_zone(timezone)).isoformat()
                for (device, metric), value in metrics.items():
                    series_id = self._series_id(conn, gateway_id, device, metric)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO device_samples "
                        "(series_id, ts, value) VALUES (?,?,?)",
                        (series_id, its, value),
                    )
                    if cur.rowcount != 1:
                        continue  # duplicate timestamp: never double count
                    conn.execute(
                        "INSERT INTO device_daily "
                        "(series_id, day, min_value, max_value, sum_value, "
                        "count, last_ts) VALUES (?,?,?,?,?,1,?) "
                        "ON CONFLICT(series_id, day) DO UPDATE SET "
                        "min_value=MIN(min_value, excluded.min_value), "
                        "max_value=MAX(max_value, excluded.max_value), "
                        "sum_value=sum_value+excluded.sum_value, "
                        "count=count+1, last_ts=excluded.last_ts",
                        (series_id, day, value, value, value, its),
                    )
                conn.commit()
                return True
            except Exception as e:  # storage must never break polling
                self._note_write_failure(e)
                return False

    @staticmethod
    def _series_filter(
        gateway: Optional[str],
        devices: Optional[Iterable[str]],
        metrics: Optional[Iterable[str]],
    ) -> Tuple[str, List[Any]]:
        """Build a SQL WHERE clause selecting device_series rows.

        Args:
            gateway: Restrict to one gateway ID (None = all).
            devices: Restrict to these device blocks (None/empty = all).
            metrics: Restrict to these metric ids (None/empty = all).

        Returns:
            (" WHERE ..." or "", parameters), ready to append to a
            ``SELECT ... FROM device_series`` query.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if gateway:
            clauses.append("gateway_id=?")
            params.append(gateway)
        for column, values in (("device", devices), ("metric", metrics)):
            values = [v for v in (values or []) if v]
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params.extend(values)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    async def get_signal_series(self, gateway: Optional[str] = None) -> Dict[str, Any]:
        """Recorded device series with their raw and daily coverage.

        The response has the same keys whether or not the store is enabled.
        """
        if not self.enabled:
            return {**self._signal_series_base(), "enabled": False, "series": []}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._query_executor(), partial(self._get_signal_series_sync, gateway)
        )

    def _signal_series_base(self) -> Dict[str, Any]:
        """Keys shared by every get_signal_series() response."""
        return {
            "enabled": True,
            "signals_enabled": self.signals_enabled,
            "interval_seconds": self._signal_interval,
            "metrics": SIGNAL_METRICS,
            "groups": SIGNAL_GROUPS,
        }

    def _get_signal_series_sync(self, gateway: Optional[str]) -> Dict[str, Any]:
        """List recorded signal series with their raw and daily coverage.

        Args:
            gateway: Restrict to one gateway ID (None = all gateways).

        Returns:
            The get_signal_series() response: catalog keys plus ``series``.
        """
        base = self._signal_series_base()
        with self._reader() as open_conn:
            try:
                conn = open_conn()
                where, params = self._series_filter(gateway, None, None)
                rows = conn.execute(
                    "SELECT s.series_id, s.gateway_id, s.device, s.metric, "
                    "(SELECT MIN(ts) FROM device_samples d "
                    "WHERE d.series_id=s.series_id) AS first_ts, "
                    "(SELECT MAX(ts) FROM device_samples d "
                    "WHERE d.series_id=s.series_id) AS last_ts, "
                    "(SELECT MIN(day) FROM device_daily d "
                    "WHERE d.series_id=s.series_id) AS first_day, "
                    "(SELECT MAX(day) FROM device_daily d "
                    "WHERE d.series_id=s.series_id) AS last_day "
                    f"FROM device_series s{where} "
                    "ORDER BY s.gateway_id, s.device, s.metric",
                    params,
                ).fetchall()
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore device series query failed: %s", e)
                return {**base, "series": []}
        series = [
            {
                "gateway": row["gateway_id"],
                "device": row["device"],
                "metric": row["metric"],
                "unit": SIGNAL_METRICS.get(row["metric"], {}).get("unit"),
                "label": SIGNAL_METRICS.get(row["metric"], {}).get(
                    "label", row["metric"]
                ),
                "first_ts": row["first_ts"],
                "last_ts": row["last_ts"],
                "first_day": row["first_day"],
                "last_day": row["last_day"],
            }
            for row in rows
        ]
        return {**base, "series": series}

    async def get_signal_trend(
        self,
        metrics: Optional[List[str]] = None,
        gateway: Optional[str] = None,
        devices: Optional[List[str]] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        hours: int = 24,
        resolution: str = "auto",
        timezones: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Device signal history for charting, one entry per series.

        ``raw`` averages the stored samples into ~360 buckets (with bucket
        min/max); ``daily`` returns one min/avg/max point per stored local
        day. ``auto`` (default) uses raw for windows up to 14 days that
        raw retention still covers, else daily.

        Args:
            metrics:    Metric ids to include (None = all).
            gateway:    Restrict to one gateway ID.
            devices:    Restrict to these device blocks.
            start:      Window start (epoch seconds); default end - hours.
            end:        Window end (epoch seconds); default now.
            hours:      Window length when no explicit start.
            resolution: "auto", "raw" or "daily".
            timezones:  Gateway ID -> timezone name. Daily rollups are keyed
                        by gateway-local day, so the window bounds are
                        converted to local days per gateway (UTC when a
                        gateway is missing).
        """
        if not self.enabled:
            # Same keys as an enabled response (see _get_signal_trend_sync)
            return {
                "enabled": False,
                "start": start,
                "end": end,
                "resolution": None,
                "bucket_seconds": None,
                "series": [],
            }
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._query_executor(),
            partial(
                self._get_signal_trend_sync,
                metrics,
                gateway,
                devices,
                start,
                end,
                hours,
                resolution,
                dict(timezones or {}),
            ),
        )

    def _get_signal_trend_sync(
        self,
        metrics: Optional[List[str]],
        gateway: Optional[str],
        devices: Optional[List[str]],
        start: Optional[float],
        end: Optional[float],
        hours: int,
        resolution: str,
        timezones: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Signal history for charting (see get_signal_trend()).

        Args:
            metrics: Metric ids to include (None = all).
            gateway: Restrict to one gateway ID.
            devices: Restrict to these device blocks.
            start: Window start (epoch seconds); default end - hours.
            end: Window end (epoch seconds); default now.
            hours: Window length when no explicit start.
            resolution: "auto", "raw" or "daily".
            timezones: Gateway ID -> timezone name for local-day rollups.

        Returns:
            ``{enabled, start, end, resolution, bucket_seconds, series}``.
        """
        timezones = timezones or {}
        now = time.time()
        end = float(end) if end is not None else now + 300.0
        start = float(start) if start is not None else end - max(1, hours) * 3600.0
        if start > end:
            start, end = end, start
        span = max(60.0, end - start)
        result: Dict[str, Any] = {
            "enabled": True,
            "start": start,
            "end": end,
            "resolution": None,
            "bucket_seconds": None,
            "series": [],
        }
        with self._reader() as open_conn:
            try:
                conn = open_conn()
                where, params = self._series_filter(gateway, devices, metrics)
                series_rows = conn.execute(
                    "SELECT series_id, gateway_id, device, metric "
                    f"FROM device_series{where} "
                    "ORDER BY gateway_id, device, metric",
                    params,
                ).fetchall()
                if not series_rows:
                    return result
                ids = [row["series_id"] for row in series_rows]
                marks = ",".join("?" * len(ids))
                # Series per gateway: daily rollups use gateway-local days
                by_gateway: Dict[str, List[int]] = {}
                for row in series_rows:
                    by_gateway.setdefault(row["gateway_id"], []).append(
                        row["series_id"]
                    )
                zones = {gw: _get_zone(timezones.get(gw)) for gw in by_gateway}

                # Estimated raw rows: series x samples per series in the window
                raw_rows = len(ids) * span / max(1, self._signal_interval)
                if resolution == "raw" and (
                    span > SIGNAL_RAW_MAX_SPAN or raw_rows > SIGNAL_RAW_MAX_ROWS
                ):
                    resolution = "daily"  # bounded: never scan unbounded raw
                elif resolution not in ("raw", "daily"):
                    resolution = (
                        "daily"
                        if raw_rows > SIGNAL_RAW_MAX_ROWS
                        else self._pick_signal_resolution(
                            conn, by_gateway, zones, start, span
                        )
                    )
                points: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
                if resolution == "raw":
                    # ~360 points per window, never finer than the sample
                    # interval; steps of a minute or more snap to whole
                    # minutes, shorter ones to multiples of the interval.
                    interval = float(self._signal_interval)
                    target = span / 360.0
                    unit = 60.0 if target >= 60.0 else interval
                    bucket = max(interval, round(target / unit) * unit)
                    rows = conn.execute(
                        "SELECT series_id, "
                        "CAST(ts / ? AS INTEGER) * ? AS bstart, "
                        "AVG(value) AS avg_v, MIN(value) AS min_v, "
                        "MAX(value) AS max_v, COUNT(*) AS n FROM device_samples "
                        f"WHERE series_id IN ({marks}) AND ts>=? AND ts<=? "
                        "GROUP BY series_id, bstart ORDER BY series_id, bstart",
                        (bucket, bucket, *ids, int(start), int(end) + 1),
                    ).fetchall()
                    for row in rows:
                        points[row["series_id"]].append(
                            {
                                "ts": row["bstart"],
                                "avg": row["avg_v"],
                                "min": row["min_v"],
                                "max": row["max_v"],
                                "n": row["n"],
                            }
                        )
                    result["bucket_seconds"] = bucket
                else:
                    # Rows are keyed by gateway-local day: convert the window
                    # bounds to local days in each gateway's timezone.
                    rows = []
                    for gw, gw_ids in by_gateway.items():
                        zone = zones[gw]
                        lo = _local_date(start, zone).isoformat()
                        hi = _local_date(min(end, now), zone).isoformat()
                        gw_marks = ",".join("?" * len(gw_ids))
                        rows.extend(
                            (zone, row)
                            for row in conn.execute(
                                "SELECT series_id, day, min_value, max_value, count, "
                                "sum_value / count AS avg_v FROM device_daily "
                                f"WHERE series_id IN ({gw_marks}) "
                                "AND day>=? AND day<=? ORDER BY series_id, day",
                                (*gw_ids, lo, hi),
                            ).fetchall()
                        )
                    for zone, row in rows:
                        noon = (
                            datetime.strptime(row["day"], "%Y-%m-%d")
                            .replace(hour=12, tzinfo=zone)
                            .timestamp()
                        )
                        points[row["series_id"]].append(
                            {
                                "ts": noon,
                                "day": row["day"],
                                "avg": row["avg_v"],
                                "min": row["min_value"],
                                "max": row["max_value"],
                                "n": row["count"],
                            }
                        )
                    result["bucket_seconds"] = 86400.0
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore device trend query failed: %s", e)
                return result
        result["resolution"] = resolution
        result["series"] = [
            {
                "gateway": row["gateway_id"],
                "device": row["device"],
                "metric": row["metric"],
                "unit": SIGNAL_METRICS.get(row["metric"], {}).get("unit"),
                "label": SIGNAL_METRICS.get(row["metric"], {}).get(
                    "label", row["metric"]
                ),
                "points": points[row["series_id"]],
            }
            for row in series_rows
        ]
        return result

    @staticmethod
    def _pick_signal_resolution(
        conn: sqlite3.Connection,
        by_gateway: Dict[str, List[int]],
        zones: Dict[str, ZoneInfo],
        start: float,
        span: float,
    ) -> str:
        """Raw when the window is short and raw samples cover it, else daily.

        A window that starts before the oldest raw sample still reads raw if
        there is no older daily history either (a fresh install should show
        its first hour at full detail, not as one daily point). Daily rows
        are keyed by gateway-local day, so "older" is judged per gateway in
        its own timezone.

        Args:
            conn: Database connection.
            by_gateway: Gateway ID -> series ids in the query.
            zones: Gateway ID -> timezone for its local days.
            start: Window start (epoch seconds).
            span: Window length in seconds.

        Returns:
            "raw" or "daily".
        """
        if span > SIGNAL_RAW_MAX_SPAN:
            return "daily"
        oldest_all: Optional[float] = None
        older_daily = False
        for gw, gw_ids in by_gateway.items():
            marks = ",".join("?" * len(gw_ids))
            oldest = conn.execute(
                f"SELECT MIN(ts) FROM device_samples WHERE series_id IN ({marks})",
                gw_ids,
            ).fetchone()[0]
            if oldest is None:
                continue
            oldest_all = oldest if oldest_all is None else min(oldest_all, oldest)
            oldest_day = _local_date(oldest, zones[gw]).isoformat()
            if conn.execute(
                "SELECT 1 FROM device_daily "
                f"WHERE series_id IN ({marks}) AND day < ? LIMIT 1",
                (*gw_ids, oldest_day),
            ).fetchone():
                older_daily = True
        if oldest_all is None:
            return "daily"
        if start >= oldest_all - 3600:
            return "raw"
        return "daily" if older_daily else "raw"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_daily_energy(
        self,
        days: int = 7,
        gateway: Optional[str] = None,
        start_day: Optional[str] = None,
        end_day: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Daily energy totals, most recent day first.

        Args:
            days:      Number of days to include (counting back from today).
            gateway:   Restrict to one gateway ID (None = all gateways).
            start_day: Inclusive first local day (YYYY-MM-DD). When either
                       bound is given, ``days`` is ignored and every stored
                       day in the range is returned.
            end_day:   Inclusive last local day (YYYY-MM-DD).
        """
        if not self.enabled:
            return {"enabled": False, "days": [], "last_updated": None}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._query_executor(),
            partial(self._get_daily_energy_sync, days, gateway, start_day, end_day),
        )

    def _get_daily_energy_sync(
        self,
        days: int,
        gateway: Optional[str],
        start_day: Optional[str] = None,
        end_day: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._reader() as open_conn:
            try:
                conn = open_conn()
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore query failed: %s", e)
                return {"enabled": True, "days": [], "last_updated": None}
            # Days are stored as gateway-local dates, which can trail or lead
            # the UTC date around midnight. Widen the SQL cutoff by one day so
            # late-local-day rows are never dropped, then trim to `days` after
            # grouping (ISO day strings sort correctly across gateways).
            ranged = start_day is not None or end_day is not None
            if ranged:
                lo = start_day or "0000-00-00"
                hi = end_day or "9999-99-99"
            else:
                lo = (datetime.now(_UTC) - timedelta(days=max(days, 1))).strftime(
                    "%Y-%m-%d"
                )
                hi = "9999-99-99"
            if gateway:
                rows = conn.execute(
                    "SELECT * FROM daily_energy WHERE day>=? AND day<=? "
                    "AND gateway_id=? ORDER BY day DESC",
                    (lo, hi, gateway),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM daily_energy WHERE day>=? AND day<=? "
                    "ORDER BY day DESC",
                    (lo, hi),
                ).fetchall()
            by_day: Dict[str, Dict[str, Dict[str, Any]]] = {}
            last_updated: Optional[float] = None
            for row in rows:
                entry = dict(row)
                by_day.setdefault(row["day"], {})[row["gateway_id"]] = entry
                if row["last_sample_ts"]:
                    last_updated = max(last_updated or 0.0, row["last_sample_ts"])
            return {
                "enabled": True,
                "days": [
                    {"day": day, "gateways": gateways}
                    for day, gateways in sorted(
                        by_day.items(), key=lambda item: item[0], reverse=True
                    )[: (None if ranged else days)]
                ],
                "last_updated": last_updated,
            }

    async def get_today(
        self, gateway_id: str, timezone: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Today's running totals for one gateway (gateway-local day)."""
        if not self.enabled:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._ensure_executor(), partial(self._get_today_sync, gateway_id, timezone)
        )

    def _get_today_sync(
        self, gateway_id: str, timezone: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        zone = _get_zone(timezone)
        today = _local_date(time.time(), zone).isoformat()
        with self._lock:
            try:
                conn = self._ensure_conn()
            except Exception:
                return None
            return self._get_day_row(gateway_id, today, conn)

    async def get_trend(
        self,
        hours: int = 24,
        gateway: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        fit: bool = False,
    ) -> Dict[str, Any]:
        """Bucketed time series for charting.

        Raw 5s samples are averaged into ~240-second buckets (per gateway,
        then summed across gateways) so a 24-hour window returns ~360 points
        instead of ~17k rows. Power columns keep the PowerwallData sign
        convention (battery positive = discharging, grid positive =
        importing) and are returned in kW; ``battery_level`` is the mean
        state of energy (%) in each bucket. Battery level lives only in raw
        samples — it is never downsampled into daily aggregates.

        Args:
            hours:   Window length (1-168) when no explicit ``start``.
            gateway: Restrict to one gateway ID (None = all, summed).
            start:   Explicit window start (epoch seconds). Overrides
                     ``hours``.
            end:     Explicit window end (epoch seconds); default now.
            fit:     Fit the window to the full range of retained raw data
                     (earliest → latest sample, per gateway when filtered).
        """
        if not self.enabled:
            return {"enabled": False, "points": [], "count": 0}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._query_executor(),
            partial(self._get_trend_sync, hours, gateway, start, end, fit),
        )

    def _get_trend_sync(
        self,
        hours: int,
        gateway: Optional[str],
        start: Optional[float] = None,
        end: Optional[float] = None,
        fit: bool = False,
    ) -> Dict[str, Any]:
        hours = max(1, min(int(hours), 168))
        now = time.time()
        if start is None:
            span = hours * 3600.0
            start = now - span
        end = end or (now + 300.0)  # +300s tolerance for clock skew
        # No clamp to now: raw samples may sit slightly in the future
        # (clock skew / rounding) and excluding them drops live buckets.
        span = max(60.0, end - start)
        # Target ~360 buckets, rounded to a whole minute, never below 60s.
        bucket = max(60.0, round(span / 360.0 / 60.0) * 60.0)
        with self._reader() as open_conn:
            try:
                conn = open_conn()
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore query failed: %s", e)
                return {
                    "enabled": True,
                    "points": [],
                    "count": 0,
                    "bucket_seconds": bucket,
                    "hours": hours,
                    "start": start,
                    "end": end,
                }
            if fit:
                # Fit spans the full range of retained raw data (per gateway
                # when filtered); span floor below handles the 1-sample case.
                row = conn.execute(
                    "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM samples"
                    + (" WHERE gateway_id=?" if gateway else ""),
                    ((gateway,) if gateway else ()),
                ).fetchone()
                if row is not None and row["lo"] is not None:
                    start = float(row["lo"])
                    end = float(row["hi"])
            span = max(60.0, end - start)
            bucket = max(60.0, round(span / 360.0 / 60.0) * 60.0)
            # Inner query: mean per (bucket, gateway) so multi-gateway setups
            # sum instead of average; outer query collapses to fleet totals
            # (mean SoE). Single-gateway deployments get plain bucket means.
            gw_filter = "AND gateway_id=? " if gateway else ""
            sql = (
                "SELECT bstart, "
                "SUM(solar_avg)/1000.0 AS solar_kw, "
                "SUM(home_avg)/1000.0 AS home_kw, "
                "SUM(batt_avg)/1000.0 AS battery_kw, "
                "SUM(grid_avg)/1000.0 AS grid_kw, "
                "AVG(soe_avg) AS battery_level "
                "FROM (SELECT (CAST(ts/? AS INTEGER))*? AS bstart, "
                "gateway_id, AVG(solar_w) AS solar_avg, "
                "AVG(home_w) AS home_avg, "
                "AVG(battery_discharge_w - battery_charge_w) AS batt_avg, "
                "AVG(grid_import_w - grid_export_w) AS grid_avg, "
                "AVG(soe) AS soe_avg FROM samples WHERE ts>=? AND ts<=? "
                + gw_filter
                + "GROUP BY bstart, gateway_id) "
                "GROUP BY bstart ORDER BY bstart"
            )
            rows = conn.execute(
                sql,
                (
                    bucket,
                    bucket,
                    start,
                    end,
                    *((gateway,) if gateway else ()),
                ),
            ).fetchall()
            points = [
                {
                    "ts": row["bstart"],
                    "solar_kw": row["solar_kw"],
                    "home_kw": row["home_kw"],
                    "battery_kw": row["battery_kw"],
                    "grid_kw": row["grid_kw"],
                    "battery_level": row["battery_level"],
                }
                for row in rows
            ]
            return {
                "enabled": True,
                "hours": hours,
                "bucket_seconds": bucket,
                "start": start,
                "end": end,
                "points": points,
                "count": len(points),
                "last_updated": now,
            }

    async def get_samples(
        self,
        gateway: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Raw samples, ascending by time (for troubleshooting).

        Args:
            gateway: Restrict to one gateway ID (None = all).
            start:   Inclusive start timestamp (unix).
            end:     Inclusive end timestamp (unix).
            limit:   Maximum rows to return (capped at 10,000).
        """
        if not self.enabled:
            return {"enabled": False, "samples": [], "count": 0}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._query_executor(),
            partial(self._get_samples_sync, gateway, start, end, limit),
        )

    def _get_samples_sync(
        self,
        gateway: Optional[str],
        start: Optional[float],
        end: Optional[float],
        limit: int,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 10_000))
        with self._reader() as open_conn:
            try:
                conn = open_conn()
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore query failed: %s", e)
                return {"enabled": True, "samples": [], "count": 0}
            clauses, params = [], []
            if gateway:
                clauses.append("gateway_id=?")
                params.append(gateway)
            if start is not None:
                clauses.append("ts>=?")
                params.append(float(start))
            if end is not None:
                clauses.append("ts<=?")
                params.append(float(end))
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM samples{where} ORDER BY ts DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            samples = [dict(row) for row in reversed(rows)]
            return {"enabled": True, "samples": samples, "count": len(samples)}

    async def status(self) -> Dict[str, Any]:
        """Subsystem status for /api/timeseries/status and the UI."""
        if not self.enabled:
            return {
                "enabled": False,
                "retention_seconds": -1,
                "daily_retention_seconds": self._daily_retention,
                "db_file": None,
                "db_size_bytes": 0,
                "samples": 0,
                "daily_rows": 0,
                "signals_enabled": False,
                "signal_retention_seconds": -1,
                "signal_interval_seconds": self._signal_interval,
                "signal_series": 0,
                "signal_samples": 0,
                "signal_daily_rows": 0,
                "write_failures": 0,
                "gateways": [],
            }
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._query_executor(), self._status_sync)

    def _status_sync(self) -> Dict[str, Any]:
        db_size = 0
        samples = daily_rows = 0
        device_series = device_samples = device_daily = 0
        gateways: List[str] = []
        with self._reader() as open_conn:
            if Path(self._db_path).exists():
                db_size = os.path.getsize(self._db_path)
                wal = Path(self._db_path + "-wal")
                if wal.exists():
                    db_size += wal.stat().st_size
                try:
                    conn = open_conn()
                    samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
                    daily_rows = conn.execute(
                        "SELECT COUNT(*) FROM daily_energy"
                    ).fetchone()[0]
                    device_series = conn.execute(
                        "SELECT COUNT(*) FROM device_series"
                    ).fetchone()[0]
                    device_samples = conn.execute(
                        "SELECT COUNT(*) FROM device_samples"
                    ).fetchone()[0]
                    device_daily = conn.execute(
                        "SELECT COUNT(*) FROM device_daily"
                    ).fetchone()[0]
                    gateways = [
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT gateway_id FROM integration_state"
                        ).fetchall()
                    ]
                except sqlite3.Error as e:
                    logger.debug("TimeSeriesStore status query failed: %s", e)
        return {
            "enabled": True,
            "retention_seconds": self._retention,
            "daily_retention_seconds": self._daily_retention,
            # Filename only — the unauthenticated status endpoint must not
            # disclose filesystem layout (matches the /stats masking policy).
            "db_file": os.path.basename(self._db_path),
            "db_size_bytes": db_size,
            "samples": samples,
            "daily_rows": daily_rows,
            "signals_enabled": self.signals_enabled,
            "signal_retention_seconds": self._signal_retention,
            "signal_interval_seconds": self._signal_interval,
            "signal_series": device_series,
            "signal_samples": device_samples,
            "signal_daily_rows": device_daily,
            "write_failures": self._write_failures,
            "gateways": gateways,
        }

    # ------------------------------------------------------------------
    # Maintenance (pruning)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background maintenance loop (no-op when disabled)."""
        if not self.enabled:
            return
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="timeseries-maintenance"
            )
            logger.info(
                "TimeSeriesStore enabled — raw retention %ss, daily retention %ss, "
                "db: %s",
                self._retention,
                self._daily_retention,
                self._db_path,
            )

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
                await self.maintenance()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("TimeSeriesStore maintenance error: %s", e)

    async def maintenance(self) -> None:
        """Prune raw samples and stale daily aggregates, checkpoint WAL."""
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._ensure_executor(), self._maintenance_sync)

    def _maintenance_sync(self) -> None:
        with self._lock:
            try:
                conn = self._ensure_conn()
                now = time.time()
                if self._retention > 0:
                    # Raw samples older than the retention window go away,
                    # but the last RAW_KEEP_FLOOR of data always stays.
                    cutoff = now - max(self._retention, RAW_KEEP_FLOOR)
                    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                if self._daily_retention > 0:
                    cutoff_day = (
                        datetime.fromtimestamp(now, _UTC)
                        - timedelta(seconds=self._daily_retention)
                    ).strftime("%Y-%m-%d")
                    conn.execute(
                        "DELETE FROM daily_energy WHERE day < ?", (cutoff_day,)
                    )
                    conn.execute(
                        "DELETE FROM device_daily WHERE day < ?", (cutoff_day,)
                    )
                # Signal samples: pruned even when recording is off (-1), on
                # the default window, so earlier history still ages out.
                signal_retention = self._signal_retention
                if signal_retention == -1:
                    signal_retention = parse_duration(SIGNAL_DEFAULT_RETENTION)
                if signal_retention > 0:
                    cutoff = now - max(signal_retention, RAW_KEEP_FLOOR)
                    conn.execute(
                        "DELETE FROM device_samples WHERE ts < ?", (int(cutoff),)
                    )
                # Series with nothing left in either table. Their ids may be
                # cached: drop the cache so a later sample recreates the series
                # instead of writing under a deleted id.
                cur = conn.execute(
                    "DELETE FROM device_series WHERE series_id NOT IN "
                    "(SELECT series_id FROM device_samples) AND series_id NOT IN "
                    "(SELECT series_id FROM device_daily)"
                )
                if cur.rowcount:
                    self._series_ids.clear()
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as e:
                logger.debug("TimeSeriesStore maintenance failed: %s", e)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop maintenance and close the database. Safe to call repeatedly."""
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        loop = asyncio.get_running_loop()
        if loop.is_running():
            await loop.run_in_executor(None, self._close_sync)
        else:  # pragma: no cover - defensive
            self._close_sync()

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._conn.close()
                except sqlite3.Error as e:
                    logger.debug("TimeSeriesStore close failed: %s", e)
                self._conn = None
            self._state.clear()
            self._signal_last.clear()
            self._series_ids.clear()
        if self._executor is not None:
            # cancel_futures: queued writes must not reopen the closed DB
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        with self._read_lock:
            if self._read_conn is not None:
                try:
                    self._read_conn.close()
                except sqlite3.Error as e:
                    logger.debug("TimeSeriesStore read close failed: %s", e)
                self._read_conn = None
        if self._read_executor is not None:
            self._read_executor.shutdown(wait=False, cancel_futures=True)
            self._read_executor = None


# module-level singleton, built lazily from settings (never at import time,
# so disabled deployments and tests never touch the filesystem)
_store: Optional[TimeSeriesStore] = None


def get_timeseries_store() -> TimeSeriesStore:
    """Return the process-wide TimeSeriesStore built from settings."""
    global _store
    if _store is None:
        from app.config import settings  # late import — avoids import cycle

        _store = TimeSeriesStore(
            db_path=settings.timeseries_path,
            retention=settings.timeseries_retention,
            daily_retention=settings.timeseries_daily_retention,
            signal_retention=settings.timeseries_signal_retention,
            signal_interval=settings.timeseries_signal_interval,
        )
    return _store


def reset_timeseries_store() -> None:
    """Close and drop the singleton (used by tests and config reloads).

    Cancels the maintenance task without awaiting (callers are typically
    synchronous teardown paths), then closes SQLite synchronously.
    """
    global _store
    if _store is not None:
        if _store._maintenance_task and not _store._maintenance_task.done():
            _store._maintenance_task.cancel()
        _store._close_sync()  # pylint: disable=protected-access
        _store = None
