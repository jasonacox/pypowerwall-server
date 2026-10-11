"""Tests for reserve_hold: set backup reserve to the cached SoC.

HTTP: POST /control/reserve_hold (no "value" field).
MQTT: pypowerwall/{gw}/control/reserve_hold/set with {} (Bit 1, like reserve).
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from aiomqtt import Message
from aiomqtt.client import MessagesIterator

from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData
from app.mqtt.ha_discovery import build_discovery_payloads
from app.mqtt.publisher import CONTROL_COALESCE_WINDOW_S, MqttPublisher

_CONTROL_TOKEN = "test-secret-token"


@pytest.fixture
def hold_client(monkeypatch):
    """Test client with control features enabled."""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "control_secret", _CONTROL_TOKEN)
    return TestClient(app)


def _auth():
    return {"Authorization": _CONTROL_TOKEN}


def test_hold_sets_reserve_to_soc_via_cloud(hold_client, connected_gateway):
    """SoC 85.5 raw -> ~84.7 display -> rounded 85 via cloud set_reserve."""
    mock_cloud = Mock()
    mock_cloud.set_reserve.return_value = {"result": "Updated"}
    gateway_manager._cloud_control = mock_cloud

    response = hold_client.post("/control/reserve_hold", json={}, headers=_auth())

    assert response.status_code == 200
    mock_cloud.set_reserve.assert_called_once_with(85)
    assert response.json() == {"result": "Updated"}


def test_hold_local_when_no_cloud(hold_client, connected_gateway, monkeypatch):
    """Without cloud control the write goes to the local connection."""
    gateway_manager._cloud_control = None
    mock_local = AsyncMock(return_value={"result": "Updated"})
    monkeypatch.setattr(gateway_manager, "local_control", mock_local)
    monkeypatch.setattr(gateway_manager, "cloud_control", AsyncMock(return_value=None))

    response = hold_client.post("/control/reserve_hold", json={}, headers=_auth())

    assert response.status_code == 200
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "set_reserve"
    assert mock_local.call_args[0][2] == 85


def test_hold_no_soc_returns_503(hold_client, connected_gateway):
    """Missing SoC fails closed with 503, never 0."""
    connected_gateway.data.soe = None
    gateway_manager._cloud_control = Mock()

    response = hold_client.post("/control/reserve_hold", json={}, headers=_auth())

    assert response.status_code == 503


def test_hold_nonfinite_soc_returns_503(hold_client, connected_gateway):
    """NaN/inf SoC fails closed with 503 instead of raising 500."""
    gateway_manager._cloud_control = Mock()
    for bad in (float("nan"), float("inf"), float("-inf")):
        connected_gateway.data.soe = bad
        response = hold_client.post("/control/reserve_hold", json={}, headers=_auth())
        assert response.status_code == 503


def test_hold_value_field_rejected(hold_client, connected_gateway):
    """Any non-empty object is rejected (empty-object-only contract)."""
    for body in ({"value": 20}, {"unexpected": True}):
        response = hold_client.post(
            "/control/reserve_hold", json=body, headers=_auth()
        )
        assert response.status_code == 400


def test_hold_requires_auth(hold_client, connected_gateway):
    """Missing token is rejected."""
    response = hold_client.post("/control/reserve_hold", json={})

    assert response.status_code in (401, 403)


def test_hold_discovery_button():
    """HA discovery announces the Hold Battery button under Bit 1."""
    results = {
        t: json.loads(p)
        for t, p in build_discovery_payloads(
            "home",
            "Home",
            "pypowerwall",
            "homeassistant",
            controls=1,
            writable=True,
        )
    }
    payload = results["homeassistant/button/pypowerwall_home_reserve_hold/config"]
    assert payload["command_topic"] == "pypowerwall/home/control/reserve_hold/set"
    assert payload["payload_press"] == "{}"


def test_hold_shares_reserve_bit(monkeypatch):
    """reserve_hold is Bit 1, like reserve (same bit, plain mapping)."""
    from app.config import MQTT_CONTROL_BITS

    assert MQTT_CONTROL_BITS["reserve_hold"] == MQTT_CONTROL_BITS["reserve"] == 1
    monkeypatch.setattr(settings, "mqtt_controls", 1)
    assert settings.mqtt_control_allowed("reserve_hold")
    assert settings.mqtt_control_allowed("reserve")
    assert settings.mqtt_control_names() == ["reserve"]
    assert not settings.mqtt_control_allowed("mode")


class FakeClient:
    """aiomqtt.Client stand-in: aiomqtt's real MessagesIterator over a queue."""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._disconnected = self._loop.create_future()
        self.messages = MessagesIterator(self)
        self.published = []

    async def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, *args, **kwargs):
        pass

    def deliver(self, topic, payload, retain=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        self._queue.put_nowait(Message(topic, payload, 1, retain, 1, None))


def _hold_gateway(monkeypatch, soe=67.3):
    """Register gateway 'cloud' (writable) with cached SoC; returns mocks."""
    for name, value in {
        "mqtt_host": "localhost",
        "mqtt_username": "u",
        "mqtt_password": "p",
        "control_secret": "secret",
        "mqtt_controls": 1,
        "mqtt_topic_prefix": "pypowerwall",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(gateway_manager, "gateways", {})
    monkeypatch.setattr(gateway_manager, "cache", {})
    gw = Gateway(id="cloud", name="cloud", online=True, cloud_mode=True)
    gateway_manager.gateways["cloud"] = gw
    gateway_manager.cache["cloud"] = GatewayStatus(
        gateway=gw,
        data=PowerwallData(soe=soe),
        online=True,
        last_updated=time.time(),
    )
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    mock_local = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(gateway_manager, "local_control", mock_local)
    monkeypatch.setattr(gateway_manager, "cloud_control", AsyncMock(return_value=None))
    return mock_local


async def _run(*messages):
    pub = MqttPublisher()
    client = FakeClient()
    for topic, payload, *rest in messages:
        client.deliver(topic, payload, *(rest or [False]))
    task = asyncio.create_task(pub._control_message_loop(client))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if client._queue.empty():
            break
    await asyncio.sleep(CONTROL_COALESCE_WINDOW_S + 0.1)
    pub._shutdown = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return client


def _cmd(payload, retain=False):
    return ("pypowerwall/cloud/control/reserve_hold/set", payload, retain)


@pytest.mark.asyncio
async def test_mqtt_reserve_hold_applies_soc(monkeypatch):
    """MQTT reserve_hold reads the cached SoC and calls set_reserve(67)."""
    mock_local = _hold_gateway(monkeypatch, soe=67.3)
    await _run(_cmd({}))
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "set_reserve"
    assert mock_local.call_args[0][2] == 67


@pytest.mark.asyncio
async def test_mqtt_reserve_hold_invalid_rejected(monkeypatch):
    """Non-empty payloads are ignored (no write)."""
    mock_local = _hold_gateway(monkeypatch, soe=67.3)
    await _run(_cmd({"value": 50}), _cmd({"unexpected": True}))
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_mqtt_reserve_hold_no_soc_ignored(monkeypatch):
    """Missing/non-finite SoC is ignored (no write)."""
    mock_local = _hold_gateway(monkeypatch, soe=None)
    await _run(_cmd({}))
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_mqtt_reserve_hold_needs_bit(monkeypatch):
    """Without Bit 1 the command is rejected (bit not set)."""
    mock_local = _hold_gateway(monkeypatch, soe=67.3)
    monkeypatch.setattr(settings, "mqtt_controls", 2)  # mode only
    await _run(_cmd({}))
    mock_local.assert_not_called()
