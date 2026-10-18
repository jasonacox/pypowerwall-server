"""Regression tests for TEDAPI probe/recovery against drifted connections.

Issue #155: pypowerwall's connect() failover can switch a connection to
cloud mode inside a timed-out connect() (``pw.tedapi`` False, mode
"cloud").  The old probe loop gated on ``pw.tedapi`` and skipped such a
connection silently, so a gateway stayed on cloud reads for hours.

The loop now:

- probes by configuration (only PW3 Basic LAN is exempt — its connection
  has no TEDAPI client at all),
- counts a mode drift as a failed probe and enters fallback after
  ``_TEDAPI_FALLBACK_THRESHOLD``,
- recovers by building a fresh connection (``_create_local_connection``)
  instead of calling ``connect()`` on the drifted object, which cannot
  recover (pypowerwall restores the configured mode only when every mode
  fails — pypowerwall #366),
- swaps the fresh connection in only when it comes up in TEDAPI mode and
  reports a version; otherwise the existing connection keeps serving.
"""
import asyncio
import logging
import sys
from asyncio import sleep as _real_sleep
from contextlib import suppress
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import GatewayConfig
from app.core.gateway_manager import GatewayManager
from app.models.gateway import Gateway

# The app.core package exports the singleton as gateway_manager, shadowing
# the module attribute — reach the module itself through sys.modules.
gm_module = sys.modules["app.core.gateway_manager"]

pytestmark = pytest.mark.asyncio


@pytest.fixture
def probe_sleep(monkeypatch):
    """Gated replacement for asyncio.sleep in the probe loop.

    An instant fake sleep lets the background loop free-run: while the
    test polls in real time, the loop burns thousands of cycles, flooding
    the shared executor and the event loop's wakeup pipe.  On Python 3.11
    that deadlocks the loop (an executor thread gets stuck writing to the
    full self-pipe while the loop blocks on the future's condition lock,
    seen on the Windows proactor and on CI runners).  Instead every sleep
    parks the loop until the test releases the next cycle, so
    cancellation always finds the task parked between cycles.
    """
    state = {"cycles": 0, "pending": 0, "parked": False, "wakeup": None}

    async def _gated(delay, result=None):
        state["cycles"] += 1
        state["parked"] = True
        while state["pending"] <= 0:
            state["wakeup"] = asyncio.Event()
            await state["wakeup"].wait()
        state["pending"] -= 1
        state["parked"] = False
        return result

    def release(cycles=1):
        state["pending"] += cycles
        if state["wakeup"] is not None:
            state["wakeup"].set()

    monkeypatch.setattr(gm_module.asyncio, "sleep", _gated)
    state["release"] = release
    return state


async def _wait_until(predicate, timeout=2.0):
    """Poll predicate() using the real sleep (immune to the patch)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for probe loop condition")
        await _real_sleep(0.001)


async def _run_probe_loop(gm, gateway_id, predicate, state, timeout=2.0):
    """Run the probe loop until predicate() holds and it is parked.

    Waiting for ``state["parked"]`` guarantees the task sits between two
    cycles before it is cancelled, so no executor job is in flight.
    """
    task = asyncio.create_task(
        gm._tedapi_probe_loop(gateway_id), name="probe-test"
    )
    try:
        await _wait_until(lambda: predicate() and state["parked"], timeout)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _make_gm(basic_lan=False, fallback=False):
    """GatewayManager with one tracked TEDAPI gateway."""
    gm = GatewayManager()
    gm.gateways["gw1"] = Gateway(
        id="gw1", name="gw1", host="192.168.91.1", gw_pwd="pw",
        basic_lan=basic_lan,
    )
    gm._gateway_configs["gw1"] = GatewayConfig(
        id="gw1", host="192.168.91.1", gw_pwd="pw"
    )
    gm._fallback_state["gw1"] = gm._new_fallback_state()
    gm._tedapi_probe_failures["gw1"] = 0
    if fallback:
        gm._enter_fallback_mode("gw1", "test setup")
    return gm


def _conn(tedapi, mode, version="23.44.0"):
    """Stand-in pypowerwall connection with explicit mode flags."""
    conn = Mock()
    conn.tedapi = tedapi
    conn.mode = mode
    conn.version.return_value = version
    return conn


class TestProbeByConfiguration:
    """The probe must not skip a connection that drifted off TEDAPI."""

    async def test_mode_drift_counts_as_failed_probe_and_enters_fallback(
        self, probe_sleep, monkeypatch, caplog
    ):
        """State left behind by a timed-out connect() must go to fallback.

        Regression for #155: pw.tedapi is False after the library's
        failover switched the connection to cloud reads.  The loop must
        count that as a failed probe instead of skipping silently.
        """
        gm = _make_gm()
        gm.connections["gw1"] = _conn(tedapi=False, mode="cloud")
        monkeypatch.setattr(
            gm, "_create_local_connection",
            AsyncMock(return_value=_conn(False, "cloud")),
        )
        enter = Mock(wraps=gm._enter_fallback_mode)
        monkeypatch.setattr(gm, "_enter_fallback_mode", enter)

        caplog.set_level(logging.WARNING)
        probe_sleep["release"](3)
        await _run_probe_loop(
            gm, "gw1", lambda: enter.call_count >= 1, probe_sleep
        )

        assert enter.call_count == 1
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is True
        assert (
            gm._tedapi_probe_failures["gw1"]
            == gm._TEDAPI_FALLBACK_THRESHOLD
        )
        assert "not TEDAPI" in caplog.text
        gm.connections["gw1"].connect.assert_not_called()

    async def test_healthy_connection_probes_and_resets_failures(
        self, probe_sleep, monkeypatch
    ):
        """A TEDAPI-mode connection is probed and clears failure counts."""
        gm = _make_gm()
        gm._tedapi_probe_failures["gw1"] = 2
        conn = _conn(tedapi=Mock(), mode="tedapi")
        gm.connections["gw1"] = conn
        monkeypatch.setattr(
            gm, "_create_local_connection", AsyncMock()
        )

        probe_sleep["release"](1)
        await _run_probe_loop(
            gm, "gw1", lambda: gm._tedapi_probe_failures["gw1"] == 0,
            probe_sleep,
        )

        assert conn.version.called
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is False

    async def test_basic_lan_gateway_is_exempt(
        self, probe_sleep, monkeypatch
    ):
        """PW3 Basic LAN has no TEDAPI client: no probe, no penalty."""
        gm = _make_gm(basic_lan=True)
        conn = _conn(tedapi=None, mode="basic-lan")
        gm.connections["gw1"] = conn
        monkeypatch.setattr(gm, "_create_local_connection", AsyncMock())
        enter = Mock(wraps=gm._enter_fallback_mode)
        monkeypatch.setattr(gm, "_enter_fallback_mode", enter)

        probe_sleep["release"](3)
        await _run_probe_loop(
            gm, "gw1", lambda: probe_sleep["cycles"] >= 3, probe_sleep
        )

        assert enter.call_count == 0
        assert gm._tedapi_probe_failures["gw1"] == 0
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is False
        conn.version.assert_not_called()


class TestRecoveryBuildsFreshConnection:
    """Recovery must build a fresh connection, never re-connect the old one."""

    async def test_fresh_tedapi_connection_is_swapped_in(
        self, probe_sleep, monkeypatch, caplog
    ):
        """A fresh connection in TEDAPI mode with a version replaces the old."""
        gm = _make_gm(fallback=True)
        old = _conn(tedapi=False, mode="cloud")
        gm.connections["gw1"] = old
        fresh = _conn(tedapi=Mock(), mode="tedapi", version="24.4.0")
        create = AsyncMock(return_value=fresh)
        monkeypatch.setattr(gm, "_create_local_connection", create)

        caplog.set_level(logging.INFO)
        # One fallback cycle: probe-interval tick, then the backoff tick.
        probe_sleep["release"](2)
        await _run_probe_loop(
            gm, "gw1", lambda: gm.connections.get("gw1") is fresh,
            probe_sleep,
        )

        create.assert_awaited_once_with(gm._gateway_configs["gw1"])
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is False
        assert gm._tedapi_probe_failures["gw1"] == 0
        # The drifted object is never re-connected: the library's failover
        # left it on cloud, so connect() on it cannot recover (#155).
        old.connect.assert_not_called()
        assert "replacing connection" in caplog.text

    async def test_fresh_connection_that_landed_in_cloud_is_discarded(
        self, probe_sleep, monkeypatch, caplog
    ):
        """An in-time connect() ending on cloud reads is not a recovery.

        The fresh object must not be swapped in (it is not serving TEDAPI),
        and the gateway stays in fallback while the old connection keeps
        serving SolarOnly data.
        """
        gm = _make_gm(fallback=True)
        old = _conn(tedapi=False, mode="cloud")
        gm.connections["gw1"] = old
        create = AsyncMock(return_value=_conn(tedapi=False, mode="cloud"))
        monkeypatch.setattr(gm, "_create_local_connection", create)

        caplog.set_level(logging.WARNING)
        probe_sleep["release"](2)
        await _run_probe_loop(
            gm, "gw1", lambda: create.await_count >= 1, probe_sleep
        )

        assert gm.connections["gw1"] is old
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is True
        assert "not in TEDAPI mode" in caplog.text
        # The drifted object must never be re-connected (it would stay on
        # cloud reads — this is the variant that must not count as recovered).
        old.connect.assert_not_called()

    async def test_fresh_connection_without_version_is_discarded(
        self, probe_sleep, monkeypatch, caplog
    ):
        """TEDAPI mode but version() returns None: do not swap it in."""
        gm = _make_gm(fallback=True)
        old = _conn(tedapi=False, mode="cloud")
        gm.connections["gw1"] = old
        create = AsyncMock(
            return_value=_conn(tedapi=Mock(), mode="tedapi", version=None)
        )
        monkeypatch.setattr(gm, "_create_local_connection", create)

        caplog.set_level(logging.WARNING)
        probe_sleep["release"](2)
        await _run_probe_loop(
            gm, "gw1", lambda: create.await_count >= 1, probe_sleep
        )

        assert gm.connections["gw1"] is old
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is True
        assert "reported no version" in caplog.text
        old.connect.assert_not_called()

    async def test_recovery_exception_keeps_existing_connection(
        self, probe_sleep, monkeypatch, caplog
    ):
        """A failing build logs the repr and keeps the old connection.

        Regression for the #155 diagnosis: TimeoutError has an empty
        str(), so the log must use %r to stay legible.
        """
        gm = _make_gm(fallback=True)
        old = _conn(tedapi=False, mode="cloud")
        gm.connections["gw1"] = old
        create = AsyncMock(side_effect=asyncio.TimeoutError())
        monkeypatch.setattr(gm, "_create_local_connection", create)

        caplog.set_level(logging.WARNING)
        probe_sleep["release"](2)
        await _run_probe_loop(
            gm, "gw1", lambda: create.await_count >= 1, probe_sleep
        )

        assert gm.connections["gw1"] is old
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is True
        assert "TimeoutError()" in caplog.text
        assert gm._fallback_state["gw1"]["recovery_attempts"] >= 1
        old.connect.assert_not_called()

    async def test_recovery_without_stored_config_is_handled(
        self, probe_sleep, monkeypatch, caplog
    ):
        """A missing stored config must warn, not crash the loop."""
        gm = _make_gm(fallback=True)
        old = _conn(tedapi=False, mode="cloud")
        gm.connections["gw1"] = old
        gm._gateway_configs.clear()
        create = AsyncMock()
        monkeypatch.setattr(gm, "_create_local_connection", create)

        caplog.set_level(logging.WARNING)
        probe_sleep["release"](2)
        await _run_probe_loop(
            gm,
            "gw1",
            lambda: gm._fallback_state["gw1"]["recovery_attempts"] >= 1,
            probe_sleep,
        )

        create.assert_not_awaited()
        assert gm.connections["gw1"] is old
        assert gm._fallback_state["gw1"]["is_fallback_mode"] is True
        assert "no stored configuration" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("pw_timeout, expected", [(5, 15.0), (30, 35.0)])
async def test_connection_build_timeout_follows_pw_timeout(
    monkeypatch, pw_timeout, expected
):
    """Building a connection allows max(15 s, PW_TIMEOUT + 5 s), the budget
    recovery used before: with PW_TIMEOUT=30 a replacement that connects in
    20 s must not be abandoned."""
    from app.config import settings

    monkeypatch.setattr(settings, "timeout", pw_timeout)
    monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", Mock())
    seen = {}
    real_wait_for = gm_module.asyncio.wait_for

    async def spy_wait_for(aw, timeout):
        seen["timeout"] = timeout
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(gm_module.asyncio, "wait_for", spy_wait_for)
    gm = GatewayManager()
    await gm._create_local_connection(
        GatewayConfig(id="gw", host="10.42.1.40", gw_pwd="pw")
    )
    assert seen["timeout"] == expected
