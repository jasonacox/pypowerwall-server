# PyPowerwall Server API Reference

This is the reference for every pypowerwall-server setting and HTTP/WebSocket
route. For installation and quick start see [README.md](README.md); for MQTT
topics and payloads see [MQTT.md](MQTT.md).

Jump to: [Configuration](#configuration) | [Routes](#routes) |
[Control and security](#control-and-security) | [Errors and safe defaults](#errors-and-safe-defaults)

**Base URL:** `http://<host>:8675/` (`PW_PORT`). Behind a reverse proxy with
`PROXY_BASE_URL=/pypowerwall`, every route below is also served under that
prefix. Interactive docs generated from the code are at `/docs` (Swagger UI)
and `/redoc`.

---

## Configuration

Settings come from environment variables. Names are case-insensitive, except
`PW_CONFIG` and `PW_GATEWAYS`, which must be uppercase. Command
line flags (`pypowerwall-server --help`) override the matching variables.
Booleans accept `yes`/`no`, `true`/`false`, `1`/`0` or `on`/`off`. For boolean
and integer settings, surrounding spaces and an inline ` # comment` are ignored,
an empty value means the default, and an unrecognized value logs a warning and
uses the default, so a proxy `pypowerwall.env` can't stop the server from
starting. String settings, such as passwords, are used exactly as given.

`/stats` echoes many of the effective settings under `config`, with passwords,
the control secret, the email, the site id and paths masked.

**Proxy column:** ✓ means the variable has the same name as in the legacy
[pypowerwall proxy](https://github.com/jasonacox/pypowerwall/tree/main/proxy),
so an existing proxy environment can be reused. Differences in meaning or
default are noted in the description.

### Connection modes

The mode is chosen per gateway from the settings it has:

| Mode | Settings | Notes |
|------|----------|-------|
| TEDAPI | `PW_HOST` + `PW_GW_PWD` | Local data over the gateway WiFi (normally `192.168.91.1`). Read-only unless hybrid. |
| TEDAPI v1r | `PW_HOST` + `PW_GW_PWD` + `PW_RSA_KEY_PATH` | Local data over the wired LAN with a registered RSA key. Can write settings and island locally. |
| Local | `PW_HOST` + `PW_PASSWORD`, no gateway password or RSA key | Powerwall 2 local API, like the proxy's local mode. Detected on connect: the gateway answers `/api/status`. |
| Basic LAN | `PW_HOST` + `PW_PASSWORD`, no gateway password or RSA key | Powerwall 3 wired LAN, detected on connect when `/api/status` isn't served. Core metrics only: aggregates, battery level, grid status. |
| Cloud | `PW_EMAIL` without `PW_HOST`, or `.pypowerwall.auth` in `PW_AUTH_PATH` (its account fills a missing `PW_EMAIL`) | Tesla cloud. Run `pypowerwall-server --setup` once to create the auth files. |
| FleetAPI | `.pypowerwall.fleetapi` in `PW_AUTH_PATH` without `PW_HOST` (picked before Cloud, as the proxy does), or `fleetapi: true` on a gateway in `PW_GATEWAYS` or the config file | Tesla Fleet API. |
| Hybrid | Any local mode plus `PW_EMAIL`, and `PW_CONTROL_SECRET` or `PW_CLOUD_HYBRID=yes` | Reads stay local; control writes use a Tesla cloud connection, which also fills values the local API lacks (Basic LAN mode and reserve, grid charging/export settings). |

In the tables, **Local** means TEDAPI, TEDAPI v1r, the Powerwall 2 local API and Basic LAN.

### Gateway configuration sources

The server uses the first of these that is set:

1. **Config file:** `PW_CONFIG=/path/to/gateways.yaml` (or `--config`). YAML or
   JSON with an optional `server` block (`host`, `port`, `cors_origins`) and a
   `gateways` list. If the file is set but unreadable, no gateways are
   configured (the server never falls back to another source). See
   [gateways.yaml.example](gateways.yaml.example).
2. **`PW_GATEWAYS`:** a JSON array of gateway objects. Invalid entries are
   skipped and logged; invalid JSON configures no gateways.
3. **Single gateway:** `PW_HOST`, `PW_GW_PWD`, `PW_PASSWORD`,
   `PW_RSA_KEY_PATH`, `PW_WIFI_HOST`, `PW_EMAIL`, `PW_AUTH_PATH` and
   `PW_TIMEZONE` build one gateway with id `default`. Cloud mode is selected
   when `PW_EMAIL` is set without `PW_HOST`.

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_CONFIG` | none | Path to the gateway config file (above). |
| `PW_GATEWAYS` | none | JSON array of gateway objects (above). |

Gateway object fields (`GatewayConfig`), used by `PW_GATEWAYS` and the config
file:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | str | required | Unique id, used in `/api/gateways/{gateway_id}` and MQTT topics. |
| `name` | str | `id` | Display name. |
| `host` | str | none | Gateway or Powerwall IP for local modes. |
| `port` | int | none | Non-standard HTTPS port (1-65535), e.g. a travel router forwarding to `192.168.91.1:443`. |
| `gw_pwd` | str | none | Gateway WiFi password (TEDAPI, v1r). |
| `password` | str | none | Customer password for Basic LAN or PW2 local mode. Falls back to `PW_PASSWORD`. |
| `rsa_key_path` | str | none | RSA-4096 private key PEM for TEDAPI v1r. |
| `wifi_host` | str | none | Gateway WiFi address for v1r follower Powerwalls and LAN fallback. |
| `email` | str | none | Tesla account email (Cloud, FleetAPI, Hybrid). |
| `authpath` | str | none | Directory holding the `.pypowerwall.auth` and `.pypowerwall.site` files. |
| `timezone` | str | `America/Los_Angeles` | Gateway time zone; daily energy totals reset at its midnight. |
| `cloud_mode` | bool | `false` | Use the Tesla cloud. Set automatically when `email` is given without `host`. |
| `fleetapi` | bool | `false` | Use the Tesla Fleet API. |
| `type` | str | `powerwall` | `powerwall`, or `inverter` for a solar-only gateway (the Console hides battery panels). |

```bash
PW_GATEWAYS='[
  {"id": "home", "host": "192.168.91.1", "gw_pwd": "pass1"},
  {"id": "remote", "email": "user@example.com", "authpath": "/auth"}
]'
```

### Server

| Variable | Default | Proxy | Description |
|----------|---------|:-----:|-------------|
| `PW_BIND_ADDRESS` | `0.0.0.0` | ✓ | Address to listen on. Also `server.host` in the config file. |
| `PW_PORT` | `8675` | ✓ | Port to listen on. Also `server.port` in the config file. |

`PW_BIND_ADDRESS` and `PW_PORT` apply when the server is started with the
`pypowerwall-server` command. The Docker image starts uvicorn directly on
`0.0.0.0:8675`, so map a different host port instead.

| Variable | Default | Proxy | Description |
|----------|---------|:-----:|-------------|
| `PROXY_BASE_URL` | `/` | ✓ | Sub-path when served behind a reverse proxy, e.g. `/pypowerwall`. The prefix is stripped from incoming paths and added to URLs in the UI pages. |
| `CORS_ORIGINS` | `["*"]` | | JSON array of allowed CORS origins. `*` allows any origin without credentials; an explicit list allows credentials. Also `server.cors_origins` in the config file. |
| `PW_STYLE` | `clear` | ✓ | Power Flow theme at `/`: `clear`, `black`, `white`, `grafana`, `grafana-dark`, `solar` or `dakboard`. `/?style=` overrides it per request. |

### Connection

| Variable | Default | Applies to | Proxy | Description |
|----------|---------|------------|:-----:|-------------|
| `PW_HOST` | none | Local | ✓ | Gateway or Powerwall IP (single-gateway form). |
| `PW_GW_PWD` | none | TEDAPI, v1r | ✓ | Gateway WiFi password (single-gateway form). |
| `PW_PASSWORD` | none | Basic LAN | ✓ | Customer password for the local API (Powerwall 3 Basic LAN or PW2 local mode), usually the last 5 characters of the gateway password. Also the default `password` for every gateway. |
| `PW_RSA_KEY_PATH` | none | v1r | ✓ | RSA-4096 private key PEM; selects TEDAPI v1r (single-gateway form). |
| `PW_WIFI_HOST` | none | v1r | ✓ | Gateway WiFi address used for follower Powerwalls and as the fallback when the wired LAN fails (single-gateway form). |
| `PW_EMAIL` | none | Cloud, Hybrid | ✓ | Tesla account email (single-gateway form). |
| `PW_AUTH_PATH` | none | Cloud, Hybrid | ✓ | Directory with the Tesla auth files (single-gateway form). Also the default auth directory for the hybrid cloud connection. |
| `PW_SITEID` | none | Cloud, FleetAPI, Hybrid, Local | ✓ | Tesla energy site id on accounts with several sites; picks the site for Cloud and FleetAPI gateways and the site the hybrid cloud connection controls. |
| `PW_TIMEZONE` | `America/Los_Angeles` | All | ✓ | Gateway time zone (single-gateway form). |
| `PW_FAILOVER` | `yes` | All | ✓ | pypowerwall transport failover. `no` pins the configured transports: the v1r leader stays on the wired LAN, failed requests are not rerouted, and a local connection never falls back to Cloud or FleetAPI. |

### Auth and control

| Variable | Default | Applies to | Proxy | Description |
|----------|---------|------------|:-----:|-------------|
| `PW_CONTROL_SECRET` | none | All | ✓ | Enables all write routes; clients send it as the control token (see [Control and security](#control-and-security)). Unset means read-only. The proxy uses the same variable but a different request format. |
| `PW_ISLANDING_COOLDOWN` | `30` | v1r | | Minimum seconds between islanding commands per gateway; `0` disables the cooldown. |
| `PW_CLOUD_HYBRID` | `no` | Local with `PW_EMAIL` | | `yes` starts the hybrid cloud connection without `PW_CONTROL_SECRET`, for its read-only values (Basic LAN mode and reserve, grid charging/export settings). With `PW_CONTROL_SECRET` set it starts anyway. Like the proxy, `PW_EMAIL` alone doesn't start it. |
| `PW_LOCAL_FIRST` | `no` | v1r, Hybrid | | `yes` sends reserve, mode and grid writes over a confirmed local v1r connection first, even when a hybrid cloud connection exists (see [write path](#post-controlpath)). |

`MQTT_CONTROLS` (below) separately enables control over MQTT.

### MQTT

MQTT publishing is on when `MQTT_HOST` is set. Topics, payloads, Home Assistant
discovery and broker security are documented in [MQTT.md](MQTT.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | none | Broker host or IP. Required to enable MQTT. |
| `MQTT_PORT` | `1883` | Broker port. |
| `MQTT_USERNAME` | none | Broker username. |
| `MQTT_PASSWORD` | none | Broker password. |
| `MQTT_TLS` | `false` | Connect with TLS. |
| `MQTT_TLS_CA_CERT` | none | CA certificate path for TLS. |
| `MQTT_TLS_INSECURE` | `false` | Skip broker certificate verification (testing only). |
| `MQTT_TOPIC_PREFIX` | `pypowerwall` | Root topic prefix. |
| `MQTT_RETAIN` | `true` | Publish retained messages. |
| `MQTT_QOS` | `1` | QoS level (0, 1 or 2). |
| `MQTT_HA_DISCOVERY` | `true` | Publish Home Assistant discovery payloads. |
| `MQTT_HA_PREFIX` | `homeassistant` | Home Assistant discovery prefix. |
| `MQTT_CLIENT_ID` | `pypowerwall-server` | MQTT client id. |
| `MQTT_KEEPALIVE` | `60` | Keepalive in seconds. |
| `MQTT_CONTROLS` | `0` | Bitmask of MQTT controls: `1` reserve (and reserve hold), `2` mode, `4` grid charging, `8` grid export, `16` islanding; `31` = all. Anything other than an integer 0-31 means `0` (monitoring only). Controls also need `PW_CONTROL_SECRET`, `MQTT_USERNAME` and `MQTT_PASSWORD`. **Read the warning in [MQTT.md](MQTT.md#control-command-topics-opt-in-mqtt_controls) first.** |

### Time-series and history

Durations accept a number of seconds or a suffix: `90s`, `30m`, `48h`, `7d`,
`2w`.

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_TIMESERIES_RETENTION` | `24h` | Raw power sample retention. `0` = unlimited; `-1` disables the whole subsystem (no SQLite file, no `/history` data). |
| `PW_TIMESERIES_DAILY_RETENTION` | `0` | Retention of daily kWh totals and daily temperature/fan rollups. `0` = unlimited. |
| `PW_TIMESERIES_SIGNAL_RETENTION` | `30d` | Raw Powerwall temperature/fan sample retention. `0` = unlimited; `-1` stops recording them. |
| `PW_TIMESERIES_SIGNAL_INTERVAL` | `60s` | Seconds between temperature/fan samples per gateway; values below `30s` are raised to `30s`. |
| `PW_TIMESERIES_PATH` | `/data/timeseries.db` if `/data` exists, else `data/timeseries.db` | SQLite database path. |

### Logging

| Variable | Default | Proxy | Description |
|----------|---------|:-----:|-------------|
| `PW_DEBUG` | `no` | ✓ | Debug logging, including one access log line per HTTP request. |

### Rate limiting

A per-client-IP fixed-window limit on every HTTP route. Over the limit a
request gets `429 {"detail": "Rate limit exceeded"}`. Behind a reverse proxy all
clients share the proxy's IP.

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_RATE_LIMIT_ENABLED` | `no` | Enable rate limiting. |
| `PW_RATE_LIMIT_MAX_REQUESTS` | `1000` | Requests per window per client IP. |
| `PW_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length in seconds. |
| `PW_RATE_LIMIT_MAX_BUCKETS` | `10000` | Client IPs tracked before stale or oldest entries are pruned. |

### Advanced: polling and resilience

| Variable | Default | Applies to | Proxy | Description |
|----------|---------|------------|:-----:|-------------|
| `PW_CACHE_EXPIRE` | `5` | All | ✓ | Seconds between background polls. Data routes read from this cache. |
| `PW_TIMEOUT` | `10` | Local | ✓ | HTTP timeout in seconds for local gateway requests. |
| `PW_POOL_MAXSIZE` | `15` | Local | ✓ | pypowerwall HTTP connection pool size. |
| `PW_CACHE_FILE` | `PW_AUTH_PATH/.powerwall`, else `/tmp/.powerwall` | Local | ✓ | pypowerwall auth cache file. |
| `PW_GRACEFUL_DEGRADATION` | `yes` | All | ✓ | While a gateway is offline, keep serving its last good data for up to `PW_CACHE_TTL` seconds. |
| `PW_CACHE_TTL` | `30` | All | ✓ | Maximum age in seconds of the last good data served while offline; after that, data routes return their empty defaults. |
| `PW_TEDAPI_RECOVERY` | `yes` | Local | ✓ | Probe local gateways and reconnect when the connection drops to SolarOnly fallback. State shows in `/health` and `/stats`. |
| `PW_TEDAPI_PROBE_INTERVAL` | `30` | Local | ✓ | Seconds between those probes. |
| `PW_NEG_SOLAR` | `yes` | All | ✓ | Allow negative solar power, as in the proxy. With `no`, negative solar is set to 0 and added to home load. |

### Accepted but not used

These proxy variables are accepted (and some echoed in `/stats`) so an existing
proxy environment starts cleanly, but they change nothing in the server:
`PW_BROWSER_CACHE` (default `0`), `PW_HTTPS` (`no`; the proxy's `http` is accepted), `PW_AUTH_MODE` (`cookie`),
`PW_SUPPRESS_NETWORK_ERRORS` (`no`), `PW_NETWORK_ERROR_RATE_LIMIT` (`5`),
`PW_FAIL_FAST` (`no`) and `PW_HEALTH_CHECK` (`yes`).

The server does not read the proxy's `PW_FIRMWARE_CHECK_INTERVAL`,
`PW_SITE_ZERO_THRESHOLD`, `PW_TEDAPI_API_VERSION` or `PW_TEDAPI_AUTH_MODE`.

---

## Routes

All routes return JSON unless noted. **Auth** is blank for open routes and
**token** for routes that need the control token. Legacy, `/pw` and control
routes serve the **default gateway**: the one with id `default`, otherwise the
first configured. Unless marked on demand, data comes from the poll cache, so
a request never waits on the gateway.

### Legacy proxy-compatible

Same paths and response shapes as the pypowerwall proxy, so existing
dashboards, Telegraf and Home Assistant configurations keep working.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/aggregates` | | Site, battery, load and solar meter data. |
| GET | `/soe` | | `{"percentage", "raw_percentage"}`: raw gateway battery level in both, as in the proxy. The Tesla app scaled level is at `/api/system_status/soe`. |
| GET | `/vitals` | | Device vitals keyed by device. |
| GET | `/strings` | | Solar string data. |
| GET | `/temps` | | Powerwall temperatures (°C) keyed by device. |
| GET | `/temps/pw` | | Temperatures as `PWn_temp`. |
| GET | `/alerts` | | Active alerts (array). |
| GET | `/alerts/pw` | | Active alerts as `{"<alert>": 1}`. |
| GET | `/fans` | | Fan data keyed by device. |
| GET | `/fans/pw` | | `FANn_actual` / `FANn_target` RPM; Powerwall 3 fans add `FANn_duty` (%) and have a null target. |
| GET | `/freq` | | Per-Powerwall frequency, voltage and power, island/meter readings and `grid_status` (1 up, 0 down, -1 syncing, `null` unknown). |
| GET | `/pod` | | Per-Powerwall battery data (`PWn_*`), pack energy, time remaining and reserve. |
| GET | `/json` | | Power flows, battery level, grid status, reserve, time remaining, pack energy and strings in one object. |
| GET | `/battery` | | `{"power"}`: battery power (W). |
| GET | `/csv` | | Text: `Grid,Home,Solar,Battery,BatteryLevel`. Missing readings are 0. Add `?headers` for a header row. |
| GET | `/csv/v2` | | Text: adds `GridStatus,Reserve`. Add `?headers` for a header row. |
| GET | `/version` | | `{"version", "vint"}`: gateway firmware; `vint` is the version as an integer (`26.26.11` → `262611`). `"SolarOnly"` with `vint` 0 when there is no firmware version. |
| GET | `/tedapi` | | Lists the `/tedapi/*` routes (also `/tedapi/`). |
| GET | `/tedapi/` | | Same as `/tedapi`. |
| GET | `/tedapi/config` | | Gateway configuration. On demand, TEDAPI only. |
| GET | `/tedapi/status` | | Gateway status. On demand, TEDAPI only. |
| GET | `/tedapi/components` | | Component data. On demand, TEDAPI only. |
| GET | `/tedapi/battery` | | Battery blocks. On demand, TEDAPI only. |
| GET | `/tedapi/controller` | | Device controller data. On demand, TEDAPI only. |
| GET | `/api/meters/aggregates` | | Same data as `/aggregates`. |
| GET | `/api/meters/site` | | Site meter CT configuration. On demand; `[]` when offline. |
| GET | `/api/meters/solar` | | Solar meter data (array). |
| GET | `/api/meters` | | Meter list; `[]` where the mode doesn't provide it. |
| GET | `/api/meters/readings` | | Always `{}`. |
| GET | `/api/system_status` | | Full system status (battery blocks, capacity). |
| GET | `/api/system_status/soe` | | `{"percentage", "raw_percentage"}`: the Tesla app scaled battery level in `percentage`, as in the proxy (the Power Flow animation reads it), and the raw level in `raw_percentage`. `/soe` is the raw level. |
| GET | `/api/system_status/grid_status` | | `{"grid_status", "grid_services_active"}`. |
| GET | `/api/system_status/grid_faults` | | Always `[]`. |
| GET | `/api/status` | | Gateway din, firmware, uptime and device type. |
| GET | `/api/site_info` | | Site information (also `HEAD`). |
| GET | `/api/site_info/site_name` | | `{"site_name", "timezone"}`. |
| GET | `/api/site_info/grid_codes` | | Always `[]`. |
| GET | `/api/operation` | | `{"real_mode", "backup_reserve_percent", "grid_charging", "grid_export", "stale", "last_updated"}`. Values the cache lacks are filled from the hybrid cloud connection and marked `stale`; unknown values are `null`. |
| GET | `/api/sitemaster` | | Sitemaster status; `connected_to_tesla` reflects the gateway connection. |
| GET | `/api/powerwalls` | | Powerwall list. |
| GET | `/api/networks` | | Network configuration. |
| GET | `/api/system/networks` | | Same as `/api/networks`. |
| GET | `/api/solars` | | Solar inverter list. |
| GET | `/api/solars/brands` | | Static list of inverter brands. |
| GET | `/api/solar_powerwall` | | Solar and Powerwall data where available. |
| GET | `/api/system/update/status` | | Firmware update status (reports the current version). |
| GET | `/api/synchrometer/ct_voltage_references` | | Static CT voltage references. |
| GET | `/api/customer` | | Static `{"registered": true}`. |
| GET | `/api/customer/registration` | | Disabled: `{"status": "404 Response - API Disabled"}`, as in the proxy. |
| GET | `/api/installer` | | Static installer record. |
| GET | `/api/troubleshooting/problems` | | Always `{"problems": []}`. |
| GET | `/api/auth/toggle/supported` | | Always `{"toggle_auth_supported": false}`. |
| POST | `/api/login/Basic` | | Login stub for the Power Flow app: sets the `AuthCookie` and `UserRecord` cookies. Checks no credentials. |

Like the proxy, `/soe`, `/json` (`soe`) and `/csv` report the raw gateway
battery level; `/api/system_status/soe` reports the Tesla app scaled level.

### Convenience `/pw`

Shortcuts named after pypowerwall library calls.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/pw/level` | | `{"level"}`: raw battery level (%). |
| GET | `/pw/power` | | `{"site", "solar", "battery", "load"}` in W. |
| GET | `/pw/site` | | Site meter block. |
| GET | `/pw/grid` | | Site meter block (same as `/pw/site`). |
| GET | `/pw/solar` | | Solar meter block. |
| GET | `/pw/battery` | | Battery meter block. |
| GET | `/pw/load` | | Load meter block. |
| GET | `/pw/home` | | Load meter block (same as `/pw/load`). |
| GET | `/pw/battery_blocks` | | Battery blocks keyed by serial, with temperature and state from vitals. |
| GET | `/pw/aggregates` | | Same as `/aggregates`. |
| GET | `/pw/vitals` | | Same as `/vitals`. |
| GET | `/pw/temps` | | Same as `/temps`. |
| GET | `/pw/strings` | | Per-inverter string detail. |
| GET | `/pw/alerts` | | `{"alerts": [...]}`. |
| GET | `/pw/din` | | `{"din"}`. |
| GET | `/pw/uptime` | | `{"uptime"}`. |
| GET | `/pw/version` | | `{"version", "vint"}`. |
| GET | `/pw/status` | | Same as `/api/status`. |
| GET | `/pw/system_status` | | Same as `/api/system_status`. |
| GET | `/pw/grid_status` | | Same as `/api/system_status/grid_status`. |
| GET | `/pw/site_name` | | `{"site_name"}`. |
| GET | `/pw/is_connected` | | `{"is_connected"}`: whether the gateway is online. |
| GET | `/pw/get_reserve` | | `{"reserve"}`: backup reserve (%). |
| GET | `/pw/get_mode` | | `{"mode"}`: operating mode. |
| GET | `/pw/get_time_remaining` | | `{"time_remaining"}`: estimated backup hours. |

### Control

Writes go to the default gateway. See [Control and security](#control-and-security)
for the request format and write path.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/control/status` | | `{"enabled"}`: whether `PW_CONTROL_SECRET` is set. |
| POST | `/control/{path}` | token | Set `reserve`, `mode`, `grid_charging`, `grid_export` or `reserve_hold`; returns the library result. |
| POST | `/control/islanding` | token | Go off grid or reconnect (TEDAPI v1r only). |
| GET | `/api/tesla/tariff_rate` | | The site's Tesla tariff from the cloud. Cached 5 minutes; the last good value is served if a refresh fails. |
| POST | `/api/tesla/time_of_use_settings` | token | Update the Time-of-Use tariff. Body `{"tou_settings": {..., "tariff_content_v2": {...}}}`; only `tou_settings` is sent to Tesla. |

The tariff routes need a Tesla cloud connection (hybrid, Cloud or FleetAPI):
otherwise `503`; a Tesla error is `502`. `tariff_content_v2` uses Tesla's v2
tariff schema, which differs from what the GET returns, so a read result can't
be posted back unchanged. Current settings are read with
`/api/operation`, `/pw/get_reserve` and `/pw/get_mode`; the proxy's
`GET /control/<name>` reads and `max_backup` are not available.

### Gateways (multi-gateway)

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/api/gateways/` | | Status of every gateway, keyed by id: `gateway` (config without secrets), `data`, `online`, `last_updated`, `error`. `data.connection_mode` is the connection in use (`TEDAPI v1r`, `TEDAPI`, `Local`, `Basic LAN`, `Cloud`, `FleetAPI`), which failover can move off the configured mode. |
| GET | `/api/gateways/{gateway_id}` | | That gateway's status. |
| GET | `/api/gateways/{gateway_id}/vitals` | | That gateway's vitals. |
| GET | `/api/gateways/{gateway_id}/strings` | | That gateway's strings. |
| GET | `/api/gateways/{gateway_id}/aggregates` | | That gateway's aggregates. |
| GET | `/api/gateways/{gateway_id}/api/{path}` | | On demand: pypowerwall `poll("/api/{path}")` on that gateway. `503` if it fails. |
| POST | `/api/gateways/{gateway_id}/api/{path}` | token | On demand: raw POST of the JSON body to `/api/{path}` on that gateway. `503` if it fails. |

An unknown `gateway_id` returns `404`.

### Aggregates (all gateways)

Combined data from every gateway with data: power is summed and battery levels
averaged. With `PW_GRACEFUL_DEGRADATION`, an offline gateway still contributes
its last good data for up to `PW_CACHE_TTL` seconds and is counted in
`num_degraded`, not `num_online`. Battery capacity is not collected yet, so
the capacity fields are always `0`.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/api/aggregate/` | | All totals: battery level (scaled and raw), capacity, site/battery/load/solar/grid power, primary `grid_status`, `num_gateways`, `num_online`, `num_degraded`, `timestamp`, and each gateway's status under `gateways`. |
| GET | `/api/aggregate/power` | | `{"site", "battery", "load", "solar", "grid", "timestamp"}` in W. |
| GET | `/api/aggregate/soe` | | `{"percentage", "raw_percentage", "num_gateways", "timestamp"}`; `num_gateways` is the online count. |
| GET | `/api/aggregate/battery` | | Capacity, battery level, battery power and gateway counts. |
| GET | `/api/aggregate/strings` | | Strings keyed by gateway id. |
| GET | `/api/aggregate/alerts` | | Alerts keyed by gateway id. |
| GET | `/api/aggregate/vitals` | | Vitals keyed by gateway id. |

### Time-series and history

Read from the local SQLite store. When the subsystem is disabled
(`PW_TIMESERIES_RETENTION=-1`) they answer `{"enabled": false, ...}`. Invalid
query values return `422`.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/api/timeseries/today` | | Today's running kWh totals per gateway, in each gateway's time zone. |
| GET | `/api/timeseries/daily` | | Daily kWh totals per gateway and category, newest first. Query: `days` (1-366, default 7), `gateway`, or `start`/`end` (`YYYY-MM-DD`) for a range. |
| GET | `/api/timeseries/trend` | | About 360 bucketed points of solar/home/battery/grid kW and battery level. Query: `hours` (1-168, default 24), `gateway`, `start`/`end` (epoch seconds), `fit`. |
| GET | `/api/timeseries/samples` | | Raw power samples. Query: `gateway`, `start`, `end`, `limit` (1-10000, default 500). |
| GET | `/api/timeseries/status` | | Retention settings, database size, row counts and gateway names. |
| GET | `/api/timeseries/signals` | | Recorded temperature/fan series with their time ranges and `powerwall` labels, plus the metric and chart-group catalog. Query: `gateway` (one id). |
| GET | `/api/timeseries/signal_trend` | | Temperature/fan history (avg/min/max/count per point). Query: `metrics` and `devices` (comma lists), `gateway` (one id), `start`/`end` (epoch seconds up to 2100-01-01), `hours`, `resolution` (`auto`, `raw` or `daily`). |

### WebSockets

Each pushes one JSON message per second.

| Path | Message |
|------|---------|
| `/ws/aggregate` | The `/api/aggregate/` object (including each gateway's status). |
| `/ws/gateway/{gateway_id}` | That gateway's status (as `/api/gateways/{gateway_id}`), or `{"error": "Gateway not found"}`. |

### Health and stats

| Method | Path | Auth | Response |
|--------|------|------|----------|
| GET | `/health` | | `status` (`healthy`, `degraded`, `unhealthy` or `no_gateways`), server version, online/offline counts, per-gateway errors and SolarOnly fallback state. Also `HEAD`. |
| POST | `/health/reset` | token | Clears the SolarOnly fallback state for all gateways. |
| GET | `/stats` | | Proxy-style server stats: versions, mode (the live connection once polled), request counters, uptime, memory, connection flags, masked `config`, connection health, hybrid cloud link, per-gateway status. |
| GET | `/api/mqtt/status` | | MQTT enabled/connected state and broker settings (no credentials). |

### UI pages and static files

| Method | Path | Response |
|--------|------|----------|
| GET | `/` | Power Flow animation (HTML). Query: `style` overrides `PW_STYLE`. |
| GET | `/console` | Management console (HTML). |
| GET | `/history` | Daily energy and temperature/fan history (HTML). |
| GET | `/example` | Power Flow iframe example (HTML). |
| GET | `/example.html` | Same as `/example`. |
| GET | `/docs` | Swagger UI. |
| GET | `/docs/oauth2-redirect` | Swagger UI helper. |
| GET | `/redoc` | ReDoc. |
| GET | `/openapi.json` | OpenAPI schema. |
| GET | `/static` | Static assets (`/static/...`). |
| GET | `/favicon.ico` | Favicon. |
| GET | `/favicon-16x16.png` | Favicon. |
| GET | `/favicon-32x32.png` | Favicon. |
| GET | `/[object Object]` | Returns `{}` for a malformed URL the Power Flow bundle requests (also `/%5Bobject%20Object%5D`). |
| GET | `/%5Bobject%20Object%5D` | Same as above. |

### What each mode serves

- **Basic LAN** polls only aggregates, battery level and grid status. Routes
  built from other data (vitals, strings, temps, fans, alerts, `/freq`,
  `/pod`, `/api/status`, `/api/system_status`, `/api/site_info`,
  `/api/networks`, `/api/powerwalls`) return their empty defaults. With hybrid
  cloud control, mode and reserve come from the cloud.
- **`/tedapi/*`** and fan data need a TEDAPI connection.
- **Cloud and FleetAPI** report less device-level detail than TEDAPI.

---

## Control and security

Control is off until `PW_CONTROL_SECRET` is set. Every write route then needs
the secret in the `Authorization` header, either `Bearer <secret>` or the bare
secret. Bodies are JSON objects.

```bash
curl -X POST http://localhost:8675/control/reserve \
  -H "Authorization: Bearer $PW_CONTROL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"value": 20}'
```

This differs from the proxy, which takes a form-encoded `token` parameter.

### `POST /control/{path}`

| `path` | Body | Notes |
|--------|------|-------|
| `reserve` | `{"value": 0-100}` | Integer backup reserve (%). Optional `"mode"` sets both in one call. |
| `mode` | `{"value": "self_consumption" \| "backup" \| "autonomous"}` | Optional integer `"level"` sets both in one call. |
| `grid_charging` | `{"value": true \| false}` | Must be a JSON boolean. |
| `grid_export` | `{"value": "battery_ok" \| "pv_only" \| "never"}` | |
| `reserve_hold` | `{}` | Sets the reserve to the current battery level (rounded down). `503` if the level is unknown. |

Any other `path` is sent as a raw POST of the body to the default gateway's
`/api/{path}`.

**Write path.** When a hybrid cloud connection is up (a local gateway with
`PW_EMAIL` and `PW_CONTROL_SECRET`), writes go through it. Otherwise they use the default gateway's own
connection, which can write in Cloud, FleetAPI and TEDAPI v1r modes. A failed
or unsupported write returns `503`. The hybrid connection is shared: on a Tesla
account with several sites, set `PW_SITEID`.

With `PW_LOCAL_FIRST=yes`, a gateway confirmed on TEDAPI v1r, with its wired LAN
up and a local connection, writes over that local connection first, even when
a hybrid cloud connection exists. A write that was sent is final: there's no
second attempt through the cloud, even after a timeout, since it may already
have been applied. When the LAN is down or there's no local connection, the
path above applies. Islanding is unaffected.

### Islanding: `POST /control/islanding`

Physically opens or closes the grid contactor of the default gateway. It needs
a local TEDAPI v1r connection and never uses the cloud, even in hybrid mode.

> ⚠️ **WARNING — Grid island control physically operates your home's grid
> contactor. Use with extreme care.** Going off-grid can interrupt power and
> temporarily interrupt solar production. While islanded, Powerwall may raise
> the home's electrical frequency to limit or stop solar production,
> particularly when the battery is near full or available charging capacity is
> limited. This can cause lights to flicker and affect frequency-sensitive
> equipment.
>
> Your home depends on available battery and solar power while disconnected
> from the grid; if those cannot support the load, your home can lose power. Do
> not assume that a nearly full battery guarantees a seamless transition.
>
> Do not automate these commands without understanding the failure modes; test
> only when someone is present. An acknowledgement is not confirmation that the
> home changed grid state: check `GET /api/system_status/grid_status` after the
> transition, allowing for the configured polling interval. After an error or
> timeout, the outcome may be unknown and the command may still complete; do
> not automatically retry or send the opposite command.

| Body | Action |
|------|--------|
| `{"value": "off_grid", "confirm": true}` | Go off grid (`confirm` must be JSON `true`). |
| `{"value": "on_grid"}` | Reconnect to the grid. |

Success is `200` with the library's response when the gateway acknowledges
(`result: 1`). Other results: `502` not acknowledged (library response in
`detail.response`), `503` no response or v1r unavailable, `409` a command is
still in progress, `429` with `Retry-After` inside the `PW_ISLANDING_COOLDOWN`
window. The cooldown counts failed and timed-out commands too.

### Security notes

- Read routes, `/stats` and `/console` need no authentication. Bind to a
  trusted network (`PW_BIND_ADDRESS`) and terminate TLS at a reverse proxy if
  the server is reachable from elsewhere.
- `/stats` masks passwords, the control secret, the Tesla email, the site id
  and file paths.
- The `gateway` object in `/api/gateways/` and `/api/gateways/{gateway_id}`
  omits gateway passwords, RSA key paths and emails. The raw
  `/api/gateways/{gateway_id}/api/{path}` route returns the gateway's response
  unchanged and is open for GET.
- `PW_RATE_LIMIT_*` throttles per client IP, but it is not a substitute for a
  reverse proxy's limits on an internet-facing server.

---

## Errors and safe defaults

Data routes don't fail when data is missing (startup, gateway offline longer
than `PW_CACHE_TTL`, or a mode that doesn't provide it). They return an empty
default instead: `{}`, `[]`, an object with `null` values, or zeros for
`/csv`. Exceptions:

| Case | Response |
|------|----------|
| No gateways configured | `503` on routes that use the default gateway. |
| Unknown `gateway_id` | `404` on `/api/gateways/{gateway_id}...`. |
| `/tedapi/*` offline or unsupported | `200` with `{"error": "..."}`. |
| On-demand raw gateway calls fail | `503`. |
| Control disabled | `403` `{"detail": "Control features not enabled"}`. |
| Missing or wrong token | `401`. |
| Invalid control value | `400` with the reason in `detail`. |
| Body not a JSON object, or bad query value | `422`. |
| Rate limit exceeded | `429`. |
