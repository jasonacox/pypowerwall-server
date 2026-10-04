"""
Tests for MQTT controls (opt-in MQTT_CONTROLS bitmask, broker trust).

Every safety guard has a test that fails when the guard is removed. The
control loop runs against aiomqtt's real MessagesIterator, and discovery runs
through publish_gateway() with real Gateway/GatewayStatus objects, so
capability checks are exercised end to end rather than passed in as flags.
"""
import asyncio
import json
import logging
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiomqtt import Message
from aiomqtt.client import MessagesIterator

from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus, PowerwallData
from app.mqtt.ha_discovery import build_discovery_payloads, control_config_topics
from app.mqtt.publisher import CONTROL_COALESCE_WINDOW_S, MqttPublisher

LOGGER = "app.mqtt.publisher"

# Gateway id -> (Gateway kwargs, tedapi_mode). "hybrid" is bound to the
# shared cloud connection; "full" (TEDAPI full) can't write anything.
GATEWAYS = {
    "cloud": ({"cloud_mode": True}, "Cloud"),
    "fleet": ({"fleetapi": True}, "FleetAPI"),
    "hybrid": ({"host": "10.0.0.2"}, "full"),
    "v1r": ({"host": "10.0.0.3", "rsa_key_configured": True}, "v1r"),
    "full": ({"host": "10.0.0.4"}, "full"),
}
VALUE_CONTROLS = ["grid_charging_control", "grid_export_control", "mode_control", "reserve_control"]
ISLANDING = ["go_off_grid", "reconnect_grid"]


def _status(gateway, tedapi_mode):
    data = PowerwallData(
        tedapi_mode=tedapi_mode, mode="self_consumption", reserve=20.0,
        grid_charging=True, grid_export="pv_only", grid_status="UP",
    )
    return GatewayStatus(gateway=gateway, data=data, online=True, last_updated=time.time())


@pytest.fixture
def env(monkeypatch):
    """Controls fully enabled (all bits, broker credentials, secret)."""
    for name, value in {
        "mqtt_host": "localhost",
        "mqtt_username": "pypowerwall",
        "mqtt_password": "secret",
        "control_secret": "secret",
        "mqtt_controls": 31,
        "mqtt_topic_prefix": "pypowerwall",
        "mqtt_ha_prefix": "homeassistant",
        "mqtt_ha_discovery": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
def gm(monkeypatch):
    """gateway_manager with the five GATEWAYS and recording control mocks."""
    monkeypatch.setattr(gateway_manager, "gateways", {})
    monkeypatch.setattr(gateway_manager, "cache", {})
    for gid, (kwargs, mode) in GATEWAYS.items():
        gw = Gateway(id=gid, name=gid, online=True, **kwargs)
        gateway_manager.gateways[gid] = gw
        gateway_manager.cache[gid] = _status(gw, mode)
    monkeypatch.setattr(gateway_manager, "_cloud_control", object())
    monkeypatch.setattr(gateway_manager, "_cloud_control_gateway_id", "hybrid")
    monkeypatch.setattr(gateway_manager, "local_control", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(gateway_manager, "cloud_control", AsyncMock(return_value={"ok": True}))
    return gateway_manager


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


async def run_commands(*messages):
    """Deliver (topic, payload[, retain]) messages, run the loop until handled."""
    pub = MqttPublisher()
    client = FakeClient()
    for message in messages:
        client.deliver(*message)
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


def cmd(gateway, control, payload, retain=False):
    return (f"pypowerwall/{gateway}/control/{control}/set", payload, retain)


def calls(mock):
    return [(c.args, c.kwargs) for c in mock.call_args_list]


# --- Configuration -----------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("15", 15), (" 31 ", 31), ("0", 0), ("", 0),
    ("reserve,mode", 0), ("48", 0), ("-1", 0), ("3.5", 0), ("32", 0),
])
def test_mqtt_controls_fails_closed(monkeypatch, caplog, raw, expected):
    """Anything but an integer 0-31 means monitoring only, without stopping
    the server (48 = 16 + an invalid bit must not enable islanding)."""
    from app.config import Settings

    monkeypatch.setenv("MQTT_CONTROLS", raw)
    with caplog.at_level(logging.ERROR, logger="app.config"):
        assert Settings().mqtt_controls == expected
    invalid = raw.strip() not in ("", "0") and expected == 0
    assert any("Invalid MQTT_CONTROLS" in r.getMessage() for r in caplog.records) == invalid


@pytest.mark.parametrize("missing", ["mqtt_username", "mqtt_password", "control_secret", "mqtt_controls"])
def test_controls_need_credentials_secret_and_bits(env, monkeypatch, missing):
    assert settings.mqtt_controls_available
    monkeypatch.setattr(settings, missing, 0 if missing == "mqtt_controls" else None)
    assert not settings.mqtt_controls_available


def test_control_names(env, monkeypatch):
    monkeypatch.setattr(settings, "mqtt_controls", 17)
    assert settings.mqtt_control_names() == ["reserve", "islanding"]
    assert settings.mqtt_control_allowed("islanding")
    assert not settings.mqtt_control_allowed("mode")


# --- Discovery -----------------------------------------------------------------


def _announced(**kwargs):
    payloads = build_discovery_payloads("home", "Home", "pypowerwall", "homeassistant", **kwargs)
    return sorted(
        json.loads(p)["unique_id"].split("home_", 1)[1]
        for _, p in payloads
        if "/control/" in json.loads(p).get("command_topic", "")
    )


def test_discovery_follows_bits_and_capability():
    assert _announced(controls=0, writable=True, is_v1r=True) == []
    assert _announced(controls=31, writable=True, is_v1r=True) == sorted(VALUE_CONTROLS + ISLANDING)
    # Islanding needs its own bit, even on v1r
    assert _announced(controls=15, writable=True, is_v1r=True) == VALUE_CONTROLS
    # Value controls need a gateway that can write them
    assert _announced(controls=31, writable=False, is_v1r=False) == []
    assert _announced(controls=1, writable=True) == ["reserve_control"]
    assert _announced(controls=2, writable=True) == ["mode_control"]
    assert _announced(controls=4, writable=True) == ["grid_charging_control"]
    assert _announced(controls=8, writable=True) == ["grid_export_control"]
    assert _announced(controls=16, writable=True, is_v1r=False) == []


def test_control_config_topics_cover_every_control_entity():
    payloads = build_discovery_payloads(
        "home", "Home", "pypowerwall", "homeassistant", controls=31, writable=True, is_v1r=True
    )
    control_topics = {t for t, p in payloads if "/control/" in json.loads(p).get("command_topic", "")}
    assert control_topics == set(control_config_topics("home", "homeassistant"))


def test_reserve_entity_payload():
    payloads = dict(build_discovery_payloads(
        "home", "Home", "pypowerwall", "homeassistant", controls=1, writable=True
    ))
    entity = json.loads(payloads["homeassistant/number/pypowerwall_home_reserve_control/config"])
    assert entity["command_topic"] == "pypowerwall/home/control/reserve/set"
    assert entity["state_topic"] == "pypowerwall/home/reserve"
    assert (entity["min"], entity["max"], entity["step"]) == (0, 100, 1)


async def _discover(gateway_ids):
    """publish_gateway() per gateway; returns ({gw: announced}, {gw: cleared})."""
    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True
    sent = []

    async def record(self, topic, payload, retain, qos):
        sent.append((topic, payload))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MqttPublisher, "_safe_publish", record)
        for gid in gateway_ids:
            await pub.publish_gateway(gid, gateway_manager.get_gateway(gid))
    announced, cleared = {}, {}
    for topic, payload in sent:
        if topic not in sum((control_config_topics(g, "homeassistant") for g in gateway_ids), []):
            continue
        gid, suffix = topic.split("/")[2].split("pypowerwall_", 1)[1].split("_", 1)
        (announced if payload else cleared).setdefault(gid, []).append(suffix)
    return ({g: sorted(v) for g, v in announced.items()}, {g: sorted(v) for g, v in cleared.items()})


@pytest.mark.asyncio
async def test_discovery_per_gateway_type(env, gm):
    """Through publish_gateway(): only controls the gateway can run."""
    announced, _ = await _discover(GATEWAYS)
    assert announced == {
        "cloud": VALUE_CONTROLS,
        "fleet": VALUE_CONTROLS,
        "hybrid": VALUE_CONTROLS,
        "v1r": sorted(VALUE_CONTROLS + ISLANDING),
    }  # "full" (TEDAPI full) can't write anything


@pytest.mark.asyncio
async def test_unbound_hybrid_cloud_announces_nothing(env, gm, monkeypatch):
    monkeypatch.setattr(gateway_manager, "_cloud_control_gateway_id", None)
    announced, _ = await _discover(["hybrid"])
    assert announced == {}


@pytest.mark.asyncio
async def test_unknown_transport_mode_is_not_v1r(env, gm):
    """Fail closed: a v1r gateway whose mode isn't known yet (cold start,
    cloud failover) gets no islanding buttons and no value controls."""
    gw = gateway_manager.gateways["v1r"]
    gateway_manager.cache["v1r"] = _status(gw, None)
    announced, _ = await _discover(["v1r"])
    assert announced == {}


@pytest.mark.asyncio
async def test_discovery_clears_controls_it_does_not_announce(env, gm, monkeypatch):
    """Stateless: a fresh process (e.g. restarted with MQTT_CONTROLS=0)
    clears every control entity, so none survive a restart."""
    monkeypatch.setattr(settings, "mqtt_controls", 0)
    announced, cleared = await _discover(["v1r"])
    assert announced == {}
    assert cleared == {"v1r": sorted(VALUE_CONTROLS + ISLANDING)}


@pytest.mark.asyncio
async def test_discovery_refires_when_v1r_resolves(env, gm):
    pub = MqttPublisher()
    pub._client = AsyncMock()
    pub._connected = True
    gw = gateway_manager.gateways["v1r"]
    gateway_manager.cache["v1r"] = _status(gw, None)
    with pytest.MonkeyPatch.context() as mp:
        sent = []

        async def record(self, topic, payload, retain, qos):
            sent.append((topic, payload))

        mp.setattr(MqttPublisher, "_safe_publish", record)
        await pub.publish_gateway("v1r", gateway_manager.get_gateway("v1r"))
        sent.clear()
        gateway_manager.cache["v1r"] = _status(gw, "v1r")
        await pub.publish_gateway("v1r", gateway_manager.get_gateway("v1r"))
    assert "homeassistant/button/pypowerwall_v1r_go_off_grid/config" in {
        t for t, p in sent if p
    }


# --- Dispatch --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_one_path_per_gateway(env, gm):
    """Each command runs on exactly one connection; the shared cloud only for
    the gateway it is bound to; gateways that can't write get no call."""
    await run_commands(*(cmd(g, "reserve", {"value": 30}) for g in GATEWAYS))
    assert calls(gm.cloud_control) == [(("set_reserve", 30), {"timeout": 10.0})]
    assert sorted(calls(gm.local_control)) == sorted(
        ((g, "set_reserve", 30), {"timeout": 10.0}) for g in ("cloud", "fleet", "v1r")
    )


@pytest.mark.asyncio
async def test_no_retry_after_a_failed_write(env, gm):
    gm.cloud_control.return_value = None  # e.g. timeout
    await run_commands(cmd("hybrid", "mode", {"value": "backup"}))
    assert gm.cloud_control.await_count == 1
    assert gm.local_control.await_count == 0


@pytest.mark.asyncio
async def test_unbound_shared_cloud_is_never_used(env, gm, monkeypatch, caplog):
    monkeypatch.setattr(gateway_manager, "_cloud_control_gateway_id", "cloud")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("hybrid", "reserve", {"value": 30}))
    assert gm.cloud_control.await_count == 0 and gm.local_control.await_count == 0
    assert "can't write it" in caplog.text


@pytest.mark.parametrize("control, value, ok", [
    ("reserve", 0, True), ("reserve", 100, True), ("reserve", 101, False),
    ("reserve", -1, False), ("reserve", True, False), ("reserve", "30", False),
    ("reserve", 30.5, False), ("reserve", 40.0, False), ("reserve", None, False),
    ("mode", "autonomous", True), ("mode", "eco", False), ("mode", ["backup"], False),
    ("grid_charging", False, True), ("grid_charging", "true", False), ("grid_charging", 1, False),
    ("grid_export", "pv_only", True), ("grid_export", "always", False), ("grid_export", True, False),
])
@pytest.mark.asyncio
async def test_value_checks(env, gm, caplog, control, value, ok):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("cloud", control, {"value": value}))
    assert (gm.local_control.await_count == 1) == ok
    if not ok:
        # The rejection says what is accepted (e.g. 30.5 for reserve)
        hint = {"reserve": "an integer from 0 to 100", "mode": "self_consumption",
                "grid_charging": "true or false", "grid_export": "battery_ok"}[control]
        assert "invalid value" in caplog.text and hint in caplog.text


@pytest.mark.asyncio
async def test_disabled_bit_is_rejected(env, gm, monkeypatch, caplog):
    monkeypatch.setattr(settings, "mqtt_controls", 15)  # everything but islanding
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("v1r", "islanding", {"action": "off_grid", "confirm": True}))
    assert gm.local_control.await_count == 0
    assert "bit not set" in caplog.text


@pytest.mark.parametrize("topic, payload, reason", [
    ("pypowerwall/nope/control/reserve/set", {"value": 30}, "unknown gateway"),
    ("pypowerwall/cloud/control/charge/set", {"value": 30}, "unknown control"),
    ("pypowerwall/cloud/control/reserve/set/x", {"value": 30}, "malformed topic"),
    ("pypowerwall/cloud/control/reserve/set", "not json", "not a JSON object"),
    ("pypowerwall/cloud/control/reserve/set", [30], "not a JSON object"),
    ("pypowerwall/cloud/control/reserve/set",
     json.dumps({"value": 30, "pad": "x" * 2000}), "byte cap"),
])
@pytest.mark.asyncio
async def test_bad_commands_are_rejected(env, gm, caplog, topic, payload, reason):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands((topic, payload))
    assert gm.local_control.await_count == 0
    assert reason in caplog.text


@pytest.mark.asyncio
async def test_multi_level_topic_prefix(env, gm, monkeypatch):
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "home/powerwall")
    await run_commands(("home/powerwall/cloud/control/reserve/set", {"value": 30}),
                       ("pypowerwall/cloud/control/reserve/set", {"value": 40}))
    assert calls(gm.local_control) == [(("cloud", "set_reserve", 30), {"timeout": 10.0})]


@pytest.mark.parametrize("payload", [
    {"action": "off_grid"},
    {"action": "off_grid", "confirm": False},
    {"action": "off_grid", "confirm": "true"},
    {"action": "island", "confirm": True},
])
@pytest.mark.asyncio
async def test_islanding_needs_action_and_confirm(env, gm, caplog, payload):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("v1r", "islanding", payload))
    assert gm.local_control.await_count == 0
    assert "confirm:true" in caplog.text


@pytest.mark.parametrize("gateway", ["cloud", "hybrid", "full"])
@pytest.mark.asyncio
async def test_islanding_only_on_v1r(env, gm, caplog, gateway):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd(gateway, "islanding", {"action": "off_grid", "confirm": True}))
    assert gm.local_control.await_count == 0 and gm.cloud_control.await_count == 0
    assert "no confirmed v1r transport" in caplog.text


@pytest.mark.asyncio
async def test_islanding_rejected_while_mode_unknown(env, gm):
    gateway_manager.cache["v1r"] = _status(gateway_manager.gateways["v1r"], None)
    await run_commands(cmd("v1r", "islanding", {"action": "off_grid", "confirm": True}))
    assert gm.local_control.await_count == 0


@pytest.mark.asyncio
async def test_islanding_dispatch(env, gm):
    gm.local_control.return_value = {"result": 1}
    await run_commands(cmd("v1r", "islanding", {"action": "off_grid", "confirm": True}))
    await run_commands(cmd("v1r", "islanding", {"action": "on_grid", "confirm": True}))
    assert calls(gm.local_control) == [
        (("v1r", "go_off_grid"), {"timeout": 10.0, "confirm": True}),
        (("v1r", "reconnect_grid"), {"timeout": 10.0}),
    ]


@pytest.mark.parametrize("result, applied", [
    ({"result": 1}, True), ({"result": 0}, False), ({"result": True}, False),
    ({"result": "1"}, False), ({"error": "x", "result": 1}, False), (None, False),
])
@pytest.mark.asyncio
async def test_islanding_needs_hardware_ack(env, gm, caplog, result, applied):
    gm.local_control.return_value = result
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await run_commands(cmd("v1r", "islanding", {"action": "off_grid", "confirm": True}))
    assert ("applied" in caplog.text) == applied
    assert ("failed" in caplog.text) == (not applied)


@pytest.mark.asyncio
async def test_islanding_cooldown_does_not_stop_the_loop(env, gm, caplog):
    gm.local_control.side_effect = [RuntimeError("rate limited"), {"ok": True}]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(
            cmd("v1r", "islanding", {"action": "off_grid", "confirm": True}),
        )
        await run_commands(cmd("v1r", "reserve", {"value": 25}))
    assert "rate limited" in caplog.text
    assert gm.local_control.await_count == 2


@pytest.mark.parametrize("result", [None, {"error": "Failed to write config"}, {"ERROR": "x"}])
@pytest.mark.asyncio
async def test_failed_writes_are_not_reported_as_applied(env, gm, caplog, result):
    gm.local_control.return_value = result
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await run_commands(cmd("cloud", "reserve", {"value": 30}))
    assert "applied" not in caplog.text and "failed" in caplog.text


@pytest.mark.asyncio
async def test_audit_log_names_gateway_value_and_path(env, gm, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await run_commands(cmd("hybrid", "mode", {"value": "backup"}),
                           cmd("fleet", "reserve", {"value": 40}))
    assert "'mode' for 'hybrid' applied (value='backup' via hybrid cloud)" in caplog.text
    assert "'reserve' for 'fleet' applied (value=40 via fleetapi)" in caplog.text


@pytest.mark.asyncio
async def test_received_text_is_escaped_in_logs(env, gm, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("cloud", "mode", {"value": "x\nINFO forged line"}))
    assert "\nINFO forged line" not in caplog.text
    assert "'x\\nINFO forged line'" in caplog.text


@pytest.mark.asyncio
async def test_unexpected_error_is_contained(env, gm, caplog):
    gm.local_control.side_effect = [ValueError("boom"), {"ok": True}]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_commands(cmd("cloud", "reserve", {"value": 10}),
                           cmd("fleet", "reserve", {"value": 20}))
    assert gm.local_control.await_count == 2
    assert "boom" in caplog.text


# --- Control loop: bursts, retained commands, message safety -------------------


@pytest.mark.asyncio
async def test_burst_collapses_to_latest_per_topic(env, gm):
    await run_commands(*(cmd("cloud", "reserve", {"value": v}) for v in range(50)))
    assert calls(gm.local_control) == [(("cloud", "set_reserve", 49), {"timeout": 10.0})]


@pytest.mark.asyncio
async def test_burst_keeps_the_order_of_latest_commands(env, gm):
    await run_commands(cmd("cloud", "mode", {"value": "backup"}),
                       cmd("cloud", "reserve", {"value": 30}),
                       cmd("cloud", "mode", {"value": "autonomous"}))
    assert [c[0][1:] for c in calls(gm.local_control)] == [
        ("set_reserve", 30), ("set_mode", "autonomous")
    ]


@pytest.mark.asyncio
async def test_retained_replay_is_not_executed(env, gm, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        client = await run_commands(cmd("cloud", "reserve", {"value": 30}, retain=True))
    assert gm.local_control.await_count == 0
    assert "retained commands are not executed" in caplog.text
    assert ("pypowerwall/cloud/control/reserve/set", b"", True) in client.published


@pytest.mark.asyncio
async def test_every_command_topic_is_cleared(env, gm):
    """A command published retained while we're connected arrives with
    retain=0, so after running it the retained copy must be deleted."""
    client = await run_commands(cmd("cloud", "reserve", {"value": 30}))
    assert gm.local_control.await_count == 1
    assert client.published == [("pypowerwall/cloud/control/reserve/set", b"", True)]


@pytest.mark.asyncio
async def test_empty_payload_is_ignored_quietly(env, gm, caplog):
    """The echo of a retained clear is an empty message: nothing to run,
    nothing to warn about, no clear of its own."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        client = await run_commands(cmd("cloud", "reserve", b""))
    assert gm.local_control.await_count == 0
    assert caplog.text == ""
    assert client.published == []


@pytest.mark.asyncio
async def test_no_message_lost_at_the_window_deadline(env, gm):
    """Regression: wait_for() around __anext__() could drop a message that
    arrived just as the coalesce window closed. Deliver the second command at
    many offsets around the deadline; it must always run."""
    pub = MqttPublisher()
    loop = asyncio.get_running_loop()
    for i in range(-50, 51):
        client = FakeClient()
        messages = client.messages.__aiter__()
        client.deliver(*cmd("cloud", "reserve", {"value": 1}))
        first = await messages.__anext__()
        loop.call_at(
            loop.time() + CONTROL_COALESCE_WINDOW_S + i * 40e-6,
            client.deliver, *cmd("fleet", "reserve", {"value": 2}),
        )
        batch = await pub._collect_control_burst(messages, first)
        await asyncio.sleep(0.005)
        assert len(batch) + client._queue.qsize() == 2, f"lost at offset {i * 40} us"


@pytest.mark.asyncio
async def test_loop_end_forces_reconnect(env, gm, caplog):
    pub = MqttPublisher()
    pub._connected = True
    client = FakeClient()
    client._disconnected.set_exception(RuntimeError("broker gone"))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(pub._control_message_loop(client), timeout=5)
    assert pub._connected is False
    assert "forcing reconnect" in caplog.text


# --- Connection loop: startup logging and the control task --------------------


def _fake_aiomqtt(monkeypatch, enter):
    """Replace aiomqtt with a stub whose Client context runs `enter`."""

    class FakeCM:
        async def __aenter__(self):
            return enter()

        async def __aexit__(self, *args):
            return False

    module = types.ModuleType("aiomqtt")
    module.Client = lambda **kwargs: FakeCM()
    module.Will = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "aiomqtt", module)
    monkeypatch.setattr(MqttPublisher, "_safe_publish", AsyncMock())


@pytest.mark.parametrize("missing", ["mqtt_username", "mqtt_password", "control_secret"])
@pytest.mark.asyncio
async def test_says_why_controls_are_off(env, monkeypatch, caplog, missing):
    monkeypatch.setattr(settings, missing, None)
    pub = MqttPublisher()

    def enter():
        pub._shutdown = True  # one pass
        return AsyncMock()

    _fake_aiomqtt(monkeypatch, enter)
    env_name = {"mqtt_username": "MQTT_USERNAME", "mqtt_password": "MQTT_PASSWORD",
                "control_secret": "PW_CONTROL_SECRET"}[missing]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(pub._connection_loop(), timeout=5)
        pub._shutdown = False
        await asyncio.wait_for(pub._connection_loop(), timeout=5)  # reconnect
    warnings = [r.getMessage() for r in caplog.records if "but disabled" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].endswith(f"set {env_name}")


@pytest.mark.asyncio
async def test_startup_names_enabled_controls_and_warns_on_islanding(env, monkeypatch, caplog):
    monkeypatch.setattr(settings, "mqtt_topic_prefix", "home")
    pub = MqttPublisher()

    def enter():
        pub._shutdown = True
        return MagicMock(subscribe=AsyncMock(), messages=MagicMock())

    _fake_aiomqtt(monkeypatch, enter)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await asyncio.wait_for(pub._connection_loop(), timeout=5)
    assert "enabled: reserve, mode, grid_charging, grid_export, islanding" in caplog.text
    assert "ACL home/+/control/#" in caplog.text


@pytest.mark.asyncio
async def test_subscribe_error_reconnects_and_retries(env, monkeypatch, caplog):
    """A failed control subscribe must not leave the connection up without
    controls: reconnect (after the backoff) and subscribe again."""
    pub = MqttPublisher()
    clients = []

    def enter():
        client = MagicMock(messages=MagicMock())
        if not clients:
            client.subscribe = AsyncMock(side_effect=RuntimeError("timeout"))
        else:
            client.subscribe = AsyncMock(return_value=(1,))
            pub._shutdown = True
        clients.append(client)
        return client

    _fake_aiomqtt(monkeypatch, enter)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await asyncio.wait_for(pub._connection_loop(), timeout=10)
    assert len(clients) == 2
    assert "control subscribe failed: timeout" in caplog.text
    assert "MQTT controls subscribed" in caplog.text


@pytest.mark.asyncio
async def test_refused_subscribe_is_an_error_not_a_reconnect_loop(env, monkeypatch, caplog):
    """A broker that refuses the subscription (0x80, e.g. its ACL) doesn't
    raise: say so, run no control task, keep the connection for monitoring."""
    pub = MqttPublisher()
    clients = []

    def enter():
        pub._shutdown = True
        client = MagicMock(messages=MagicMock(), subscribe=AsyncMock(return_value=(0x80,)))
        clients.append(client)
        return client

    _fake_aiomqtt(monkeypatch, enter)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await asyncio.wait_for(pub._connection_loop(), timeout=5)
    assert len(clients) == 1
    assert "refused the subscription to pypowerwall/+/control/+/set" in caplog.text
    assert "MQTT controls subscribed" not in caplog.text
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "mqtt-control-handler"]


@pytest.mark.asyncio
async def test_dead_control_task_triggers_reconnect(env, monkeypatch, caplog):
    pub = MqttPublisher()
    connects = []

    def enter():
        connects.append(1)
        if len(connects) > 1:
            pub._shutdown = True  # stop after the reconnect
        return FakeClient()

    _fake_aiomqtt(monkeypatch, enter)

    async def kill_control_task():
        for _ in range(100):
            await asyncio.sleep(0.05)
            for task in asyncio.all_tasks():
                if task.get_name() == "mqtt-control-handler":
                    task.cancel()
                    return
        raise AssertionError("control task never started")

    loop_task = asyncio.create_task(pub._connection_loop())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(kill_control_task(), timeout=10)
        await asyncio.wait_for(loop_task, timeout=30)
    assert "ended unexpectedly" in caplog.text
    assert len(connects) >= 2


# --- Shared cloud connection: bound only to an unambiguous site ----------------


class _CloudClient:
    def __init__(self, sites, sitefile=None):
        self._sites, self.sitefile = sites, sitefile

    def getsites(self):
        return self._sites


@pytest.mark.asyncio
async def test_no_site_check_without_mqtt_controls(env, monkeypatch, caplog):
    """The binding is only for MQTT controls: with them off, no cloud call
    and no warning, even on an ambiguous multi-site account."""
    monkeypatch.setattr(settings, "mqtt_controls", 0)
    monkeypatch.setattr(settings, "siteid", None)
    client = MagicMock(sitefile=None)
    monkeypatch.setattr(gateway_manager, "_cloud_control", MagicMock(client=client))
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        assert await gateway_manager._cloud_site_unambiguous(False) is False
    client.getsites.assert_not_called()
    assert caplog.text == ""


@pytest.mark.parametrize("siteid, fleetapi, sitefile, sites, bound", [
    ("123", False, False, [1, 2], True),   # PW_SITEID picks the site
    (None, True, False, [1, 2], True),     # FleetAPI: site from its own setup
    (None, False, True, [1, 2], True),     # site chosen with pypowerwall setup
    (None, False, False, [1], True),       # single-site account
    (None, False, False, [1, 2], False),   # ambiguous: would default to sites[0]
    (None, False, False, None, False),     # unknown
])
@pytest.mark.asyncio
async def test_shared_cloud_binds_only_to_an_unambiguous_site(
    env, monkeypatch, tmp_path, caplog, siteid, fleetapi, sitefile, sites, bound
):
    from app.config import GatewayConfig
    gm_module = sys.modules["app.core.gateway_manager"]

    path = tmp_path / ".pypowerwall.site"
    if sitefile:
        path.write_text("2")
    created = {}

    def fake_powerwall(**kwargs):
        created.update(kwargs)
        return types.SimpleNamespace(client=_CloudClient(sites, str(path)))

    monkeypatch.setattr(gm_module.pypowerwall, "Powerwall", fake_powerwall)
    monkeypatch.setattr(settings, "siteid", siteid)
    monkeypatch.setattr(gateway_manager, "_cloud_control", None)
    monkeypatch.setattr(gateway_manager, "_cloud_control_gateway_id", None)
    config = GatewayConfig(id="home", host="10.0.0.2", email="a@b.c", fleetapi=fleetapi)
    with caplog.at_level(logging.WARNING, logger="app.core.gateway_manager"):
        await gateway_manager._init_cloud_control([config])
    assert gateway_manager._cloud_control_gateway_id == ("home" if bound else None)
    assert ("PW_SITEID" in caplog.text) == (not bound)
    # PW_SITEID reaches the cloud client as the integer it compares with
    assert created.get("siteid") == (123 if siteid else None)
