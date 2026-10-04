# pypowerwall-server MQTT Tools

This folder contains tooling to help you set up and use the MQTT integration
built into pypowerwall-server.

---

## Contents

| File | Purpose |
|------|---------|
| `monitor.py` | Live Python/tkinter GUI - connects to your broker and displays real-time Powerwall data |

---

## 1. Setting Up an MQTT Broker

pypowerwall-server can publish to **any** MQTT broker.
The simplest option for home use is **Mosquitto**, the reference open-source broker.

### Option A - Docker (recommended, one command)

```bash
# Run a basic Mosquitto broker on port 1883
docker run -d \
  --name mosquitto \
  --restart unless-stopped \
  -p 1883:1883 \
  -p 9001:9001 \
  eclipse-mosquitto
```

> **Note:** The default Docker image starts with no persistent config, which is
> fine for testing. For production see Option B. Never use an open broker like
> this one with Home Assistant controls (`MQTT_CONTROLS`) turned on.

### Option B - Docker with authentication and persistence

1. Create a config folder:

   ```bash
   mkdir -p ~/mosquitto/{config,data,log}
   ```

2. Create `~/mosquitto/config/mosquitto.conf`:

   ```
   # Allow connections on standard port
   listener 1883
   protocol mqtt

   # Require password authentication
   allow_anonymous false
   password_file /mosquitto/config/passwd

   # Persistence
   persistence true
   persistence_location /mosquitto/data/

   # Logging
   log_dest file /mosquitto/log/mosquitto.log
   log_type all
   ```

3. Create a user account (replace `mqttuser` / `mqttpassword`):

   ```bash
   docker run --rm -v ~/mosquitto/config:/mosquitto/config \
     eclipse-mosquitto \
     mosquitto_passwd -c /mosquitto/config/passwd mqttuser
   # Enter password when prompted
   ```

4. Start the broker:

   ```bash
   docker run -d \
     --name mosquitto \
     --restart unless-stopped \
     -p 1883:1883 \
     -v ~/mosquitto/config:/mosquitto/config \
     -v ~/mosquitto/data:/mosquitto/data \
     -v ~/mosquitto/log:/mosquitto/log \
     eclipse-mosquitto
   ```

### Option C - Native install (Debian / Ubuntu / Raspberry Pi OS)

```bash
sudo apt update && sudo apt install -y mosquitto mosquitto-clients

# Enable and start
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Optional: create a user
sudo mosquitto_passwd -c /etc/mosquitto/passwd mqttuser
# Add to /etc/mosquitto/mosquitto.conf:
#   allow_anonymous false
#   password_file /etc/mosquitto/passwd
sudo systemctl restart mosquitto
```

### Option D - macOS (Homebrew)

```bash
brew install mosquitto
brew services start mosquitto
# Config file: /opt/homebrew/etc/mosquitto/mosquitto.conf
```

### Verify the broker works

```bash
# Subscribe to all topics in one terminal
mosquitto_sub -h localhost -t '#' -v

# Publish a test message in another terminal
mosquitto_pub -h localhost -t test/hello -m "world"
```

---

## 2. Configuring pypowerwall-server for MQTT

Set environment variables before starting the server.  The only **required**
variable is `MQTT_HOST`:

```bash
export MQTT_HOST=192.168.1.100       # Your broker's IP or hostname
export MQTT_PORT=1883                # Default: 1883
export MQTT_USERNAME=mqttuser        # Optional
export MQTT_PASSWORD=mqttpassword    # Optional
export MQTT_TOPIC_PREFIX=pypowerwall # Default: pypowerwall
```

Or add them to `docker-compose.yml` (see the commented-out block in the root
`docker-compose.yml`):

```yaml
services:
  pypowerwall-server:
    environment:
      - MQTT_HOST=192.168.1.100
      - MQTT_PORT=1883
      - MQTT_USERNAME=mqttuser
      - MQTT_PASSWORD=mqttpassword
      - MQTT_HA_DISCOVERY=true      # Auto-configure Home Assistant sensors
```

### Full variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | *(none)* | Broker hostname/IP. **Required to enable MQTT.** |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` | *(none)* | Username for authentication |
| `MQTT_PASSWORD` | *(none)* | Password for authentication |
| `MQTT_TLS` | `false` | Enable TLS/SSL |
| `MQTT_TLS_CA_CERT` | *(none)* | Path to CA certificate file |
| `MQTT_TLS_INSECURE` | `false` | Disable certificate verification (testing only) |
| `MQTT_TOPIC_PREFIX` | `pypowerwall` | Root topic prefix |
| `MQTT_RETAIN` | `true` | Retain messages on broker |
| `MQTT_QOS` | `1` | MQTT QoS level (0, 1, or 2) |
| `MQTT_HA_DISCOVERY` | `true` | Publish Home Assistant auto-discovery payloads |
| `MQTT_HA_PREFIX` | `homeassistant` | Home Assistant discovery prefix |
| `MQTT_CLIENT_ID` | `pypowerwall-server` | MQTT client identifier |
| `MQTT_KEEPALIVE` | `60` | Connection keepalive in seconds |
| `MQTT_CONTROLS` | `0` | Opt-in Home Assistant controls: `1` reserve, `2` mode, `4` grid charging, `8` grid export, `16` go off grid / reconnect (v1r only). Add them up, e.g. `15`. `0` = monitoring only. **Use at your own risk:** read the warning in [Controls](#controls-optional) first |

---

## 3. Topic Layout

All topics are published under `{MQTT_TOPIC_PREFIX}/{gateway_id}/`:

| Topic suffix | Type | Example value | Notes |
|---|---|---|---|
| `battery` | float | `75.3` | State of Energy % |
| `solar` | float | `3120.0` | Solar power W (positive = producing) |
| `grid` | float | `-400.0` | Grid power W (negative = exporting) |
| `home` | float | `2720.0` | Home load W |
| `powerwall` | float | `0.0` | Powerwall power W (positive = discharging) |
| `reserve` | float | `20.0` | Backup reserve % |
| `total_capacity` | int | `13500` | Total battery capacity Wh |
| `current_charge` | int | `11547` | Current battery charge Wh |
| `grid_status` | string | `UP` | `UP`, `DOWN`, or `unknown` |
| `mode` | string | `self_consumption` | Operation mode |
| `version` | string | `23.44.0` | Powerwall firmware version |
| `online` | string | `true` | Gateway connection status |
| `aggregates` | JSON | `{...}` | Full aggregates dict |
| `status` | JSON | `{...}` | Summary JSON (all scalar fields) |
| `availability` | string | `online` | LWT topic - `online` or `offline` |

**Example** (single gateway named `default`):
```
pypowerwall/default/battery     → 75.3
pypowerwall/default/solar       → 3120.0
pypowerwall/default/grid        → -400.0
pypowerwall/default/home        → 2720.0
pypowerwall/default/powerwall   → 0.0
pypowerwall/default/reserve     → 20.0
pypowerwall/default/total_capacity → 13500
pypowerwall/default/current_charge → 11547
pypowerwall/default/grid_status → UP
pypowerwall/default/mode        → self_consumption
pypowerwall/default/online      → true
pypowerwall/default/availability → online
```

### Per-unit temperatures and fan speeds

Powerwall temperature and fan speed readings are published per physical unit,
keyed by that unit's serial number (the same units as the web console's
Powerwall Status table). These come from gateway vitals, so they are
available in TEDAPI modes (Basic LAN skips vitals) — not in cloud-only
mode. Only the signals each unit reports are published (a Powerwall 2 unit
has fan rpm but no duty cycle; an expansion pack has pack temps but no
fans):

| Topic suffix | Type | Example value | Notes |
|---|---|---|---|
| `devices/{serial}/temperature/pack_max` | float | `23.5` | Battery pack max temp °C (PW3) |
| `devices/{serial}/temperature/pack_min` | float | `22.1` | Battery pack min temp °C (PW3) |
| `devices/{serial}/temperature/shunt` | float | `24.0` | Shunt temp °C (PW3) |
| `devices/{serial}/temperature/ambient` | float | `31.2` | Inverter ambient temp °C (PW3) |
| `devices/{serial}/temperature/controller` | float | `25.0` | Thermal controller temp °C (PW2/2+) |
| `devices/{serial}/fan/a/rpm` | int | `1200` | Fan A measured rpm (PW3) |
| `devices/{serial}/fan/a/duty` | float | `35.5` | Fan A duty cycle % (PW3) |
| `devices/{serial}/fan/b/rpm` | int | `1180` | Fan B measured rpm (PW3) |
| `devices/{serial}/fan/b/duty` | float | `33.2` | Fan B duty cycle % (PW3) |
| `devices/{serial}/fan/rpm` | int | `810` | Fan measured rpm (PW2/2+) |
| `devices/{serial}/fan/target_rpm` | int | `900` | Fan target rpm (PW2/2+) |
| `devices/{serial}` | JSON | `{...}` | All signals for that unit |

**Example**:
```
pypowerwall/default/devices/TG2312H0001/temperature/pack_max → 23.5
pypowerwall/default/devices/TG2312H0001/fan/a/rpm            → 1200
pypowerwall/default/devices/TG123456789/fan/rpm              → 810
```

When `MQTT_HA_DISCOVERY` is enabled these become Home Assistant sensors
(diagnostics, grouped under the gateway's device) — e.g. *Powerwall
TG2312H0001 Pack temp (max)* — ready for automations and history charts.

---

## 4. Command-Line Monitoring

The quickest way to watch live updates is with `mosquitto_sub` (part of the
`mosquitto-clients` package):

```bash
# Watch all pypowerwall topics
mosquitto_sub -h localhost -t 'pypowerwall/#' -v

# Watch a specific gateway
mosquitto_sub -h localhost -t 'pypowerwall/default/#' -v

# Watch a single sensor
mosquitto_sub -h localhost -t 'pypowerwall/default/battery' -v

# With authentication
mosquitto_sub -h 192.168.1.100 -u mqttuser -P mqttpassword -t 'pypowerwall/#' -v
```

Each line of output shows the topic followed by its current value:
```
pypowerwall/default/battery 75.3
pypowerwall/default/solar 3120.0
pypowerwall/default/grid -400.0
pypowerwall/default/availability online
```

---

## 5. Monitor GUI

`monitor.py` is a zero-dependency (only `paho-mqtt` and the standard-library
`tkinter`) desktop application that connects to your broker and shows live
Powerwall readings.

### Install

```bash
pip install paho-mqtt
```

`tkinter` is included with the standard Python installer on Windows.
On macOS and Linux it may need to be installed separately:

```bash
# macOS - Homebrew Python (match your Python version)
brew install python-tk@3.13

# Debian/Ubuntu/Raspberry Pi OS
sudo apt install python3-tk

# Fedora/RHEL
sudo dnf install python3-tkinter
```

### Run

```bash
python monitor.py                              # broker at localhost:1883
python monitor.py --host 192.168.1.100         # custom broker host
python monitor.py --host broker.local --port 8883 --username user --password pass
python monitor.py --prefix myhome             # custom topic prefix
```

The window automatically discovers all gateways present on the broker and
creates one card per gateway. Values update in real time as the server
publishes new telemetry (default: every 5 seconds).

### Screenshot

<img width="792" height="564" alt="pyPowerwall MQTT Monitor" src="https://github.com/user-attachments/assets/abb4bfbc-8c91-4e72-bd33-abf397cc5acf" />

---

## 6. Home Assistant Integration

When `MQTT_HA_DISCOVERY=true` (the default), pypowerwall-server automatically
publishes MQTT discovery payloads the first time each gateway connects. Home
Assistant reads these and creates a full device entity with no manual YAML
required.

### Prerequisites

1. **MQTT integration** must be installed in Home Assistant.
2. Home Assistant and pypowerwall-server must point to the **same broker**.

### Step-by-step setup

#### Step 1 - Install the HA MQTT integration

- In HA go to **Settings → Devices & Services → Add Integration**.
- Search for **MQTT** and select it.
- Enter your broker's IP, port, and credentials.
- Click **Submit**. HA will confirm the connection.

#### Step 2 - Connect pypowerwall-server to the same broker

Ensure `MQTT_HOST` is set (and `MQTT_HA_DISCOVERY=true`, which is the default).

When the server starts you will see in its log:

```
INFO  MQTT connected to 192.168.1.100:1883
INFO  MQTT HA discovery published for gateway 'default' (23 entities)
```

#### Step 3 - Find the device in Home Assistant

- Go to **Settings → Devices & Services → MQTT → Devices**.
- Look for a device named after your gateway (e.g. "Home Powerwall").
- All 23 base entities appear grouped on the device card (solar string and
  Tesla Remote Meter sensors are added when the gateway reports them):

  | Entity | Device Class | Unit |
  |--------|-------------|------|
  | Battery | battery | % |
  | Battery Raw | diagnostic | % |
  | Solar Power | power | W |
  | Grid Power | power | W |
  | Home Load | power | W |
  | Powerwall Power | power | W |
  | Backup Reserve | - | % |
  | Total Battery Capacity | energy_storage | Wh |
  | Current Battery Charge | energy_storage | Wh |
  | Grid Energy Imported/Exported | energy | Wh |
  | Home/Solar/Battery Lifetime Energy | energy | Wh |
  | Grid Status | - | text |
  | Operation Mode | - | text |
  | Firmware Version | diagnostic | text |
  | Grid Export | - | text |
  | Time Remaining | duration | h |
  | Gateway Online | connectivity | binary |
  | Grid Connected | connectivity | binary |
  | Grid Charging | - | binary |

#### Step 4 - Add to an Energy Dashboard

HA's Energy Dashboard requires **energy sensors** (kWh), not power sensors (W).
Use a Riemann Sum helper to integrate power into energy:

1. **Settings → Devices & Services → Helpers → Create Helper → Riemann Sum Integral**.
2. Settings:
   - **Input sensor**: `sensor.battery_solar_power` (or whichever power sensor)
   - **Integration method**: Left Riemann sum (or trapezoidal)
   - **Unit time**: hours  → produces kWh
3. Add the resulting energy sensors to **Settings → Energy → Solar production**,
   **Grid consumption**, etc.

#### Example Lovelace card (YAML)

Paste into a manual card in your dashboard:

```yaml
type: entities
title: Powerwall
entities:
  - entity: sensor.home_powerwall_battery
    name: Battery
  - entity: sensor.home_powerwall_solar_power
    name: Solar
  - entity: sensor.home_powerwall_grid_power
    name: Grid
  - entity: sensor.home_powerwall_home_load
    name: Home
  - entity: sensor.home_powerwall_powerwall_power
    name: Powerwall
  - entity: sensor.home_powerwall_backup_reserve
    name: Reserve
  - entity: sensor.home_powerwall_grid_status
    name: Grid Status
  - entity: sensor.home_powerwall_operation_mode
    name: Mode
  - entity: binary_sensor.home_powerwall_gateway_online
    name: Online
```

> **Tip:** Entity IDs follow the pattern `sensor.{gateway_name}_{sensor_name}`.
> If your gateway is named "Home Powerwall" and the sensor is "Battery", the
> entity ID is `sensor.home_powerwall_battery`. Adjust accordingly.

#### Automations

**Notify when grid goes down:**

```yaml
alias: "Powerwall: Grid Outage Alert"
trigger:
  - platform: state
    entity_id: sensor.home_powerwall_grid_status
    to: "DOWN"
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "⚡ Grid Outage"
      message: "Grid is DOWN - Powerwall is running on battery."
```

**Alert when battery is low:**

```yaml
alias: "Powerwall: Low Battery Warning"
trigger:
  - platform: numeric_state
    entity_id: sensor.home_powerwall_battery
    below: 15
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "🔋 Low Battery"
      message: "Powerwall battery is below 15%."
```

#### Controls (optional)

> **⚠️ WARNING: USE AT YOUR OWN RISK**
>
> MQTT controls let anything allowed to publish to the control topics on your MQTT broker (`{MQTT_TOPIC_PREFIX}/+/control/+/set`; on a broker without an ACL, that's every client) change how your Powerwall runs: the backup reserve, the operating mode, grid charging and grid export, and (with `16`) disconnecting your home from the grid. A misconfigured or compromised broker, a hacked smart-home device, a buggy automation or a simple mistake could:
>
> - cause a **power outage** in your home,
> - leave you **without backup power** when the grid goes down (for example, a reserve set to 0),
> - **damage** equipment or appliances, or
> - raise your energy costs or conflict with your utility agreement.
>
> This software is provided "as is", without warranty of any kind (see the [MIT license](../LICENSE)), and is not made or supported by Tesla. **By setting `MQTT_CONTROLS` to anything other than `0`, you acknowledge these risks and accept full responsibility for the results.** Think twice before turning this on: enable only the controls you need, leave going off grid (`16`) off unless you truly need it, and secure your broker first.

pypowerwall-server can also take commands from Home Assistant: backup reserve, operating mode, grid charging, grid export, and going off grid / reconnecting. They are off by default. To turn them on:

1. Secure the broker: no anonymous clients, and only Home Assistant and pypowerwall-server itself may publish to `{MQTT_TOPIC_PREFIX}/+/control/+/set` (default prefix `pypowerwall`). pypowerwall-server needs that write access to clear retained commands. [MQTT.md](../MQTT.md#securing-the-broker-required-for-controls) has a Mosquitto example.
2. Give pypowerwall-server its own broker login (`MQTT_USERNAME` / `MQTT_PASSWORD`) and set `PW_CONTROL_SECRET`.
3. Set `MQTT_CONTROLS`, e.g. `15` for everything except going off grid, and restart.

The controls appear on the Powerwall device: **Backup Reserve Control**, **Operation Mode Control**, **Grid Charging Control**, **Grid Export Control**, and with `16` on a TEDAPI v1r connection the **Go Off Grid** / **Reconnect Grid** buttons. Only controls your connection can run are shown. Every command is logged by pypowerwall-server with the gateway, value and connection used. Example automation:

```yaml
alias: "Powerwall: Raise reserve before a storm"
trigger:
  - platform: state
    entity_id: binary_sensor.storm_warning
    to: "on"
action:
  - service: number.set_value
    target:
      entity_id: number.home_powerwall_backup_reserve_control
    data:
      value: 80
```

#### Troubleshooting

| Problem | Solution |
|---------|----------|
| Device doesn't appear in HA | Check broker logs; confirm `MQTT_HA_DISCOVERY=true`; restart pypowerwall-server |
| Entities show "unavailable" | Check the `availability` topic; gateway may be offline |
| Wrong entity names | The gateway `name` field in `gateways.yaml` is used as the device name |
| Duplicate devices | Delete old MQTT devices in HA and restart pypowerwall-server to re-publish discovery |
| Energy dashboard missing kWh | Create Riemann Sum helpers as described in Step 4 above |
| Control entities missing | Check the pypowerwall-server log: it names any missing setting (`MQTT_USERNAME`, `MQTT_PASSWORD`, `PW_CONTROL_SECRET`), and only controls your connection can run are shown |
| A control does nothing | Check the pypowerwall-server log: `MQTT broker refused the subscription ...` means the broker ACL doesn't let pypowerwall-server read the control topics; `MQTT control ... rejected` or `failed` says why a single command didn't run (e.g. a value out of range) |

---

## 7. Security Notes

- **Do not expose port 1883 to the internet.** Use a VPN or SSH tunnel for remote access.
- For LAN deployments with authentication, use `MQTT_USERNAME` / `MQTT_PASSWORD`.
- **MQTT controls are at your own risk.** Misuse or abuse can cause power outages or damage;
  see the warning in [Controls](#controls-optional).
- **With controls on (`MQTT_CONTROLS`), the broker is the lock.** Anyone who can publish to
  `{MQTT_TOPIC_PREFIX}/+/control/#` (default `pypowerwall/+/control/#`) can change your Powerwall settings, so disable anonymous access and
  restrict those topics with an ACL ([example](../MQTT.md#securing-the-broker-required-for-controls)).
- For TLS, set `MQTT_TLS=true` and provide a CA cert via `MQTT_TLS_CA_CERT`.
  Many home users run Mosquitto with a self-signed certificate; set
  `MQTT_TLS_INSECURE=true` only for testing on a trusted LAN.
- Passwords are passed via environment variables only - never hard-coded in
  source files.
