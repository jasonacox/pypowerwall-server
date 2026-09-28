"""
Tests for MQTT HA controls autodiscovery (broker-trust, no token in payload).

Covers:
- build_discovery_payloads(controls=31) adds control entities per bit
- Controls are absent when MQTT_CONTROLS=0 or PW_CONTROL_SECRET missing
- Controls require broker auth (MQTT_USERNAME + MQTT_PASSWORD)
- Control topics are publishable via _control_message_loop with validation
- Reserve rejects booleans (bool is an int subclass)
- Islanding is rejected without confirmed v1r transport, accepted for PW2 + PW3 v1r
- Discovery re-fires when v1r capability arrives late (cold start)
- Cloud-mode gateways are driven on their own connection first
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mqtt.ha_discovery import build_discovery_payloads
from app.mqtt.publisher import MqttPublisher
from app.models.gateway import Gateway, GatewayStatus, PowerwallData


def test_controls_disabled_no_extra_entities():
    results = build_discovery_payloads(
        gateway_id="home",
        gateway_name="Home",
        topic_prefix="pypowerwall",
        ha_prefix="homeassistant",
        controls=0,
    )
    assert len(results) == 23


def test_controls_mask_adds_entities():
    # Without v1r transport: 4 controls (no islanding); grid setters need
    # a capable connection (cloud/FleetAPI, bound hybrid cloud, or v1r)
    results = build_discovery_payloads(
        gateway_id="home",
        gateway_name="Home",
        topic_prefix="pypowerwall",
        ha_prefix="homeassistant",
        controls=31,
        grid_capable=True,
    )
    assert len(results) == 27
    topics = {t for t, _ in results}
    assert "homeassistant/number/pypowerwall_home_reserve_control/config" in topics
    assert "homeassistant/select/pypowerwall_home_mode_control/config" in topics
    assert "homeassistant/switch/pypowerwall_home_grid_charging_control/config" in topics
    assert "homeassistant/select/pypowerwall_home_grid_export_control/config" in topics
    assert "homeassistant/button/pypowerwall_home_go_off_grid/config" not in topics

    # With v1r transport (PW2 or PW3): +2 islanding buttons
    results_v1r = build_discovery_payloads(
        gateway_id="home",
        gateway_name="Home",
        topic_prefix="pypowerwall",
        ha_prefix="homeassistant",
        controls=31,
        is_v1r=True,
        grid_capable=True,
    )
    assert len(results_v1r) == 29
    topics_v1r = {t for t, _ in results_v1r}
    assert "homeassistant/button/pypowerwall_home_go_off_grid/config" in topics_v1r
    assert "homeassistant/button/pypowerwall_home_reconnect_grid/config" in topics_v1r


def test_controls_bits_gate_entities():
    # Only bits in the mask are announced (islanding needs its own bit)
    base = dict(
        gateway_id="home",
        gateway_name="Home",
        topic_prefix="pypowerwall",
        ha_prefix="homeassistant",
        is_v1r=True,
        grid_capable=True,
    )
    topics = {t for t, _ in build_discovery_payloads(**base, controls=1)}
    assert "homeassistant/number/pypowerwall_home_reserve_control/config" in topics
    assert "homeassistant/select/pypowerwall_home_mode_control/config" not in topics

    # Mask without the islanding bit: no buttons despite v1r transport
    topics = {t for t, _ in build_discovery_payloads(**base, controls=15)}
    assert "homeassistant/button/pypowerwall_home_go_off_grid/config" not in topics

    # Grid controls need a capable connection even with the bit set
    no_grid = dict(base, grid_capable=False)
    topics = {t for t, _ in build_discovery_payloads(**no_grid, controls=12)}
    assert "homeassistant/switch/pypowerwall_home_grid_charging_control/config" not in topics
    assert "homeassistant/select/pypowerwall_home_grid_export_control/config" not in topics


def test_reserve_control_payload():
    results = {t: json.loads(p) for t, p in build_discovery_payloads("home", "Home", "pypowerwall", "homeassistant", controls=31, grid_capable=True)}
    payload = results["homeassistant/number/pypowerwall_home_reserve_control/config"]
    assert payload["command_topic"] == "pypowerwall/home/control/reserve/set"
    assert payload["state_topic"] == "pypowerwall/home/reserve"
    assert payload["min"] == 0
    assert payload["max"] == 100
    assert payload["step"] == 1


@pytest.mark.asyncio
async def test_control_message_reserve_valid(monkeypatch):
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")

    gw = Gateway(id="home", name="Home", host="1.1.1.1", online=True)
    gateway_manager.gateways["home"] = gw
    gateway_manager._cloud_control = None

    mock_local = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(gateway_manager, "local_control", mock_local)
    monkeypatch.setattr(gateway_manager, "cloud_control", AsyncMock(return_value=None))

    pub = MqttPublisher()

    class FakeMsg:
        def __init__(self, topic, payload, retain=False):
            self.topic = MagicMock()
            self.topic.value = topic
            self.payload = payload.encode()
            self.retain = retain

    class FakeClient:
        def __init__(self):
            self._queue = asyncio.Queue()

        async def subscribe(self, topic, qos=1):
            pass

        @property
        def messages(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._queue.get()

        async def put(self, msg):
            await self._queue.put(msg)

    client = FakeClient()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)
    await client.put(FakeMsg("pypowerwall/home/control/reserve/set", json.dumps({"value": 50})))
    await asyncio.sleep(0.2)
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "set_reserve"

    # Invalid reserve should not call
    mock_local.reset_mock()
    await client.put(FakeMsg("pypowerwall/home/control/reserve/set", json.dumps({"value": 150})))
    await asyncio.sleep(0.2)
    mock_local.assert_not_called()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    del gateway_manager.gateways["home"]


@pytest.mark.asyncio
async def test_control_message_invalid_json_ignored(monkeypatch):
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")

    pub = MqttPublisher()

    class FakeMsg:
        def __init__(self, topic, payload):
            self.topic = MagicMock()
            self.topic.value = topic
            self.payload = payload.encode()
            self.retain = False

    class FakeClient:
        def __init__(self):
            self._queue = asyncio.Queue()

        async def subscribe(self, topic, qos=1):
            pass

        @property
        def messages(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._queue.get()

        async def put(self, msg):
            await self._queue.put(msg)

    client = FakeClient()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)
    # malformed JSON should be ignored, not crash
    await client.put(FakeMsg("pypowerwall/home/control/mode/set", "not json"))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_mqtt_controls_require_broker_auth(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")
    # No broker user/password -> controls unavailable (open broker)
    monkeypatch.setattr(settings, "mqtt_username", None)
    monkeypatch.setattr(settings, "mqtt_password", None)
    assert settings.mqtt_controls_available is False
    # Username alone is not enough (ACLs are enforced per user+password)
    monkeypatch.setattr(settings, "mqtt_username", "user")
    assert settings.mqtt_controls_available is False
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    assert settings.mqtt_controls_available is True
    # Mask 0 (monitoring only) disables even with full credentials
    monkeypatch.setattr(settings, "mqtt_controls", 0)
    assert settings.mqtt_controls_available is False
    # Missing secret disables even with bits + broker auth
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", None)
    assert settings.mqtt_controls_available is False


def test_mqtt_controls_mask_names(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_controls", 15)
    assert settings.mqtt_control_names() == [
        "reserve", "mode", "grid_charging", "grid_export",
    ]
    assert settings.mqtt_control_allowed("islanding") is False
    assert settings.mqtt_control_allowed("reserve") is True
    monkeypatch.setattr(settings, "mqtt_controls", 63)
    assert settings.mqtt_controls_mask == 31
    assert settings.mqtt_control_allowed("nope") is False


def test_mqtt_controls_env_sanitize(monkeypatch, caplog):
    """Validator strips unknown bits / negatives with a warning (fail-closed)."""
    import logging

    from app.config import Settings

    monkeypatch.setenv("MQTT_CONTROLS", "63")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = Settings()
    assert settings.mqtt_controls == 31
    assert settings.mqtt_control_names() == [
        "reserve", "mode", "grid_charging", "grid_export", "islanding",
    ]
    assert any("unknown" in r.getMessage().lower() for r in caplog.records)

    monkeypatch.setenv("MQTT_CONTROLS", "-5")
    settings = Settings()
    assert settings.mqtt_controls == 0
    assert settings.mqtt_controls_available is False


class _Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic = MagicMock()
        self.topic.value = topic
        self.payload = payload.encode()
        self.retain = retain


class _Client:
    def __init__(self):
        self._queue = asyncio.Queue()
        self.publish = AsyncMock()

    async def subscribe(self, topic, qos=1):
        pass

    @property
    def messages(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()

    async def put(self, msg):
        await self._queue.put(msg)


_USE_OK_RESULT = object()


async def _run_control_messages(monkeypatch, gateway_id, gw_kwargs, messages,
                                cloud_result=None, hybrid=None, mask=31,
                                bound_id=None, local_result=_USE_OK_RESULT,
                                local_effect=None):
    """Feed control messages through the loop; return (mock_local, mock_cloud)."""
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    monkeypatch.setattr(settings, "mqtt_controls", mask)
    gw = Gateway(id=gateway_id, name=gateway_id, online=True, **gw_kwargs)
    gateway_manager.gateways[gateway_id] = gw
    if local_effect is not None:
        mock_local = AsyncMock(side_effect=local_effect)
    else:
        mock_local = AsyncMock(
            return_value={"ok": True}
            if local_result is _USE_OK_RESULT
            else local_result
        )
    mock_cloud = AsyncMock(return_value=cloud_result)
    monkeypatch.setattr(gateway_manager, "local_control", mock_local)
    monkeypatch.setattr(gateway_manager, "cloud_control", mock_cloud)
    if hybrid is None:
        hybrid = cloud_result is not None
    monkeypatch.setattr(gateway_manager, "_cloud_control",
                        object() if hybrid else None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_gateway_id", bound_id)

    pub = MqttPublisher()
    client = _Client()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)
    for topic, payload in messages:
        await client.put(_Msg(topic, payload))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        del gateway_manager.gateways[gateway_id]
    return mock_local, mock_cloud, client


@pytest.mark.asyncio
async def test_control_message_reserve_rejects_bool(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")

    # True/False must not pass as 1/0 (bool subclasses int)
    for bad in (True, False):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "home", {"host": "1.1.1.1"},
            [("pypowerwall/home/control/reserve/set", json.dumps({"value": bad}))],
        )
        mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_control_message_islanding_needs_v1r(monkeypatch):
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    payload = json.dumps({"action": "off_grid", "confirm": True})

    # Plain TEDAPI gateway (no RSA key): islanding rejected
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "plain", {"host": "1.1.1.1"},
        [("pypowerwall/plain/control/islanding/set", payload)],
    )
    mock_local.assert_not_called()

    # RSA gateway but mode unknown (cold start, no status yet): fail closed
    # like the Console gate (tedapi_mode === 'v1r') — rejected until the
    # transport confirms v1r
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "cold", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/cold/control/islanding/set", payload)],
    )
    mock_local.assert_not_called()

    # RSA + confirmed PW2 hardware: still routed (library signs for both)
    status = GatewayStatus(
        gateway=Gateway(id="pw2", name="PW2", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=PowerwallData(pw3=False, tedapi_mode="v1r"),
        online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "pw2", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/pw2/control/islanding/set", payload)],
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "go_off_grid"

    # RSA + confirmed PW3 hardware: routed to go_off_grid
    status = GatewayStatus(
        gateway=Gateway(id="v1r", name="V1R", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=PowerwallData(pw3=True, tedapi_mode="v1r"), online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "v1r", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/v1r/control/islanding/set", payload)],
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "go_off_grid"

    # Data definitively reports non-v1r transport: rejected despite RSA flag
    status = GatewayStatus(
        gateway=Gateway(id="full", name="Full", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=PowerwallData(pw3=True, tedapi_mode="full"),
        online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "full", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/full/control/islanding/set", payload)],
    )
    mock_local.assert_not_called()


def test_discovery_signature_tracks_v1r():
    from app.mqtt.ha_discovery import discovery_signature

    off = discovery_signature(None, None, controls=31, is_v1r=False)
    on = discovery_signature(None, None, controls=31, is_v1r=True)
    assert ("controls", 31, False, False) in off
    assert ("controls", 31, True, False) in on
    assert not on <= off  # capability flip re-fires discovery
    # Mask flip re-fires too (bit turned off removes entities)
    fewer = discovery_signature(None, None, controls=15, is_v1r=True)
    assert not fewer <= on
    # Unknown bits are sanitized away (same state as the masked value)
    assert discovery_signature(None, None, controls=63) == discovery_signature(
        None, None, controls=31
    )
    # Controls disabled: no tracking key (unchanged legacy behavior)
    assert discovery_signature(None, None) == frozenset()


@pytest.mark.asyncio
async def test_discovery_resent_when_v1r_arrives_late(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")
    monkeypatch.setattr(settings, "mqtt_ha_discovery", True)
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    monkeypatch.setattr(settings, "mqtt_ha_prefix", "homeassistant")
    monkeypatch.setattr(settings, "mqtt_qos", 1)
    monkeypatch.setattr(settings, "mqtt_retain", True)

    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True
    topics = []

    async def fake_safe(self, topic, payload, retain, qos):
        topics.append(topic)

    monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)

    def make_status(rsa, pw3, mode=None):
        gw = Gateway(id="gw", name="GW", host="1.1.1.1",
                     rsa_key_configured=rsa, online=True)
        return GatewayStatus(
            gateway=gw, data=PowerwallData(pw3=pw3, tedapi_mode=mode),
            online=True, last_updated=1.0)

    await pub.publish_gateway("gw", make_status(False, None))
    first = len([t for t in topics if "homeassistant" in t])
    assert first > 0

    # Same capability: no re-send
    topics.clear()
    await pub.publish_gateway("gw", make_status(False, None))
    assert [t for t in topics if "homeassistant" in t] == []

    # v1r transport confirmed (PW2 hardware!): discovery re-fires with buttons
    topics.clear()
    await pub.publish_gateway("gw", make_status(True, False, "v1r"))
    resent = [t for t in topics if "homeassistant" in t]
    assert len(resent) > 0
    assert any("go_off_grid" in t for t in resent)


@pytest.mark.asyncio
async def test_controls_half_config_warns_once(monkeypatch, caplog):
    import logging
    import sys
    import types

    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")
    monkeypatch.setattr(settings, "mqtt_username", None)
    monkeypatch.setattr(settings, "mqtt_password", None)

    pub = MqttPublisher()

    async def fake_safe(self, topic, payload, retain, qos):
        return None

    monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)

    class FakeCM:
        def __init__(self, holder):
            self._holder = holder

        async def __aenter__(self):
            self._holder[0]._shutdown = True  # one pass, then stop
            return AsyncMock()

        async def __aexit__(self, *args):
            return False

    holder = [pub]
    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = lambda **kwargs: FakeCM(holder)
    fake_aiomqtt.Will = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await asyncio.wait_for(pub._connection_loop(), timeout=5)
        warnings = [r for r in caplog.records
                    if "MQTT_USERNAME" in r.getMessage()]
        assert len(warnings) == 1

        # Second connect: no duplicate warning
        caplog.clear()
        pub._shutdown = False
        await asyncio.wait_for(pub._connection_loop(), timeout=5)
        assert [r for r in caplog.records
                if "MQTT_USERNAME" in r.getMessage()] == []


@pytest.mark.asyncio
async def test_cloud_gateway_uses_own_connection(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")

    # Hybrid cloud present, but a cloud_mode gateway must be driven on its
    # own connection (shared cloud_control cannot target a gateway)
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "remote",
        {"cloud_mode": True, "email": "user@example.com"},
        [("pypowerwall/remote/control/reserve/set", json.dumps({"value": 20}))],
        cloud_result={"ok": True},
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][0] == "remote"
    assert mock_local.call_args[0][1] == "set_reserve"
    mock_cloud.assert_not_called()

    # TEDAPI gateway with hybrid cloud bound to ANOTHER gateway: the shared
    # cloud must not be used (wrong site) — own local connection instead
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [("pypowerwall/home/control/mode/set", json.dumps({"value": "backup"}))],
        cloud_result={"ok": True}, hybrid=True, bound_id="other",
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][0] == "home"
    mock_cloud.assert_not_called()

    # TEDAPI gateway with hybrid cloud bound to ITSELF: shared cloud path
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [("pypowerwall/home/control/mode/set", json.dumps({"value": "backup"}))],
        cloud_result={"ok": True}, hybrid=True, bound_id="home",
    )
    mock_cloud.assert_called_once()
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_no_retry_on_other_path(monkeypatch):
    """A None result (incl. timeout) never retries on the other path."""
    # Bound hybrid: cloud returns None -> local must NOT be tried
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [("pypowerwall/home/control/reserve/set", json.dumps({"value": 20}))],
        cloud_result=None, hybrid=True, bound_id="home",
    )
    mock_cloud.assert_called_once()
    mock_local.assert_not_called()

    # Unbound local: local returns None -> cloud must NOT be tried
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [("pypowerwall/home/control/reserve/set", json.dumps({"value": 20}))],
        cloud_result={"ok": True}, hybrid=True, bound_id="other",
        local_result=None,
    )
    mock_local.assert_called_once()
    mock_cloud.assert_not_called()


@pytest.mark.asyncio
async def test_mode_invalid_rejected_with_warning(monkeypatch, caplog):
    """Invalid mode values reject with WARNING (latest-wins safe: solo batch)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "home", {"host": "1.1.1.1"},
            [("pypowerwall/home/control/mode/set",
              json.dumps({"value": "turbo"}))],
        )
    mock_local.assert_not_called()
    assert any("invalid value" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_bit_gate_rejects_disabled_control(monkeypatch, caplog):
    """A control whose MQTT_CONTROLS bit is off is rejected with WARNING."""
    import logging

    # Only reserve enabled: mode + islanding must be rejected
    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "home", {"host": "1.1.1.1"},
            [("pypowerwall/home/control/mode/set",
              json.dumps({"value": "backup"}))],
            mask=1,
        )
    mock_local.assert_not_called()
    assert any("bit not set" in r.getMessage() for r in caplog.records)

    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "v1r", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/v1r/control/islanding/set",
          json.dumps({"action": "off_grid", "confirm": True}))],
        mask=15,  # everything but islanding
    )
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_retained_command_warned_and_cleared(monkeypatch, caplog):
    """Retained replays never execute: WARNING + retained slot cleared."""
    import logging

    mock_local, _, client = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [],
    )
    from app.mqtt.publisher import MqttPublisher

    pub = MqttPublisher()
    client2 = _Client()
    task = asyncio.create_task(pub._control_message_loop(client2))
    await asyncio.sleep(0.05)
    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await client2.put(_Msg("pypowerwall/home/control/reserve/set",
                               json.dumps({"value": 50}), retain=True))
        await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        from app.core.gateway_manager import gateway_manager
        if "home" in gateway_manager.gateways:
            del gateway_manager.gateways["home"]
    mock_local.assert_not_called()
    assert any("retained" in r.getMessage().lower() for r in caplog.records)
    # Retained slot cleared (empty retained publish on the same topic)
    cleared = [
        c for c in client2.publish.call_args_list
        if c.args[0] == "pypowerwall/home/control/reserve/set"
        and c.args[1] == ""
    ]
    assert cleared, "retained command was not cleared from the broker"


@pytest.mark.asyncio
async def test_value_allowlists_reject(monkeypatch):
    """Invalid mode/grid_export values and missing confirm never dispatch."""
    bad = [
        ("pypowerwall/home/control/mode/set", json.dumps({"value": "turbo"})),
        ("pypowerwall/home/control/grid_export/set", json.dumps({"value": True})),
        ("pypowerwall/home/control/grid_export/set", json.dumps({"value": "all"})),
        ("pypowerwall/home/control/reserve/set", json.dumps({"value": 101})),
        ("pypowerwall/home/control/reserve/set", json.dumps({})),
        ("pypowerwall/home/control/grid_charging/set", json.dumps({"value": 1})),
        ("pypowerwall/home/control/mode/set", "not json"),
        ("pypowerwall/home/control/mode/set", json.dumps([1, 2])),
    ]
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"}, bad,
    )
    mock_local.assert_not_called()

    # Islanding without literal confirm:true is rejected
    from app.core.gateway_manager import gateway_manager
    from app.models.gateway import GatewayStatus as _GS
    from app.models.gateway import PowerwallData as _PD

    status = _GS(
        gateway=Gateway(id="v", name="V", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=_PD(tedapi_mode="v1r"), online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "v", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/v/control/islanding/set",
          json.dumps({"action": "off_grid", "confirm": False}))],
    )
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_gateway_and_control_warn(monkeypatch, caplog):
    """Unknown gateways/controls log WARNING and dispatch nothing."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "home", {"host": "1.1.1.1"},
            [
                ("pypowerwall/nope/control/reserve/set", json.dumps({"value": 20})),
                ("pypowerwall/home/control/selfdestruct/set", json.dumps({})),
                ("pypowerwall/home/control/reserve", json.dumps({"value": 20})),
            ],
        )
    mock_local.assert_not_called()
    messages = [r.getMessage() for r in caplog.records]
    assert any("unknown gateway" in m for m in messages)
    assert any("unknown control" in m for m in messages)


@pytest.mark.asyncio
async def test_error_dict_treated_as_failure(monkeypatch, caplog):
    """{'error': ...} stub responses are failures, never 'applied'."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.mqtt.publisher"):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "home", {"host": "1.1.1.1"},
            [("pypowerwall/home/control/reserve/set", json.dumps({"value": 20}))],
            local_result={"error": "stub"},
        )
    mock_local.assert_called_once()
    messages = [r.getMessage() for r in caplog.records]
    assert not any("applied" in m for m in messages)
    assert any("failed" in m for m in messages)


@pytest.mark.asyncio
async def test_islanding_ack_checked(monkeypatch, caplog):
    """Only {"result": 1} counts; anything else is a failure, same as HTTP."""
    import logging
    from app.core.gateway_manager import gateway_manager
    from app.models.gateway import GatewayStatus as _GS
    from app.models.gateway import PowerwallData as _PD

    status = _GS(
        gateway=Gateway(id="v", name="V", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=_PD(tedapi_mode="v1r"), online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)

    async def run_islanding(result):
        with caplog.at_level(logging.INFO, logger="app.mqtt.publisher"):
            caplog.clear()
            mock_local, _, _ = await _run_control_messages(
                monkeypatch, "v",
                {"host": "1.1.1.1", "rsa_key_configured": True},
                [("pypowerwall/v/control/islanding/set",
                  json.dumps({"action": "off_grid", "confirm": True}))],
                local_result=result,
            )
            return mock_local, [r.getMessage() for r in caplog.records]

    mock_local, messages = await run_islanding({"result": 1})
    mock_local.assert_called_once()
    # Same 10 s timeout as HTTP POST /control/islanding
    assert mock_local.call_args[1].get("timeout") == 10.0
    assert any("applied" in m for m in messages)

    for bad in ({"result": 0}, {}, {"result": True}, None, {"error": "x"}):
        mock_local, messages = await run_islanding(bad)
        assert not any("applied" in m for m in messages), bad
        assert any(
            "not acknowledged" in m or "failed" in m for m in messages
        ), bad


@pytest.mark.asyncio
async def test_oversized_payload_rejected(monkeypatch):
    """Payloads over the 1 KB cap never reach validation or dispatch."""
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [("pypowerwall/home/control/reserve/set",
          json.dumps({"value": 20, "pad": "x" * 2048}))],
    )
    mock_local.assert_not_called()


@pytest.mark.asyncio
async def test_burst_coalesces_to_latest(monkeypatch):
    """50 rapid reserve commands become one write with the last value."""
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"},
        [
            (
                "pypowerwall/home/control/reserve/set",
                json.dumps({"value": i % 101}),
            )
            for i in range(50)
        ],
    )
    assert mock_local.call_count == 1
    assert mock_local.call_args[0][2] == 49


@pytest.mark.asyncio
async def test_grid_controls_need_capability(monkeypatch):
    """Grid setters on an incapable gateway (plain local) are rejected."""
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "plain", {"host": "1.1.1.1"},
        [("pypowerwall/plain/control/grid_charging/set",
          json.dumps({"value": True}))],
    )
    mock_local.assert_not_called()

    # Same gateway with a bound hybrid cloud: executable
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "plain", {"host": "1.1.1.1"},
        [("pypowerwall/plain/control/grid_charging/set",
          json.dumps({"value": True}))],
        cloud_result={"ok": True}, hybrid=True, bound_id="plain",
    )
    mock_local.assert_not_called()  # routed via bound cloud instead
    # (cloud mock asserted separately in routing tests)


@pytest.mark.asyncio
async def test_discovery_removes_disabled_controls(monkeypatch):
    """Turning a bit off removes the entity via empty retained config."""
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_ha_discovery", True)
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    monkeypatch.setattr(settings, "mqtt_ha_prefix", "homeassistant")
    monkeypatch.setattr(settings, "mqtt_qos", 1)
    monkeypatch.setattr(settings, "mqtt_retain", True)

    from app.core.gateway_manager import gateway_manager

    gw = Gateway(id="gw", name="GW", host="1.1.1.1", online=True)
    gateway_manager.gateways["gw"] = gw
    try:
        pub = MqttPublisher()
        pub._client = AsyncMock()
        pub._connected = True
        sent = []

        async def fake_safe(self, topic, payload, retain, qos):
            sent.append((topic, payload, retain))

        monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)
        status = GatewayStatus(
            gateway=gw, data=PowerwallData(), online=True, last_updated=1.0,
        )
        await pub._publish_ha_discovery("gw", status)
        announced = {t for t, p, r in sent if "reserve_control" in t}
        assert announced, "reserve control was not announced"

        # Disable everything: stale control configs must be cleared
        monkeypatch.setattr(settings, "mqtt_controls", 0)
        sent.clear()
        await pub._publish_ha_discovery("gw", status)
        cleared = {
            t for t, p, r in sent
            if p == "" and r is True and "reserve_control" in t
        }
        assert cleared, "disabled control entity was not removed"
    finally:
        del gateway_manager.gateways["gw"]


@pytest.mark.asyncio
async def test_mask_to_zero_refires_through_publish_gateway(monkeypatch):
    """Mask 31 -> 0 through publish_gateway removes entities (no silent keep).

    The discovery signature union only grows, so without explicit
    controls-state tracking a shrink to 0 would never re-fire discovery
    and stale entities would stay in HA forever.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_ha_discovery", True)
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "pypowerwall")
    monkeypatch.setattr(settings, "mqtt_ha_prefix", "homeassistant")
    monkeypatch.setattr(settings, "mqtt_qos", 1)
    monkeypatch.setattr(settings, "mqtt_retain", True)

    from app.core.gateway_manager import gateway_manager

    gw = Gateway(id="gw", name="GW", host="1.1.1.1",
                 rsa_key_configured=True, online=True)
    gateway_manager.gateways["gw"] = gw
    try:
        pub = MqttPublisher()
        pub._client = AsyncMock()
        pub._connected = True
        sent = []

        async def fake_safe(self, topic, payload, retain, qos):
            sent.append((topic, payload, retain))

        monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)
        status = GatewayStatus(
            gateway=gw, data=PowerwallData(tedapi_mode="v1r"),
            online=True, last_updated=1.0,
        )
        await pub.publish_gateway("gw", status)
        announced = {t for t, p, r in sent if "reserve_control" in t}
        assert announced, "reserve control was not announced"

        # Shrink the mask to 0 through the real publish path: the stale
        # control entity must be cleared, not silently kept
        monkeypatch.setattr(settings, "mqtt_controls", 0)
        sent.clear()
        await pub.publish_gateway("gw", status)
        cleared = {
            t for t, p, r in sent
            if p == "" and r is True and "reserve_control" in t
        }
        assert cleared, "mask shrink to 0 did not remove the entity"
    finally:
        del gateway_manager.gateways["gw"]


@pytest.mark.asyncio
async def test_grid_export_valid_paths(monkeypatch):
    """Valid grid_export: incapable rejects, v1r-capable routes."""
    from app.core.gateway_manager import gateway_manager
    from app.models.gateway import GatewayStatus as _GS
    from app.models.gateway import PowerwallData as _PD

    # Incapable plain local gateway: rejected despite valid value
    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "plain", {"host": "1.1.1.1"},
        [("pypowerwall/plain/control/grid_export/set",
          json.dumps({"value": "pv_only"}))],
    )
    mock_local.assert_not_called()

    # v1r-confirmed gateway: routed on its own connection
    status = _GS(
        gateway=Gateway(id="v", name="V", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=_PD(tedapi_mode="v1r"), online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "v", {"host": "1.1.1.1", "rsa_key_configured": True},
        [("pypowerwall/v/control/grid_export/set",
          json.dumps({"value": "never"}))],
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][1] == "set_grid_export"
    mock_cloud.assert_not_called()


@pytest.mark.asyncio
async def test_islanding_dispatch_error_no_crash(monkeypatch, caplog):
    """Cooldown/in-progress exceptions become WARNING, never a loop death."""
    import logging
    from app.core.gateway_manager import gateway_manager
    from app.models.gateway import GatewayStatus as _GS
    from app.models.gateway import PowerwallData as _PD

    status = _GS(
        gateway=Gateway(id="v", name="V", host="1.1.1.1",
                        rsa_key_configured=True, online=True),
        data=_PD(tedapi_mode="v1r"), online=True, last_updated=1.0,
    )
    monkeypatch.setattr(gateway_manager, "get_gateway", lambda gid: status)
    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        mock_local, _, _ = await _run_control_messages(
            monkeypatch, "v",
            {"host": "1.1.1.1", "rsa_key_configured": True},
            [("pypowerwall/v/control/islanding/set",
              json.dumps({"action": "off_grid", "confirm": True}))],
            local_effect=Exception("cooldown 30s"),
        )
    mock_local.assert_called_once()
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_capability_and_route_helpers():
    """Direct unit coverage for capability/routing/error helpers."""
    from app.mqtt.publisher import (
        _control_config_topics,
        _gateway_is_v1r,
        _grid_controls_capable,
    )

    assert _grid_controls_capable(object(), None) is False
    # Malformed discovery payloads are skipped, not crashed on
    assert _control_config_topics([("t", "not json")]) == set()
    # Unknown manager surfaces fail closed
    assert _gateway_is_v1r(None, "x") is False


@pytest.mark.asyncio
async def test_control_loop_death_forces_reconnect(monkeypatch, caplog):
    """An ended message stream warns and marks the connection for reconnect."""
    import logging

    from app.mqtt.publisher import MqttPublisher

    pub = MqttPublisher()
    pub._connected = True

    class DeadClient:
        @property
        def messages(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await pub._control_message_loop(DeadClient())
    assert pub._connected is False
    assert any("forcing reconnect" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_route_unknown_gateway():
    """Routing an unknown gateway returns (None, 'none') without dispatch."""
    from app.mqtt.publisher import _route_gateway_control

    result, path = await _route_gateway_control(
        "no-such-gw-xyz", "set_reserve", 20
    )
    assert (result, path) == (None, "none")


@pytest.mark.asyncio
async def test_retained_clear_variants(monkeypatch, caplog):
    """Retained clearing tolerates exotic clients (no publish / raising)."""
    import logging

    from app.mqtt.publisher import MqttPublisher

    pub = MqttPublisher()

    class NoPublish:
        pass

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await pub._warn_retained_command(NoPublish(), "t/x")
    assert any("retained" in r.getMessage().lower() for r in caplog.records)

    class RaisingPublish:
        async def publish(self, *args, **kwargs):
            raise RuntimeError("broker gone")

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await pub._warn_retained_command(RaisingPublish(), "t/x")
    # Best-effort: warning stands, no exception escapes


@pytest.mark.asyncio
async def test_coalesce_decode_failure(monkeypatch, caplog):
    """Undecodable messages are dropped with WARNING, loop survives."""
    import logging
    from unittest.mock import MagicMock

    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"}, [],
    )
    from app.core.gateway_manager import gateway_manager
    from app.mqtt.publisher import MqttPublisher

    # Helper cleaned up: re-register for the manual loop part below
    gateway_manager.gateways["home"] = Gateway(
        id="home", name="home", host="1.1.1.1", online=True,
    )
    pub = MqttPublisher()
    client = _Client()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)

    class BadTopic:
        payload = b'{"value": 50}'
        retain = False

        @property
        def topic(self):
            raise RuntimeError("no topic")

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await client.put(BadTopic())
        await client.put(_Msg("pypowerwall/home/control/reserve/set",
                              json.dumps({"value": 60})))
        await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if "home" in gateway_manager.gateways:
            del gateway_manager.gateways["home"]
    assert any("decode failed" in r.getMessage() for r in caplog.records)
    # The valid sibling still executed (loop survived the bad message)
    assert mock_local.call_count == 1
    assert mock_local.call_args[0][2] == 60


@pytest.mark.asyncio
async def test_evil_retain_flag_treated_as_not_retained(monkeypatch):
    """A retain flag that raises is treated as not-retained (fail-open read)."""
    from unittest.mock import MagicMock

    class EvilRetain:
        def __init__(self, topic, payload):
            self.topic = MagicMock()
            self.topic.value = topic
            self.payload = payload.encode()

        @property
        def retain(self):
            raise RuntimeError("boom")

    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"}, [],
    )
    from app.core.gateway_manager import gateway_manager
    from app.mqtt.publisher import MqttPublisher

    gateway_manager.gateways["home"] = Gateway(
        id="home", name="home", host="1.1.1.1", online=True,
    )
    pub = MqttPublisher()
    client = _Client()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)
    await client.put(EvilRetain("pypowerwall/home/control/reserve/set",
                                json.dumps({"value": 42})))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if "home" in gateway_manager.gateways:
            del gateway_manager.gateways["home"]
    mock_local.assert_called_once()
    assert mock_local.call_args[0][2] == 42


@pytest.mark.asyncio
async def test_control_loop_broken_stream_reconnects(monkeypatch, caplog):
    """An unreadable message stream warns and forces reconnect."""
    import logging

    from app.mqtt.publisher import MqttPublisher

    pub = MqttPublisher()
    pub._connected = True

    class BrokenStream:
        @property
        def messages(self):
            raise RuntimeError("stream gone")

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await pub._control_message_loop(BrokenStream())
    assert pub._connected is False
    assert any("loop error" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_logs_enabled_controls(monkeypatch, caplog):
    """Subscribe logs enabled names + dedicated islanding warning."""
    import logging
    import sys
    import types

    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")

    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True

    async def fake_safe(self, topic, payload, retain, qos):
        return None

    monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)

    class FakeCM:
        def __init__(self, holder=None):
            self._holder = holder

        async def __aenter__(self):
            pub._shutdown = True  # one pass: subscribe block runs, then stop
            return _Client()

        async def __aexit__(self, *args):
            return False

    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = lambda **kwargs: FakeCM()
    fake_aiomqtt.Will = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    with caplog.at_level(logging.INFO, logger="app.mqtt.publisher"):
        await asyncio.wait_for(pub._connection_loop(), timeout=10)
    messages = [r.getMessage() for r in caplog.records]
    subscribed = [m for m in messages if "MQTT controls subscribed" in m]
    assert subscribed, messages
    assert "islanding" in subscribed[0]
    assert any("ISLANDING" in m for m in messages)


@pytest.mark.asyncio
async def test_dead_control_task_triggers_reconnect(monkeypatch, caplog):
    """Heartbeat notices a silently dead control task and reconnects."""
    import logging
    import sys
    import types

    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")

    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True
    connects = []

    async def fake_safe(self, topic, payload, retain, qos):
        return None

    monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)

    class FakeCM:
        async def __aenter__(self):
            connects.append(1)
            if len(connects) > 1:
                pub._shutdown = True  # stop after the reconnect
            return _ClientBlocking()

        async def __aexit__(self, *args):
            return False

    class _ClientBlocking(_Client):
        pass  # queue stays empty: control task pends until killed below

    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = lambda **kwargs: FakeCM()
    fake_aiomqtt.Will = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    async def killer():
        # Wait for the handler task, then kill it silently from outside
        for _ in range(100):
            await asyncio.sleep(0.05)
            targets = [
                t for t in asyncio.all_tasks()
                if t.get_name() == "mqtt-control-handler"
            ]
            if targets:
                targets[0].cancel()
                return
        raise AssertionError("control handler task never appeared")

    loop_task = asyncio.create_task(pub._connection_loop())
    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await asyncio.wait_for(killer(), timeout=10)
        await asyncio.wait_for(loop_task, timeout=30)
    messages = [r.getMessage() for r in caplog.records]
    assert any("ended unexpectedly" in m for m in messages), messages
    assert len(connects) >= 2, "no reconnect after handler death"
    pub._shutdown = True


@pytest.mark.asyncio
async def test_subscribe_failure_warns(monkeypatch, caplog):
    """A failing control-topic subscribe warns instead of crashing."""
    import logging
    import sys
    import types

    from app.config import settings

    monkeypatch.setattr(settings, "mqtt_host", "localhost")
    monkeypatch.setattr(settings, "mqtt_username", "user")
    monkeypatch.setattr(settings, "mqtt_password", "pass")
    monkeypatch.setattr(settings, "mqtt_controls", 31)
    monkeypatch.setattr(settings, "control_secret", "secret")

    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True

    async def fake_safe(self, topic, payload, retain, qos):
        return None

    monkeypatch.setattr(MqttPublisher, "_safe_publish", fake_safe)

    class RefusingClient(_Client):
        async def subscribe(self, topic, qos=1):
            raise RuntimeError("sub refused")

    class FakeCM:
        async def __aenter__(self):
            pub._shutdown = True
            return RefusingClient()

        async def __aexit__(self, *args):
            return False

    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = lambda **kwargs: FakeCM()
    fake_aiomqtt.Will = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
        await asyncio.wait_for(pub._connection_loop(), timeout=10)
    assert any("subscribe failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_unexpected_error_contained(monkeypatch, caplog):
    """An unexpected lookup failure is contained with WARNING, loop lives on."""
    import logging

    from app.core.gateway_manager import gateway_manager

    mock_local, _, _ = await _run_control_messages(
        monkeypatch, "home", {"host": "1.1.1.1"}, [],
    )
    real_gateways = gateway_manager.gateways
    real_gateways["home"] = Gateway(
        id="home", name="home", host="1.1.1.1", online=True,
    )

    class BoomDict(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("store gone")

    monkeypatch.setattr(gateway_manager, "gateways", BoomDict())
    from app.mqtt.publisher import MqttPublisher

    pub = MqttPublisher()
    client = _Client()
    task = asyncio.create_task(pub._control_message_loop(client))
    await asyncio.sleep(0.05)
    try:
        with caplog.at_level(logging.WARNING, logger="app.mqtt.publisher"):
            await client.put(_Msg("pypowerwall/home/control/reserve/set",
                                  json.dumps({"value": 50})))
            await client.put(_Msg("pypowerwall/home/control/reserve/set",
                                  json.dumps({"value": 60})))
            await asyncio.sleep(0.3)
        assert any("handler error" in r.getMessage() for r in caplog.records)
        assert task.done() is False, "loop died on unexpected error"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        mock_local.assert_not_called()
    finally:
        real_gateways.pop("home", None)


@pytest.mark.asyncio
async def test_grid_bound_cloud_dispatched(monkeypatch):
    """Bound hybrid cloud actually receives the grid write (capability True)."""
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "plain", {"host": "1.1.1.1"},
        [("pypowerwall/plain/control/grid_charging/set",
          json.dumps({"value": True}))],
        cloud_result={"ok": True}, hybrid=True, bound_id="plain",
    )
    mock_local.assert_not_called()
    mock_cloud.assert_called_once()
    assert mock_cloud.call_args[0][0] == "set_grid_charging"


@pytest.mark.asyncio
async def test_grid_cloud_gateway_own_connection(monkeypatch):
    """Cloud-mode gateway runs grid writes on its own connection."""
    mock_local, mock_cloud, _ = await _run_control_messages(
        monkeypatch, "remote",
        {"cloud_mode": True, "email": "user@example.com"},
        [("pypowerwall/remote/control/grid_export/set",
          json.dumps({"value": "pv_only"}))],
        cloud_result={"ok": True},
    )
    mock_local.assert_called_once()
    assert mock_local.call_args[0][0] == "remote"
    assert mock_local.call_args[0][1] == "set_grid_export"
    mock_cloud.assert_not_called()
