# MQTT Support — Design & Implementation Plan

Related: [Issue #1](https://github.com/jasonacox/pypowerwall-server/issues/1)

---

## Overview

Add an **opt-in MQTT publisher** to pypowerwall-server that pushes Powerwall telemetry to any MQTT broker after every successful poll cycle. Designed primarily for Home Assistant integration but compatible with any MQTT-based system (Node-RED, InfluxDB MQTT adapter, etc.).

Key design goals:
- **Zero impact when disabled** — if `MQTT_HOST` is not set, no code path changes
- **Non-blocking** — publish happens asynchronously after the poll cache is updated; it never delays HTTP responses
- **Multi-gateway aware** — each gateway publishes to its own sub-topic tree
- **Home Assistant auto-discovery** — optional `homeassistant/` discovery payloads so sensors appear automatically in HA without manual YAML

---

## Architecture

```
                        ┌─────────────────────────┐
                        │      GatewayManager      │
                        │  (background poll loop)  │
                        └────────────┬────────────┘
                                     │ after each successful _poll_gateway()
                                     ▼
                        ┌─────────────────────────┐
                        │      MqttPublisher       │  app/mqtt/publisher.py
                        │  (asyncio coroutine)     │
                        │                          │
                        │  • build topic payloads  │
                        │  • connect / reconnect   │
                        │  • publish with retain   │
                        └────────────┬────────────┘
                                     │ aiomqtt (async MQTT client)
                                     ▼
                        ┌─────────────────────────┐
                        │       MQTT Broker        │
                        │  (Mosquitto, HiveMQ …)   │
                        └─────────────────────────┘
                                     │
                        ┌────────────┴────────────┐
                        │                         │
               ┌────────▼────────┐    ┌───────────▼──────────┐
               │  Home Assistant │    │  Node-RED / Grafana   │
               │  (auto-discover)│    │  / InfluxDB / etc.    │
               └─────────────────┘    └──────────────────────┘
```

### Integration point in `gateway_manager.py`

```python
# In _poll_gateway(), after self.cache[gateway_id] is updated:
from app.mqtt.publisher import mqtt_publisher
if mqtt_publisher.enabled:
    asyncio.create_task(
        mqtt_publisher.publish_gateway(gateway_id, status)
    )
```

The `asyncio.create_task()` call is fire-and-forget — MQTT failures never propagate back to the poll loop.

---

## New Files

```
app/
  mqtt/
    __init__.py          # exports mqtt_publisher singleton
    publisher.py         # MqttPublisher class — connection mgmt + publish logic
    ha_discovery.py      # Home Assistant MQTT discovery payload builders
```

---

## Environment Variables

All use `MQTT_` prefix (no `PW_` prefix — MQTT is not a Powerwall concept).

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | *(unset)* | Broker hostname or IP. **Required to enable MQTT.** |
| `MQTT_PORT` | `1883` | Broker port (`8883` for TLS) |
| `MQTT_USERNAME` | *(unset)* | Optional broker username |
| `MQTT_PASSWORD` | *(unset)* | Optional broker password |
| `MQTT_TLS` | `no` | Enable TLS/SSL (`yes`/`no`) |
| `MQTT_TLS_CA_CERT` | *(unset)* | Path to CA certificate for TLS verification |
| `MQTT_TLS_INSECURE` | `no` | Skip TLS certificate verification (dev only) |
| `MQTT_TOPIC_PREFIX` | `pypowerwall` | Root topic prefix |
| `MQTT_RETAIN` | `yes` | Publish with MQTT retain flag |
| `MQTT_QOS` | `1` | QoS level (0, 1, or 2) |
| `MQTT_HA_DISCOVERY` | `yes` | Publish Home Assistant auto-discovery payloads |
| `MQTT_HA_PREFIX` | `homeassistant` | HA discovery topic prefix |
| `MQTT_CLIENT_ID` | `pypowerwall-server` | MQTT client identifier |
| `MQTT_KEEPALIVE` | `60` | Broker keepalive interval (seconds) |

Add to `app/config.py` Settings class:

```python
# MQTT settings
mqtt_host: Optional[str] = Field(default=None, alias="MQTT_HOST")
mqtt_port: int = Field(default=1883, alias="MQTT_PORT")
mqtt_username: Optional[str] = Field(default=None, alias="MQTT_USERNAME")
mqtt_password: Optional[str] = Field(default=None, alias="MQTT_PASSWORD")
mqtt_tls: bool = Field(default=False, alias="MQTT_TLS")
mqtt_tls_ca_cert: Optional[str] = Field(default=None, alias="MQTT_TLS_CA_CERT")
mqtt_tls_insecure: bool = Field(default=False, alias="MQTT_TLS_INSECURE")
mqtt_topic_prefix: str = Field(default="pypowerwall", alias="MQTT_TOPIC_PREFIX")
mqtt_retain: bool = Field(default=True, alias="MQTT_RETAIN")
mqtt_qos: int = Field(default=1, alias="MQTT_QOS")
mqtt_ha_discovery: bool = Field(default=True, alias="MQTT_HA_DISCOVERY")
mqtt_ha_prefix: str = Field(default="homeassistant", alias="MQTT_HA_PREFIX")
mqtt_client_id: str = Field(default="pypowerwall-server", alias="MQTT_CLIENT_ID")
mqtt_keepalive: int = Field(default=60, alias="MQTT_KEEPALIVE")

@property
def mqtt_enabled(self) -> bool:
    return bool(self.mqtt_host)
```

---

## Topic Structure

Base path: `{MQTT_TOPIC_PREFIX}/{gateway_id}/`

### Individual sensor topics (scalar values — ideal for HA)

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/battery` | `85.3` | `%` |
| `pypowerwall/{gw}/solar` | `2340` | `W` |
| `pypowerwall/{gw}/grid` | `-1100` | `W` (negative = exporting) |
| `pypowerwall/{gw}/home` | `1240` | `W` |
| `pypowerwall/{gw}/powerwall` | `1200` | `W` (positive = discharging) |
| `pypowerwall/{gw}/grid_status` | `UP` or `DOWN` | — |
| `pypowerwall/{gw}/mode` | `self_consumption` | — |
| `pypowerwall/{gw}/reserve` | `20.0` | `%` |
| `pypowerwall/{gw}/online` | `true` or `false` | — |

### Full JSON topics (for advanced consumers)

| Topic | Value |
|-------|-------|
| `pypowerwall/{gw}/aggregates` | Full `/api/meters/aggregates` JSON |
| `pypowerwall/{gw}/status` | `{"online": true, "soe": 85.3, "mode": "...", ...}` summary |

### Availability topic (for HA)

| Topic | Value |
|-------|-------|
| `pypowerwall/{gw}/availability` | `online` or `offline` |

Published `online` on each successful poll; `offline` published as a **Last Will and Testament (LWT)** message so HA marks sensors unavailable if the server crashes.

---

## Home Assistant Auto-Discovery

When `MQTT_HA_DISCOVERY=yes`, on first connection (and once per server restart) the publisher sends HA [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) payloads.

Discovery topic pattern: `{MQTT_HA_PREFIX}/sensor/pypowerwall_{gw}_{sensor}/config`

Example for battery SOE:
```json
Topic: homeassistant/sensor/pypowerwall_default_battery/config
{
  "name": "Battery",
  "unique_id": "pypowerwall_default_battery",
  "state_topic": "pypowerwall/default/battery",
  "availability_topic": "pypowerwall/default/availability",
  "unit_of_measurement": "%",
  "device_class": "battery",
  "state_class": "measurement",
  "device": {
    "identifiers": ["pypowerwall_default"],
    "name": "Powerwall (default)",
    "manufacturer": "Tesla",
    "model": "Powerwall",
    "sw_version": "23.44.0"
  }
}
```

Sensors to auto-discover per gateway:

| Sensor | HA device_class | Unit | Icon |
|--------|----------------|------|------|
| Battery SOE | `battery` | `%` | — |
| Solar Power | `power` | `W` | `mdi:solar-power` |
| Grid Power | `power` | `W` | `mdi:transmission-tower` |
| Home Power | `power` | `W` | `mdi:home-lightning-bolt` |
| Powerwall Power | `power` | `W` | `mdi:battery-charging` |
| Grid Status | — | — | `mdi:transmission-tower` |
| Operation Mode | — | — | `mdi:cog` |
| Backup Reserve | — | `%` | `mdi:battery-lock` |

Binary sensors:
| Sensor | HA device_class |
|--------|----------------|
| Grid Connected | `connectivity` |
| Gateway Online | `connectivity` |

---

## `MqttPublisher` Class Design

```python
# app/mqtt/publisher.py

class MqttPublisher:
    """Async MQTT publisher for Powerwall telemetry."""

    def __init__(self):
        self._client = None          # aiomqtt.Client
        self._connected = False
        self._discovery_sent: set[str] = set()  # gateway IDs already discovered

    @property
    def enabled(self) -> bool:
        from app.config import settings  # late import
        return settings.mqtt_enabled

    async def connect(self): ...
    async def disconnect(self): ...
    async def publish_gateway(self, gateway_id: str, status: GatewayStatus): ...
    async def _publish_discovery(self, gateway_id: str, status: GatewayStatus): ...
    async def _publish_scalar(self, topic: str, value): ...
    async def _reconnect_if_needed(self): ...

mqtt_publisher = MqttPublisher()  # module-level singleton
```

### Reconnection strategy

- On publish failure: log warning, mark disconnected, attempt reconnect next cycle
- Use exponential backoff (max 60s) to avoid hammering an unreachable broker
- Do not raise exceptions — MQTT failures are logged and silently swallowed so HTTP responses are never affected

---

## `main.py` Lifespan Changes

```python
# In lifespan(), after gateway_manager.initialize():
from app.mqtt.publisher import mqtt_publisher
if mqtt_publisher.enabled:
    await mqtt_publisher.connect()
    logger.info(f"MQTT publisher connected to {settings.mqtt_host}:{settings.mqtt_port}")

# In lifespan() shutdown section:
if mqtt_publisher.enabled:
    await mqtt_publisher.disconnect()
```

---

## Dependencies

Add to `requirements.txt`:

```
aiomqtt>=2.3.0
```

`aiomqtt` wraps `paho-mqtt` with a native asyncio interface that fits the existing event loop without needing executor bridging.

---

## Implementation Phases

### Phase 1 — Core publisher (MVP)
- [x] Add MQTT settings to `app/config.py`
- [x] Create `app/mqtt/__init__.py` and `app/mqtt/publisher.py`
- [x] Implement `connect()`, `disconnect()`, `publish_gateway()`
- [x] Publish scalar sensor topics and `aggregates` JSON topic
- [x] Publish LWT (`offline`) and `availability` topics
- [x] Hook into `gateway_manager._poll_gateway()` post-cache update
- [x] Start/stop in `main.py` lifespan
- [x] Add `aiomqtt` to `requirements.txt`
- [x] Log MQTT activity at DEBUG level (silent when not configured)

### Phase 2 — Home Assistant discovery
- [x] Create `app/mqtt/ha_discovery.py`
- [x] Implement discovery payload builders for all sensors
- [x] Publish discovery on connect (once per gateway per run)
- [x] Include `device` block so all sensors group under one HA device card
- [x] Test with real Home Assistant instance

### Phase 3 — Polish & docs
- [x] Add MQTT section to README
- [x] Add MQTT variables to `docker-compose.yml` example (commented out)
- [x] Add tests: `tests/test_mqtt_publisher.py` and `tests/test_mqtt_ha_discovery.py` (mock broker)
- [x] Update `AGENTS.md` with MQTT architecture notes
- [x] Update `RELEASE.md` when shipped
- [x] `mqtt-tools/` folder — broker setup guide (`README.md`) and live monitor GUI (`monitor.py`)
- [x] Console MQTT Broker panel (`GET /api/mqtt/status` + dashboard card)

---

## Example `docker-compose.yml` snippet

```yaml
services:
  pypowerwall-server:
    image: jasonacox/pypowerwall-server:latest
    environment:
      PW_HOST: 192.168.91.1
      PW_GW_PWD: your_gateway_password
      # MQTT (optional — remove to disable)
      MQTT_HOST: 192.168.1.10
      MQTT_PORT: 1883
      MQTT_USERNAME: mqtt_user
      MQTT_PASSWORD: mqtt_pass
      MQTT_HA_DISCOVERY: "yes"
```

---

## Example Home Assistant Result

After enabling MQTT, the following entities appear automatically under a **"Powerwall (default)"** device in HA:

```
Powerwall (default)
  ├── Battery              85.3 %
  ├── Solar Power          2340 W
  ├── Grid Power           -1100 W
  ├── Home Power           1240 W
  ├── Powerwall Power      1200 W
  ├── Grid Status          Connected
  ├── Operation Mode       self_consumption
  └── Backup Reserve       20.0 %
```

---

## Security Considerations

- `MQTT_PASSWORD` is never logged or exposed in API responses
- TLS support (`MQTT_TLS=yes`) for production broker connections
- `MQTT_TLS_INSECURE` defaults to `no` — must be explicitly enabled for dev
- No MQTT subscribe / inbound command handling in this design (publish-only); control commands remain exclusively through the existing `POST /control/*` HTTP endpoints
