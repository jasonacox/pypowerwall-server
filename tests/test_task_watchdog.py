"""Tests for the task-liveness watchdog.

A silently wedged TEDAPI probe task once held a gateway on cloud reads
for hours after a firmware-update reboot with zero log output. The
watchdog recreates finished cyclic tasks and force-recreates live tasks
whose heartbeat went stale three times running (with cooldown).
"""
import asyncio
import logging
import time
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core.gateway_manager import gateway_manager

LOGGER = "app.core.gateway_manager"


@pytest.fixture
def gm(monkeypatch):
    """Manager with one local gateway, empty task registries, no beats."""
    monkeypatch.setattr(
        gateway_manager,
        "gateways",
        {"gw": Mock(host="10.0.0.2", cloud_mode=False, fleetapi=False)},
    )
    monkeypatch.setattr(gateway_manager, "_poll_tasks", {})
    monkeypatch.setattr(gateway_manager, "_probe_tasks", {})
    monkeypatch.setattr(gateway_manager, "_task_beats", {})
    monkeypatch.setattr(gateway_manager, "_task_stale_counts", {})
    monkeypatch.setattr(gateway_manager, "_task_last_forced", {})
    monkeypatch.setattr(gateway_manager, "_executor", None)
    created = []
    orig_create = asyncio.create_task

    def tracking_create(coro, *, name=None):
        task = orig_create(coro, name=name)
        created.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", tracking_create)
    yield gateway_manager
    for task in created:
        if not task.done():
            task.cancel()


def test_beat_records_monotonic_timestamp(gm):
    """_beat stores a monotonic timestamp under the task name."""
    before = time.monotonic()
    gateway_manager._beat("poll-gw")
    after = time.monotonic()
    assert before <= gateway_manager._task_beats["poll-gw"] <= after


async def _live_seed(name):
    """Seed a live task with a fresh heartbeat; caller must cancel it."""
    task = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw"] = task
    gateway_manager._beat(name)
    return task


@pytest.mark.asyncio
async def test_watchdog_recreates_finished_task(gm, monkeypatch, caplog):
    """A done poll task is recreated and the failure is logged."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    assert finished.done()
    gateway_manager._poll_tasks["gw"] = finished
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert any("ended" in r.getMessage() for r in caplog.records)
    assert gateway_manager._poll_tasks["gw"] is not finished


@pytest.mark.asyncio
async def test_watchdog_reports_wedged_task_without_recreating(
    gm, monkeypatch, caplog
):
    """A live task with a stale heartbeat is reported first (single strike)."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    stuck = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = stuck
    gateway_manager._task_beats["poll-gw"] = (
        time.monotonic() - gateway_manager._WATCHDOG_POLL_STALE_S - 1
    )
    probe = await _live_seed("tedapi-probe-gw")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert any("wedged" in r.getMessage() for r in caplog.records)
    assert gateway_manager._poll_tasks["gw"] is stuck
    stuck.cancel()
    probe.cancel()
    try:
        await stuck
    except asyncio.CancelledError:
        pass
    try:
        await probe
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_watchdog_quiet_for_healthy_tasks(gm, monkeypatch, caplog):
    """Fresh heartbeats on live tasks: no warnings."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    probe = await _live_seed("tedapi-probe-gw")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert not [r for r in caplog.records if "watchdog" in r.getMessage().lower()]
    alive.cancel()
    probe.cancel()
    try:
        await alive
    except asyncio.CancelledError:
        pass
    try:
        await probe
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_watchdog_warns_on_executor_saturation(
    gm, monkeypatch, caplog
):
    """A backed-up executor queue is reported for diagnosis."""

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    probe = await _live_seed("tedapi-probe-gw")
    executor = Mock()
    executor._max_workers = 10
    queue = Mock()
    queue.qsize.return_value = 25
    executor._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_executor", executor)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert any("saturated" in r.getMessage() for r in caplog.records)
    alive.cancel()
    probe.cancel()
    try:
        await alive
    except asyncio.CancelledError:
        pass
    try:
        await probe
    except asyncio.CancelledError:
        pass


def test_pool_pending_none_without_executor(gm):
    """No executor (tests, early startup): saturation check is skipped."""
    assert gateway_manager._pool_pending(gateway_manager._executor) is None


def test_pool_pending_none_when_queue_unreadable(gm, monkeypatch):
    """A broken queue read degrades to unknown instead of raising."""

    executor = Mock()
    queue = Mock()
    queue.qsize.side_effect = RuntimeError("nope")
    executor._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_executor", executor)
    assert gateway_manager._pool_pending(gateway_manager._executor) is None


@pytest.mark.asyncio
async def test_watchdog_handles_gateways_independently(
    gm, monkeypatch, caplog
):
    """Per-gateway slots: a dead task on gw2 is recreated with its own
    name while gw1 is untouched (factory closure check)."""

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    local = Mock(host="10.0.0.2", cloud_mode=False, fleetapi=False)
    monkeypatch.setattr(
        gateway_manager, "gateways", {"gw1": local, "gw2": local}
    )
    healthy = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw1"] = healthy
    gateway_manager._beat("poll-gw1")
    probe1 = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw1"] = probe1
    gateway_manager._beat("tedapi-probe-gw1")
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    gateway_manager._poll_tasks["gw2"] = finished
    probe2 = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw2"] = probe2
    gateway_manager._beat("tedapi-probe-gw2")
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw1"] is healthy
        replaced = gateway_manager._poll_tasks["gw2"]
        assert replaced is not finished
        assert replaced.get_name() == "poll-gw2"
    finally:
        for task in (healthy, probe1, probe2):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        current = gateway_manager._poll_tasks.get("gw2")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_retries_missing_cloud_control(
    gm, monkeypatch, caplog
):
    """Configured but no connection and no running init: re-run init."""

    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    stored = [Mock(id="gw")]
    monkeypatch.setattr(gateway_manager, "_hybrid_configs", stored)
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", None)
    seen = []

    async def _fake_init(cfgs):
        seen.append(cfgs)
        await asyncio.sleep(0)

    monkeypatch.setattr(gateway_manager, "_init_cloud_control", _fake_init)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    await asyncio.sleep(0)
    assert any("retrying init" in r.getMessage() for r in caplog.records)
    task = gateway_manager._cloud_control_task
    assert task is not None and task.get_name() == "cloud-control-init"
    assert seen == [stored]
    if not task.done():
        task.cancel()


@pytest.mark.asyncio
async def test_watchdog_skips_cloud_retry_when_connected(gm, monkeypatch):
    """Existing connection: no retry, quiet."""

    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    monkeypatch.setattr(
        gateway_manager, "_hybrid_configs", [Mock(id="gw")]
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", object())
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    probe = await _live_seed("tedapi-probe-gw")
    try:
        gateway_manager._watchdog_check()
        assert gateway_manager._cloud_control_task is None
    finally:
        alive.cancel()
        probe.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await probe
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_retries_after_failed_init_task(
    gm, monkeypatch, caplog
):
    """A finished (failed) init task does not block the next retry."""

    async def _fail():
        raise RuntimeError("init boom")

    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    monkeypatch.setattr(
        gateway_manager, "_hybrid_configs", [Mock(id="gw")]
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    old = asyncio.create_task(_fail())
    try:
        await old
    except RuntimeError:
        pass
    assert old.done()
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", old)
    monkeypatch.setattr(
        gateway_manager, "_init_cloud_control", lambda cfgs: asyncio.sleep(0)
    )
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert any("retrying init" in r.getMessage() for r in caplog.records)
        assert gateway_manager._cloud_control_task is not old
    finally:
        current = gateway_manager._cloud_control_task
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_skips_cloud_retry_when_init_running(gm, monkeypatch):
    """A running init is never duplicated."""

    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    monkeypatch.setattr(
        gateway_manager, "_hybrid_configs", [Mock(id="gw")]
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    running = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", running)
    try:
        gateway_manager._watchdog_check()
        assert gateway_manager._cloud_control_task is running
    finally:
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_cloud_retry_cooldown(gm, monkeypatch, caplog):
    """A recent retry suppresses the next one (no auth hammering)."""

    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    monkeypatch.setattr(
        gateway_manager, "_hybrid_configs", [Mock(id="gw")]
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", None)
    monkeypatch.setattr(
        gateway_manager,
        "_cloud_control_retry_at",
        time.monotonic(),
    )
    monkeypatch.setattr(
        gateway_manager, "_init_cloud_control", lambda cfgs: asyncio.sleep(0)
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert gateway_manager._cloud_control_task is None
    assert not [
        r for r in caplog.records if "retrying init" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_watchdog_no_cloud_retry_when_unconfigured(
    gm, monkeypatch, caplog
):
    """Nothing configured: no retry, quiet."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert gateway_manager._cloud_control_task is None
    assert not [
        r for r in caplog.records if "retrying init" in r.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured,configs",
    [(False, ["cfg"]), (True, [])],
    ids=["flag-off-with-configs", "flag-on-without-configs"],
)
async def test_watchdog_cloud_retry_needs_flag_and_configs(
    gm, monkeypatch, caplog, configured, configs
):
    """Each half of the guard gates the retry independently."""
    monkeypatch.setattr(
        gateway_manager, "_cloud_control_configured", configured
    )
    monkeypatch.setattr(gateway_manager, "_hybrid_configs", list(configs))
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", None)
    monkeypatch.setattr(
        gateway_manager, "_init_cloud_control", lambda cfgs: asyncio.sleep(0)
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert gateway_manager._cloud_control_task is None
    assert not [
        r for r in caplog.records if "retrying init" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_probe_loop_records_heartbeat(gm, monkeypatch):
    """The probe loop beats each iteration (integration, ~5 s floor)."""
    from app.config import settings

    monkeypatch.setattr(settings, "tedapi_probe_interval", 0)
    task = asyncio.create_task(gateway_manager._tedapi_probe_loop("gw"))
    try:
        for _ in range(100):
            await asyncio.sleep(0.1)
            if "tedapi-probe-gw" in gateway_manager._task_beats:
                break
        assert "tedapi-probe-gw" in gateway_manager._task_beats
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_stable_over_many_passes(gm, monkeypatch, caplog):
    """20 passes over healthy tasks: same objects, no warnings, no drift."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            for _ in range(20):
                gateway_manager._beat("poll-gw")
                gateway_manager._beat("tedapi-probe-gw")
                gateway_manager._watchdog_check()
                await asyncio.sleep(0)
        assert gateway_manager._poll_tasks["gw"] is alive
        assert gateway_manager._probe_tasks["gw"] is probe
        assert not [
            r for r in caplog.records if "watchdog" in r.getMessage().lower()
        ]
        assert gateway_manager._task_stale_counts == {}
    finally:
        alive.cancel()
        probe.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await probe
        except asyncio.CancelledError:
            pass


def test_probe_executor_selection(gm, monkeypatch):
    """Probe calls use the dedicated pool when present, else the shared one."""
    from unittest.mock import Mock

    assert gateway_manager._probe_executor_or_main() is None
    main = Mock()
    monkeypatch.setattr(gateway_manager, "_executor", main)
    assert gateway_manager._probe_executor_or_main() is main
    probe = Mock()
    monkeypatch.setattr(gateway_manager, "_probe_executor", probe)
    assert gateway_manager._probe_executor_or_main() is probe


@pytest.mark.asyncio
async def test_watchdog_skips_force_when_saturated(
    gm, monkeypatch, caplog
):
    """A wedged task is not force-recreated into a starved pool."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck
    executor = Mock()
    executor._max_workers = 10
    queue = Mock()
    queue.qsize.return_value = 25
    executor._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_executor", executor)
    try:
        for _ in range(3):
            _stale_beat()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert any("skipping force" in r.getMessage() for r in caplog.records)
        assert gateway_manager._poll_tasks["gw"] is stuck
        assert not stuck.done()
    finally:
        probe.cancel()
        stuck.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_pool_saturated_false_without_pool(gm):
    """No executor, no saturation verdict."""
    assert gateway_manager._pool_saturated(gateway_manager._executor) is False


@pytest.mark.asyncio
async def test_chaos_saturated_pool_skips_then_heals_after_drain(
    gm, monkeypatch, caplog
):
    """Real starved pool: no force into starvation; force after drain.

    Two real worker threads park on a gate while extra jobs queue up,
    reproducing the incident dynamics (threads held far longer than the
    poll cadence). The watchdog must skip the force while saturated and
    heal once the pool drains.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chaos")
    monkeypatch.setattr(gateway_manager, "_executor", pool)
    gate = threading.Event()

    def _hang():
        gate.wait(timeout=30)

    loop = asyncio.get_running_loop()
    hangers = [loop.run_in_executor(pool, _hang) for _ in range(7)]
    stuck = None
    try:
        for _ in range(60):
            await asyncio.sleep(0.1)
            if gateway_manager._pool_saturated(gateway_manager._executor):
                break
        assert gateway_manager._pool_saturated(gateway_manager._executor) is True
        stuck = asyncio.create_task(asyncio.sleep(3600))
        gateway_manager._poll_tasks["gw"] = stuck
        gateway_manager._task_stale_counts["poll-gw"] = 2
        _stale_beat()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw"] is stuck
        assert any("saturated" in r.getMessage() for r in caplog.records)
        # Drain: release the parked threads, let the queue empty.
        gate.set()
        for _ in range(60):
            await asyncio.sleep(0.1)
            if not gateway_manager._pool_saturated(gateway_manager._executor):
                break
        assert gateway_manager._pool_saturated(gateway_manager._executor) is False
        _stale_beat()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw"] is not stuck
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
    finally:
        gate.set()
        for task in [x for x in (stuck, probe) if x is not None]:
            task.cancel()
        for task in [x for x in (stuck, probe) if x is not None]:
            try:
                await task
            except asyncio.CancelledError:
                pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_watchdog_force_works_on_fresh_boot(gm, monkeypatch, caplog):
    """Regression: small monotonic() (fresh CI runner / reboot) must not
    suppress the first force — a 0.0 default used to block it for minutes."""
    monkeypatch.setattr(time, "monotonic", lambda: 120.0)
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = stuck
    try:
        for _ in range(3):
            gateway_manager._task_beats["poll-gw"] = 120.0 - 601
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
        assert gateway_manager._poll_tasks["gw"] is not stuck
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_cloud_retry_works_on_fresh_boot(
    gm, monkeypatch, caplog
):
    """Regression: small monotonic() must not suppress the first retry."""
    monkeypatch.setattr(time, "monotonic", lambda: 120.0)
    monkeypatch.setattr(gateway_manager, "_cloud_control_configured", True)
    monkeypatch.setattr(
        gateway_manager, "_hybrid_configs", [Mock(id="gw")]
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_task", None)
    monkeypatch.setattr(
        gateway_manager, "_init_cloud_control", lambda cfgs: asyncio.sleep(0)
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert any("retrying init" in r.getMessage() for r in caplog.records)
    task = gateway_manager._cloud_control_task
    assert task is not None and task.get_name() == "cloud-control-init"
    if not task.done():
        task.cancel()


@pytest.mark.asyncio
async def test_watchdog_reports_probe_pool_saturation(
    gm, monkeypatch, caplog
):
    """A saturated probe pool is detected and named (main stays quiet)."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    pool = Mock()
    pool._max_workers = 2
    queue = Mock()
    queue.qsize.return_value = 5
    pool._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_probe_executor", pool)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert any(
            "probe executor saturated" in r.getMessage()
            for r in caplog.records
        )
    finally:
        alive.cancel()
        probe.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await probe
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_forces_poll_task_when_only_probe_saturated(
    gm, monkeypatch, caplog
):
    """A poll task is healed although the unrelated probe pool is full."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck
    pool = Mock()
    pool._max_workers = 2
    queue = Mock()
    queue.qsize.return_value = 5
    pool._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_probe_executor", pool)
    try:
        for _ in range(3):
            _stale_beat()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw"] is not stuck
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
    finally:
        probe.cancel()
        stuck.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_skips_probe_force_when_probe_saturated(
    gm, monkeypatch, caplog
):
    """A wedged probe task is skipped while its own pool is saturated."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    stuck = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw"] = stuck
    gateway_manager._task_beats["tedapi-probe-gw"] = (
        time.monotonic() - gateway_manager._WATCHDOG_PROBE_STALE_S - 1
    )
    pool = Mock()
    pool._max_workers = 2
    queue = Mock()
    queue.qsize.return_value = 5
    pool._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_probe_executor", pool)
    try:
        for _ in range(3):
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert gateway_manager._probe_tasks["gw"] is stuck
        assert any(
            "probe" in r.getMessage() and "skipping force" in r.getMessage()
            for r in caplog.records
        )
    finally:
        alive.cancel()
        stuck.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_forces_probe_task_when_only_main_saturated(
    gm, monkeypatch, caplog
):
    """A wedged probe task is healed although only the main pool is full."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    stuck = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw"] = stuck
    gateway_manager._task_beats["tedapi-probe-gw"] = (
        time.monotonic() - gateway_manager._WATCHDOG_PROBE_STALE_S - 1
    )
    main = Mock()
    main._max_workers = 10
    main_q = Mock()
    main_q.qsize.return_value = 25
    main._work_queue = main_q
    monkeypatch.setattr(gateway_manager, "_executor", main)
    healthy_probe = Mock()
    healthy_probe._max_workers = 2
    healthy_q = Mock()
    healthy_q.qsize.return_value = 0
    healthy_probe._work_queue = healthy_q
    monkeypatch.setattr(gateway_manager, "_probe_executor", healthy_probe)
    try:
        for _ in range(3):
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert gateway_manager._probe_tasks["gw"] is not stuck
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
    finally:
        alive.cancel()
        stuck.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass
        current = gateway_manager._probe_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_both_pools_saturated_skips_both(
    gm, monkeypatch, caplog
):
    """Both pools full: each wedged task is judged by its own pool."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    stuck_poll = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck_poll
    _stale_beat()
    stuck_probe = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._probe_tasks["gw"] = stuck_probe
    gateway_manager._task_beats["tedapi-probe-gw"] = (
        time.monotonic() - gateway_manager._WATCHDOG_PROBE_STALE_S - 1
    )
    main = Mock()
    main._max_workers = 10
    main_q = Mock()
    main_q.qsize.return_value = 25
    main._work_queue = main_q
    monkeypatch.setattr(gateway_manager, "_executor", main)
    probe_pool = Mock()
    probe_pool._max_workers = 2
    probe_q = Mock()
    probe_q.qsize.return_value = 5
    probe_pool._work_queue = probe_q
    monkeypatch.setattr(gateway_manager, "_probe_executor", probe_pool)
    try:
        for _ in range(3):
            _stale_beat()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw"] is stuck_poll
        assert gateway_manager._probe_tasks["gw"] is stuck_probe
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "main" in m and "skipping force" in m for m in messages
        )
        assert any(
            "probe" in m and "skipping force" in m for m in messages
        )
    finally:
        stuck_poll.cancel()
        stuck_probe.cancel()
        try:
            await stuck_poll
        except asyncio.CancelledError:
            pass
        try:
            await stuck_probe
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_force_during_executor_wait_ends_old_task(
    gm, monkeypatch, caplog
):
    """Force while the old task awaits run_in_executor: it ends reliably,
    the registry points at the replacement, the thread outlives briefly
    (inherent) and then ends too — no uncontrolled parallel recovery."""
    import threading

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    gate = threading.Event()
    thread_exited = threading.Event()
    old_cancelled = asyncio.Event()

    def _park():
        gate.wait()
        thread_exited.set()

    async def _old_loop():
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _park)
        except asyncio.CancelledError:
            old_cancelled.set()
            raise

    old = asyncio.create_task(_old_loop())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gateway_manager._poll_tasks["gw"] = old
    gateway_manager._task_stale_counts["poll-gw"] = 2
    _stale_beat()
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        replacement = gateway_manager._poll_tasks["gw"]
        assert replacement is not old
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
        # Overlap window exists by asyncio design: cancel is delivered,
        # not synchronous. The old task ends on delivery...
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert old.done()
        assert old_cancelled.is_set()
        # ...while its thread is still parked (inherent to executors) ...
        assert not thread_exited.is_set()
        # ...and ends once unblocked. No parallel recovery afterwards:
        # the replacement is a fresh loop, the old one is done.
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if thread_exited.is_set():
                break
        assert thread_exited.is_set()
    finally:
        gate.set()
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_poll_loop_records_heartbeat(gm):
    """The poll loop beats each iteration (backoff skip, no network)."""
    gateway_manager._next_poll_time["nogw"] = time.time() + 3600
    task = asyncio.create_task(gateway_manager._poll_gateway_loop("nogw"))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if "poll-nogw" in gateway_manager._task_beats:
                break
        assert "poll-nogw" in gateway_manager._task_beats
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_shutdown_stops_watchdog_first(gm):
    """shutdown() cancels the watchdog and clears the reference."""

    async def _loop():
        await asyncio.sleep(3600)

    wd = asyncio.create_task(_loop())
    gateway_manager._watchdog_task = wd
    await gateway_manager.shutdown()
    assert gateway_manager._watchdog_task is None
    assert wd.done()


@pytest.mark.asyncio
async def test_shutdown_stops_probe_executor(gm, monkeypatch):
    """shutdown() shuts down the dedicated probe pool and clears it."""
    from unittest.mock import Mock

    pool = Mock()
    monkeypatch.setattr(gateway_manager, "_probe_executor", pool)
    await gateway_manager.shutdown()
    pool.shutdown.assert_called_once_with(wait=False)
    assert gateway_manager._probe_executor is None


def test_pool_saturated_false_on_broken_pool(gm):
    """A pool that raises on introspection degrades to unknown (False)."""

    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("no introspection")

    assert gateway_manager._pool_saturated(_Boom()) is False
    assert gateway_manager._pool_pending(_Boom()) is None


@pytest.mark.asyncio
async def test_shutdown_tolerates_failed_watchdog_task(gm, caplog):
    """A watchdog task that died with an error does not break shutdown."""

    async def _boom():
        raise RuntimeError("watchdog boom")

    wd = asyncio.create_task(_boom())
    try:
        await wd
    except RuntimeError:
        pass
    assert wd.done()
    gateway_manager._watchdog_task = wd
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await gateway_manager.shutdown()
    assert gateway_manager._watchdog_task is None
    assert any(
        "shutdown error" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_watchdog_force_recreates_despite_cancel_error(
    gm, monkeypatch, caplog
):
    """A task whose cancel() raises is still replaced."""

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stubborn = Mock()
    stubborn.done.return_value = False
    stubborn.cancel.side_effect = RuntimeError("cannot cancel")
    gateway_manager._poll_tasks["gw"] = stubborn
    try:
        for _ in range(3):
            _stale_beat()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
        assert gateway_manager._poll_tasks["gw"] is not stubborn
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_executor_small_queue_is_debug_only(
    gm, monkeypatch, caplog
):
    """A healthy queue depth stays at debug level (no warning)."""

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    executor = Mock()
    executor._max_workers = 10
    queue = Mock()
    queue.qsize.return_value = 3
    executor._work_queue = queue
    monkeypatch.setattr(gateway_manager, "_executor", executor)
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "watchdog" in r.getMessage().lower()
        ]
        assert any(
            "queued / 10 workers" in r.getMessage() for r in caplog.records
        )
    finally:
        alive.cancel()
        probe.cancel()
        try:
            await alive
        except asyncio.CancelledError:
            pass
        try:
            await probe
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_recreates_missing_slot_as_missing(
    gm, monkeypatch, caplog
):
    """No task object at all: recreated and reported as missing."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert any("missing" in r.getMessage() for r in caplog.records)
        assert "gw" in gateway_manager._poll_tasks
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_reports_task_error_and_retrieves_it(
    gm, monkeypatch, caplog
):
    """A task that died with an exception: error logged, retrieved, replaced."""

    async def _boom():
        raise ValueError("boom")

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    failed = asyncio.create_task(_boom())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert failed.done()
        gateway_manager._poll_tasks["gw"] = failed
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert any("boom" in r.getMessage() for r in caplog.records)
        assert gateway_manager._poll_tasks["gw"] is not failed
        assert not [
            r
            for r in caplog.records
            if "never retrieved" in r.getMessage().lower()
        ]
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_recreates_cancelled_task(gm, monkeypatch, caplog):
    """A cancelled task is reported as cancelled and replaced."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    cancelled = asyncio.create_task(asyncio.sleep(3600))
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    assert cancelled.done()
    gateway_manager._poll_tasks["gw"] = cancelled
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert any("cancelled" in r.getMessage() for r in caplog.records)
        assert gateway_manager._poll_tasks["gw"] is not cancelled
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_loop_runs_and_exits_cleanly(
    gm, monkeypatch, caplog
):
    """The loop itself: passes run quietly, cancellation ends it."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    monkeypatch.setattr(gateway_manager, "_WATCHDOG_INTERVAL_S", 0.01)
    probe = await _live_seed("tedapi-probe-gw")
    alive = asyncio.create_task(asyncio.sleep(3600))
    gateway_manager._poll_tasks["gw"] = alive
    gateway_manager._beat("poll-gw")
    loop_task = asyncio.create_task(gateway_manager._task_watchdog_loop())
    try:
        await asyncio.sleep(0.05)
        assert not loop_task.done()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await asyncio.sleep(0.05)
        assert not [
            r for r in caplog.records if "watchdog" in r.getMessage().lower()
        ]
    finally:
        loop_task.cancel()
        alive.cancel()
        probe.cancel()
        for task in (loop_task, alive, probe):
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_watchdog_loop_survives_check_errors(gm, monkeypatch, caplog):
    """A failing check is logged; the loop keeps running."""
    def _raise():
        raise RuntimeError("check boom")

    monkeypatch.setattr(gateway_manager, "_watchdog_check", _raise)
    monkeypatch.setattr(gateway_manager, "_WATCHDOG_INTERVAL_S", 0.01)
    loop_task = asyncio.create_task(gateway_manager._task_watchdog_loop())
    try:
        await asyncio.sleep(0.05)
        assert not loop_task.done()
        assert any(
            "check boom" in r.getMessage() for r in caplog.records
        )
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_never_creates_probe_for_cloud_gateway(
    gm, monkeypatch, caplog
):
    """Cloud/FleetAPI gateways must not gain a probe loop from the watchdog."""

    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        gateway_manager,
        "gateways",
        {"c": Mock(host=None, cloud_mode=True, fleetapi=False)},
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert "c" not in gateway_manager._probe_tasks
    assert not [
        r for r in caplog.records if "tedapi-probe" in r.getMessage()
    ]
    assert settings.tedapi_recovery  # guard: test is meaningful only then


@pytest.mark.asyncio
async def test_watchdog_skips_probe_when_recovery_disabled(
    gm, monkeypatch, caplog
):
    """With recovery disabled, missing probe slots stay missing."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    monkeypatch.setattr(settings, "tedapi_recovery", False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gateway_manager._watchdog_check()
    assert "gw" not in gateway_manager._probe_tasks
    assert not [
        r for r in caplog.records if "tedapi-probe" in r.getMessage()
    ]


async def _stuck_task():
    task = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    return task


def _stale_beat():
    gateway_manager._task_beats["poll-gw"] = (
        time.monotonic() - gateway_manager._WATCHDOG_POLL_STALE_S - 1
    )


@pytest.mark.asyncio
async def test_watchdog_force_recreates_after_three_strikes(
    gm, monkeypatch, caplog
):
    """Three consecutive stale checks: wedged task is cancelled + replaced."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck
    try:
        for _ in range(3):
            _stale_beat()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                gateway_manager._watchdog_check()
        assert any(
            "cancelling and recreating" in r.getMessage()
            for r in caplog.records
        )
        assert gateway_manager._poll_tasks["gw"] is not stuck
        await asyncio.sleep(0)
        assert stuck.cancelled() or stuck.done()
    finally:
        probe.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        current = gateway_manager._poll_tasks.get("gw")
        if current is not None and not current.done():
            current.cancel()


@pytest.mark.asyncio
async def test_watchdog_force_cooldown_prevents_recreate_loop(
    gm, monkeypatch, caplog
):
    """A second force within the cooldown is skipped (strike counted)."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck
    gateway_manager._task_last_forced["poll-gw"] = time.monotonic()
    try:
        for _ in range(3):
            _stale_beat()
            gateway_manager._watchdog_check()
        assert gateway_manager._poll_tasks["gw"] is stuck
        assert not stuck.done()
    finally:
        probe.cancel()
        stuck.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watchdog_strike_counter_resets_on_fresh_beat(
    gm, monkeypatch, caplog
):
    """Two strikes then a fresh beat: counter cleared, task kept, quiet."""
    monkeypatch.setattr(
        gateway_manager, "_poll_gateway_loop", lambda gid: asyncio.sleep(0)
    )
    probe = await _live_seed("tedapi-probe-gw")
    stuck = await _stuck_task()
    gateway_manager._poll_tasks["gw"] = stuck
    try:
        for _ in range(2):
            _stale_beat()
            gateway_manager._watchdog_check()
        assert gateway_manager._task_stale_counts.get("poll-gw") == 2
        gateway_manager._beat("poll-gw")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            gateway_manager._watchdog_check()
        assert "poll-gw" not in gateway_manager._task_stale_counts
        assert gateway_manager._poll_tasks["gw"] is stuck
        assert not [r for r in caplog.records if "watchdog" in r.getMessage().lower()]
    finally:
        probe.cancel()
        stuck.cancel()
        try:
            await probe
        except asyncio.CancelledError:
            pass
        try:
            await stuck
        except asyncio.CancelledError:
            pass