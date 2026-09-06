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
| `pypowerwall/{gw}/total_capacity` | `13500` | `Wh` (total battery capacity) |
| `pypowerwall/{gw}/current_charge` | `11547` | `Wh` (current battery charge) |
| `pypowerwall/{gw}/online` | `true` or `false` | — |

### Lifetime energy topics (Wh accumulators)

Lifetime energy totals from `/api/meters/aggregates` — on PW3/TEDAPI these are
populated by `pypowerwall>=0.16.5` (overlaid from the gateway's native local API,
see pypowerwall PR #372); PW2/local mode has always carried them. Gateways whose
firmware lacks the endpoint report `0`, mirroring the HTTP API.

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/grid_energy_imported` | `4902666` | `Wh` (lifetime grid import) |
| `pypowerwall/{gw}/grid_energy_exported` | `1469391` | `Wh` (lifetime grid export) |
| `pypowerwall/{gw}/home_energy_imported` | `11679089` | `Wh` (lifetime home consumption) |
| `pypowerwall/{gw}/solar_energy_exported` | `8337073` | `Wh` (lifetime solar production) |
| `pypowerwall/{gw}/battery_energy_imported` | `4803385` | `Wh` (lifetime battery charged) |
| `pypowerwall/{gw}/battery_energy_exported` | `4712126` | `Wh` (lifetime battery discharged) |

*These are lifetime totals (same semantics as PW2's counters) — daily-energy
dashboards should delta them, or use the HA Energy dashboard which does this
automatically via `state_class: total_increasing`.*

### Full JSON topics (for advanced consumers)

| Topic | Value |
|-------|-------|
| `pypowerwall/{gw}/aggregates` | Full `/api/meters/aggregates` JSON |
| `pypowerwall/{gw}/status` | `{"online": true, "soe": 85.3, "mode": "...", ...}` summary |

### Solar string topics (per-string voltage, current, power)

Individual string topics are published when string data is available from the gateway:

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/strings/{A-F}/voltage` | `240.50` | `V` |
| `pypowerwall/{gw}/strings/{A-F}/current` | `1.50` | `A` |
| `pypowerwall/{gw}/strings/{A-F}/power` | `360.75` | `W` |
| `pypowerwall/{gw}/strings/{A-F}` | `{"Voltage": ..., "Current": ..., "Power": ...}` | JSON |

### PW3 paired-string rollups (AB, CD, EF)

For Powerwall 3 systems where inputs are physically paired, derived rollups are published:

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/strings/{AB,CD,EF}/voltage` | `240.50` | `V` (from first string in pair) |
| `pypowerwall/{gw}/strings/{AB,CD,EF}/current` | `3.00` | `A` (sum of both strings) |
| `pypowerwall/{gw}/strings/{AB,CD,EF}/power` | `721.50` | `W` (sum of both strings) |

For multi-PW3 single-gateway setups (e.g. two PW3s on one gateway), the strings endpoint
may return `A`–`F` *and* `A1`–`F1`. Paired rollups are generated per suffix automatically.
**Paired rollup topics are only published when both strings in the pair exist** — if only one
string of a pair is present (e.g. A without B), no AB rollup is emitted.

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/strings/AB1/voltage` | `310.00` | `V` |
| `pypowerwall/{gw}/strings/AB1/current` | `0.40` | `A` |
| `pypowerwall/{gw}/strings/AB1/power` | `124.00` | `W` |

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
| Battery Raw SOE | — | `%` | `mdi:battery-medium` |
| Solar Power | `power` | `W` | `mdi:solar-power` |
| Grid Power | `power` | `W` | `mdi:transmission-tower` |
| Home Power | `power` | `W` | `mdi:home-lightning-bolt` |
| Powerwall Power | `power` | `W` | `mdi:battery-charging` |
| Grid Status | — | — | `mdi:transmission-tower` |
| Operation Mode | — | — | `mdi:cog` |
| Backup Reserve | — | `%` | `mdi:battery-lock` |
| Total Battery Capacity | `energy_storage` | `Wh` | `mdi:battery-high` |
| Current Battery Charge | `energy_storage` | `Wh` | `mdi:battery-medium` |
| Lifetime Energy (6 sensors) | `energy` | `Wh` | — |

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


## Test Instructions - Quick Start

These steps let you verify MQTT end-to-end without a real Powerwall — using only Docker, Mosquitto, and the built-in simulator.

### 1. Start a local Mosquitto broker

```bash
docker run -d --rm --name mosquitto \
  -p 1883:1883 \
  eclipse-mosquitto \
  mosquitto -c /mosquitto-no-auth.conf
```

> The `-c /mosquitto-no-auth.conf` flag starts the broker with no authentication, which is fine for local testing.

### 2. Subscribe to all pypowerwall topics (in a separate terminal)

```bash
docker run --rm eclipse-mosquitto \
  mosquitto_sub -h host.docker.internal -p 1883 -v -t "pypowerwall/#"
```

You should see messages like `pypowerwall/default/battery 85.3` appear once per poll cycle.

### 3. Clone the PR branch and install dependencies

```bash
git clone -b mqtt https://github.com/jasonacox/pypowerwall-server.git
cd pypowerwall-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 4. Run the server with MQTT enabled

Replace `<gateway_ip>` and `<password>` with your Powerwall's IP and local password (or use the simulator — see step 4b below):

```bash
MQTT_HOST=localhost \
MQTT_PORT=1883 \
MQTT_HA_DISCOVERY=true \
PW_HOST=<gateway_ip> \
PW_GW_PWD=<password> \
uvicorn app.main:app --host 127.0.0.1 --port 8675
```

#### 4b. No Powerwall? Use the built-in simulator

In one terminal:

```bash
cd sandbox/pwsimulator
docker build -t pwsimulator .
docker run --rm -p 443:443 pwsimulator
```

Then start the server pointing at the simulator:

```bash
MQTT_HOST=localhost \
MQTT_PORT=1883 \
MQTT_HA_DISCOVERY=true \
PW_HOST=localhost \
PW_GW_PWD=password \
uvicorn app.main:app --host 127.0.0.1 --port 8675
```

### 5. Verify Home Assistant discovery payloads

```bash
docker run --rm eclipse-mosquitto \
  mosquitto_sub -h host.docker.internal -p 1883 -v -t "homeassistant/#"
```

You should see `homeassistant/sensor/pypowerwall_default_battery/config` (and others) with JSON payloads. These are the auto-discovery messages that tell HA how to create the sensor entities.

### 6. Run the unit tests

No broker required — all MQTT tests use mocks:

```bash
pytest tests/test_mqtt_publisher.py tests/test_mqtt_ha_discovery.py -v
```

### 7. (Optional) Run the live monitor GUI

```bash
pip install paho-mqtt
python mqtt-tools/monitor.py --host localhost
```

The GUI shows live battery %, capacity, charge, power flows, grid status, and mode, updating every poll cycle. Close the window to exit.

---

### Expected topic output (one poll cycle)

```
pypowerwall/default/availability     online
pypowerwall/default/battery          85.3
pypowerwall/default/solar            2340
pypowerwall/default/grid             -1100
pypowerwall/default/home             1240
pypowerwall/default/powerwall        1200
pypowerwall/default/grid_status      UP
pypowerwall/default/mode             self_consumption
pypowerwall/default/reserve          20.0
pypowerwall/default/total_capacity   13500
pypowerwall/default/current_charge   11547
pypowerwall/default/online           true
pypowerwall/default/aggregates       {...}
pypowerwall/default/status           {...}
pypowerwall/default/strings/A/voltage   240.50
pypowerwall/default/strings/A/current   1.50
pypowerwall/default/strings/A/power     360.75
pypowerwall/default/strings/A           {"Voltage": 240.5, "Current": 1.5, "Power": 360.75, ...}
pypowerwall/default/strings/B/voltage   240.25
pypowerwall/default/strings/B/current   1.25
pypowerwall/default/strings/B/power     300.31
pypowerwall/default/strings/B           {...}
...
pypowerwall/default/strings/AB/voltage  240.50
pypowerwall/default/strings/AB/current  2.75
pypowerwall/default/strings/AB/power    661.06
...
```
