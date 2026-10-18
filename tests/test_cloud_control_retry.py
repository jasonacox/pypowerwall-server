"""Tests for the cloud-control init retry with exponential backoff.

A Tesla cloud that is unreachable at boot (or during a first-setup token
exchange) must not leave hybrid write control permanently unavailable.
_init_cloud_control now retries in a loop, backing off from
_CLOUD_CONTROL_RETRY_INITIAL up to _CLOUD_CONTROL_RETRY_MAX, and stops
as soon as a connection exists (or one already did).
"""
import asyncio
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings
from app.core.gateway_manager import GatewayManager

# The app.core package exports the singleton as gateway_manager, shadowing
# the module attribute — reach the module itself through sys.modules.
gm_module = sys.modules["app.core.gateway_manager"]

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fast_sleep(monkeypatch):
    """Replace asyncio.sleep with an instant tick, recording delays."""
    real_sleep = asyncio.sleep
    delays = []

    async def _fast(delay, result=None):
        delays.append(delay)
        await real_sleep(0)
        return result

    monkeypatch.setattr(gm_module.asyncio, "sleep", _fast)
    return delays


def _make_gm(monkeypatch):
    """GatewayManager with a mocked Powerwall factory and site lookup."""
    monkeypatch.setattr(settings, "siteid", None)
    gm = GatewayManager()
    monkeypatch.setattr(
        gm, "_cloud_site_unambiguous", AsyncMock(return_value=True)
    )
    return gm


def _config(gid="gw1"):
    return Mock(
        id=gid,
        email="user@example.com",
        authpath="",
        fleetapi=False,
        timezone="America/Los_Angeles",
    )


class TestCloudControlRetry:
    """_init_cloud_control retry behavior."""

    async def test_success_path_terminates(self, fast_sleep, monkeypatch):
        """A successful init returns without any sleep (task must end)."""
        gm = _make_gm(monkeypatch)
        pw = Mock()
        factory = Mock(return_value=pw)
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        assert gm._cloud_control is pw
        assert gm._cloud_control_gateway_id == "gw1"
        assert fast_sleep == []

    async def test_returns_immediately_when_already_connected(
        self, fast_sleep, monkeypatch
    ):
        """An existing connection short-circuits the whole loop."""
        gm = _make_gm(monkeypatch)
        gm._cloud_control = Mock()
        factory = Mock()
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        factory.assert_not_called()
        assert fast_sleep == []

    async def test_retries_until_success_with_backoff(
        self, fast_sleep, monkeypatch
    ):
        """Failures back off 60s then 120s before the retry succeeds."""
        gm = _make_gm(monkeypatch)
        pw = Mock()
        factory = Mock(
            side_effect=[
                ConnectionError("cloud down"),
                ConnectionError("cloud down"),
                pw,
            ]
        )
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        assert gm._cloud_control is pw
        assert fast_sleep == [
            gm._CLOUD_CONTROL_RETRY_INITIAL,
            gm._CLOUD_CONTROL_RETRY_INITIAL * 2,
        ]

    async def test_backoff_is_capped(self, fast_sleep, monkeypatch):
        """Backoff doubles but never exceeds _CLOUD_CONTROL_RETRY_MAX."""
        gm = _make_gm(monkeypatch)
        pw = Mock()
        factory = Mock(
            side_effect=[ConnectionError("x")] * 6 + [pw]
        )
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        cap = gm._CLOUD_CONTROL_RETRY_MAX
        assert gm._cloud_control is pw
        assert fast_sleep == [
            gm._CLOUD_CONTROL_RETRY_INITIAL,  # 60
            gm._CLOUD_CONTROL_RETRY_INITIAL * 2,  # 120
            gm._CLOUD_CONTROL_RETRY_INITIAL * 4,  # 240
            cap,
            cap,
            cap,
        ]

    async def test_every_config_tried_before_backoff_sleep(
        self, fast_sleep, monkeypatch
    ):
        """All hybrid configs are tried once per pass before sleeping."""
        gm = _make_gm(monkeypatch)
        pw = Mock()
        factory = Mock(
            side_effect=[ConnectionError("a"), ConnectionError("b"), pw]
        )
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config("gw1"), _config("gw2")]),
            timeout=2.0,
        )

        assert gm._cloud_control is pw
        assert fast_sleep == [gm._CLOUD_CONTROL_RETRY_INITIAL]
        assert factory.call_count == 3

    async def test_unconnected_object_from_constructor_is_retried(
        self, fast_sleep, monkeypatch
    ):
        """pypowerwall's constructor doesn't raise when the cloud is
        unreachable: it returns an object its failover left in local mode
        with is_connected() False. That must not count as established."""
        gm = _make_gm(monkeypatch)
        dead = Mock(cloudmode=False, fleetapi=False, mode="local")
        dead.is_connected.return_value = False
        pw = Mock(cloudmode=True)
        pw.is_connected.return_value = True
        factory = Mock(side_effect=[dead, pw])
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        assert gm._cloud_control is pw
        assert fast_sleep == [gm._CLOUD_CONTROL_RETRY_INITIAL]

    async def test_cloud_mode_but_not_connected_is_retried(
        self, fast_sleep, monkeypatch
    ):
        gm = _make_gm(monkeypatch)
        stale = Mock(cloudmode=True, mode="cloud")
        stale.is_connected.return_value = False
        pw = Mock(cloudmode=True)
        pw.is_connected.return_value = True
        factory = Mock(side_effect=[stale, pw])
        monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", factory)

        await asyncio.wait_for(
            gm._init_cloud_control([_config()]), timeout=2.0
        )

        assert gm._cloud_control is pw
        assert stale is not gm._cloud_control
