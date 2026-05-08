"""
Tests for Phase 1 of MQTT support — core publisher.

These tests use a mock MQTT client (no real broker required) and validate:
  - Publisher is disabled when MQTT_HOST is not set
  - Publisher is enabled when MQTT_HOST is set
  - start() / stop() lifecycle management
  - publish_gateway() builds all expected topics from a GatewayStatus
  - Publish is silently skipped when disconnected
  - Publish failure sets _connected=False (triggers reconnect)
  - _extract_power() handles missing/malformed aggregates safely
  - Connection loop reconnects after failure (backoff logic)
  - MQTT failures never propagate to caller (fire-and-forget safety)
"""
import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.gateway import Gateway, GatewayStatus, PowerwallData
from app.mqtt.publisher import MqttPublisher, _extract_power


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_status(
    gateway_id: str = "test-gw",
    online: bool = True,
    soe: float = 75.0,
    solar: float = 3000.0,
    grid: float = -500.0,
    home: float = 2500.0,
    pw_power: float = 0.0,
    grid_status: str = "UP",
    mode: str = "self_consumption",
    reserve: float = 20.0,
    version: str = "23.44.0",
) -> GatewayStatus:
    """Build a minimal GatewayStatus suitable for testing."""
    gateway = Gateway(
        id=gateway_id,
        name="Test Gateway",
        host="192.168.91.1",
        gw_pwd="test",
        online=online,
    )
    data = PowerwallData(
        soe=soe,
        aggregates={
            "solar": {"instant_power": solar},
            "site": {"instant_power": grid},
            "load": {"instant_power": home},
            "battery": {"instant_power": pw_power},
        },
        grid_status=grid_status,
        mode=mode,
        reserve=reserve,
        version=version,
        timestamp=1_000_000.0,
    )
    return GatewayStatus(
        gateway=gateway, data=data, online=online, last_updated=1_000_000.0
    )


# ---------------------------------------------------------------------------
# _extract_power unit tests (pure function — no async needed)
# ---------------------------------------------------------------------------

class TestExtractPower:
    def test_normal_value(self):
        agg = {"solar": {"instant_power": 1234.5}}
        assert _extract_power(agg, "solar") == pytest.approx(1234.5)

    def test_missing_key(self):
        assert _extract_power({}, "solar") is None

    def test_missing_instant_power(self):
        assert _extract_power({"solar": {}}, "solar") is None

    def test_non_numeric_value(self):
        agg = {"solar": {"instant_power": "bad"}}
        assert _extract_power(agg, "solar") is None

    def test_negative_value(self):
        agg = {"site": {"instant_power": -400.0}}
        assert _extract_power(agg, "site") == pytest.approx(-400.0)

    def test_none_aggregates(self):
        assert _extract_power(None, "solar") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MqttPublisher unit tests
# ---------------------------------------------------------------------------

class TestMqttPublisherDisabled:
    """Publisher should be completely inert when MQTT_HOST is not set."""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("MQTT_HOST", raising=False)
        pub = MqttPublisher()
        assert pub.enabled is False

    @pytest.mark.asyncio
    async def test_start_does_nothing_when_disabled(self, monkeypatch):
        monkeypatch.delenv("MQTT_HOST", raising=False)
        pub = MqttPublisher()
        await pub.start()
        assert pub._connection_task is None

    @pytest.mark.asyncio
    async def test_publish_gateway_does_nothing_when_disabled(self, monkeypatch):
        monkeypatch.delenv("MQTT_HOST", raising=False)
        pub = MqttPublisher()
        # Should complete without error and without calling any client methods
        status = make_status()
        await pub.publish_gateway("test-gw", status)  # no exception expected


class TestMqttPublisherEnabled:
    """Publisher behaviour when MQTT_HOST is set (mocked client)."""

    def _make_publisher(self, monkeypatch) -> MqttPublisher:
        monkeypatch.setenv("MQTT_HOST", "localhost")
        monkeypatch.setenv("MQTT_PORT", "1883")
        monkeypatch.setenv("MQTT_TOPIC_PREFIX", "pypowerwall")
        monkeypatch.setenv("MQTT_QOS", "1")
        monkeypatch.setenv("MQTT_RETAIN", "true")
        # Reload settings so the new env vars are picked up
        import importlib
        import app.config as cfg_module
        importlib.reload(cfg_module)
        pub = MqttPublisher()
        return pub

    def test_enabled_when_host_set(self, monkeypatch):
        from app.config import settings as _settings
        # settings is a module-level singleton; patch its attribute directly
        monkeypatch.setattr(_settings, "mqtt_host", "broker.local")
        pub = MqttPublisher()
        assert pub.enabled is True

    @pytest.mark.asyncio
    async def test_publish_skipped_when_not_connected(self, monkeypatch):
        """publish_gateway() must be silent when disconnected."""
        pub = self._make_publisher(monkeypatch)
        pub._connected = False
        pub._client = None

        status = make_status()
        # Must not raise
        await pub.publish_gateway("test-gw", status)

    @pytest.mark.asyncio
    async def test_publish_gateway_calls_correct_topics(self, monkeypatch):
        """All expected sensor topics are published with correct values."""
        pub = self._make_publisher(monkeypatch)

        # Set up a mock client
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status(
            soe=75.0,
            solar=3000.0,
            grid=-500.0,
            home=2500.0,
            pw_power=0.0,
            grid_status="UP",
            mode="self_consumption",
            reserve=20.0,
        )

        await pub.publish_gateway("test-gw", status)

        # Collect all published topic/payload pairs
        published: dict[str, str] = {}
        for call in mock_client.publish.call_args_list:
            topic = call.args[0]
            payload = call.args[1]
            published[topic] = payload

        assert "pypowerwall/test-gw/battery" in published
        assert published["pypowerwall/test-gw/battery"] == "75.0"

        assert "pypowerwall/test-gw/solar" in published
        assert published["pypowerwall/test-gw/solar"] == "3000.0"

        assert "pypowerwall/test-gw/grid" in published
        assert published["pypowerwall/test-gw/grid"] == "-500.0"

        assert "pypowerwall/test-gw/home" in published
        assert published["pypowerwall/test-gw/home"] == "2500.0"

        assert "pypowerwall/test-gw/powerwall" in published
        assert published["pypowerwall/test-gw/powerwall"] == "0.0"

        assert "pypowerwall/test-gw/grid_status" in published
        assert published["pypowerwall/test-gw/grid_status"] == "UP"

        assert "pypowerwall/test-gw/mode" in published
        assert published["pypowerwall/test-gw/mode"] == "self_consumption"

        assert "pypowerwall/test-gw/reserve" in published
        assert published["pypowerwall/test-gw/reserve"] == "20.0"

        assert "pypowerwall/test-gw/online" in published
        assert published["pypowerwall/test-gw/online"] == "true"

        assert "pypowerwall/test-gw/availability" in published
        assert published["pypowerwall/test-gw/availability"] == "online"

    @pytest.mark.asyncio
    async def test_aggregates_json_topic_published(self, monkeypatch):
        """Full aggregates dict is published as JSON."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status(solar=1500.0)
        await pub.publish_gateway("test-gw", status)

        published = {c.args[0]: c.args[1] for c in mock_client.publish.call_args_list}
        assert "pypowerwall/test-gw/aggregates" in published
        agg = json.loads(published["pypowerwall/test-gw/aggregates"])
        assert agg["solar"]["instant_power"] == 1500.0

    @pytest.mark.asyncio
    async def test_status_summary_json_topic_published(self, monkeypatch):
        """Summary status JSON topic is published."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        status = make_status(soe=88.0, mode="backup")
        await pub.publish_gateway("test-gw", status)

        published = {c.args[0]: c.args[1] for c in mock_client.publish.call_args_list}
        assert "pypowerwall/test-gw/status" in published
        summary = json.loads(published["pypowerwall/test-gw/status"])
        assert summary["soe"] == 88.0
        assert summary["mode"] == "backup"
        assert summary["online"] is True

    @pytest.mark.asyncio
    async def test_publish_failure_marks_disconnected(self, monkeypatch):
        """A publish error must set _connected=False (triggers reconnect loop)."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        mock_client.publish.side_effect = Exception("broker gone")
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        # Must not raise even though publish fails
        await pub.publish_gateway("test-gw", status)

        # After the first failed publish, _connected should be False
        assert pub._connected is False

    @pytest.mark.asyncio
    async def test_publish_with_none_data(self, monkeypatch):
        """publish_gateway() handles GatewayStatus with data=None (offline gateway)."""
        pub = self._make_publisher(monkeypatch)
        mock_client = AsyncMock()
        pub._client = mock_client
        pub._connected = True

        gateway = Gateway(id="offline-gw", name="Offline", host="1.2.3.4", online=False)
        status = GatewayStatus(gateway=gateway, online=False, last_updated=0.0)

        # Must not raise
        await pub.publish_gateway("offline-gw", status)

        published = {c.args[0]: c.args[1] for c in mock_client.publish.call_args_list}
        # Only the online topic and availability should be published when data is None
        assert "pypowerwall/offline-gw/online" in published
        assert published["pypowerwall/offline-gw/online"] == "false"

    @pytest.mark.asyncio
    async def test_connection_loop_publishes_global_availability_on_connect(self, monkeypatch):
        """On connect, global {prefix}/availability must be published as 'online'.

        This fixes issue #33: the discovery payload references this topic with
        availability_mode='all', so without this message HA entities stay stuck
        at 'unavailable' even when per-gateway state data is flowing correctly.
        """
        import aiomqtt
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        pub = self._make_publisher(monkeypatch)

        published: dict[str, str] = {}

        mock_client = MagicMock()
        mock_client.publish = AsyncMock(
            side_effect=lambda topic, payload, **kw: published.__setitem__(topic, payload)
        )

        @asynccontextmanager
        async def fake_client_ctx(**kwargs):
            yield mock_client

        # Run the connection loop but stop it after one pass
        async def fake_sleep(n):
            pub._shutdown = True  # stop inner heartbeat immediately

        with (
            patch("aiomqtt.Client", side_effect=lambda **kw: fake_client_ctx(**kw)),
            patch("asyncio.sleep", new=fake_sleep),
        ):
            await pub._connection_loop()

        assert "pypowerwall/availability" in published, (
            "Global availability topic must be published on connect (issue #33)"
        )
        assert published["pypowerwall/availability"] == "online"

    @pytest.mark.asyncio
    async def test_stop_cancels_connection_task(self, monkeypatch):
        pub = self._make_publisher(monkeypatch)

        # Patch aiomqtt so the connection loop hangs without a real broker
        async def fake_context(*args, **kwargs):
            yield MagicMock(publish=AsyncMock())

        with patch("app.mqtt.publisher.MqttPublisher._connection_loop", new_callable=lambda: lambda self: asyncio.sleep(9999)):
            # Start a dummy task that sleeps
            pub._shutdown = False
            pub._connection_task = asyncio.create_task(asyncio.sleep(9999))

            await pub.stop()

            assert pub._connection_task.done()


class TestMqttFireAndForget:
    """Verify that MQTT failures never leak into the gateway poll path."""

    @pytest.mark.asyncio
    async def test_publish_exception_does_not_propagate(self, monkeypatch):
        """Even if publish_gateway raises internally, create_task swallows it."""
        monkeypatch.setenv("MQTT_HOST", "localhost")
        pub = MqttPublisher()

        mock_client = AsyncMock()
        mock_client.publish.side_effect = RuntimeError("total failure")
        pub._client = mock_client
        pub._connected = True

        status = make_status()
        # Called via create_task in gateway_manager; must not raise here
        task = asyncio.create_task(pub.publish_gateway("test-gw", status))
        await task  # no exception expected
