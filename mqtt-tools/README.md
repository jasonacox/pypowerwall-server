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
> fine for testing. For production see Option B.

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
pypowerwall/default/grid_status → UP
pypowerwall/default/mode        → self_consumption
pypowerwall/default/online      → true
pypowerwall/default/availability → online
```

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
INFO  MQTT HA discovery published for gateway 'default' (10 entities)
```

#### Step 3 - Find the device in Home Assistant

- Go to **Settings → Devices & Services → MQTT → Devices**.
- Look for a device named after your gateway (e.g. "Home Powerwall").
- All 10 entities appear grouped on the device card:

  | Entity | Device Class | Unit |
  |--------|-------------|------|
  | Battery | battery | % |
  | Solar Power | power | W |
  | Grid Power | power | W |
  | Home Load | power | W |
  | Powerwall Power | power | W |
  | Backup Reserve | - | % |
  | Grid Status | - | text |
  | Operation Mode | - | text |
  | Firmware Version | diagnostic | text |
  | Gateway Online | connectivity | binary |

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

#### Troubleshooting

| Problem | Solution |
|---------|----------|
| Device doesn't appear in HA | Check broker logs; confirm `MQTT_HA_DISCOVERY=true`; restart pypowerwall-server |
| Entities show "unavailable" | Check the `availability` topic; gateway may be offline |
| Wrong entity names | The gateway `name` field in `gateways.yaml` is used as the device name |
| Duplicate devices | Delete old MQTT devices in HA and restart pypowerwall-server to re-publish discovery |
| Energy dashboard missing kWh | Create Riemann Sum helpers as described in Step 4 above |

---

## 7. Security Notes

- **Do not expose port 1883 to the internet.** Use a VPN or SSH tunnel for remote access.
- For LAN deployments with authentication, use `MQTT_USERNAME` / `MQTT_PASSWORD`.
- For TLS, set `MQTT_TLS=true` and provide a CA cert via `MQTT_TLS_CA_CERT`.
  Many home users run Mosquitto with a self-signed certificate; set
  `MQTT_TLS_INSECURE=true` only for testing on a trusted LAN.
- Passwords are passed via environment variables only - never hard-coded in
  source files.
