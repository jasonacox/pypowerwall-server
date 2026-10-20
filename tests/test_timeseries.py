"""Tests for the TimeSeriesStore (app/core/timeseries.py).

Covers:
    - parse_duration retention parsing
    - Directional sign splitting (battery charge/discharge, grid import/export)
    - Trapezoidal power->kWh integration accuracy
    - Local-midnight day splitting (energy accrues to the correct local day)
    - 1-hour gap threshold (no integration across stale gaps)
    - Restart persistence (integration state + daily aggregates survive close)
    - Out-of-order / duplicate sample handling
    - Raw sample pruning vs. the keep-floor
    - Disabled subsystem (PW_TIMESERIES_RETENTION=-1)
    - /api/timeseries/* endpoints (enabled and disabled)
"""

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.timeseries import (
    GAP_THRESHOLD,
    RAW_KEEP_FLOOR,
    TimeSeriesStore,
    parse_duration,
)

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_suffixed(self):
        assert parse_duration("24h") == 86400
        assert parse_duration("7d") == 604800
        assert parse_duration("30d") == 2592000
        assert parse_duration("365d") == 31536000
        assert parse_duration("90s") == 90
        assert parse_duration("2w") == 1209600
        assert parse_duration("10m") == 600

    def test_bare_number_is_seconds(self):
        assert parse_duration("3600") == 3600
        assert parse_duration("0") == 0

    def test_disabled_and_negative(self):
        assert parse_duration("-1") == -1
        assert parse_duration("-24h") == -1
        assert parse_duration("-5") == -1

    def test_invalid_raises(self):
        for bad in ("", "abc", "12x", "h", "1.5h"):
            with pytest.raises(ValueError):
                parse_duration(bad)

    def test_coerce_in_constructor(self):
        store = TimeSeriesStore(db_path=":memory:", retention="garbage")
        # Falls back to default 24h rather than raising
        assert store._retention == 86400
        store = TimeSeriesStore(db_path=":memory:", retention=7 * 86400)
        assert store._retention == 604800
        store = TimeSeriesStore(db_path=":memory:", retention=-1)
        assert store.enabled is False


class TestResolveDbPath:
    """PW_TIMESERIES_PATH may be misconfigured as a directory rather than a
    file; the store should fall back to a default filename instead of
    failing to open the SQLite connection."""

    def test_memory_sentinel_untouched(self):
        assert TimeSeriesStore._resolve_db_path(":memory:") == ":memory:"

    def test_trailing_slash_appends_filename(self):
        assert TimeSeriesStore._resolve_db_path("/data/") == "/data/timeseries.db"

    def test_existing_directory_appends_filename(self, tmp_path):
        # No trailing slash, but it's a real directory on disk.
        assert TimeSeriesStore._resolve_db_path(str(tmp_path)) == str(
            tmp_path / "timeseries.db"
        )

    def test_file_path_untouched(self, tmp_path):
        target = str(tmp_path / "custom.db")
        assert TimeSeriesStore._resolve_db_path(target) == target

    def test_constructor_resolves_directory(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path))
        assert store._db_path == str(tmp_path / "timeseries.db")


# ---------------------------------------------------------------------------
# Core recording + integration
# ---------------------------------------------------------------------------


async def record(
    store, ts, solar=0.0, home=0.0, battery=0.0, site=0.0, soe=None, tz="UTC", gw="gw1"
):
    return await store.record_sample(
        gateway_id=gw,
        ts=ts,
        solar_w=solar,
        home_w=home,
        battery_w=battery,
        site_w=site,
        soe=soe,
        timezone=tz,
    )


class TestIntegration:
    @pytest.mark.asyncio
    async def test_ramp_up_integration(self, tmp_path):
        """First sample is 0 W, second is 3600 W one hour later.
        Trapezoid = average power * time = 1800 W * 1 h = 1.8 kWh per
        category — proving linear interpolation rather than assuming either
        endpoint's power for the whole interval.
        """
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        await record(store, 1000.0)
        row = await record(
            store, 1000.0 + 3600, solar=3600, home=3600, battery=-3600, site=3600
        )
        assert row["solar_kwh"] == pytest.approx(1.8)
        assert row["home_kwh"] == pytest.approx(1.8)
        assert row["battery_charge_kwh"] == pytest.approx(1.8)
        assert row["grid_import_kwh"] == pytest.approx(1.8)
        # Discharge/export stayed zero (negative battery = charging)
        assert row["battery_discharge_kwh"] == pytest.approx(0.0)
        assert row["grid_export_kwh"] == pytest.approx(0.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_steady_state_accuracy(self, tmp_path):
        """Both endpoints at the same power: 3600 W * 1 h = 1 kWh exactly."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        await record(store, 2000.0, solar=3600, home=2000, battery=-1000, site=-1000)
        row = await record(
            store, 2000.0 + 3600, solar=3600, home=2000, battery=-1000, site=-1000
        )
        assert row["solar_kwh"] == pytest.approx(3.6)
        assert row["home_kwh"] == pytest.approx(2.0)
        assert row["battery_charge_kwh"] == pytest.approx(1.0)
        assert row["grid_export_kwh"] == pytest.approx(1.0)
        assert row["grid_import_kwh"] == pytest.approx(0.0)
        assert row["battery_discharge_kwh"] == pytest.approx(0.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_directional_split_no_netting(self, tmp_path):
        """Sign flips fill the correct bucket; buckets never net to zero.

        Note: integration interpolates the directional components (charge
        and discharge as separate non-negative series), so an interval where
        power crosses zero accrues to BOTH buckets (triangle halves). At the
        5s poll interval the crossing window is sub-second, making this
        negligible in practice while guaranteeing directions never net.
        """
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        # Hour 1: battery discharging 2000W, grid importing 1000W
        await record(store, 0.0, battery=2000, site=1000)
        row = await record(store, 3600.0, battery=2000, site=1000)
        assert row["battery_discharge_kwh"] == pytest.approx(2.0)
        assert row["grid_import_kwh"] == pytest.approx(1.0)
        assert row["battery_charge_kwh"] == pytest.approx(0.0)
        assert row["grid_export_kwh"] == pytest.approx(0.0)
        # Hour 2: full sign flip to charging 2000W / exporting 1000W.
        # Both directions accrue from the component ramps (import 1000->0,
        # export 0->1000): +0.5 kWh each.
        row = await record(store, 7200.0, battery=-2000, site=-1000)
        assert row["battery_charge_kwh"] == pytest.approx(1.0)
        assert row["grid_export_kwh"] == pytest.approx(0.5)
        # Import/discharge grew only via their component ramps — never netted
        assert row["grid_import_kwh"] == pytest.approx(1.5)
        assert row["battery_discharge_kwh"] == pytest.approx(3.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_gap_threshold_no_integration(self, tmp_path):
        """A gap longer than 1 hour must not fabricate energy."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        await record(store, 0.0, solar=5000)
        # Jump forward past the gap threshold
        row = await record(store, GAP_THRESHOLD + 100, solar=5000)
        assert row is None  # gap -> no integration, no daily row returned
        daily = await store.get_daily_energy(days=7)
        totals = [g for d in daily["days"] for g in d["gateways"].values()]
        assert all(t["solar_kwh"] == 0.0 for t in totals)
        # The raw sample itself was still stored
        samples = await store.get_samples()
        assert samples["count"] == 2
        await store.stop()

    @pytest.mark.asyncio
    async def test_midnight_split(self, tmp_path):
        """Energy on either side of local midnight lands on its own day."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        # UTC timezone; midnight falls on integer 86400 boundaries.
        # Three samples at 23:00, 00:00, 01:00 — each interval is exactly
        # 3600s (the gap threshold, still integrated) at steady 1200 W.
        midnight = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        t0 = midnight - 3600
        tmid = midnight
        t1 = midnight + 3600
        day1 = datetime.fromtimestamp(t0, timezone.utc).date().isoformat()
        day2 = datetime.fromtimestamp(t1, timezone.utc).date().isoformat()
        assert day1 != day2
        await record(store, t0, solar=1200)
        await record(store, tmid, solar=1200)
        await record(store, t1, solar=1200)
        daily = await store.get_daily_energy(days=7)
        by_day = {d["day"]: d["gateways"]["gw1"] for d in daily["days"]}
        assert by_day[day1]["solar_kwh"] == pytest.approx(1.2)  # 1200W * 1h
        assert by_day[day2]["solar_kwh"] == pytest.approx(1.2)
        await store.stop()

    @pytest.mark.asyncio
    async def test_dst_timezone_supported(self, tmp_path):
        """A named timezone with DST resolves without error; totals right
        regardless of which local day they land on."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        t0 = time.time() - 3600
        await record(store, t0, solar=1000, tz="America/Los_Angeles")
        await record(store, t0 + 3600, solar=1000, tz="America/Los_Angeles")
        daily = await store.get_daily_energy(days=7)
        total = sum(
            g["solar_kwh"] for d in daily["days"] for g in d["gateways"].values()
        )
        assert total == pytest.approx(1.0)
        await store.stop()

    @pytest.mark.asyncio
    async def test_unknown_timezone_falls_back_to_utc(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        midnight = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        t0 = midnight - 3600
        await record(store, t0, solar=1200, tz="Not/ARealZone")
        await record(store, midnight, solar=1200, tz="Not/ARealZone")
        await record(store, t0 + 2 * 3600, solar=1200, tz="Not/ARealZone")
        daily = await store.get_daily_energy(days=7)
        assert len(daily["days"]) >= 1  # no crash; days split at UTC midnight
        await store.stop()

    @pytest.mark.asyncio
    async def test_out_of_order_and_duplicate_samples(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        await record(store, 1000.0, solar=1000)
        # 5s at 1000 W -> 0.0013888 kWh
        row = await record(store, 1005.0, solar=1000)
        assert row["solar_kwh"] == pytest.approx(1000 * 5 / 3.6e6)
        # Duplicate timestamp: stored, no double integration
        assert await record(store, 1005.0, solar=1000) is None
        # Out-of-order older sample: stored as raw data, not integrated
        assert await record(store, 1002.0, solar=999) is None
        # Next in-order sample integrates only from ts=1005 (state unmoved)
        row = await record(store, 1010.0, solar=1000)
        assert row["solar_kwh"] == pytest.approx(2 * 1000 * 5 / 3.6e6)
        samples = await store.get_samples(limit=10)
        assert samples["count"] == 4  # raw rows kept even when not integrated
        await store.stop()

    @pytest.mark.asyncio
    async def test_multi_gateway_isolation(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        # Anchor mid-day UTC three days back so the 1-hour interval can
        # never straddle a UTC midnight (which would split the day row and
        # make the assertion time-of-day dependent). Stays inside days=7.
        base = (
            (datetime.now(timezone.utc) - timedelta(days=3))
            .replace(hour=12, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        await record(store, base, solar=1000, gw="a")
        await record(store, base + 3600, solar=1000, gw="a")
        await record(store, base, solar=2000, gw="b")
        await record(store, base + 3600, solar=2000, gw="b")
        daily = await store.get_daily_energy(days=7)
        day = daily["days"][0]["gateways"]
        assert day["a"]["solar_kwh"] == pytest.approx(1.0)
        assert day["b"]["solar_kwh"] == pytest.approx(2.0)
        await store.stop()


# ---------------------------------------------------------------------------
# Persistence + maintenance
# ---------------------------------------------------------------------------


class TestPersistenceAndMaintenance:
    @pytest.mark.asyncio
    async def test_restart_resumes_without_double_counting(self, tmp_path):
        path = str(tmp_path / "ts.db")
        store = TimeSeriesStore(db_path=path)
        await record(store, 0.0, solar=1000)
        await record(store, 3600.0, solar=1000)
        await store.stop()

        # "Restart": fresh instance on the same file
        store2 = TimeSeriesStore(db_path=path)
        # Same-timestamp duplicate: no new energy
        row = await record(store2, 3600.0, solar=1000)
        assert row is None
        row = await record(store2, 7200.0, solar=1000)
        assert row["solar_kwh"] == pytest.approx(2.0)  # 1h + 1h
        await store2.stop()

    @pytest.mark.asyncio
    async def test_maintenance_prunes_old_samples_keeps_floor(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"), retention=7200)
        now = time.time()
        # Samples spanning well beyond the 2h retention
        for i in range(20):
            await record(store, now - 86400 + i * 300, solar=1000)
        for i in range(20):
            await record(store, now - 3600 + i * 60, solar=1000)
        await store.maintenance()
        samples = await store.get_samples(limit=10000)
        oldest = samples["samples"][0]["ts"]
        # Everything older than max(2h, 1h floor) is gone...
        assert oldest >= now - max(7200, RAW_KEEP_FLOOR) - 60
        # ...and daily aggregates were preserved despite pruning
        daily = await store.get_daily_energy(days=7)
        assert (
            sum(g["solar_kwh"] for d in daily["days"] for g in d["gateways"].values())
            > 0
        )
        await store.stop()

    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        await record(store, time.time(), solar=100)
        await record(store, time.time() + 5, solar=100)
        s = await store.status()
        assert s["enabled"] is True
        assert s["samples"] == 2
        assert s["db_size_bytes"] > 0
        assert "gw1" in s["gateways"]
        assert s["write_failures"] == 0
        # Status must not disclose the filesystem path (only the filename)
        assert s["db_file"] == "ts.db"
        await store.stop()

    @pytest.mark.asyncio
    async def test_samples_query_filters(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        for i in range(10):
            await record(store, 1000.0 + i * 10, solar=100, gw="a")
            await record(store, 1000.0 + i * 10, solar=100, gw="b")
        out = await store.get_samples(gateway="a", start=1015, end=1040, limit=5)
        assert out["count"] == 3
        assert all(s["gateway_id"] == "a" for s in out["samples"])
        ts_list = [s["ts"] for s in out["samples"]]
        assert ts_list == sorted(ts_list)
        await store.stop()


# ---------------------------------------------------------------------------
# Disabled subsystem
# ---------------------------------------------------------------------------


class TestDisabled:
    @pytest.mark.asyncio
    async def test_disabled_never_touches_disk(self, tmp_path):
        db = tmp_path / "ts.db"
        store = TimeSeriesStore(db_path=str(db), retention="-1")
        assert store.enabled is False
        row = await record(store, time.time(), solar=1000)
        assert row is None
        await store.start()  # no-op
        assert (await store.get_daily_energy())["enabled"] is False
        assert (await store.get_samples())["enabled"] is False
        s = await store.status()
        assert s["enabled"] is False and s["db_file"] is None
        await store.stop()
        assert not db.exists()  # nothing was ever written

    def test_maintenance_disabled_noop(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "x.db"), retention="-1")
        assert asyncio.run(store.maintenance()) is None


# ---------------------------------------------------------------------------
# Trend query (bucketed kW + battery level for the console chart)
# ---------------------------------------------------------------------------


class TestTrend:
    @pytest.mark.asyncio
    async def test_trend_buckets_and_battery_level(self, tmp_path):
        """24h trend buckets average raw samples, sum across gateways, and
        carry battery level (%) from raw samples only."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        # 24h window -> 240s buckets; align to a bucket edge so all three
        # samples land in one bucket deterministically.
        base = math.floor(time.time() / 240) * 240
        for ts, soe in ((base, 50.0), (base + 100, 60.0), (base + 200, 70.0)):
            await record(
                store,
                ts,
                solar=1000,
                home=2000,
                battery=-1000,
                site=500,
                soe=soe,
                gw="a",
            )
            await record(
                store,
                ts,
                solar=2000,
                home=0,
                battery=0,
                site=0,
                soe=soe + 10,
                gw="b",
            )
        trend = await store.get_trend(hours=24)
        assert trend["enabled"] is True
        assert trend["hours"] == 24
        assert trend["bucket_seconds"] == 240
        assert trend["count"] == 1  # single bucket
        p = trend["points"][0]
        assert p["ts"] == base
        # Fleet totals: gateway a + gateway b
        assert p["solar_kw"] == pytest.approx(3.0)
        assert p["home_kw"] == pytest.approx(2.0)
        assert p["battery_kw"] == pytest.approx(-1.0)  # charging = negative
        assert p["grid_kw"] == pytest.approx(0.5)  # importing = positive
        # Mean battery level across samples and gateways: (60 + 70) / 2
        assert p["battery_level"] == pytest.approx(65.0)

        # Gateway filter returns just that gateway's values
        solo = await store.get_trend(hours=24, gateway="a")
        assert solo["points"][0]["solar_kw"] == pytest.approx(1.0)
        assert solo["points"][0]["battery_level"] == pytest.approx(60.0)

        # Battery level lives in raw samples only — never in daily aggregates
        samples = await store.get_samples()
        assert samples["count"] == 6
        assert all(s["soe"] is not None for s in samples["samples"])
        daily = await store.get_daily_energy(days=1)
        row = daily["days"][0]["gateways"]["a"]
        assert "soe" not in row
        await store.stop()

    @pytest.mark.asyncio
    async def test_trend_disabled(self, tmp_path):
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"), retention=-1)
        trend = await store.get_trend()
        assert trend == {"enabled": False, "points": [], "count": 0}

    @pytest.mark.asyncio
    async def test_trend_explicit_window_and_fit(self, tmp_path):
        """start/end define an arbitrary window; fit spans all samples."""
        store = TimeSeriesStore(db_path=str(tmp_path / "ts.db"))
        now = time.time()
        # Old sample well outside a 1h window, recent samples inside it
        await record(store, now - 48 * 3600, solar=9000, gw="a")
        await record(store, now - 600, solar=1000, gw="a")
        await record(store, now - 300, solar=2000, gw="a")

        # Explicit window: last 30 minutes only -> 60s buckets, one point
        # per bucket; the 48h-old sample is excluded
        trend = await store.get_trend(start=now - 1800, end=now)
        assert trend["enabled"] is True
        assert trend["start"] == pytest.approx(now - 1800)
        assert trend["end"] == pytest.approx(now)
        assert trend["bucket_seconds"] == 60
        assert trend["count"] == 2
        values = sorted(p["solar_kw"] for p in trend["points"])
        assert values == pytest.approx([1.0, 2.0])
        assert all(p["solar_kw"] < 9.0 for p in trend["points"])

        # end in the future is honored as-is (bucket sizing follows span)
        future = await store.get_trend(start=now - 1800, end=now + 3600)
        assert future["end"] == pytest.approx(now + 3600)

        # Fit: spans back to the 48h-old sample
        fit = await store.get_trend(fit=True)
        assert fit["start"] <= now - 48 * 3600
        assert fit["bucket_seconds"] == 480  # 48h span -> 8min buckets
        assert fit["count"] >= 2

        # Fit window ends at the newest sample, not "now" — the data fills
        # the full graph width
        assert fit["end"] == pytest.approx(now - 300)

        # Gateway-filtered fit spans only that gateway's samples
        await record(store, now - 60, solar=500, gw="b")
        fit_b = await store.get_trend(fit=True, gateway="b")
        assert fit_b["start"] == pytest.approx(now - 60)
        assert fit_b["end"] == pytest.approx(now - 60)
        await store.stop()


# ---------------------------------------------------------------------------
# API endpoints (uses the app TestClient; conftest isolates the DB path)
# ---------------------------------------------------------------------------


class TestAPI:
    def test_status_endpoint_enabled(self, client):
        resp = client.get("/api/timeseries/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert "retention_seconds" in body

    def test_today_endpoint_shape(self, client):
        resp = client.get("/api/timeseries/today")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["gateways"] == {}  # no gateways configured in tests
        assert "server_time" in body

    def test_daily_endpoint_shape(self, client):
        resp = client.get("/api/timeseries/daily?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["days"] == []

    def test_daily_endpoint_validation(self, client):
        assert client.get("/api/timeseries/daily?days=0").status_code == 422
        assert client.get("/api/timeseries/daily?days=999").status_code == 422

    def test_samples_endpoint(self, client):
        resp = client.get("/api/timeseries/samples")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_trend_endpoint(self, client):
        resp = client.get("/api/timeseries/trend?hours=24")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["points"] == []
        assert body["hours"] == 24
        assert "bucket_seconds" in body
        assert client.get("/api/timeseries/trend?hours=0").status_code == 422
        assert client.get("/api/timeseries/trend?hours=999").status_code == 422

    def test_disabled_reports_enabled_false(self, client, monkeypatch):
        from app.core import timeseries as ts_mod
        from app.config import settings

        monkeypatch.setattr(settings, "timeseries_retention", "-1")
        ts_mod.reset_timeseries_store()
        body = client.get("/api/timeseries/status").json()
        assert body["enabled"] is False
        body = client.get("/api/timeseries/today").json()
        assert body["enabled"] is False
        body = client.get("/api/timeseries/daily").json()
        assert body["enabled"] is False


# ---------------------------------------------------------------------------
# Poll-loop wiring (gateway_manager feeds the store)
# ---------------------------------------------------------------------------


class TestPollLoopWiring:
    @pytest.mark.asyncio
    async def test_poll_records_sample(
        self, tmp_path, monkeypatch, mock_gateway_manager, mock_pypowerwall
    ):
        """A successful poll cycle must land in the time-series store."""
        import app.core.timeseries as ts_mod
        from app.config import settings
        from app.models.gateway import Gateway, GatewayStatus

        monkeypatch.setattr(settings, "timeseries_path", str(tmp_path / "wired.db"))
        ts_mod.reset_timeseries_store()

        gw = Gateway(id="gw1", name="G1", host="1.2.3.4", gw_pwd="x", timezone="UTC")
        mock_gateway_manager.gateways["gw1"] = gw
        mock_gateway_manager.connections["gw1"] = mock_pypowerwall
        mock_gateway_manager.cache["gw1"] = GatewayStatus(gateway=gw, online=False)

        await mock_gateway_manager._poll_gateway("gw1")

        store = ts_mod.get_timeseries_store()
        # The poll queues the write without waiting for it
        await asyncio.gather(*store._pending_writes)
        samples = await store.get_samples(gateway="gw1")
        assert samples["count"] == 1
        s = samples["samples"][0]
        assert s["solar_w"] == 5000
        assert s["home_w"] == 3100
        assert s["battery_charge_w"] == 2000  # battery -2000 => charging
        assert s["battery_discharge_w"] == 0
        assert s["grid_import_w"] == 100
        assert s["grid_export_w"] == 0
        ts_mod.reset_timeseries_store()
