"""Tests for Powerwall device-signal history and the /history page.

Covers:
    - extract_device_metrics (PW3 vitals + fan_speeds, PW2, bad values)
    - record_signal_sample interval gating, series creation, daily rollup
    - Duplicate timestamps never double count the daily rollup
    - Device sample pruning vs. daily rollup retention
    - Device recording disabled (PW_TIMESERIES_SIGNAL_RETENTION=-1)
    - get_signal_trend raw / daily / auto resolution
    - /api/timeseries/daily start/end range
    - /api/timeseries/signals and /api/timeseries/signal_trend endpoints
    - Poll-loop wiring and the /history route
    - Read lane: read-only, WAL and open fallbacks, shutdown; /status safety
"""

import sqlite3
import time

import pytest
import pytest_asyncio

from app.core.signals import SIGNAL_GROUPS, SIGNAL_METRICS
from app.core.timeseries import TimeSeriesStore, extract_device_metrics

POD = "TEPOD--1707000-11-J--TG1"
INV = "TEPINV--1707000-11-J--TG1"

PW3_VITALS = {
    POD: {
        "HVP_PackTempMax": 40.2,
        "HVP_PackTempMin": 35.5,
        "HVP_ShuntTemperature": 41.2,
        "BMS_LOG_tempOutOfBounds": 0,
    },
    INV: {"PCH_AmbientTemp": 47.0, "PCH_heatsinkTemp": 45.45, "PINV_Fout": 60.0},
}
PW3_FANS = {
    INV: {
        "PCH_FanSpeed_A": 1395,
        "PCH_FanSpeed_B": 1397,
        "PCH_FanDuty_A": 19.1,
        "PCH_FanDuty_B": 19.1,
    }
}


def store_for(tmp_path, **kwargs):
    return TimeSeriesStore(db_path=str(tmp_path / "ts.db"), **kwargs)


@pytest_asyncio.fixture
async def make_store(tmp_path):
    """store_for() whose stores are always stopped, even when a test fails.

    Leaves no worker threads or SQLite connections behind.
    """
    stores = []

    def make(**kwargs):
        store = store_for(tmp_path, **kwargs)
        stores.append(store)
        return store

    yield make
    for store in stores:
        await store.stop()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestExtract:
    def test_pw3_signals(self):
        m = extract_device_metrics(PW3_VITALS, PW3_FANS)
        assert m == {
            (POD, "pack_temp_max"): 40.2,
            (POD, "pack_temp_min"): 35.5,
            (POD, "shunt_temp"): 41.2,
            (INV, "inverter_ambient"): 47.0,
            (INV, "fan_a_rpm"): 1395.0,
            (INV, "fan_b_rpm"): 1397.0,
            (INV, "fan_a_duty"): 19.1,
            (INV, "fan_b_duty"): 19.1,
        }

    def test_heatsink_not_recorded(self):
        # Constant on current firmware - deliberately excluded
        recorded = {s for m in SIGNAL_METRICS.values() for s in m["signals"]}
        assert "PCH_heatsinkTemp" not in recorded

    def test_pw2_signals(self):
        m = extract_device_metrics(
            {"TETHC--1": {"THC_AmbientTemp": 25.5}},
            {
                "PVAC--1": {
                    "PVAC_Fan_Speed_Actual_RPM": 2000,
                    "PVAC_Fan_Speed_Target_RPM": 2100,
                }
            },
        )
        assert m == {
            ("TETHC--1", "controller_ambient"): 25.5,
            ("PVAC--1", "fan_rpm"): 2000.0,
            ("PVAC--1", "fan_target_rpm"): 2100.0,
        }

    def test_uses_shared_registry_and_value_check(self):
        # One vocabulary with MQTT: the store records from app/core/signals.py
        # and applies its finite-number check (huge ints can't raise)
        import app.core.timeseries as ts
        from app.core import signals

        assert ts.SIGNAL_METRICS is signals.SIGNAL_METRICS
        assert ts.SIGNAL_GROUPS is signals.SIGNAL_GROUPS
        assert extract_device_metrics({POD: {"HVP_PackTempMax": 10**400}}) == {}

    def test_pw3_pvac_block_never_adds_fans(self):
        # A PW3 unit's PVAC block (empty on current firmware) must not add
        # PW2-style fan series next to the real fans - in either source,
        # whatever the order (get_fan_speeds() lists PVAC first)
        pvac = "PVAC--1707000-11-J--TG1"
        pvac_fans = {
            "PVAC_Fan_Speed_Actual_RPM": 700,
            "PVAC_Fan_Speed_Target_RPM": 900,
        }
        for vitals, fans in (
            ({pvac: pvac_fans, **PW3_VITALS}, None),
            (PW3_VITALS, {pvac: pvac_fans, **PW3_FANS}),
            (None, {pvac: pvac_fans, **PW3_FANS}),
        ):
            metrics = extract_device_metrics(vitals, fans)
            assert not [k for k in metrics if k[0] == pvac], metrics
            assert any(k[0] == INV for k in metrics)  # the PW3 unit itself

    def test_pw2_pvac_block_still_recorded(self):
        # No TEPINV for that serial: a Powerwall 2/+ fan is recorded
        m = extract_device_metrics(
            {
                "PVAC--1707000-11-J--TG2": {"PVAC_Fan_Speed_Actual_RPM": 700},
                INV: {"PCH_FanSpeed_A": 1200},
            }
        )
        assert m[("PVAC--1707000-11-J--TG2", "fan_rpm")] == 700.0

    def test_skips_missing_and_bad_values(self):
        m = extract_device_metrics(
            {
                POD: {
                    "HVP_PackTempMax": None,
                    "HVP_PackTempMin": "35",
                    "HVP_ShuntTemperature": float("nan"),
                },
                INV: {"PCH_AmbientTemp": True},
                "junk": "not-a-dict",
            },
            None,
        )
        assert m == {}

    def test_empty_inputs(self):
        assert extract_device_metrics(None, None) == {}
        assert extract_device_metrics("x", []) == {}

    def test_registry_entries_are_complete(self):
        signals = []
        for metric, entry in SIGNAL_METRICS.items():
            assert set(entry) >= {"signals", "label", "unit", "group", "order"}
            assert entry["signals"], metric
            assert entry["group"] in SIGNAL_GROUPS, metric
            signals.extend(entry["signals"])
        # A signal feeds exactly one metric
        assert len(signals) == len(set(signals))
        for group in SIGNAL_GROUPS.values():
            assert set(group) >= {"label", "order", "zero_based", "decimals"}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecordDevice:
    @pytest.mark.asyncio
    async def test_interval_gating(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        metrics = extract_device_metrics(PW3_VITALS, PW3_FANS)
        t0 = 1_700_000_000.0
        assert await store.record_signal_sample("gw1", t0, metrics) is True
        assert await store.record_signal_sample("gw1", t0 + 5, metrics) is False
        assert await store.record_signal_sample("gw1", t0 + 55, metrics) is False
        # Poll jitter: 58s still counts as the next minute
        assert await store.record_signal_sample("gw1", t0 + 58, metrics) is True
        # Gating is per gateway
        assert await store.record_signal_sample("gw2", t0 + 60, metrics) is True
        status = await store.status()
        assert status["signal_series"] == 16
        assert status["signal_samples"] == 24
        await store.stop()

    @pytest.mark.asyncio
    async def test_daily_rollup_min_max_avg(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t0 = 1_700_006_400.0  # 00:00 UTC
        for i, value in enumerate([30.0, 40.0, 35.0]):
            await store.record_signal_sample(
                "gw1", t0 + i * 60, {(POD, "pack_temp_max"): value}, timezone="UTC"
            )
        trend = await store.get_signal_trend(
            start=t0 - 3600, end=t0 + 3600, resolution="daily"
        )
        (point,) = trend["series"][0]["points"]
        assert point["min"] == 30.0
        assert point["max"] == 40.0
        assert point["avg"] == pytest.approx(35.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_duplicate_ts_not_double_counted(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t0 = 1_700_006_400.0
        m = {(POD, "pack_temp_max"): 30.0}
        await store.record_signal_sample("gw1", t0, m, timezone="UTC")
        store._signal_last.clear()  # simulate restart
        await store.record_signal_sample(
            "gw1", t0, {(POD, "pack_temp_max"): 90.0}, timezone="UTC"
        )
        status = await store.status()
        assert status["signal_samples"] == 1
        trend = await store.get_signal_trend(
            start=t0 - 60, end=t0 + 60, resolution="daily"
        )
        assert trend["series"][0]["points"][0]["max"] == 30.0
        await store.stop()

    @pytest.mark.asyncio
    async def test_rollup_uses_gateway_local_day(self, tmp_path):
        store = store_for(tmp_path)
        # 2023-11-15 03:00 UTC is still Nov 14 in Los Angeles
        ts = 1_700_017_200.0
        await store.record_signal_sample(
            "gw1",
            ts,
            {(POD, "pack_temp_max"): 30.0},
            timezone="America/Los_Angeles",
        )
        info = await store.get_signal_series()
        assert info["series"][0]["first_day"] == "2023-11-14"
        await store.stop()

    @pytest.mark.asyncio
    async def test_disabled(self, tmp_path):
        store = store_for(tmp_path, signal_retention="-1")
        assert store.enabled is True
        assert store.signals_enabled is False
        metrics = extract_device_metrics(PW3_VITALS, PW3_FANS)
        assert await store.record_signal_sample("gw1", time.time(), metrics) is False
        status = await store.status()
        assert status["signals_enabled"] is False
        await store.stop()

    @pytest.mark.asyncio
    async def test_disabled_with_subsystem(self, tmp_path):
        store = store_for(tmp_path, retention="-1")
        assert store.signals_enabled is False
        body = await store.get_signal_trend()
        assert body["enabled"] is False and body["series"] == []
        series = await store.get_signal_series()
        await store.stop()
        # Same keys as an enabled store's responses
        enabled = store_for(tmp_path / "on")
        assert set(body) == set(await enabled.get_signal_trend())
        assert set(series) == set(await enabled.get_signal_series())
        await enabled.stop()

    @pytest.mark.asyncio
    async def test_pruning_keeps_daily(self, tmp_path):
        store = store_for(tmp_path, signal_retention="2h", signal_interval="60s")
        now = time.time()
        old = now - 3 * 86400
        await store.record_signal_sample("gw1", old, {(POD, "pack_temp_max"): 30.0})
        await store.record_signal_sample("gw1", now, {(POD, "pack_temp_max"): 31.0})
        await store.maintenance()
        status = await store.status()
        assert status["signal_samples"] == 1
        assert status["signal_daily_rows"] == 2
        await store.stop()


# ---------------------------------------------------------------------------
# Trend queries
# ---------------------------------------------------------------------------


class TestDeviceTrend:
    async def _seed(self, store, start, minutes, value=lambda i: 30.0 + i % 10):
        for i in range(minutes):
            await store.record_signal_sample(
                "gw1",
                start + i * 60,
                {(POD, "pack_temp_max"): value(i), (INV, "fan_a_rpm"): 1000.0 + i},
                timezone="UTC",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interval,window,expected",
        [
            ("30s", 1800, 30.0),  # short window: one point per sample
            ("30s", 3600, 30.0),  # never finer than the interval
            ("60s", 3600, 60.0),
            ("30s", 6 * 3600, 60.0),  # ~360 points, multiple of the interval
            ("30s", 86400, 240.0),  # a minute or more: whole minutes
            ("60s", 86400, 240.0),
        ],
    )
    async def test_raw_bucket_follows_interval(
        self, make_store, interval, window, expected
    ):
        store = make_store(signal_interval=interval)
        now = time.time()
        await self._seed(store, now - 600, 5)
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"], start=now - window, end=now, resolution="raw"
        )
        assert body["bucket_seconds"] == expected

    @pytest.mark.asyncio
    async def test_raw_buckets_and_filter(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3 * 3600, 180)
        body = await store.get_signal_trend(metrics=["pack_temp_max"], hours=4)
        assert body["resolution"] == "raw"
        assert body["bucket_seconds"] == 60.0
        (series,) = body["series"]
        assert series["metric"] == "pack_temp_max"
        assert series["unit"] == "°C"
        assert series["label"] == "Pack temp (max)"
        assert 170 <= len(series["points"]) <= 181
        p = series["points"][0]
        assert p["min"] <= p["avg"] <= p["max"]
        await store.stop()

    @pytest.mark.asyncio
    async def test_raw_long_window_buckets(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 10 * 3600, 600)
        body = await store.get_signal_trend(
            metrics=["fan_a_rpm"], start=now - 48 * 3600, end=now
        )
        assert body["resolution"] == "raw"
        assert body["bucket_seconds"] == 480.0  # 48h / 360 rounded to minutes
        assert len(body["series"][0]["points"]) <= 80
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_daily_for_long_windows(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3600, 30)
        body = await store.get_signal_trend(start=now - 30 * 86400, end=now)
        assert body["resolution"] == "daily"
        assert body["bucket_seconds"] == 86400.0
        assert all("day" in p for s in body["series"] for p in s["points"])
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_raw_when_no_older_history(self, tmp_path):
        """A fresh install shows its first hour at full detail even when the
        window starts before recording began."""
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 3600, 30)
        body = await store.get_signal_trend(start=now - 7 * 86400, end=now)
        assert body["resolution"] == "raw"
        await store.stop()

    @pytest.mark.asyncio
    async def test_auto_daily_when_older_daily_history(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s", signal_retention="2h")
        now = time.time()
        await self._seed(store, now - 5 * 86400, 5)  # older, pruned below
        await self._seed(store, now - 1800, 20)
        await store.maintenance()
        body = await store.get_signal_trend(start=now - 7 * 86400, end=now)
        assert body["resolution"] == "daily"
        await store.stop()

    @pytest.mark.asyncio
    async def test_device_and_gateway_filters(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 600, 5)
        body = await store.get_signal_trend(devices=[INV], hours=1)
        assert {s["device"] for s in body["series"]} == {INV}
        body = await store.get_signal_trend(gateway="other", hours=1)
        assert body["series"] == []
        await store.stop()

    @pytest.mark.asyncio
    async def test_default_interval_is_one_minute(self, make_store):
        store = make_store()
        assert store._signal_interval == 60
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(13)
        ]
        assert stored == [True] + [False] * 11 + [True]

    @pytest.mark.asyncio
    async def test_thirty_second_interval(self, make_store):
        store = make_store(signal_interval="30s")
        assert store._signal_interval == 30
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(7)
        ]
        assert stored == [True] + [False] * 5 + [True]

    @pytest.mark.asyncio
    async def test_interval_below_minimum_is_raised(self, make_store, caplog):
        with caplog.at_level("WARNING"):
            store = make_store(signal_interval="5s")
        assert store._signal_interval == 30
        assert "below the 30s minimum" in caplog.text
        assert "invalid" not in caplog.text
        now = time.time()
        metrics = {("TEPOD--1", "pack_temp_max"): 25.0}
        stored = [
            await store.record_signal_sample("default", now + i * 5, metrics)
            for i in range(7)
        ]
        assert stored == [True] + [False] * 5 + [True]

    @pytest.mark.parametrize("value", ["0", "-1", "-30s", 0, -5])
    def test_invalid_interval_uses_default(self, tmp_path, caplog, value):
        with caplog.at_level("WARNING"):
            store = store_for(tmp_path, signal_interval=value)
        assert store._signal_interval == 60
        assert "is invalid" in caplog.text
        assert "using the default 60s" in caplog.text
        assert "minimum" not in caplog.text

    @pytest.mark.asyncio
    async def test_series_listing(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await self._seed(store, now - 600, 5)
        info = await store.get_signal_series()
        assert info["signals_enabled"] is True
        assert info["interval_seconds"] == 60
        assert {s["metric"] for s in info["series"]} == {"pack_temp_max", "fan_a_rpm"}
        s = info["series"][0]
        assert s["first_ts"] <= s["last_ts"]
        assert s["first_day"] <= s["last_day"]
        assert "pack_temp_max" in info["metrics"]
        await store.stop()


# ---------------------------------------------------------------------------
# Daily energy date range
# ---------------------------------------------------------------------------


class TestDeviceTimezones:
    """Daily device rollups are keyed by gateway-local day."""

    @staticmethod
    def _ts(day, hour, tz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        local = datetime.fromisoformat(f"{day}T{hour:02d}:00")
        return local.replace(tzinfo=ZoneInfo(tz)).timestamp()

    @pytest.mark.asyncio
    async def test_daily_window_uses_gateway_local_days(self, make_store):
        tz = "Australia/Sydney"  # UTC+10/+11: local midnight is the UTC day before
        store = make_store(signal_interval="60s")
        days = (("2026-03-09", 10.0), ("2026-03-10", 20.0), ("2026-03-11", 30.0))
        for day, value in days:
            await store.record_signal_sample(
                "gw1",
                self._ts(day, 12, tz),
                {(POD, "pack_temp_max"): value},
                timezone=tz,
            )
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"],
            start=self._ts("2026-03-10", 0, tz),
            end=self._ts("2026-03-10", 23, tz),
            resolution="daily",
            timezones={"gw1": tz},
        )
        (series,) = body["series"]
        # Only the local day asked for: the UTC date of local midnight
        # (2026-03-09) must not pull in the previous day
        assert [p["day"] for p in series["points"]] == ["2026-03-10"]
        assert series["points"][0]["ts"] == self._ts("2026-03-10", 12, tz)

    @pytest.mark.asyncio
    async def test_daily_window_end_uses_gateway_local_day(self, make_store):
        tz = "America/Los_Angeles"  # evening local = next UTC day
        store = make_store(signal_interval="60s")
        for day, value in (("2026-03-10", 10.0), ("2026-03-11", 20.0)):
            await store.record_signal_sample(
                "gw1",
                self._ts(day, 12, tz),
                {(POD, "pack_temp_max"): value},
                timezone=tz,
            )
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"],
            start=self._ts("2026-03-10", 0, tz),
            end=self._ts("2026-03-10", 20, tz),
            resolution="daily",
            timezones={"gw1": tz},
        )
        (series,) = body["series"]
        # 20:00 local is already 03-11 in UTC; that local day isn't in range
        assert [p["day"] for p in series["points"]] == ["2026-03-10"]

    @pytest.mark.asyncio
    async def test_auto_resolution_judges_days_in_local_time(self, make_store):
        tz = "America/Los_Angeles"  # evening local = next UTC day
        store = make_store(signal_interval="60s")
        # First samples ever: 20:00-21:00 local on 03-09 (03:00+ UTC on 03-10)
        first = self._ts("2026-03-09", 20, tz)
        for i in range(60):
            await store.record_signal_sample(
                "gw1", first + i * 60, {(POD, "pack_temp_max"): 25.0}, timezone=tz
            )
        # Window starts well before the first sample; there is no older daily
        # history, so auto must stay raw (in UTC the 03-09 rollup looked older)
        body = await store.get_signal_trend(
            metrics=["pack_temp_max"],
            start=first - 6 * 3600,
            end=first + 3600,
            timezones={"gw1": tz},
        )
        assert body["resolution"] == "raw"


class TestSeriesCache:
    @pytest.mark.asyncio
    async def test_samples_survive_series_pruning(self, tmp_path):
        """A series pruned away is recreated, not written under a dead id."""
        store = store_for(tmp_path, signal_retention="90s", signal_interval="60s")
        now = time.time()
        metrics = {(POD, "pack_temp_max"): 30.0}
        await store.record_signal_sample("gw1", now - 7200, metrics, timezone="UTC")
        # Drop the daily rollup too so the whole series is removed
        store._ensure_conn().execute("DELETE FROM device_daily")
        store._maintenance_sync()
        assert (await store.status())["signal_series"] == 0
        await store.record_signal_sample("gw1", now, metrics, timezone="UTC")
        series = (await store.get_signal_series())["series"]
        assert [s["metric"] for s in series] == ["pack_temp_max"]
        assert (await store.status())["signal_samples"] == 1
        await store.stop()


class TestCatalogAndSchema:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("retention", ["24h", "-1"])
    async def test_catalog_is_a_copy(self, make_store, retention):
        store = make_store(retention=retention)  # enabled and disabled
        info = await store.get_signal_series()
        info["metrics"]["pack_temp_max"]["label"] = "changed"
        info["metrics"].clear()
        info["groups"]["temperature"].clear()
        assert SIGNAL_METRICS["pack_temp_max"]["label"] == "Pack temp (max)"
        assert SIGNAL_GROUPS["temperature"]
        info = await store.get_signal_series()
        assert info["metrics"] == SIGNAL_METRICS
        assert info["groups"] == SIGNAL_GROUPS

    @pytest.mark.asyncio
    async def test_series_table_stores_no_unit(self, make_store):
        store = make_store()
        await store.get_signal_series()  # create schema
        columns = [
            row["name"]
            for row in store._ensure_conn().execute("PRAGMA table_info(device_series)")
        ]
        assert columns == ["series_id", "gateway_id", "device", "metric"]

    @pytest.mark.asyncio
    async def test_dev_database_with_unit_column_still_records(
        self, make_store, tmp_path
    ):
        # A database from an earlier build of this feature kept a unit column
        conn = sqlite3.connect(str(tmp_path / "ts.db"))
        conn.execute(
            "CREATE TABLE device_series (series_id INTEGER PRIMARY KEY, "
            "gateway_id TEXT NOT NULL, device TEXT NOT NULL, "
            "metric TEXT NOT NULL, unit TEXT, UNIQUE (gateway_id, device, metric))"
        )
        conn.commit()
        conn.close()
        store = make_store(signal_interval="60s")
        stored = await store.record_signal_sample(
            "gw1", time.time(), {(POD, "pack_temp_max"): 30.0}
        )
        assert stored is True
        (series,) = (await store.get_signal_series())["series"]
        assert series["unit"] == "°C"


class TestPowerwallNumbering:
    """Units are numbered by the /pod index, exactly like /pod and the Console."""

    def test_labels_follow_pod_index(self):
        from app.api.legacy import powerwall_unit_labels

        system_status = {
            "battery_blocks": [
                {"PackageSerialNumber": "TG1LEAD"},
                {"PackageSerialNumber": "TG1EXP1"},
                {"PackageSerialNumber": "TG1FOLLOW"},
            ]
        }
        config = {
            "battery_blocks": [
                {
                    "vin": "1707000-11-J--TG1LEAD",
                    "battery_expansions": [{"din": "1807000-20-A--TG1EXP1"}],
                },
                {"vin": "1707000-11-J--TG1FOLLOW"},
            ]
        }
        assert powerwall_unit_labels(system_status, config) == {
            "TG1LEAD": {"label": "PW1", "order": 100},
            "TG1EXP1": {"label": "PW1 Exp 1", "order": 101},
            # /pod calls the third block PW3, so history does too
            "TG1FOLLOW": {"label": "PW3", "order": 300},
        }

    def test_type_only_expansion(self):
        from app.api.legacy import powerwall_unit_labels

        system_status = {
            "battery_blocks": [
                {"PackageSerialNumber": "TG1LEAD"},
                {"PackageSerialNumber": "TG1EXP", "Type": "BatteryExpansion"},
            ]
        }
        # No TEDAPI config: the block Type alone marks the expansion
        assert powerwall_unit_labels(system_status, None) == {
            "TG1LEAD": {"label": "PW1", "order": 100},
            "TG1EXP": {"label": "PW2 Exp", "order": 200},
        }
        assert powerwall_unit_labels(None, None) == {}

    def test_series_are_annotated(self, monkeypatch):
        import app.api.timeseries as api

        monkeypatch.setattr(
            api,
            "_powerwall_labels",
            lambda: {
                "gw1": {
                    "TG1LEAD": {"label": "PW1", "order": 100},
                    "TG1ORPHAN": {"label": "PW2 Exp", "order": 200},
                }
            },
        )
        result = api._annotate_powerwalls(
            {
                "series": [
                    {"gateway": "gw1", "device": "TEPOD--1707000-11-J--TG1LEAD"},
                    {"gateway": "gw1", "device": "TEPINV--1707000-11-J--TG1LEAD"},
                    # Not in the battery list yet: numbered after every known
                    # unit, never sharing an order with the orphan expansion
                    {"gateway": "gw1", "device": "TEPOD--1707000-11-J--TG1NEW"},
                ]
            }
        )
        labels = [(s["powerwall"], s["powerwall_order"]) for s in result["series"]]
        assert labels == [("PW1", 100), ("PW1", 100), ("PW3", 300)]

    def test_labels_count_every_pod_block(self):
        """/pod numbers by position, so unusable blocks still take a number."""
        from app.api.legacy import powerwall_unit_labels

        system_status = {
            "battery_blocks": [
                "not-a-block",
                {"Type": "Powerwall"},  # no serial
                {"PackageSerialNumber": "TG1A"},
                None,
                {"PackageSerialNumber": ""},
                {"PackageSerialNumber": "TG1B"},
            ]
        }
        assert powerwall_unit_labels(system_status, None) == {
            "TG1A": {"label": "PW3", "order": 300},
            "TG1B": {"label": "PW6", "order": 600},
        }

    def test_unknown_serial_follows_highest_unit(self, monkeypatch):
        import app.api.timeseries as api

        # Two known units numbered up to 3: next is PW4, not PW3 (= count + 1)
        monkeypatch.setattr(
            api,
            "_powerwall_labels",
            lambda: {
                "gw1": {
                    "TG1EXP1": {"label": "PW1 Exp 1", "order": 101},
                    "TG1FOLLOW": {"label": "PW3", "order": 300},
                }
            },
        )
        result = api._annotate_powerwalls(
            {
                "series": [
                    {"gateway": "gw1", "device": "TEPOD--1707000-11-J--TG1NEW"},
                    {"gateway": "gw1", "device": "TEPOD--1707000-11-J--TG1FOLLOW"},
                ]
            }
        )
        labels = [(s["powerwall"], s["powerwall_order"]) for s in result["series"]]
        assert labels == [("PW4", 400), ("PW3", 300)]

    def test_labels_survive_cache_expiry(self, mock_gateway_manager):
        """An outage longer than PW_CACHE_TTL must not renumber the units."""
        import app.api.timeseries as api
        from app.models.gateway import Gateway, GatewayStatus, PowerwallData

        gw = Gateway(id="gw1", name="G", host="1.2.3.4", gw_pwd="x")
        mock_gateway_manager.gateways["gw1"] = gw
        # PW1 sorts after PW2 by serial, so numbering by serial would swap them
        data = PowerwallData(
            system_status={
                "battery_blocks": [
                    {"PackageSerialNumber": "TG1ZZZ"},
                    {"PackageSerialNumber": "TG1AAA"},
                ]
            },
            timestamp=time.time(),
        )
        expected = {
            "TG1ZZZ": {"label": "PW1", "order": 100},
            "TG1AAA": {"label": "PW2", "order": 200},
        }
        mock_gateway_manager.cache["gw1"] = GatewayStatus(
            gateway=gw, online=True, data=data
        )
        mock_gateway_manager._last_successful_data["gw1"] = data
        assert api._powerwall_labels() == {"gw1": expected}
        # Offline past the cache TTL: the cached status has no data left
        data.timestamp = time.time() - 3600
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)
        assert mock_gateway_manager.get_gateway("gw1").data is None
        assert api._powerwall_labels() == {"gw1": expected}
        # Before any successful poll the cached status is the fallback
        mock_gateway_manager._last_successful_data.clear()
        mock_gateway_manager.cache["gw1"] = GatewayStatus(
            gateway=gw, online=True, data=data
        )
        assert api._powerwall_labels() == {"gw1": expected}


class TestSignalTrendBounds:
    @pytest.mark.parametrize(
        "query", ["start=nan", "start=inf", "start=1e20", "end=-1"]
    )
    def test_signal_trend_bad_times_are_422(self, client, query):
        assert client.get(f"/api/timeseries/signal_trend?{query}").status_code == 422

    @pytest.mark.parametrize("query", ["start=nan", "end=inf"])
    def test_trend_rejects_only_non_finite(self, client, query):
        assert client.get(f"/api/timeseries/trend?{query}").status_code == 422

    @pytest.mark.parametrize(
        "route,query",
        [
            # Released routes keep accepting what they accepted on main
            ("trend", "end=9999999999"),
            ("trend", "start=0&end=9999999999"),
            ("trend", "start=-1"),
            ("trend", "start=1790000000000"),  # milliseconds
            ("samples", "end=9999999999"),
            ("samples", "start=-1"),
            ("samples", "start=nan"),
        ],
    )
    def test_released_routes_unchanged(self, client, route, query):
        assert client.get(f"/api/timeseries/{route}?{query}").status_code == 200

    @pytest.mark.asyncio
    async def test_raw_beyond_max_span_reads_daily(self, make_store):
        store = make_store(signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 60, {(POD, "pack_temp_max"): 30.0}
        )
        body = await store.get_signal_trend(
            start=now - 30 * 86400, end=now, resolution="raw"
        )
        assert body["resolution"] == "daily"

    @pytest.mark.asyncio
    async def test_large_row_estimate_reads_daily(self, make_store, monkeypatch):
        import app.core.timeseries as ts

        monkeypatch.setattr(ts, "SIGNAL_RAW_MAX_ROWS", 100)
        store = make_store(signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 60, {(POD, "pack_temp_max"): 30.0}
        )
        # 1 series x 6h / 60s = 360 estimated rows > 100
        for resolution in ("raw", "auto"):
            body = await store.get_signal_trend(
                start=now - 6 * 3600, end=now, resolution=resolution
            )
            assert body["resolution"] == "daily", resolution


class TestDailyRange:
    @pytest.mark.asyncio
    async def test_start_end(self, tmp_path):
        store = store_for(tmp_path)
        await store.get_daily_energy()  # create schema
        conn = store._ensure_conn()
        for day in ("2024-01-01", "2024-06-15", "2025-01-01", "2026-09-01"):
            conn.execute(
                "INSERT INTO daily_energy (gateway_id, day, solar_kwh, updated_at) "
                "VALUES ('gw1', ?, 1.0, 0)",
                (day,),
            )
        conn.commit()
        body = await store.get_daily_energy(
            start_day="2024-01-01", end_day="2025-01-01"
        )
        assert [d["day"] for d in body["days"]] == [
            "2025-01-01",
            "2024-06-15",
            "2024-01-01",
        ]
        body = await store.get_daily_energy(end_day="2024-12-31")
        assert [d["day"] for d in body["days"]] == ["2024-06-15", "2024-01-01"]
        body = await store.get_daily_energy(start_day="2026-01-01")
        assert [d["day"] for d in body["days"]] == ["2026-09-01"]
        await store.stop()


# ---------------------------------------------------------------------------
# API + route
# ---------------------------------------------------------------------------


class TestHistoryAPI:
    def test_daily_range_params(self, client):
        resp = client.get("/api/timeseries/daily?start=2026-01-01&end=2026-01-31")
        assert resp.status_code == 200
        assert resp.json()["days"] == []
        assert client.get("/api/timeseries/daily?start=2026-1-1").status_code == 422
        assert client.get("/api/timeseries/daily?end=yesterday").status_code == 422

    def test_daily_rejects_impossible_dates(self, client):
        # Well-formed but not a real calendar date
        assert client.get("/api/timeseries/daily?start=2026-02-31").status_code == 422
        assert client.get("/api/timeseries/daily?end=2026-13-01").status_code == 422
        assert client.get("/api/timeseries/daily?start=2024-02-29").status_code == 200

    @pytest.mark.parametrize("param", ["start", "end"])
    def test_daily_rejects_basic_format_dates(self, client, monkeypatch, param):
        """YYYYMMDD is rejected even where date.fromisoformat accepts it.

        Python 3.11+ (the Docker image runs 3.12) parses "20260101", so the
        YYYY-MM-DD pattern is the only guard there. Use that parser on every
        version so this test can't pass on 3.10's stricter one.
        """
        from datetime import date, datetime

        import app.api.timeseries as api

        class Py312Date:
            @staticmethod
            def fromisoformat(value):
                if len(value) == 8 and value.isdigit():
                    return datetime.strptime(value, "%Y%m%d").date()
                return date.fromisoformat(value)

        monkeypatch.setattr(api, "date", Py312Date)
        resp = client.get(f"/api/timeseries/daily?{param}=20260101")
        assert resp.status_code == 422
        assert (
            client.get(f"/api/timeseries/daily?{param}=2026-01-01").status_code == 200
        )

    def test_signals_endpoint(self, client):
        body = client.get("/api/timeseries/signals").json()
        assert body["enabled"] is True
        assert body["signals_enabled"] is True
        assert body["series"] == []
        assert "pack_temp_max" in body["metrics"]
        assert list(body["groups"]) == ["temperature", "fan_speed", "fan_duty"]
        assert body["groups"]["fan_speed"]["zero_based"] is True

    def test_signal_trend_endpoint(self, client):
        resp = client.get(
            "/api/timeseries/signal_trend?metrics=pack_temp_max,fan_a_rpm&hours=6"
        )
        assert resp.status_code == 200
        assert resp.json()["series"] == []
        assert (
            client.get("/api/timeseries/signal_trend?resolution=weekly").status_code
            == 422
        )

    def test_status_reports_signal_fields(self, client):
        body = client.get("/api/timeseries/status").json()
        for key in (
            "signals_enabled",
            "signal_retention_seconds",
            "signal_interval_seconds",
            "signal_series",
            "signal_samples",
            "signal_daily_rows",
        ):
            assert key in body

    def test_history_page(self, client):
        resp = client.get("/history")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "{PROXY_BASE" not in resp.text
        assert "/api/timeseries/signal_trend" in resp.text

    def test_history_page_is_catalog_driven(self, client):
        # Cards come from /api/timeseries/signals; the page must not name
        # individual metrics or groups (a new one should need no page change)
        page = client.get("/history").text
        for name in SIGNAL_METRICS:
            assert name not in page, name
        for group in SIGNAL_GROUPS:  # as a code identifier, not prose
            assert f"'{group}'" not in page and f'"{group}"' not in page, group

    def test_energy_trend_script_shared(self, client):
        # Console and History load one shared Energy Trend chart file
        for page in ("/history", "/console"):
            html = client.get(page).text
            assert "/static/js/charts.js" in html
            assert "/static/css/charts.css" in html
        resp = client.get("/static/js/charts.js")
        assert resp.status_code == 200
        assert "window.PWCharts" in resp.text
        assert client.get("/static/css/charts.css").status_code == 200

    def test_history_page_proxy_base(self, client, monkeypatch):
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "_proxy_base", "/pypowerwall")
        resp = client.get("/history")
        assert resp.status_code == 200
        assert 'var _BASE = "/pypowerwall"' in resp.text
        assert 'href="/pypowerwall/console"' in resp.text

    def test_console_links_to_history(self, client):
        assert 'href="/history"' in client.get("/console").text


# ---------------------------------------------------------------------------
# Poll-loop wiring
# ---------------------------------------------------------------------------


class TestPollWiring:
    @pytest.mark.asyncio
    async def test_poll_records_device_signals(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "wired.db"))
        ts_mod.reset_timeseries_store()
        mock_pypowerwall.vitals.return_value = PW3_VITALS
        mock_pypowerwall.tedapi.get_fan_speeds.return_value = PW3_FANS

        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")
        await mock_gateway_manager._poll_gateway("gw1")  # gated: same minute

        store = ts_mod.get_timeseries_store()
        info = await store.get_signal_series(gateway="gw1")
        assert {(s["device"], s["metric"]) for s in info["series"]} == set(
            extract_device_metrics(PW3_VITALS, PW3_FANS)
        )
        assert (await store.status())["signal_samples"] == 8
        ts_mod.reset_timeseries_store()

    @pytest.mark.asyncio
    async def test_poll_skips_when_disabled(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "off.db"))
        monkeypatch.setattr(settings, "timeseries_signal_retention", "-1")
        ts_mod.reset_timeseries_store()
        mock_pypowerwall.vitals.return_value = PW3_VITALS

        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")
        status = await ts_mod.get_timeseries_store().status()
        assert status["signal_samples"] == 0
        assert status["samples"] == 1  # power samples unaffected
        ts_mod.reset_timeseries_store()


# ---------------------------------------------------------------------------
# Review follow-ups: failure paths, retention, gating, bounds
# ---------------------------------------------------------------------------


class TestExtractEdges:
    def test_non_finite_values_skipped(self):
        vitals = {
            POD: {"HVP_PackTempMax": float("inf"), "HVP_PackTempMin": float("nan")}
        }
        assert extract_device_metrics(vitals, None) == {}

    def test_fan_speeds_take_precedence_over_vitals(self):
        vitals = {INV: {"PCH_FanSpeed_A": 100}}
        fans = {INV: {"PCH_FanSpeed_A": 200}}
        assert extract_device_metrics(vitals, fans)[(INV, "fan_a_rpm")] == 200.0


class TestGating:
    @pytest.mark.asyncio
    async def test_gate_is_per_series(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t = time.time()
        # Vitals timed out: only fans this poll
        assert await store.record_signal_sample("gw1", t, {(INV, "fan_a_rpm"): 900.0})
        # Next poll has temperatures: not blocked by the fan sample
        assert await store.record_signal_sample(
            "gw1", t + 5, {(POD, "pack_temp_max"): 30.0, (INV, "fan_a_rpm"): 950.0}
        )
        status = await store.status()
        assert status["signal_samples"] == 2  # fan gated, temperature recorded
        await store.stop()

    @pytest.mark.asyncio
    async def test_clock_stepping_back_resets_gate(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        t = time.time()
        metrics = {(POD, "pack_temp_max"): 30.0}
        assert await store.record_signal_sample("gw1", t, metrics)
        assert await store.record_signal_sample("gw1", t - 3600, metrics)
        await store.stop()


class TestRetention:
    @pytest.mark.asyncio
    async def test_prune_keeps_floor(self, tmp_path):
        store = store_for(tmp_path, signal_retention="90s", signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 7200, {(POD, "pack_temp_max"): 1.0}
        )
        await store.record_signal_sample(
            "gw1", now - 1800, {(POD, "pack_temp_max"): 2.0}
        )
        store._maintenance_sync()
        # 90s retention, but the last hour always stays
        assert (await store.status())["signal_samples"] == 1
        await store.stop()

    @pytest.mark.asyncio
    async def test_daily_rollups_pruned(self, tmp_path):
        store = store_for(tmp_path, daily_retention="2d", signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 10 * 86400, {(POD, "pack_temp_max"): 1.0}, timezone="UTC"
        )
        await store.record_signal_sample(
            "gw1", now, {(POD, "pack_temp_max"): 2.0}, timezone="UTC"
        )
        store._maintenance_sync()
        assert (await store.status())["signal_daily_rows"] == 1
        await store.stop()

    @pytest.mark.asyncio
    async def test_recording_off_still_prunes_existing(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 40 * 86400, {(POD, "pack_temp_max"): 1.0}
        )
        await store.record_signal_sample("gw1", now - 60, {(POD, "pack_temp_max"): 2.0})
        await store.record_signal_sample(
            "gw1", now - 40 * 86400, {("TETHC--1", "controller_ambient"): 3.0}
        )
        await store.stop()
        # Operator turns recording off: earlier data must still age out
        off = store_for(tmp_path, signal_retention="-1", daily_retention="7d")
        assert off.signals_enabled is False
        off._maintenance_sync()
        status = await off.status()
        assert status["signal_samples"] == 1
        # The TETHC series has no samples or daily rows left: removed
        assert status["signal_series"] == 1
        await off.stop()


class TestTrendWindow:
    @pytest.mark.asyncio
    async def test_daily_upper_bound(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        base = time.time() - 5 * 86400
        for i in range(4):
            await store.record_signal_sample(
                "gw1",
                base + i * 86400,
                {(POD, "pack_temp_max"): float(i)},
                timezone="UTC",
            )
        body = await store.get_signal_trend(
            start=base, end=base + 86400, resolution="daily", timezones={"gw1": "UTC"}
        )
        (series,) = body["series"]
        assert len(series["points"]) == 2  # days after the window excluded
        await store.stop()

    @pytest.mark.asyncio
    async def test_raw_upper_bound(self, make_store):
        store = make_store(signal_interval="60s")
        now = time.time()
        for ts, value in ((now - 1800, 30.0), (now - 60, 99.0)):
            await store.record_signal_sample("gw1", ts, {(POD, "pack_temp_max"): value})
        body = await store.get_signal_trend(
            start=now - 3600, end=now - 600, resolution="raw"
        )
        (series,) = body["series"]
        # The sample after the window's end is excluded
        assert [p["avg"] for p in series["points"]] == [30.0]

    @pytest.mark.asyncio
    async def test_start_end_swapped(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await store.record_signal_sample(
            "gw1", now - 600, {(POD, "pack_temp_max"): 30.0}
        )
        body = await store.get_signal_trend(start=now, end=now - 3600, resolution="raw")
        assert body["start"] < body["end"]
        assert body["series"][0]["points"]
        await store.stop()

    def test_hours_bounds(self, client):
        assert client.get("/api/timeseries/signal_trend?hours=0").status_code == 422
        assert (
            client.get("/api/timeseries/signal_trend?hours=100000000").status_code
            == 422
        )


class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_write_failure_counted(self, tmp_path, monkeypatch):
        import sqlite3

        store = store_for(tmp_path, signal_interval="60s")

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store, "_series_id", boom)
        ok = await store.record_signal_sample(
            "gw1", time.time(), {(POD, "pack_temp_max"): 30.0}
        )
        assert ok is False
        assert (await store.status())["write_failures"] == 1
        await store.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("store broke"), "timeout"])
    async def test_poll_continues_when_store_fails(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall, error
    ):
        import asyncio

        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "f.db"))
        ts_mod.reset_timeseries_store()
        store = ts_mod.get_timeseries_store()

        async def failing(*args, **kwargs):
            if error == "timeout":
                raise asyncio.TimeoutError()
            raise error

        monkeypatch.setattr(store, "record_signal_sample", failing)
        mock_pypowerwall.vitals.return_value = PW3_VITALS
        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")  # must not raise
        assert mock_gateway_manager.cache["gw1"].online is True
        assert (await store.status())["samples"] == 1  # power still recorded
        ts_mod.reset_timeseries_store()

    @pytest.mark.asyncio
    async def test_preserved_vitals_not_recorded(
        self, tmp_path, monkeypatch, mock_gateway_manager
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, PowerwallData

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "p.db"))
        ts_mod.reset_timeseries_store()
        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        data = PowerwallData(
            vitals=PW3_VITALS, fan_speeds=PW3_FANS, timestamp=time.time()
        )
        # The multi-PW guard copied last poll's vitals forward
        mock_gateway_manager._vitals_preserved.add("gw1")
        await mock_gateway_manager._record_signal_sample("gw1", gw, data)
        info = await ts_mod.get_timeseries_store().get_signal_series(gateway="gw1")
        groups = {s["metric"] for s in info["series"]}
        assert groups and all("fan" in m for m in groups)  # fans only
        ts_mod.reset_timeseries_store()


class TestReviewCoverage:
    """Guards for changes the earlier tests wouldn't have caught."""

    def test_daily_reversed_range_is_422(self, client):
        resp = client.get("/api/timeseries/daily?start=2026-03-10&end=2026-03-01")
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["query", "start"]

    def test_status_reports_gateway_names(self, client, mock_gateway_manager):
        from app.models.gateway import Gateway

        gw = Gateway(id="gw1", name="Home", host="1.2.3.4", gw_pwd="x")
        mock_gateway_manager.gateways["gw1"] = gw
        assert client.get("/api/timeseries/status").json()["gateway_names"] == {
            "gw1": "Home"
        }

    def test_vitals_preserved_is_set_and_cleared(self, mock_gateway_manager):
        from app.models.gateway import PowerwallData

        config = {"battery_blocks": [{"vin": "A--TG1"}, {"vin": "B--TG2"}]}
        full = PowerwallData(
            vitals={"TEPINV--A--TG1": {}, "TEPINV--B--TG2": {}},
            tedapi_config=config,
        )
        mock_gateway_manager._last_successful_data["gw1"] = full
        partial = PowerwallData(vitals={"TEPINV--A--TG1": {}}, tedapi_config=config)
        mock_gateway_manager._preserve_complete_multi_pw_snapshot("gw1", partial)
        assert "gw1" in mock_gateway_manager._vitals_preserved
        complete = PowerwallData(
            vitals={"TEPINV--A--TG1": {}, "TEPINV--B--TG2": {}},
            tedapi_config=config,
        )
        mock_gateway_manager._preserve_complete_multi_pw_snapshot("gw1", complete)
        assert "gw1" not in mock_gateway_manager._vitals_preserved

    @pytest.mark.asyncio
    async def test_poll_passes_gateway_timezone(
        self, tmp_path, monkeypatch, mock_gateway_manager
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, PowerwallData

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "tz.db"))
        ts_mod.reset_timeseries_store()
        store = ts_mod.get_timeseries_store()
        seen = {}

        async def capture(gateway_id, ts, metrics, timezone=None):
            seen["timezone"] = timezone
            return True

        monkeypatch.setattr(store, "record_signal_sample", capture)
        gw = Gateway(
            id="gw1", name="G", host="1.2.3.4", gw_pwd="x", timezone="Asia/Tokyo"
        )
        data = PowerwallData(vitals=PW3_VITALS, timestamp=time.time())
        await mock_gateway_manager._record_signal_sample("gw1", gw, data)
        assert seen["timezone"] == "Asia/Tokyo"
        ts_mod.reset_timeseries_store()

    def test_signal_trend_passes_timezones(
        self, client, monkeypatch, mock_gateway_manager
    ):
        import app.api.timeseries as api
        from app.models.gateway import Gateway

        mock_gateway_manager.gateways["gw1"] = Gateway(
            id="gw1", name="G", host="1.2.3.4", gw_pwd="x", timezone="Asia/Tokyo"
        )
        seen = {}

        class Store:
            async def get_signal_trend(self, **kwargs):
                seen.update(kwargs)
                return {"enabled": True, "series": []}

        monkeypatch.setattr(api, "get_timeseries_store", lambda: Store())
        assert client.get("/api/timeseries/signal_trend").status_code == 200
        assert seen["timezones"]["gw1"] == "Asia/Tokyo"

    @pytest.mark.asyncio
    async def test_ranged_daily_not_trimmed(self, tmp_path):
        store = store_for(tmp_path)
        base = time.time() - 12 * 86400
        for i in range(10):
            t = base + i * 86400
            await store.record_sample("gw1", t, 1000, 500, 0, -500, timezone="UTC")
            await store.record_sample("gw1", t + 60, 1000, 500, 0, -500, timezone="UTC")
        from datetime import datetime, timezone

        day = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        body = await store.get_daily_energy(
            start_day=day(base), end_day=day(base + 9 * 86400)
        )
        assert len(body["days"]) == 10  # a range isn't cut to the default 7
        await store.stop()

    @pytest.mark.asyncio
    async def test_daily_trend_capped_at_now(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        # A sample stamped two days ahead (clock skew) must not show up
        await store.record_signal_sample(
            "gw1", now + 2 * 86400, {(POD, "pack_temp_max"): 99.0}, timezone="UTC"
        )
        await store.record_signal_sample(
            "gw1", now - 86400, {(POD, "pack_temp_max"): 30.0}, timezone="UTC"
        )
        body = await store.get_signal_trend(
            start=now - 3 * 86400,
            end=now + 5 * 86400,
            resolution="daily",
            timezones={"gw1": "UTC"},
        )
        values = [p["avg"] for p in body["series"][0]["points"]]
        assert 99.0 not in values and 30.0 in values
        await store.stop()


class TestReadLane:
    """Queries use a read-only connection on their own thread (WAL)."""

    @pytest.mark.asyncio
    async def test_long_read_does_not_delay_writes(self, tmp_path):
        import asyncio

        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await store.record_sample("gw1", now - 10, 1000, 500, 0, -500)

        def slow_read():
            # A query holding its connection (and a read transaction) for 2 s
            with store._reader() as open_conn:
                conn = open_conn()
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM samples").fetchone()
                time.sleep(2.0)
                conn.execute("COMMIT")

        loop = asyncio.get_running_loop()
        reading = loop.run_in_executor(store._query_executor(), slow_read)
        await asyncio.sleep(0.2)  # the read is in progress
        started = time.monotonic()
        await asyncio.wait_for(
            store.record_sample("gw1", now, 1000, 500, 0, -500), timeout=1.0
        )
        await asyncio.wait_for(
            store.record_signal_sample("gw1", now, {(POD, "pack_temp_max"): 30.0}),
            timeout=1.0,
        )
        assert time.monotonic() - started < 1.0
        await reading
        await store.stop()

    @pytest.mark.asyncio
    async def test_reads_see_committed_writes(self, tmp_path):
        store = store_for(tmp_path, signal_interval="60s")
        now = time.time()
        await store.record_sample("gw1", now - 30, 1000, 500, 0, -500)
        await store.record_sample("gw1", now, 1000, 500, 0, -500)
        assert (await store.get_samples(gateway="gw1"))["count"] == 2
        assert store._read_conn is not None  # served by the read lane
        await store.record_signal_sample("gw1", now, {(POD, "pack_temp_max"): 31.0})
        series = (await store.get_signal_series())["series"]
        assert [s["metric"] for s in series] == ["pack_temp_max"]
        assert (await store.status())["samples"] == 2
        await store.stop()
        assert store._read_conn is None and store._read_executor is None

    @pytest.mark.asyncio
    async def test_memory_database_falls_back_to_one_lane(self):
        store = TimeSeriesStore(db_path=":memory:", signal_interval="60s")
        now = time.time()
        await store.record_sample("gw1", now - 30, 1000, 500, 0, -500)
        await store.record_sample("gw1", now, 1000, 500, 0, -500)
        assert (await store.get_samples(gateway="gw1"))["count"] == 2
        assert store._read_conn is None and store._read_executor is None
        await store.stop()

    @pytest.mark.asyncio
    async def test_read_connection_is_read_only(self, make_store):
        store = make_store()
        await store.record_sample("gw1", time.time(), 1000, 500, 0, -500)
        assert (await store.get_samples())["count"] == 1
        with pytest.raises(sqlite3.OperationalError):
            store._read_conn.execute("DELETE FROM samples")

    @pytest.mark.asyncio
    async def test_read_connection_busy_timeout(self, make_store):
        store = make_store()
        await store.get_samples()
        # 5 s, not the 10 s connect() timeout it would otherwise inherit
        busy = store._read_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == 5000

    @pytest.mark.asyncio
    async def test_failed_read_only_open_uses_writer_lane(
        self, make_store, monkeypatch, caplog
    ):
        import app.core.timeseries as ts_mod

        real_connect = sqlite3.connect
        attempts = []

        def connect(*args, **kwargs):
            if kwargs.get("uri"):  # the read-only open
                attempts.append(args[0])
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(ts_mod.sqlite3, "connect", connect)
        store = make_store()
        now = time.time()
        await store.record_sample("gw1", now - 30, 1000, 500, 0, -500)
        await store.record_sample("gw1", now, 1000, 500, 0, -500)
        with caplog.at_level("WARNING", logger="app.core.timeseries"):
            assert (await store.get_samples(gateway="gw1"))["count"] == 2
            assert (await store.status())["samples"] == 2
        assert len(attempts) == 1  # not retried on every query
        assert store._read_conn is None
        assert store._query_executor() is store._executor  # the writer lane
        warned = [r for r in caplog.records if "read-only open" in r.getMessage()]
        assert [r.levelname for r in warned] == ["WARNING"]

    @pytest.mark.asyncio
    async def test_without_wal_reads_use_writer_lane(
        self, make_store, monkeypatch, caplog
    ):
        import app.core.timeseries as ts_mod

        class NoWal(sqlite3.Connection):
            """A filesystem that refuses WAL: journal_mode stays "delete"."""

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA journal_mode"):
                    sql = "PRAGMA journal_mode"
                return super().execute(sql, *args)

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            ts_mod.sqlite3,
            "connect",
            lambda *args, **kwargs: real_connect(*args, factory=NoWal, **kwargs),
        )
        store = make_store()
        now = time.time()
        with caplog.at_level("WARNING", logger="app.core.timeseries"):
            await store.record_sample("gw1", now - 30, 1000, 500, 0, -500)
            await store.record_sample("gw1", now, 1000, 500, 0, -500)
            assert (await store.get_samples(gateway="gw1"))["count"] == 2
            assert (await store.status())["samples"] == 2
        assert store._read_conn is None and store._read_executor is None
        warned = [r for r in caplog.records if "WAL" in r.getMessage()]
        assert [r.levelname for r in warned] == ["WARNING"]

    @pytest.mark.asyncio
    async def test_stop_closes_read_lane_and_wal_files(self, make_store, tmp_path):
        store = make_store()
        await store.record_sample("gw1", time.time(), 1000, 500, 0, -500)
        assert (await store.get_samples())["count"] == 1
        read_conn, read_executor = store._read_conn, store._read_executor
        wal, shm = tmp_path / "ts.db-wal", tmp_path / "ts.db-shm"
        assert wal.exists() and shm.exists()
        await store.stop()
        with pytest.raises(sqlite3.ProgrammingError):
            read_conn.execute("SELECT 1")  # really closed, not just dropped
        with pytest.raises(RuntimeError):
            read_executor.submit(time.time)  # shut down
        # Closing the reader first lets the writer remove the WAL files
        assert not wal.exists() and not shm.exists()

    @pytest.mark.asyncio
    async def test_stopped_store_never_reopens(self, make_store):
        store = make_store(signal_interval="60s")
        now = time.time()
        await store.record_sample("gw1", now - 30, 1000, 500, 0, -500)
        assert (await store.get_samples())["count"] == 1
        await store.stop()

        class NoLock:
            def __enter__(self):
                raise AssertionError("a query after stop() took a store lock")

            def __exit__(self, *exc):
                return False

        # Late queries run on the event loop, so they must not wait on a lock
        locks = store._lock, store._read_lock
        store._lock = store._read_lock = NoLock()
        try:
            metrics = {(POD, "pack_temp_max"): 30.0}
            assert await store.record_sample("gw1", now, 1000, 500, 0, -500) is None
            assert await store.record_signal_sample("gw1", now, metrics) is False
            assert (await store.get_samples())["samples"] == []
            assert (await store.get_daily_energy())["days"] == []
            assert (await store.get_trend())["points"] == []
            assert (await store.get_signal_series())["series"] == []
            assert (await store.get_signal_trend())["series"] == []
            assert await store.get_today("gw1") is None
            status = await store.status()
            assert status["samples"] == 0 and status["write_failures"] == 0
            await store.maintenance()
            await store.start()
        finally:
            store._lock, store._read_lock = locks
        assert store._conn is None and store._read_conn is None
        assert store._executor is None and store._read_executor is None
        assert store._maintenance_task is None


class TestStatus:
    """/status reports zeros, never creating or failing on the database."""

    @pytest.mark.asyncio
    async def test_status_does_not_create_database(self, make_store, tmp_path):
        store = make_store()
        status = await store.status()
        assert status["samples"] == 0 and status["db_size_bytes"] == 0
        assert not (tmp_path / "ts.db").exists()

    @pytest.mark.asyncio
    async def test_status_when_database_dir_cannot_be_created(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")  # a file where the directory should be
        store = TimeSeriesStore(db_path=str(blocker / "data" / "ts.db"))
        status = await store.status()
        assert status["enabled"] is True and status["samples"] == 0
        await store.stop()

    def test_status_endpoint_when_database_dir_cannot_be_created(
        self, client, monkeypatch, tmp_path
    ):
        import app.core.timeseries as ts_mod
        from app.config import settings

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setattr(
            settings, "timeseries_path", str(blocker / "data" / "ts.db")
        )
        ts_mod.reset_timeseries_store()
        resp = client.get("/api/timeseries/status")
        assert resp.status_code == 200
        assert resp.json()["samples"] == 0


@pytest.mark.asyncio
async def test_status_counts_rows_of_an_in_memory_store():
    # ":memory:" has no file to check: rows are counted once it is open
    store = TimeSeriesStore(db_path=":memory:")
    try:
        assert (await store.status())["signal_samples"] == 0
        await store.record_signal_sample(
            "gw1", time.time(), {(POD, "pack_temp_max"): 30.0}
        )
        status = await store.status()
        assert status["signal_samples"] == 1
        assert status["signal_series"] == 1
    finally:
        await store.stop()
