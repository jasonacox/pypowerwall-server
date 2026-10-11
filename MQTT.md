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
| `MQTT_CONTROLS` | `0` | Opt-in bitmask for Home Assistant controls (needs `PW_CONTROL_SECRET` and `MQTT_USERNAME`/`MQTT_PASSWORD`): `1` reserve (incl. `reserve_hold`), `2` mode, `4` grid_charging, `8` grid_export, `16` islanding. `15` = all but islanding, `31` = all, `0` = monitoring only. Any other value is logged as an error and treated as `0`. **Use at your own risk** (see the warning under *Control command topics*) |

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
mqtt_controls: int = Field(default=0, alias="MQTT_CONTROLS")

@property
def mqtt_enabled(self) -> bool:
    return bool(self.mqtt_host)

@property
def mqtt_controls_available(self) -> bool:
    return bool(
        self.mqtt_host
        and self.mqtt_username
        and self.mqtt_password
        and self.mqtt_controls != 0
        and self.control_secret
    )
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
| `pypowerwall/{gw}/grid_connected` | `true` or `false` | — (true when `grid_status`==`UP`) |
| `pypowerwall/{gw}/grid_charging` | `true` or `false` | — (grid charging allowed) |
| `pypowerwall/{gw}/grid_export` | `battery_ok`/`pv_only`/`never` | — (grid export policy) |
| `pypowerwall/{gw}/time_remaining` | `5.50` | `h` (backup time remaining, rounded 2 dp; `status` JSON keeps raw precision) |
| `pypowerwall/{gw}/online` | `true` or `false` | — |

Optional topics are published only when the source value is available; the
last retained value persists until the gateway's `availability` goes `offline`.

### Control command topics (opt-in `MQTT_CONTROLS`)

> **⚠️ WARNING: USE AT YOUR OWN RISK**
>
> MQTT controls let anything allowed to publish to the control topics on your MQTT broker (`{MQTT_TOPIC_PREFIX}/+/control/+/set`; on a broker without an ACL, that's every client) change how your Powerwall runs: the backup reserve, the operating mode, grid charging and grid export, and (with `16`) disconnecting your home from the grid. A misconfigured or compromised broker, a hacked smart-home device, a buggy automation or a simple mistake could:
>
> - cause a **power outage** in your home,
> - leave you **without backup power** when the grid goes down (for example, a reserve set to 0),
> - **damage** equipment or appliances, or
> - raise your energy costs or conflict with your utility agreement.
>
> This software is provided "as is", without warranty of any kind (see the [MIT license](LICENSE)), and is not made or supported by Tesla. **By setting `MQTT_CONTROLS` to anything other than `0`, you acknowledge these risks and accept full responsibility for the results.** Think twice before turning this on: enable only the controls you need, leave going off grid (`16`) off unless you truly need it, and secure your broker first.

| Topic | Bit | Payload | Accepted values |
|-------|-----|---------|-----------------|
| `{prefix}/{gw}/control/reserve/set` | `1` | `{"value": 20}` | integer `0`-`100` |
| `{prefix}/{gw}/control/reserve_hold/set` | `1` | `{}` | Hold battery: sets reserve to cached SoC |
| `{prefix}/{gw}/control/mode/set` | `2` | `{"value": "self_consumption"}` | `self_consumption`, `backup`, `autonomous` |
| `{prefix}/{gw}/control/grid_charging/set` | `4` | `{"value": true}` | `true`, `false` (JSON booleans) |
| `{prefix}/{gw}/control/grid_export/set` | `8` | `{"value": "battery_ok"}` | `battery_ok`, `pv_only`, `never` |
| `{prefix}/{gw}/control/islanding/set` | `16` | `{"action": "off_grid", "confirm": true}` | `off_grid`, `on_grid`, always with `"confirm": true` |

How commands run:

- **Where they can run.** Reserve, mode and the two grid settings need a gateway that can write them: cloud, FleetAPI, the hybrid cloud connection (only for the gateway it was built for, and only when its Tesla site is certain, see below), or a v1r connection. Islanding needs a confirmed v1r connection (Powerwall 2 or 3) and goes over it directly, with the server's islanding cooldown (`PW_ISLANDING_COOLDOWN`, 30 s by default). It is reported as applied only when the gateway acknowledges it (`result == 1`). Home Assistant only gets the controls a gateway can run; commands for anything else are rejected with a warning.
- **One write per command.** Each command runs on exactly one connection and is never retried on another one. A burst of commands for the same control (a slider drag) collapses to the latest one.
- **Not retained.** Publish with `retain=false` (Home Assistant does). A retained command is never replayed: after each command the server deletes any retained copy of it, and a retained command found on connect is ignored with a warning.
- **Logged.** Every applied command is logged at INFO with gateway, control, value and connection; rejected or failed commands at WARNING.
- **Bits off.** Turning a bit off (or controls off) removes the entity from Home Assistant on the next start.
- **Hybrid site.** If the Tesla account has more than one site, set `PW_SITEID` (or pick the site with `pypowerwall setup`); otherwise the hybrid cloud connection could point at another site, so MQTT controls don't use it.

### Securing the broker (required for controls)

`PW_CONTROL_SECRET` is never sent over MQTT: anyone who can publish to `{prefix}/+/control/#` can operate every enabled control. The server checks that it connects with `MQTT_USERNAME`/`MQTT_PASSWORD`, but it can't see whether the broker rejects anonymous clients or limits who may publish there. The broker has to do both. A Mosquitto example:

```conf
# mosquitto.conf
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
```

```conf
# acl: pypowerwall-server publishes everything, reads commands and clears them;
# Home Assistant reads state and sends commands; nobody else touches control topics.
# Replace pypowerwall with your MQTT_TOPIC_PREFIX if you changed it.
user pypowerwall
topic readwrite pypowerwall/#
topic write homeassistant/#

user homeassistant
topic read pypowerwall/#
topic write pypowerwall/+/control/+/set
topic readwrite homeassistant/#
```

Islanding (bit `16`) opens the grid contactor, so it needs its own bit: `MQTT_CONTROLS=15` enables everything else.

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

### Remote meter topics (Tesla wireless CT meters)

Published when the gateway has one or more Tesla Remote Meters configured
(config.json meter type `trm_mb`) — a wireless CT meter, distinct from the
solar strings above. `{din}` is the meter's own device identifier; `{n}` is
the CT index (a meter can report more than one CT, and a gateway can have
more than one meter):

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}/voltage` | `122.68` | `V` |
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}/current` | `0.95` | `A` |
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}/power` | `158.3` | `W` |
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}/energy_imported` | `12074` | `Wh` (lifetime, whole Wh, converted from Tesla's watt-seconds) |
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}/energy_exported` | `48` | `Wh` (lifetime, whole Wh, converted from Tesla's watt-seconds) |
| `pypowerwall/{gw}/meters/remote/{din}/ct{n}` | `{"InstVoltage": ..., "InstCurrent": ..., "InstRealPower": ..., "Location": "solar", ...}` | JSON |

Sourced from `pw.vitals()`'s `TRM--{din}` blocks — requires pypowerwall
≥ 0.18.2 in TEDAPI modes (Basic LAN skips vitals) and a gateway with at least
one remote meter configured; silently absent otherwise, same as solar strings.

### Per-unit Powerwall temperature and fan topics

One set of topics per physical Powerwall unit, keyed by that unit's serial
number (`{serial}` — the same units as the web console's Powerwall Status
table). Each unit publishes only the signals it actually reports: Powerwall 3
units (and their expansion packs) carry the temperature topics and fans
A/B, Powerwall 2/+ units carry the thermal-controller temperature and the
single fan (rpm + target). Rounding follows the signal registry (whole rpm,
one decimal elsewhere):

| Topic | Value | Unit |
|-------|-------|------|
| `pypowerwall/{gw}/devices/{serial}/temperature/pack_max` | `23.4` | `°C` (PW3 battery pack max) |
| `pypowerwall/{gw}/devices/{serial}/temperature/pack_min` | `22.1` | `°C` (PW3 battery pack min) |
| `pypowerwall/{gw}/devices/{serial}/temperature/shunt` | `24.0` | `°C` (PW3 shunt) |
| `pypowerwall/{gw}/devices/{serial}/temperature/ambient` | `31.2` | `°C` (PW3 inverter enclosure) |
| `pypowerwall/{gw}/devices/{serial}/temperature/controller` | `21.5` | `°C` (PW2/+ thermal controller) |
| `pypowerwall/{gw}/devices/{serial}/fan/a/rpm` | `1200` | `rpm` (PW3 fan A) |
| `pypowerwall/{gw}/devices/{serial}/fan/a/duty` | `35.5` | `%` (PW3 fan A duty) |
| `pypowerwall/{gw}/devices/{serial}/fan/b/rpm` | `1180` | `rpm` (PW3 fan B) |
| `pypowerwall/{gw}/devices/{serial}/fan/b/duty` | `33.2` | `%` (PW3 fan B duty) |
| `pypowerwall/{gw}/devices/{serial}/fan/rpm` | `810` | `rpm` (PW2/+ fan) |
| `pypowerwall/{gw}/devices/{serial}/fan/target_rpm` | `900` | `rpm` (PW2/+ fan target) |
| `pypowerwall/{gw}/devices/{serial}` | `{"pack_temp_max": 23.4, ...}` | JSON (full per-unit set) |

Sourced from the existing `pw.vitals()` poll plus the `get_fan_speeds()`
cache — no new gateway calls. Available in TEDAPI modes (Basic LAN skips
vitals); absent in cloud-only mode, and silently absent per-signal when a unit
doesn't report it.

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
| Battery Raw | — | `%` | `mdi:battery-medium` |
| Solar Power | `power` | `W` | `mdi:solar-power` |
| Grid Power | `power` | `W` | `mdi:transmission-tower` |
| Home Power | `power` | `W` | `mdi:home-lightning-bolt` |
| Powerwall Power | `power` | `W` | `mdi:battery-charging` |
| Grid Status | — | — | `mdi:transmission-tower` |
| Operation Mode | — | — | `mdi:cog` |
| Backup Reserve | — | `%` | `mdi:battery-lock` |
| Total Battery Capacity | `energy_storage` | `Wh` | `mdi:battery-high` |
| Current Battery Charge | `energy_storage` | `Wh` | `mdi:battery-medium` |
| Grid Energy Imported | `energy` | `Wh` | `mdi:transmission-tower-import` |
| Grid Energy Exported | `energy` | `Wh` | `mdi:transmission-tower-export` |
| Home Energy Consumption | `energy` | `Wh` | `mdi:home-lightning-bolt` |
| Solar Energy Production | `energy` | `Wh` | `mdi:solar-power` |
| Battery Energy Charged | `energy` | `Wh` | `mdi:battery-charging` |
| Battery Energy Discharged | `energy` | `Wh` | `mdi:battery-minus` |
| Grid Export | — | — | `mdi:transmission-tower-export` |
| Time Remaining | `duration` | `h` | `mdi:timer-outline` |

Binary sensors:
| Sensor | HA device_class |
|--------|----------------|
| Gateway Online | `connectivity` |
| Grid Connected | `connectivity` |
| Grid Charging | — |

Controls (opt-in `MQTT_CONTROLS`, use at your own risk; only enabled bits the gateway can run are announced):
| Entity | Bit | HA type | Options / Range | Icon |
|--------|-----|---------|-----------------|------|
| Backup Reserve Control | `1` | `number` | `0-100 %` `step 1` | `mdi:battery-lock` |
| Hold Battery | `1` | `button` | `{}` presses `.../control/reserve_hold/set` | `mdi:battery-lock` |
| Operation Mode Control | `2` | `select` | `self_consumption`, `backup`, `autonomous` | `mdi:cog` |
| Grid Charging Control | `4` | `switch` | `ON` `{"value":true}` / `OFF` `{"value":false}` | `mdi:battery-charging-outline` |
| Grid Export Control | `8` | `select` | `battery_ok`, `pv_only`, `never` | `mdi:transmission-tower-export` |
| Go Off Grid | `16` | `button` | `{"action":"off_grid","confirm":true}`, v1r only | `mdi:transmission-tower-off` |
| Reconnect Grid | `16` | `button` | `{"action":"on_grid","confirm":true}`, v1r only | `mdi:transmission-tower` |

Remote meter sensors (one set of five per CT, `entity_category: diagnostic`,
named e.g. `Remote Meter EM…B10BC CT0 (solar) Voltage`, unique ID
`pypowerwall_{gw}_remote_meter_{din_slug}_ct{n}_{metric}` where `din_slug` is
the DIN lower-cased with non-alphanumerics replaced by `_`):
| Sensor | HA device_class | Unit | state_class |
|--------|----------------|------|-------------|
| Voltage | `voltage` | `V` | `measurement` |
| Current | `current` | `A` | `measurement` |
| Power | `power` | `W` | `measurement` |
| Energy Imported | `energy` | `Wh` | `total_increasing` |
| Energy Exported | `energy` | `Wh` | `total_increasing` |

Solar-string and remote-meter sensors are discovered when a poll first
reports them, including on a later poll if the first one didn't.

Per-unit temperature/fan sensors (`entity_category: diagnostic`,
`state_class: measurement`, temperature sensors carry HA `device_class:
temperature`), named e.g. `Powerwall TG2312H0001 Pack temp (max)`, unique ID
`pypowerwall_{gw}_device_{serial_slug}_{metric_id}` where `serial_slug` is the
unit serial lower-cased (e.g. `pypowerwall_default_device_tg2312h0001_pack_temp_max`;
a serial that isn't plain upper-case alphanumeric is slugged with `_` and gets
a short hash of the exact serial appended, so two units never share an
entity) and `metric_id` is one of `pack_temp_max`, `pack_temp_min`,
`shunt_temp`, `inverter_ambient`, `controller_ambient`, `fan_a_rpm`,
`fan_b_rpm`, `fan_a_duty`, `fan_b_duty`, `fan_rpm`, `fan_target_rpm` — the
canonical ids from `app/core/signals.py`, frozen once released. Like
strings and remote meters, each unit's sensors are discovered when a poll
first reports them, including on a later poll.

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
- With `MQTT_CONTROLS=0` (the default) the server subscribes to nothing and MQTT is publish-only. Turning controls on is at your own risk: misuse or abuse can cause power outages or damage (see the warning under *Control command topics*). With controls on, the broker is the trust boundary: it must reject anonymous clients and restrict `{prefix}/+/control/#` (see *Securing the broker*). HTTP `POST /control/*` keeps its `Bearer <PW_CONTROL_SECRET>` check.


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
pypowerwall/default/grid_connected   true
pypowerwall/default/grid_charging    true
pypowerwall/default/grid_export      battery_ok
pypowerwall/default/time_remaining   5.50
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
