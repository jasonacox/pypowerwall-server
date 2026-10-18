"""SQLite writer regressions for cancellation, admission, and shutdown.

Every database is temporary. Thread events hold the writer at known points so
outer poll timeouts can be tested without depending on SQLite or machine speed.
"""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Coroutine, Optional

import pytest
import pytest_asyncio

from app.core import timeseries as timeseries_module
from app.core.timeseries import TimeSeriesStore

BASE_TS = 1792231200.0
METRICS = {("TEPOD--test", "pack_temp_max"): 25.0}
SIGNAL_KEY = ("test", "TEPOD--test", "pack_temp_max")


class WriterHarness:
    """Own one disposable store and release blocked workers during teardown."""

    def __init__(self, database: Path | str) -> None:
        self.database = database
        self.store = TimeSeriesStore(
            str(database), retention=0, signal_retention=0, signal_interval="60s"
        )
        self.releases: list[threading.Event] = []
        self.tasks: list[asyncio.Task] = []
        self.admitted = asyncio.Event()
        self.last_write: Optional[asyncio.Future] = None
        original_submit = self.store._submit_write

        def observe_admission(*args: Any, **kwargs: Any) -> Optional[asyncio.Future]:
            future = original_submit(*args, **kwargs)
            if future is not None:
                self.last_write = future
                self.admitted.set()
            return future

        self.store._submit_write = observe_admission

    async def sample(self, seconds: float, solar: float) -> Any:
        return await self.store.record_sample(
            "test", BASE_TS + seconds, solar, 1000, 0, 0, 50, "UTC"
        )

    def spawn(self, awaitable: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    async def enqueue(
        self, awaitable: Coroutine[Any, Any, Any]
    ) -> tuple[asyncio.Task, asyncio.Future]:
        """Wait until the operation has actually joined the accepted backlog."""
        self.admitted.clear()
        task = self.spawn(awaitable)
        await asyncio.wait_for(self.admitted.wait(), timeout=5)
        assert self.last_write is not None
        return task, self.last_write

    def worker_barrier(self) -> tuple[asyncio.Event, threading.Event]:
        return asyncio.Event(), self.new_release()

    def new_release(self) -> threading.Event:
        release = threading.Event()
        self.releases.append(release)
        return release

    async def block_writer(self) -> threading.Event:
        started, release = self.worker_barrier()
        loop = asyncio.get_running_loop()

        def block() -> None:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(20), "writer barrier was not released"

        self.store._ensure_executor().submit(block)
        await asyncio.wait_for(started.wait(), timeout=5)
        return release

    async def flush(self) -> None:
        """Wait for admitted writes and their event-loop completion callbacks."""
        pending = list(self.store._pending_writes)
        if pending:
            await asyncio.wait_for(
                asyncio.gather(
                    *(asyncio.shield(future) for future in pending),
                    return_exceptions=True,
                ),
                timeout=5,
            )

    def rows(
        self,
        query: str = "SELECT ts FROM samples ORDER BY ts",
        params: tuple = (),
    ) -> list:
        with sqlite3.connect(f"file:{self.database}?mode=ro", uri=True) as conn:
            return conn.execute(query, params).fetchall()

    async def timeout(self, task: asyncio.Task, timeout: float = 0.05) -> None:
        # asyncio.TimeoutError is distinct from built-in TimeoutError on 3.10.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=timeout)
        assert task.cancelled()


@pytest_asyncio.fixture
async def writer(
    tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[WriterHarness]:
    database = (
        ":memory:"
        if getattr(request, "param", "file") == "memory"
        else (tmp_path / "timeseries.db")
    )
    harness = WriterHarness(database)
    try:
        await harness.sample(0, 0)
        yield harness
    finally:
        for release in harness.releases:
            release.set()
        if harness.tasks:
            await asyncio.gather(*harness.tasks, return_exceptions=True)
        await asyncio.wait_for(harness.store.stop(), timeout=10)


@pytest.mark.asyncio
async def test_queued_write_survives_actual_five_second_poll_timeout(
    tmp_path: Path,
) -> None:
    # Keep this regression independent of the new admission bookkeeping so
    # the unfixed store fails on its missing row after the real poll timeout.
    database = tmp_path / "timeseries.db"
    store = TimeSeriesStore(str(database), retention=0, signal_retention=0)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    async def sample(seconds: float, solar: float) -> Any:
        return await store.record_sample(
            "test", BASE_TS + seconds, solar, 1000, 0, 0, 50, "UTC"
        )

    def block() -> None:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(20), "writer barrier was not released"

    def rows() -> list:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT ts FROM samples ORDER BY ts").fetchall()

    try:
        await sample(0, 0)
        store._ensure_executor().submit(block)
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sample(10, 1200), timeout=5.0)

        assert rows() == [(BASE_TS,)]
        release.set()
        # This existing executor barrier also works against the old store.
        await asyncio.wait_for(
            loop.run_in_executor(store._ensure_executor(), lambda: None), timeout=5
        )
        assert rows() == [(BASE_TS,), (BASE_TS + 10,)]

        stats = await store.status()
        assert stats["writes_pending"] == 0
        assert stats["write_failures"] == 0
    finally:
        release.set()
        await asyncio.wait_for(store.stop(), timeout=10)


@pytest.mark.asyncio
async def test_running_write_finishes_exactly_once_after_wait_timeout(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = writer.store._record_sample_sync
    started, release = writer.worker_barrier()
    loop = asyncio.get_running_loop()
    calls = []

    def blocked(*args: Any) -> Any:
        calls.append(args[1])
        loop.call_soon_threadsafe(started.set)
        assert release.wait(20), "running write was not released"
        return original(*args)

    monkeypatch.setattr(writer.store, "_record_sample_sync", blocked)
    task, future = await writer.enqueue(writer.sample(60, 1200))
    await asyncio.wait_for(started.wait(), timeout=5)
    await writer.timeout(task)
    assert not future.cancelled()
    release.set()
    await writer.flush()

    assert calls == [BASE_TS + 60]
    assert writer.rows() == [(BASE_TS,), (BASE_TS + 60,)]
    assert writer.rows("SELECT solar_kwh FROM daily_energy")[0][0] == pytest.approx(
        0.01
    )


@pytest.mark.asyncio
async def test_queued_writes_preserve_admission_order_and_energy_integral(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = await writer.block_writer()
    original = writer.store._record_sample_sync
    calls = []

    def record_order(*args: Any) -> Any:
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(writer.store, "_record_sample_sync", record_order)
    tasks = []
    for seconds, solar in ((60, 1200), (120, 600), (180, 0)):
        task, _ = await writer.enqueue(writer.sample(seconds, solar))
        tasks.append(task)
    await asyncio.gather(*(writer.timeout(task) for task in tasks))
    release.set()
    await writer.flush()

    assert calls == [BASE_TS + 60, BASE_TS + 120, BASE_TS + 180]
    assert writer.rows() == [(BASE_TS + offset,) for offset in (0, 60, 120, 180)]
    assert writer.rows("SELECT solar_kwh FROM daily_energy")[0][0] == pytest.approx(
        0.03
    )
    assert writer.rows("SELECT ts FROM integration_state") == [(BASE_TS + 180,)]


@pytest.mark.asyncio
async def test_accepted_signal_gates_interval_after_wait_timeout(
    writer: WriterHarness,
) -> None:
    release = await writer.block_writer()
    task, _ = await writer.enqueue(
        writer.store.record_signal_sample("test", BASE_TS, METRICS, "UTC")
    )
    await writer.timeout(task)
    assert writer.store._signal_last[SIGNAL_KEY] == BASE_TS
    assert not await writer.store.record_signal_sample(
        "test", BASE_TS + 5, METRICS, "UTC"
    )
    release.set()
    await writer.flush()

    assert writer.rows("SELECT ts FROM device_samples") == [(int(BASE_TS),)]
    assert writer.store._signal_last[SIGNAL_KEY] == BASE_TS
    assert not await writer.store.record_signal_sample(
        "test", BASE_TS + 5, METRICS, "UTC"
    )
    assert await writer.store.record_signal_sample("test", BASE_TS + 60, METRICS, "UTC")
    assert writer.rows("SELECT ts FROM device_samples ORDER BY ts") == [
        (int(BASE_TS),),
        (int(BASE_TS + 60),),
    ]


@pytest.mark.asyncio
async def test_failed_signal_write_consumes_interval_until_next_due_poll(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = writer.store._ensure_conn
    failures = 0

    def fail_once() -> sqlite3.Connection:
        nonlocal failures
        if failures == 0:
            failures += 1
            raise sqlite3.OperationalError("injected signal write failure")
        return original()

    monkeypatch.setattr(writer.store, "_ensure_conn", fail_once)
    assert not await writer.store.record_signal_sample("test", BASE_TS, METRICS, "UTC")
    assert writer.store._signal_last[SIGNAL_KEY] == BASE_TS
    assert not await writer.store.record_signal_sample(
        "test", BASE_TS + 5, METRICS, "UTC"
    )
    assert await writer.store.record_signal_sample("test", BASE_TS + 60, METRICS, "UTC")
    assert writer.rows("SELECT ts FROM device_samples") == [(int(BASE_TS + 60),)]
    assert (await writer.store.status())["write_failures"] == 1


@pytest.mark.asyncio
async def test_backlog_overflow_is_visible_and_preserves_accepted_rows(
    writer: WriterHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(writer.store, "_max_pending_writes", 2)
    release = await writer.block_writer()
    first, _ = await writer.enqueue(writer.sample(5, 100))
    second, _ = await writer.enqueue(writer.sample(10, 200))
    assert len(writer.store._pending_writes) == 2
    assert await writer.sample(15, 300) is None
    assert len(writer.store._pending_writes) == 2
    assert "writer backlog full" in caplog.text
    release.set()
    await asyncio.gather(first, second)
    await writer.flush()

    assert writer.rows() == [(BASE_TS,), (BASE_TS + 5,), (BASE_TS + 10,)]
    stats = await writer.store.status()
    assert stats["write_rejections"] == 1
    assert stats["writes_pending"] == 0


@pytest.mark.asyncio
async def test_rejected_signal_does_not_consume_sampling_interval(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer.store, "_max_pending_writes", 1)
    release = await writer.block_writer()
    pending, _ = await writer.enqueue(writer.sample(5, 100))
    assert not await writer.store.record_signal_sample("test", BASE_TS, METRICS, "UTC")
    assert SIGNAL_KEY not in writer.store._signal_last
    release.set()
    await pending
    await writer.flush()

    assert await writer.store.record_signal_sample("test", BASE_TS + 5, METRICS, "UTC")
    assert writer.rows("SELECT ts FROM device_samples") == [(int(BASE_TS + 5),)]
    assert (await writer.store.status())["write_rejections"] == 1


@pytest.mark.asyncio
async def test_detached_worker_error_is_counted_and_consumed_then_writer_recovers(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = await writer.block_writer()
    original = writer.store._record_sample_sync
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    unhandled = []

    def unexpected_failure(*args: Any) -> None:
        raise RuntimeError("injected worker exception")

    monkeypatch.setattr(writer.store, "_record_sample_sync", unexpected_failure)
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        task, _ = await writer.enqueue(writer.sample(60, 1200))
        await writer.timeout(task)
        release.set()
        await writer.flush()
        stats = await writer.store.status()
        assert stats["write_failures"] == 1
        assert stats["writes_pending"] == 0
        assert unhandled == []
        monkeypatch.setattr(writer.store, "_record_sample_sync", original)
        await writer.sample(120, 600)
        assert writer.rows() == [(BASE_TS,), (BASE_TS + 120,)]
    finally:
        loop.set_exception_handler(original_handler)


def fail_next_commit(writer: WriterHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = writer.store._conn
    failed = False

    class FailOnceConnection:
        def __getattr__(self, name: str) -> Any:
            return getattr(connection, name)

        def commit(self) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise sqlite3.OperationalError("injected commit failure")
            connection.commit()

    proxy = FailOnceConnection()
    monkeypatch.setattr(writer.store, "_ensure_conn", lambda: proxy)


@pytest.mark.asyncio
async def test_power_commit_failure_rolls_back_rows_energy_and_integration_state(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_next_commit(writer, monkeypatch)
    assert await writer.sample(60, 1200) is None
    assert writer.rows() == [(BASE_TS,)]
    assert writer.rows("SELECT ts FROM integration_state") == [(BASE_TS,)]
    assert writer.rows("SELECT solar_kwh FROM daily_energy") == []
    await writer.sample(120, 600)

    assert writer.rows() == [(BASE_TS,), (BASE_TS + 120,)]
    assert writer.rows("SELECT solar_kwh FROM daily_energy")[0][0] == pytest.approx(
        0.01
    )
    assert (await writer.store.status())["write_failures"] == 1


@pytest.mark.asyncio
async def test_signal_commit_failure_rolls_back_series_and_retries_when_due(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_next_commit(writer, monkeypatch)
    metrics = dict(METRICS)
    metrics[("TEPINV--test", "inverter_ambient")] = 30.0
    assert not await writer.store.record_signal_sample("test", BASE_TS, metrics, "UTC")
    assert writer.rows("SELECT COUNT(*) FROM device_samples") == [(0,)]
    assert writer.rows("SELECT COUNT(*) FROM device_series") == [(0,)]
    assert writer.store._signal_last[SIGNAL_KEY] == BASE_TS
    assert not await writer.store.record_signal_sample(
        "test", BASE_TS + 5, metrics, "UTC"
    )
    assert await writer.store.record_signal_sample("test", BASE_TS + 60, metrics, "UTC")

    assert writer.rows("SELECT COUNT(*) FROM device_samples") == [(2,)]
    assert writer.rows("SELECT COUNT(*) FROM device_series") == [(2,)]
    assert writer.rows("SELECT count FROM device_daily ORDER BY series_id") == [
        (1,),
        (1,),
    ]
    assert (await writer.store.status())["write_failures"] == 1


def observe_shutdown(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> asyncio.Event:
    started = asyncio.Event()
    original = writer.store._stop_and_drain

    async def drain() -> None:
        started.set()
        await original()

    monkeypatch.setattr(writer.store, "_stop_and_drain", drain)
    return started


@pytest.mark.asyncio
async def test_stop_drains_accepted_writes_before_closing(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = await writer.block_writer()
    pending, _ = await writer.enqueue(writer.sample(60, 1200))
    await writer.timeout(pending)
    shutdown_started = observe_shutdown(writer, monkeypatch)
    stopping = writer.spawn(writer.store.stop())
    await asyncio.wait_for(shutdown_started.wait(), timeout=5)
    assert not stopping.done()
    assert await writer.sample(120, 600) is None
    assert not await writer.store.record_signal_sample("test", BASE_TS, METRICS, "UTC")
    release.set()
    await asyncio.wait_for(stopping, timeout=5)

    assert writer.rows() == [(BASE_TS,), (BASE_TS + 60,)]
    assert writer.rows("SELECT solar_kwh FROM daily_energy")[0][0] == pytest.approx(
        0.01
    )
    assert writer.store._conn is None
    assert writer.store._executor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_lock", [False, True], ids=["worker", "writer-lock"])
async def test_stop_bounds_drain_and_discards_queued_accepted_write(
    writer: WriterHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    hold_lock: bool,
) -> None:
    monkeypatch.setattr(timeseries_module, "_STOP_DRAIN_TIMEOUT", 0.05)
    original = writer.store._record_sample_sync
    started, release = writer.worker_barrier()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = []

    def blocked(*args: Any) -> Any:
        calls.append(args[1])
        if args[1] != BASE_TS + 60:
            return original(*args)
        if hold_lock:
            writer.store._lock.acquire()
        try:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(20), "running write was not released"
            return original(*args)
        finally:
            if hold_lock:
                writer.store._lock.release()
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(writer.store, "_record_sample_sync", blocked)
    await writer.enqueue(writer.sample(60, 1200))
    await asyncio.wait_for(started.wait(), timeout=5)
    await writer.enqueue(writer.sample(120, 600))
    assert len(writer.store._pending_writes) == 2
    # A regression that synchronously takes the writer lock cannot hang the
    # entire test runner; the elapsed-time assertion still detects it.
    watchdog = threading.Timer(2, release.set)
    watchdog.start()
    try:
        before = time.monotonic()
        await asyncio.wait_for(writer.store.stop(), timeout=1)
        assert time.monotonic() - before < 0.5
        assert not release.is_set()
        assert calls == [BASE_TS + 60]
        assert "abandoning 2 accepted write jobs" in caplog.text
        assert await writer.sample(180, 100) is None
        assert not await writer.store.record_signal_sample(
            "test", BASE_TS, METRICS, "UTC"
        )
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join()
        await asyncio.wait_for(finished.wait(), timeout=5)
        await asyncio.wait_for(
            asyncio.wrap_future(writer.store._close_future), timeout=5
        )

    assert calls == [BASE_TS + 60]
    assert writer.store._conn is None
    assert writer.store._executor is None
    assert writer.rows("SELECT ts FROM samples WHERE ts=?", (BASE_TS + 120,)) == []


@pytest.mark.asyncio
async def test_stop_bounds_sqlite_checkpoint_during_cleanup(
    writer: WriterHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(timeseries_module, "_STOP_DRAIN_TIMEOUT", 0.05)
    connection = writer.store._conn
    started, release = writer.worker_barrier()
    loop = asyncio.get_running_loop()

    class BlockedCheckpointConnection:
        def __getattr__(self, name: str) -> Any:
            return getattr(connection, name)

        def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
            if query == "PRAGMA wal_checkpoint(TRUNCATE)":
                loop.call_soon_threadsafe(started.set)
                assert release.wait(20), "checkpoint was not released"
            return connection.execute(query, *args, **kwargs)

    monkeypatch.setattr(writer.store, "_conn", BlockedCheckpointConnection())
    watchdog = threading.Timer(2, release.set)
    watchdog.start()
    try:
        before = time.monotonic()
        stopping = writer.spawn(writer.store.stop())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(stopping, timeout=1)
        assert time.monotonic() - before < 0.5
        assert not release.is_set()
        assert not writer.store._close_future.done()
        assert "shutdown timed out" in caplog.text
    finally:
        release.set()
        watchdog.cancel()
        watchdog.join()
        await asyncio.wait_for(
            asyncio.wrap_future(writer.store._close_future), timeout=5
        )

    assert writer.store._conn is None


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_preserves_drain_and_store_never_reopens(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = await writer.block_writer()
    pending, _ = await writer.enqueue(writer.sample(60, 1200))
    await writer.timeout(pending)
    shutdown_started = observe_shutdown(writer, monkeypatch)
    stopping = writer.spawn(writer.store.stop())
    await asyncio.wait_for(shutdown_started.wait(), timeout=5)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert not writer.store._stop_task.cancelled()
    assert not writer.store._stop_task.done()
    assert await writer.sample(120, 600) is None
    second_stop = writer.spawn(writer.store.stop())
    release.set()
    await asyncio.wait_for(second_stop, timeout=5)

    assert writer.rows() == [(BASE_TS,), (BASE_TS + 60,)]
    assert await writer.sample(180, 100) is None
    assert not await writer.store.record_signal_sample("test", BASE_TS, METRICS, "UTC")
    assert (await writer.store.get_samples())["count"] == 0
    await writer.store.start()
    await writer.store.maintenance()
    await writer.store.stop()
    assert writer.store._conn is None
    assert writer.store._executor is None
    assert writer.store._read_conn is None
    assert writer.store._read_executor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["file", "memory"], indirect=True)
async def test_queries_during_shutdown_return_empty_without_entering_either_lane(
    writer: WriterHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = await writer.block_writer()
    pending, future = await writer.enqueue(writer.sample(60, 1200))
    await writer.timeout(pending)
    shutdown_started = observe_shutdown(writer, monkeypatch)
    stopping = writer.spawn(writer.store.stop())
    await asyncio.wait_for(shutdown_started.wait(), timeout=5)
    assert not stopping.done()

    def reject_open() -> None:
        raise AssertionError("shutdown query must not enter or reopen a database lane")

    # Restore these guards before releasing the accepted write: that write
    # must still be able to use the existing connection and commit normally.
    with monkeypatch.context() as guard:
        guard.setattr(writer.store, "_ensure_executor", reject_open)
        guard.setattr(writer.store, "_ensure_read_executor", reject_open)
        guard.setattr(writer.store, "_ensure_conn", reject_open)
        samples, status, trend, daily, signals, signal_trend, today = (
            await asyncio.wait_for(
                asyncio.gather(
                    writer.store.get_samples(),
                    writer.store.status(),
                    writer.store.get_trend(),
                    writer.store.get_daily_energy(),
                    writer.store.get_signal_series(),
                    writer.store.get_signal_trend(),
                    writer.store.get_today("test"),
                ),
                timeout=1,
            )
        )

    assert samples == {"enabled": True, "samples": [], "count": 0}
    assert status["samples"] == status["daily_rows"] == 0
    assert status["signal_samples"] == status["signal_daily_rows"] == 0
    assert status["writes_pending"] == 1
    assert trend["points"] == []
    assert trend["count"] == 0
    assert daily == {"enabled": True, "days": [], "last_updated": None}
    assert signals["series"] == []
    assert signal_trend["series"] == []
    assert today is None
    assert not future.done()
    assert writer.store._read_conn is None
    assert writer.store._read_executor is None

    release.set()
    await asyncio.wait_for(stopping, timeout=5)
    result = future.result()
    assert result["solar_kwh"] == pytest.approx(0.01)
    if writer.database != ":memory:":
        assert writer.rows() == [(BASE_TS,), (BASE_TS + 60,)]
    assert writer.store._conn is None
    assert writer.store._executor is None
