"""Stale retained MQTT topics (#101): a gated topic that stops being
published while the gateway stays online is cleared from the broker."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.mqtt.publisher import MqttPublisher
from tests.test_mqtt_publisher import _status_with_battery_blocks, make_status


class TestStaleRetainedTopics:
    """A gated topic that stops being published is cleared from the broker."""

    def _make_publisher(self, monkeypatch, retain: bool = True) -> MqttPublisher:
        from app.config import settings
        monkeypatch.setattr(settings, "mqtt_host", "localhost")
        monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
        monkeypatch.setattr(settings, "mqtt_qos", 1)
        monkeypatch.setattr(settings, "mqtt_retain", retain)
        monkeypatch.setattr(settings, "mqtt_ha_discovery", False)
        pub = MqttPublisher()
        pub._client = AsyncMock()
        pub._connected = True
        return pub

    @staticmethod
    def _clears(pub) -> list:
        return [
            c.args[0]
            for c in pub._client.publish.call_args_list
            if c.args[1] == "" and c.kwargs.get("retain")
        ]

    async def _publish_without_mode(self, pub, times: int, online: bool = True):
        for _ in range(times):
            status = make_status(online=online)
            status.data.mode = None
            await pub.publish_gateway("test-gw", status)

    @pytest.mark.asyncio
    async def test_topic_cleared_after_consecutive_misses(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        assert self._clears(pub) == []

        # Missing for fewer polls than the threshold: not cleared yet
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS - 1)
        assert self._clears(pub) == []

        await self._publish_without_mode(pub, 1)
        assert self._clears(pub) == ["pypowerwall/test-gw/mode"]

        # Cleared exactly once, not on every later poll
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS + 1)
        assert self._clears(pub) == ["pypowerwall/test-gw/mode"]

    @pytest.mark.asyncio
    async def test_transient_miss_resets_counter(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS - 1)
        await pub.publish_gateway("test-gw", make_status())  # value is back
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS - 1)
        assert self._clears(pub) == []

    @pytest.mark.asyncio
    async def test_offline_gateway_does_not_clear(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS + 2, online=False)
        assert self._clears(pub) == []

    @pytest.mark.asyncio
    async def test_no_clear_when_retain_disabled(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch, retain=False)
        await pub.publish_gateway("test-gw", make_status())
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS + 1)
        assert self._clears(pub) == []

    @pytest.mark.asyncio
    async def test_broker_error_mid_publish_does_not_clear(self, monkeypatch):
        """A publish cut short by a broker error saw only part of the topics."""
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        for _ in range(STALE_TOPIC_CLEAR_POLLS + 1):
            pub._connected = True
            pub._client.publish.side_effect = [None, Exception("broker gone")]
            await pub.publish_gateway("test-gw", make_status())
        assert self._clears(pub) == []

    @pytest.mark.asyncio
    async def test_interrupted_publish_records_only_sent_topics(self, monkeypatch):
        """Topics the broker accepted before an error are still tracked."""
        pub = self._make_publisher(monkeypatch)
        pub._client.publish.side_effect = [None, None, Exception("broker gone")]
        await pub.publish_gateway("test-gw", make_status())
        calls = [c.args[0] for c in pub._client.publish.call_args_list]
        assert set(pub._retained_topics["test-gw"]) == {
            t for t in calls[:2] if t.startswith("pypowerwall/test-gw/")
        }
        assert calls[2] not in pub._retained_topics["test-gw"]

    @staticmethod
    def _fail_clears(exc):
        def publish(topic, payload, **kwargs):
            if payload == "":
                raise exc
        return publish

    @pytest.mark.asyncio
    async def test_failed_clear_retried_next_poll(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS - 1)
        pub._client.publish.side_effect = self._fail_clears(Exception("broker gone"))
        await self._publish_without_mode(pub, 1)
        assert pub._retained_topics["test-gw"]["pypowerwall/test-gw/mode"] >= STALE_TOPIC_CLEAR_POLLS

        pub._client.publish.side_effect = None
        pub._connected = True
        await self._publish_without_mode(pub, 1)
        assert self._clears(pub)[-1] == "pypowerwall/test-gw/mode"
        assert "pypowerwall/test-gw/mode" not in pub._retained_topics["test-gw"]

    @pytest.mark.asyncio
    async def test_cancelled_clear_keeps_counters(self, monkeypatch):
        """A publish task replaced mid-clear (latest-wins) loses no counts."""
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        await self._publish_without_mode(pub, STALE_TOPIC_CLEAR_POLLS - 1)
        pub._client.publish.side_effect = self._fail_clears(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await self._publish_without_mode(pub, 1)
        tracked = pub._retained_topics["test-gw"]
        assert tracked["pypowerwall/test-gw/mode"] == STALE_TOPIC_CLEAR_POLLS
        assert tracked["pypowerwall/test-gw/battery"] == 0

        pub._client.publish.side_effect = None
        await self._publish_without_mode(pub, 1)
        assert self._clears(pub)[-1] == "pypowerwall/test-gw/mode"

    @pytest.mark.asyncio
    async def test_removed_battery_and_string_topics_cleared(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        status = _status_with_battery_blocks()
        status.data.strings = {"A": {"Voltage": 300, "Current": 2, "Power": 600}}
        await pub.publish_gateway("test-gw", status)
        for _ in range(STALE_TOPIC_CLEAR_POLLS):
            await pub.publish_gateway("test-gw", make_status())
        cleared = set(self._clears(pub))
        assert "pypowerwall/test-gw/batteries/1/capacity" in cleared
        assert "pypowerwall/test-gw/strings/A/voltage" in cleared
        assert "pypowerwall/test-gw/strings/A" in cleared
        # Still-published topics are never cleared
        assert "pypowerwall/test-gw/battery" not in cleared

    @pytest.mark.asyncio
    async def test_gateways_tracked_separately(self, monkeypatch):
        from app.mqtt.publisher import STALE_TOPIC_CLEAR_POLLS

        pub = self._make_publisher(monkeypatch)
        await pub.publish_gateway("test-gw", make_status())
        for _ in range(STALE_TOPIC_CLEAR_POLLS + 1):
            other = make_status(gateway_id="other")
            other.data.mode = None
            await pub.publish_gateway("other", other)
            await pub.publish_gateway("test-gw", make_status())
        assert self._clears(pub) == []
