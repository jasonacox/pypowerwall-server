# Release Notes

## Version History

### [0.6.2] - 2026-09-05

**Added:**
- **Web Console Powerwall Control card (`PW_CONTROL_SECRET`)** — the Console gains a Powerwall Control card (shown after System Health when a control secret is configured) with mode select (Self-Consumption/Backup/Time-Based), reserve slider + number (0–100), and a token field kept client-side (session-only by default, optional "Remember my token on this device" via `localStorage`). Mode + reserve changes go through the existing `POST /control/*` API with `Authorization: Bearer <token>`; a mode change together with reserve 0 is auto-split into two calls to avoid the Tesla cloud API quirk that can silently drop the mode change. Card availability is exposed by a new unauthenticated `GET /control/status` returning only `{"enabled": bool}`. (#94)

### [0.6.1] - 2026-09-04

**Added:**
- **Firmware version logging** — gateway firmware is logged at first successful poll and on every change (`Gateway <id> firmware changed: X -> Y`), so `docker logs` answers "when did my firmware update?" (#92, Powerwall-Dashboard #854)

**Changed:**
- **Bumped pypowerwall dependency to 0.17.1** — picks up the cloud `set_operation()` fixes validated on PW3 hardware during hybrid-mode testing (#79, #85). Docker images now ship pypowerwall 0.17.1.

**Fixed:**
- **Uvicorn HTTP access logs are no longer emitted when debug mode is disabled** — the server previously logged every request line (`INFO: 192.168.1.3 - "GET /soe HTTP/1.1" 200 OK`) even without `PW_DEBUG`, because `uvicorn.run()` applies its default logging config and resets logger levels set at import time. Access logs are now disabled at the source via `access_log=settings.debug`. (#86)

### [0.6.0] - 2026-08-30

**Added:**
- **Powerwall 3 Basic LAN support (`PW_HOST` + `PW_PASSWORD`)** — you can now connect a PW3 gateway using only its local address and Basic password (the one printed on the gateway QR card / in the Tesla app), with no Tesla cloud credentials and no `PW_EMAIL`/`PW_TOKEN`. Uses the gateway's local TEDAPI via the pypowerwall library's Basic auth mode; poll-only (no cloud control), consistent with local v1r connections. Connection mode is reported in the Console gateway card.
- **Hybrid mode: Basic LAN monitoring + cloud control** — add `PW_EMAIL` (with `PW_AUTH_PATH`/token as usual) alongside `PW_HOST` + `PW_PASSWORD` and the server keeps all monitoring on the local Basic LAN connection while routing control writes (`/control/reserve`, `/control/mode`, `/control/grid_charging`) through the Tesla cloud connection — the same hybrid split already used for v1r/TEDAPI gateways. Monitoring stays independent of internet availability; control needs cloud reachability. (#79)
- **`PW_TIMESERIES_PATH` usability** — if the configured path is a directory, the default timeseries database filename is appended automatically instead of failing to open.
- **Trend panel legend toggle + hover tooltip** — click the legend to show/hide each series (Solar/Home/Battery/Grid/Battery Level); hovering the chart shows a tooltip with values at the cursor. Legend keys are keyboard-accessible, and the tooltip clears when there is no data. (#81)
- **README updates** — clarified Energy panel functionality, refreshed console/trend screenshots, and documented the connection mode selection options.
- **Per-link hybrid health in the Console and `/stats` (issue #87)** — with hybrid mode (local reads + cloud control) the Console's Connection card now shows the two links as separate sub-rows (`Local: Healthy / Cloud: Unavailable`) instead of tracking the local link only, and the overall Connection indicator is degraded when either link is down (non-hybrid setups keep the previous local-only semantics). `/stats` gains a `cloud_control` section (`configured`, `connected`, `state`, `consecutive_failures`, `last_success_time`, last-known mode/reserve with fetch times) plus `connection_health.local_healthy` / `cloud_healthy`. When the cloud link drops after having been up, `/api/operation` serves the last known cloud value explicitly marked `stale: true` (with `last_updated`) instead of silently freezing it — the Console renders e.g. `Self-Consumption (stale)`; with no cloud value ever seen it stays `--`.

**Fixed:**
- **Local control writes in local-only mode (`v1r`/TEDAPI without cloud credentials)** — `POST /control/reserve`, `/control/mode`, and `/control/grid_charging` previously fell through to a raw `POST /api/mode` against the gateway's local API, which the gateway rejects — every write returned `{"ERROR":"Unknown API: /api/mode"}` while monitoring kept working. Local writes are now routed through the pypowerwall library's `set_reserve()` / `set_mode()` / `set_grid_charging()` / `set_operation()` methods (new `GatewayManager.local_control()`), using the same write-serialization and timeout protection as cloud control. Cloud/hybrid behavior is unchanged. Verified against live `v1r` hardware. (#82, #83)

### [0.5.0] - 2026-08-23

**Added:**
- **Persistent time-series energy stats (TimeSeriesStore)** — new SQLite-backed store that records raw power samples (solar, home, battery, grid, battery charge level) every poll cycle, and downsamples them into persistent daily energy totals (kWh imported/exported/charged/discharged) per gateway. Data survives restarts — daily totals resume from persisted values and keep accruing with no reset or double counting. Storage is bounded: raw samples are kept for a configurable retention window (battery charge level is raw-only, never downsampled), daily aggregates are kept indefinitely.
- **Daily Energy panel** — new dashboard panel showing today's Solar, Home, Battery Charged/Discharged, and Grid Import/Export totals, using the standard Energy Summary color palette (solar yellow, home blue, battery green, grid gray) via the same CSS variables.
- **Energy Summary ↔ Energy Trend flip panel** — click the Energy Summary card to flip it in place into an Energy Trend panel (same footprint, no layout shift — panels below never move). The trend chart plots the last 24 hours of raw data with Solar/Home/Battery/Grid kW on the shared left axis (translucent fill to zero) and Battery Level % as a dashed line on the right axis, in standard colors. Time scale options: 30m · 1hr · 3hr · 6hr · 12hr · 24hr (rolling), full local day 0:00–23:59 (default), Day, and "Fit" (all retained data). Click again ("click for summary") to flip back.
- **Time-series API endpoints:**
  - `GET /api/timeseries/samples` — raw sample rows (includes `soe` battery level)
  - `GET /api/timeseries/daily` — persisted daily energy totals
  - `GET /api/timeseries/trend` — chart-ready bucket aggregation for the trend panel (~360 points regardless of span; supports `hours`, `start`/`end`, `fit`, per-gateway sums for multi-gateway fleets)
  - `GET /api/timeseries/status` — store health: row counts, retention, `write_failures` counter, DB filename (full path masked)

**Fixed:**
- **Timezone-correct daily buckets** — daily rows are keyed by local day, so evening local-day samples west of UTC are no longer split into the wrong day row.
- **Poll-loop resilience** — sample writes run in a dedicated worker thread with a 5s `asyncio.wait_for` guard, so a hung SQLite write (e.g. full disk on an SBC) can never stall gateway polling. Write failures log a rate-limited warning (once per 5 min) and increment the visible `write_failures` counter.
- **Clean shutdown** — executor shutdown uses `cancel_futures=True` so queued writes can't reopen the DB after close; a clean SIGTERM checkpoints the WAL.

### [0.4.3] - 2026-08-16

**Added:**
- **Lifetime energy MQTT topics + Home Assistant sensors** — six lifetime energy accumulators (Wh) from `/api/meters/aggregates` are now published as scalar MQTT topics and exposed as HA auto-discovery sensors (`device_class: energy`, `state_class: total_increasing`): `grid_energy_imported`, `grid_energy_exported`, `home_energy_imported`, `solar_energy_exported`, `battery_energy_imported`, `battery_energy_exported`. Topic names follow the existing scalar convention (aggregates `site` → `grid`, `load` → `home`). With `state_class: total_increasing` the HA Energy dashboard charts them directly — daily stats are derived by delta, the same semantics as PW2's lifetime counters. Gateways whose firmware lacks the endpoint keep reporting `0`, mirroring the HTTP API.

**Changed:**
- **Bumped `pypowerwall` dependency to `0.16.5`** (from `0.16.2`). On PW3/TEDAPI this release overlays the lifetime `energy_imported` / `energy_exported` accumulators onto `/api/meters/aggregates` using the gateway's native local API (customer login), with thread-safety hardening (bounded lock acquisition, double-checked cache, host affinity) — see pypowerwall PR #372. This is what populates the new energy topics on PW3; PW2/local mode has always carried these fields.

**Fixed:**
- **Corrected battery sign convention in the `/api/aggregate/power` docstring** — it stated the convention backwards (+ charging, - discharging); actual behavior, consistent with `/api/aggregate/battery` and the rest of the codebase, is positive = discharging, negative = charging. Thanks to @tassieKev for catching the typo (#76).

### [0.4.2] - 2026-07-19

**Changed:**
- **Gateway site name now shown on successful connection** — the startup/reconnect log line appends the Tesla-reported site name (e.g. `Successfully connected to gateway default (10.0.1.40) - Site: My Powerwall`) for easier multi-gateway identification. Falls back to the original message when `site_name` is unavailable.
- **Sanitized external input in log output** — `site_name` (sourced from the gateway) is stripped of control characters before logging to prevent log forging.

**Fixed:**
- **Removed unused `aiohttp` dependency** — resolved all 29 open Dependabot alerts by eliminating the vestigial package entirely (was not referenced anywhere in `app/`, `tests/`, or `mqtt-tools/`).

### [0.4.1] - 2026-07-19

**Changed:**
- **Bumped `pypowerwall` dependency to `0.16.2`** (from `0.15.12`). Key upstream changes across the four intermediate releases:
  - **v0.15.13**: Fleet API transport upgraded to HTTP/2 + TLS 1.3; fix for TEDAPI WiFi backoff race condition in multi-threaded deployments; Docker base image switched from Alpine to Debian-slim to avoid musl libc TLS fingerprint rejection by Tesla after long-running token expiry.
  - **v0.16.0**: Full codebase review sweep — 101 new regression tests, critical bug fixes (`set_operation` reserve scale, FleetAPI token refresh wedge, TEDAPI `available_blocks` always 0), security hardening (query-string bypass, open proxy, CSRF), 19 crash-on-None fixes, native lock-timeout performance improvements.
  - **v0.16.1**: Windows TLS fix — caps TLS to 1.2 on Windows where OpenSSL TLS 1.3 fingerprint is rejected by Tesla during PKCE code exchange; Linux/macOS retain TLS 1.3 pinning.
  - **v0.16.2**: TEDAPI SolarOnly fallback mode (`PW_TEDAPI_RECOVERY=yes`) — automatic fallback to solar-only data on TEDAPI connectivity loss with exponential-backoff recovery; v1r `PENDING_VERIFICATION`/`UNKNOWN_KEY_ID` auth warnings now surfaced at normal log level; v1r WiFi-fallback response normalization (fixes blank firmware version on LAN→WiFi failover).
- **TEDAPI connection mode logged at startup** — the connect message now states the exact mode being used (`TEDAPI full (WiFi)`, `TEDAPI v1r`, `TEDAPI v1r + WiFi`, `Cloud`, `FleetAPI`) so users can immediately see which code path pypowerwall is taking.
- **Warning when `PW_RSA_KEY_PATH` + `PW_GW_PWD` are set together without `PW_WIFI_HOST`** — this combination activates TEDAPI v1r mode without a WiFi fallback path, causing follower Powerwall data to appear as `null`. The warning names both remedies: add `PW_WIFI_HOST=<gateway-ip>` to keep v1r with WiFi follower fallback, or remove `PW_RSA_KEY_PATH` to switch to TEDAPI full mode.

**Added:**
- **TEDAPI SolarOnly fallback tracking and auto-recovery** — per-gateway background probe monitors TEDAPI health and detects SolarOnly fallback (when TEDAPI drops but solar data continues). After 3 consecutive failed probes, enters fallback mode and attempts `pw.connect()` recovery with exponential backoff (60s → max 300s). No restart, no data gap — the gateway keeps serving whatever data is available. Exposed via `fallback_mode` in `/health` and `/stats`, plus `POST /health/reset` to clear state. Config: `PW_TEDAPI_RECOVERY` (default: `yes`), `PW_TEDAPI_PROBE_INTERVAL` (default: `30`).
- **`POST /health/reset` now requires `PW_CONTROL_SECRET`** — the endpoint mutates server state and is now gated by the same bearer token as `/control/*` endpoints, preventing unauthorised resets on exposed servers.
- **12 missing API compatibility endpoints** — `regression_test.py` identified gaps vs the old pypowerwall proxy server ALLOWLIST. All are now implemented as cache-backed or static-stub endpoints so pypowerwall-server is a complete drop-in replacement:
  - `/api/meters/site` — on-demand pypowerwall passthrough returning the synchrometer CT config (like `/tedapi/*` diagnostics)
  - `/api/meters/solar` — returns the solar slice of cached aggregates as a list
  - `/api/meters` — returns cached meter hardware config (empty list in TEDAPI mode)
  - `/api/meters/readings` — stub `{}` (CT readings not available in TEDAPI/local mode)
  - `/api/solars` and `/api/solars/brands` — cached inverter list + static brand list
  - `/api/customer` — `{"registered": true}`
  - `/api/installer` — Tesla installer stub (full old-proxy key set)
  - `/api/system/update/status` — static "update_succeeded" stub with current cached firmware version
  - `/api/site_info/grid_codes` — empty list (grid code enumeration not available in TEDAPI mode)
  - `/api/synchrometer/ct_voltage_references` — static Phase1/Phase2 CT reference stub
  - `/api/solar_powerwall` — cached solar_powerwall data or `{}`
- **`regression_test.py`** — A/B endpoint comparison tool for hardware verification against the old proxy. Compares all ~76 non-control endpoints between two running instances, with structural-drift FAILs, value-drift WARNs, and volatile-field filtering. Usage: `python3 regression_test.py --old http://old-proxy:8675 --new http://localhost:8675`.

**Fixed (live-hardware A/B verification against proxy t95):**
- **`/pod` POD vitals fields were `null` on PW3** — the TEPOD serial matcher only looked for a `serialNumber` field inside vitals data, but live PW3 TEDAPI vitals carry the serial only in the device key (`TEPOD--{part}--{serial}`). Now falls back to the key-embedded serial, restoring `POD_ActiveHeating`, `POD_ChargeRequest`, `POD_nom_energy_to_be_charged`, etc.
- **8 `/pw/*` endpoints returned invented shapes** — now byte-compatible with the old proxy's `pw.*` library mappings:
  - `/pw/level` → `{"level": <raw soe>}` (was `{"percentage", "raw_percentage"}`)
  - `/pw/battery` → full battery meter block from aggregates (was `{"power": N}`)
  - `/pw/battery_blocks` → dict keyed by serial + TETHC temperature merge (was a list)
  - `/pw/strings` → verbose PVAC-device format with PVS string injection (was simplified letter format)
  - `/pw/status` → full `/api/status` payload (was `{"status": ...}`)
  - `/pw/grid_status` → raw API payload with `grid_services_active` (was simplified `UP`/`DOWN`)
  - `/pw/alerts` → `{"alerts": [...]}` wrapper (was bare list)
  - `/pw/get_time_remaining` → key `time_remaining` (was `time_remaining_hours`)
- **`/api/sitemaster`** — added missing `power_supply_mode` and `can_reboot` keys
- **`/api/customer/registration`** — now returns the old proxy's disabled response (`{"status": "404 Response - API Disabled"}`) instead of mock PII fields
- A/B regression vs live proxy t95: 21 structural failures → 8, all remaining diffs are intentional improvements (real data where the old proxy returns `null`/`TIMEOUT!` sentinels) or by-design server differences (`/stats`, `/health`)

### [0.4.0] - 2026-07-01

**Security:**
- **Unauthenticated write proxy closed** — `POST /api/gateways/{id}/api/{path}` proxied arbitrary POSTs to the gateway with no authentication, allowing anyone with network access to change operating mode or backup reserve even when control features were disabled. The endpoint now requires the same `PW_CONTROL_SECRET` bearer token as `/control/*`. `verify_control_token` moved to shared `app/api/auth.py` and now uses a constant-time comparison.
- **Gateway credentials removed from API responses** — the `Gateway` model serialized `gw_pwd` (gateway Wi-Fi password) and `email` (Tesla account) in `/api/gateways/`, `/api/aggregate/`, and WebSocket streams. Both fields are now excluded from serialization.
- **CORS no longer reflects arbitrary origins with credentials** — the wildcard default sent `Access-Control-Allow-Credentials: true` with the caller's origin reflected, letting any website make credentialed cross-origin requests. Wildcard mode now uses plain `*` without credentials; explicit `CORS_ORIGINS` lists still allow credentialed access.
- **/stats masks PII** — Tesla account email, site ID, and filesystem paths are now masked in the unauthenticated `/stats` endpoint.
- `gateways.yaml` ships as `gateways.yaml.example` and the real file is gitignored (it holds gateway passwords).

**Fixed:**
- **Dead gateways were never detected** — pypowerwall signals connection failure by returning `None` rather than raising, so a dead gateway stayed "online" with empty data, reset its backoff, and overwrote the last-known-good snapshot used for graceful degradation. `None`/`ERROR` aggregates now count as poll failures.
- **`/control/reserve` with an empty body silently set reserve to 0** (and `/control/mode` silently set `self_consumption`). Both now require a validated `value` and return HTTP 400 otherwise, matching the existing `grid_charging` validation.
- **`/api/status` returned HTTP 500** whenever the cached DIN was absent (attribute error on a non-existent `Gateway.din` field); now falls back to `PowerwallData.din`.
- **Per-gateway MQTT availability never went offline** — `publish_gateway()` published `"online"` unconditionally, so HA kept showing stale retained sensor values as live after a gateway dropped. Availability now tracks the gateway state, and shutdown flushes `"offline"` to all per-gateway topics before disconnecting.
- **Aggregate battery percent diluted by solar-only inverters** — the average divided by all online gateways instead of the gateways that reported SOE. Aggregates also now apply graceful degradation (with a new `num_degraded` field) and derive grid status from the primary configured gateway instead of the hardcoded id `"default"`.
- **Generic environment variables leaked into gateway config** — `GatewayConfig` was a `BaseSettings` with an empty env prefix, so `PORT`, `EMAIL`, `NAME`, `HOST` (ubiquitous on container platforms) silently populated gateway fields. It is now a plain model constructed only from explicit config, and `name` defaults to `id`.
- **One bad `PW_GATEWAYS` entry discarded the whole list** and silently fell back to legacy single-gateway mode; entries are now validated individually.
- **Write lock bypassed on the v1r/cloud-mode control fallback** — raw `post` calls now hold the same write lock as `set_*` methods.

**Performance:**
- **Per-gateway polling tasks** — each gateway polls on its own independent fixed tick, so a slow or degraded gateway no longer stalls data freshness for healthy ones. The whole per-gateway fetch is also capped by an overall budget (previously ~22 sequential timeouts could stretch a cycle to ~2 minutes without ever triggering backoff).
- **Timeout alignment** — per-step timeouts are derived from `PW_TIMEOUT + 2s` so pypowerwall's internal timeout fires first; abandoned executor threads were holding the library's per-function API lock and starving the worker pool. `pwcacheexpire` is now passed to pypowerwall (matching the poll interval) so its internal cache doesn't expire mid-cycle.
- **MQTT publish coalescing** — at most one in-flight publish task per gateway (latest-wins) with strong task references; slow brokers previously accumulated unbounded fire-and-forget tasks that were also eligible for garbage collection mid-publish.
- **WebSocket serialize-once** — streamed payloads are serialized at most once per second and shared across all connected clients (previously each client re-serialized the full multi-hundred-KB snapshot every second).
- **Non-blocking startup** — the hybrid cloud-control connection is established in a background task instead of blocking server startup for up to 15 seconds.

**Added:**
- **`PW_CONFIG` / `--config` file loading implemented** — the documented YAML/JSON configuration file (server section + gateways list) is now actually read, with per-entry validation. Previously the flag and README section existed but no code consumed the file. `pyyaml` added as an explicit dependency.

**Changed:**
- README and docker-compose now document the real control variable (`PW_CONTROL_SECRET`); the previously documented `CONTROL_ENABLED`/`CONTROL_TOKEN` variables were never read by the code.

### [0.3.7] - 2026-06-28

**Added:**
- **HA MQTT discovery for solar string sensors** — `ha_discovery.py` now publishes Home Assistant auto-discovery configs for all solar string MPPT metrics added in v0.3.5. Per-string sensors (voltage, current, power for strings A–F) and paired-rollup sensors (AB, CD, EF) are auto-discovered in HA when string data is present on the gateway. Multi-PW3 setups with numbered strings (A1–F2, etc.) are also supported. Previously, the string MQTT topics were publishing correctly but HA never received discovery payloads for them (#57, #59).

**Fixed:**
- **Fan speeds not fetched from TEDAPI** — `pw.vitals()` fan speed fields were silently dropped because the TEDAPI client lacked the `get_fan_speeds()` call path. Fan RPM is now included in vitals data for PW3 systems on TEDAPI (#58).
- **v1r local mode write routing** — control endpoints (`reserve`, `mode`, `grid_charging`) were skipped entirely when no cloud credentials were configured, even in v1r local mode. The `cloud_control_map` check is now split from the `_cloud_control` availability check so that mapped paths fall through to a direct `call_api` POST for local v1r control. Thanks @wabbitro (#60).

### [0.3.6] - 2026-06-27

**Changed:**
- Bumped `pypowerwall` dependency to `0.15.12` — brings in HTTP/2 support for Tesla Owner API calls (v0.15.11, required by Tesla for `auth.tesla.com` and `owner-api.teslamotors.com` endpoints) and remote setup / cloud auth improvements including headless setup 403 fix, `cloudcheck` diagnostics command, and `authtoken` dual-token output (v0.15.12).
- Minimum `pypowerwall` version in `pyproject.toml` raised from `>=0.14.0` to `>=0.15.12`.
- **`mqtt-tools/monitor.py` — refreshed dark palette** with a brighter modern dark theme and adjusted color assignments for better readability.

### [0.3.5] - 2026-06-19

**Added:**
- **MQTT solar string topics + PW3 paired rollups** — per-string solar data is now published to MQTT under `{prefix}/{gw}/strings/{A-F}/` (voltage, current, power, and full JSON). For PW3 dual-inverter setups, paired rollup topics (`AB`, `CD`, `EF`) are also published when both strings in a pair are present. Multi-gateway aware; graceful degradation when string data is unavailable (#48, #49).
- **Combined reserve+mode control endpoints** — `POST /control/reserve` and `POST /control/mode` now accept optional companion parameters (`mode=` and `level=` respectively) to update both reserve and mode in a single `set_operation()` call, avoiding duplicate Tesla audit-log entries. Fully backward compatible — omitting the companion parameter preserves original behavior. Ported from pypowerwall PR #308 (#52).
  - `POST /control/reserve` accepts optional `mode=<self_consumption|backup|autonomous>`
  - `POST /control/mode` accepts optional `level=<int>`
  - Invalid companion values return HTTP 400 without making any Powerwall call

**Fixed:**
- **Serialize concurrent write operations** — concurrent `/control/reserve` + `/control/mode` requests could race on the poll cache. A per-`GatewayManager` write lock now serializes all write calls through `call_api`, preventing corrupted state under concurrent load. Thanks @wabbitro (#55, #56).

### [0.3.4] - 2026-06-07

**Fixed:**
- **Multi-Powerwall visibility in TEDAPI full mode** — in WiFi-only TEDAPI mode (no v1r RSA key), follower Powerwalls were invisible in the `/freq` endpoint because their TEDAPI endpoints are unreachable without a WiFi session. The endpoint now uses `tedapi_config` (from gateway `config.json`, which always lists all registered units) as the authoritative Powerwall list and matches per-Powerwall data by serial number rather than sequential index. Follower units now appear with whatever data is available; fields not reachable without v1r are `null` (#47).
- **MQTT entities stuck "unavailable" on broker reconnect** — the global `{prefix}/availability` topic was never published as `"online"` after (re)connecting to the broker. Home Assistant entities therefore remained unavailable even while data was flowing. Fixed by publishing `"online"` (retained) to the global availability topic immediately after each successful connection (#33).
- **Orphan "Unknown Device" in Home Assistant** — HA discovery payloads included a `via_device: "pypowerwall-server"` field that referenced a device never registered with HA, creating a phantom device entry. Removed the field entirely (#34).
- **`grid_charging` control accepts only explicit booleans** — `POST /control/grid_charging` now returns HTTP 400 if `value` is absent or not a boolean, preventing silent state changes from malformed payloads (#29).

**Changed:**
- **Transient multi-PW TEDAPI snapshot guard** — if a single poll cycle drops follower vitals or `battery_blocks` (e.g., a momentary TEDAPI timeout), the cache layer now preserves the previous complete multi-Powerwall snapshot rather than replacing it with a degraded single-Powerwall view. Applies to all cache consumers (MQTT, WebSocket, `/pod`, `/freq`, etc.).

### [0.3.3] - 2026-05-26

**Added:**
- **`/pw/*` convenience endpoints** — 25 legacy proxy-compatible shorthand endpoints for backward compatibility with the original pypowerwall proxy. All endpoints are read-only, cache-backed (non-blocking), and thread-safe. Includes full test coverage (#13).
- **`din` and `uptime` polling** — `pw.din()` and `pw.uptime()` are now polled in the background poller alongside `pw.version()`, so the `/pw/din` and `/pw/uptime` endpoints return live data.

**Endpoint list:** `/pw/status`, `/pw/soe`, `/pw/battery`, `/pw/grid`, `/pw/home`, `/pw/solar`, `/pw/vitals`, `/pw/pods`, `/pw/strings`, `/pw/power`, `/pw/short`, `/pw/din`, `/pw/uptime`, `/pw/version`, `/pw/temp`, `/pw/alerts`, `/pw/site`, `/pw/status_aggregates`, `/pw/imei`, `/pw/fwupdate`, `/pw/solars`, `/pw/meters`, `/pw/orig`, `/pw/customer`, `/pw/networks`

### [0.3.2] - 2026-05-24

**Fixed:**
- **Battery percentage now Tesla-scaled** in API, MQTT, Home Assistant discovery, aggregate endpoints, and WebSocket outputs. Each surface now exposes both raw SOE and Tesla-app-scaled battery percentage. Previously, MQTT/HA consumers saw the raw SOE value (~4% higher than Tesla app) while the web dashboard happened to rescale client-side (#42). CSV outputs (`/csv`, `/csv/v2`) remain unchanged — they continue to publish raw SOE for backwards compatibility with Telegraf/InfluxDB. Thanks @sphen13 for the thorough diagnostics!
- **`PW_AUTH_PATH` env var** — fixed mismatched env var name (`PW_AUTHPATH` → `PW_AUTH_PATH`) in CLI argument processing so the documented variable name works everywhere (#41).

**Added:**
- Regression tests covering raw vs. scaled SOE across API, aggregate, MQTT publisher, and HA discovery surfaces.

### [0.3.1] - 2026-05-11

**Fixed:**
- **Fixed-tick polling cadence** — the background polling loop now uses fixed-tick scheduling instead of sleep-after-poll, so the effective cache refresh interval matches the configured `PW_CACHE_EXPIRE` value. Previously, poll duration + sleep time meant actual intervals were ~8–9 s instead of the configured 5 s (#38). Thanks @sphen13 for thorough testing across 5 s, 10 s, and 15 s intervals!
- Replaced deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in the polling loop.
- Corrected docstring: `loop.time()` is a monotonic clock, not wall-clock time.

### [0.3.0] - 2026-04-18

**Added:**
- **MQTT Integration** — publish live Powerwall telemetry to any MQTT broker. Set `MQTT_HOST` to enable. All sensor values are published under `{MQTT_TOPIC_PREFIX}/{gateway_id}/` with LWT (`offline`) and `availability` topics for clean broker state.
- **Home Assistant auto-discovery** — on connect, discovery payloads are published for all sensors so they appear automatically under a single HA device card, requiring zero manual HA configuration (#21).
- **`mqtt-tools/` folder** — `README.md` broker setup guide with CLI monitoring, HA integration steps, and live GUI instructions; `monitor.py` dark-theme tkinter GUI that subscribes to the broker and displays real-time Powerwall metrics for all gateways.
- **Console MQTT Broker panel** — new card on the management dashboard (`/`) showing broker connectivity, topic prefix, HA discovery, QoS, retain, and TLS status. Fetched from the new `GET /api/mqtt/status` endpoint.
- **`{prefix}/{gw}/name` topic** — friendly gateway name (from `gateways.yaml`) is published so the monitor GUI card title matches the configured name.
- **`MqttPublisher.connected` property** — safe public accessor for broker connection state (replaces internal `_connected` access).
- Exponential backoff reconnect in `MqttPublisher._connection_loop()` (2 s → 4 s → … → 60 s cap).
- TLS/SSL support via `MQTT_TLS`, `MQTT_TLS_CA_CERT`, and `MQTT_TLS_INSECURE` environment variables.
- 37 new tests: `tests/test_mqtt_publisher.py` (18) and `tests/test_mqtt_ha_discovery.py` (19), all with mock broker (no live MQTT dependency).

**Changed:**
- MQTT env variables added to `docker-compose.yml` as commented-out block for easy opt-in.

### [0.2.2] - 2026-03-04

**Added:**
- `PW_RSA_KEY_PATH` environment variable — path to an RSA-4096 private key PEM file for TEDAPI v1r LAN access (new pypowerwall authentication mode). Supported in single-gateway env-var config and `gateways.yaml` multi-gateway config.
- Console **Connect Mode** and **Connected Gateways** panels now display `TEDAPI v1r` when `rsa_key_path` is configured, or `TEDAPI` otherwise (previously always showed `TEDAPI`).
- `Dockerfile.beta` — alternative Dockerfile for beta builds that installs the production `requirements.txt` (for all transitive deps) then shadows the installed `pypowerwall` package with the local source tree via `PYTHONPATH=/app`. Used by `upload.sh` when building a beta release tag.
- Hybrid TEDAPI + cloud control for write operations — when TEDAPI is available, control commands (charge limit, operation mode) fall back to cloud if the TEDAPI write fails, ensuring reliable control across LAN and cloud paths (#19). Thanks @lemassykoi!

**Fixed:**
- Poll operation mode on every background cycle so the cached value stays current; corrected Docker healthcheck to use the correct endpoint (#14, #15). Thanks @jasonacox-sam!
- `pwModel()` incorrectly identified part number 3012170 as Powerwall 3 — corrected to Powerwall 2/2+ (#25). Thanks @jasonacox-sam!
- `rsa_key_path` excluded from API responses to prevent path disclosure; a safe boolean `rsa_key_configured` is exposed instead (#17).
- `upload.sh` trap used `-d` to test for the `pypowerwall_symlink` cleanup target — changed to `-L` so it correctly detects a symlink even if its target is temporarily missing (#17).
- `upload.sh` now checks that the `pypowerwall/` source tree exists before attempting a beta build, exiting with a clear error message if it is absent (#17).

### [0.2.0] - 2026-02-22

**Added:**
- `PROXY_BASE_URL` environment variable — serve pypowerwall-server under a sub-path (e.g. `/pypowerwall`) when hosted behind a reverse proxy alongside other services such as Grafana. All UI pages, asset references, API base URLs, redirects, and console links are rewritten at serve time to include the configured prefix.
- Fetch monkey-patch injected into both the powerflow UI and the management console when `PROXY_BASE_URL` is set, so `app.js` root-relative calls (e.g. `/stats`, `/version`, `/api/...`) are automatically prefixed without modifying the vendored JavaScript bundle.
- README: new **Reverse Proxy / HTTPS Proxy** section with a complete nginx configuration example showing how to co-host pypowerwall and Grafana on one HTTPS virtual host, explaining the `proxy_pass` trailing-slash prefix-stripping pattern and the `PROXY_BASE_URL` URL-generation role.

**Fixed:**
- **Static file 404 with Starlette 0.46+** — setting `root_path=_proxy_base` on the FastAPI app caused Starlette's `Mount.matches()` to update the child `root_path`, which `StaticFiles.get_path()` then used to double-prefix the file path (e.g. looking for `app/static/static/powerflow/app.css`). Removed `root_path` from the FastAPI constructor; path stripping is handled by the existing `strip_proxy_prefix` middleware instead.
- **`app.js` calling `/stats` without proxy prefix** — `app.js` uses root-relative fetch calls that bypass `window.apiBaseUrl`. Fixed by injecting a `window.fetch` monkey-patch into the powerflow `index.html` head (same pattern already used in the console) that prepends `PROXY_BASE_URL` to any root-relative URL.
- **`example.html` jQuery 404** — `<script src="/static/powerflow/jquery.min.js">` was hardcoded without the proxy base; changed to `{PROXY_BASE}/static/powerflow/jquery.min.js`.
- **`example.html` version URL broken on HTTPS** — old code used `window.location.hostname + ":" + window.location.port` which produces `lab.lan:` (empty port) on standard HTTPS; replaced with `window.location.host` (includes port only when non-standard) and added `{PROXY_BASE}` prefix.
- **Console iframe loading Grafana instead of Power Flow** — `<iframe src="/?style=clear">` resolved to Grafana's `location /`; changed to `{PROXY_BASE}/?style=clear`.
- **Console nav and footer links missing proxy base** — header links to `/`, `/docs`, `/api/gateways` and footer link to `/docs` all now use `{PROXY_BASE}/` so they resolve correctly when mounted under a sub-path.
- **CORS duplicate header** — both nginx `add_header` and pypowerwall's CORSMiddleware were setting `Access-Control-Allow-Origin`, causing browsers to reject credentialed iframe requests. Resolved with `proxy_hide_header` in nginx (documented in README).
- **CORS credentialed iframe** — `allow_origins=["*"]` is forbidden with `allow_credentials=True`; switched to `allow_origin_regex=".*"` so Starlette reflects the actual request `Origin` header, satisfying browsers for credentialed cross-origin requests.
- **HTTPS Mixed Content / wrong scheme** — API base URL now honours `X-Forwarded-Proto` and `X-Forwarded-Host` headers so the injected `window.apiBaseUrl` uses the correct `https://` scheme and host when running behind an HTTPS reverse proxy.
- **Stray `}` syntax errors** in all six theme files (`black.js`, `dakboard.js`, `grafana.js`, `grafana-dark.js`, `white.js`, `solar.js`) that caused `SyntaxError` in the browser console.
- **`$ is not defined` in theme files** — theme scripts ran before jQuery was globally available. Fixed by explicitly loading `jquery.min.js` after `app.js` in `powerflow/index.html` so `$` is available when themes execute.

### [0.1.13] - 2026-06-26

**Added:**
- Gateway `type` field (`"powerwall"` | `"inverter"`) — inverter-only sites can now be declared explicitly so the console skips battery panels for them (pypowerwall/issues#254)
- Gateway `port` field — non-standard HTTPS ports (e.g. behind a travel router on `:8443`) are now supported; `pypowerwall/__init__.py` updated to strip the port suffix before IP/hostname regex validation (pypowerwall/issues#254)
- New aggregate API endpoints for multi-gateway disambiguation (pypowerwall/issues#254):
  - `GET /api/aggregate/strings` — per-gateway solar string data keyed by gateway ID
  - `GET /api/aggregate/alerts` — per-gateway alert lists keyed by gateway ID
  - `GET /api/aggregate/vitals` — per-gateway vitals dicts keyed by gateway ID
- Console multi-gateway support — Alerts, Solar Strings, and Powerwall Status panels now detect multiple gateways and render per-gateway labeled sections with gateway names as section headers (pypowerwall/issues#254)
- `gateways.yaml` examples for inverter-type and travel-router-port configurations

**Changed:**
- Console initialization wrapped in async IIFE so gateway metadata (`/api/gateways/`) is fetched before all data panels load, enabling correct single-vs-multi branching on page load

### [0.1.12] - 2026-02-21

**Fixed:**
- Fix powerflow animation showing login screen after a few hours or browser restart (#7)
  - Added `POST /api/login/Basic` fake-login endpoint that the Tesla Gateway web app calls when re-authenticating
  - Added middleware to inject `AuthCookie` and `UserRecord` cookies (10-year expiry) on every successful response, matching the original pypowerwall proxy behavior
  - Patched `isAuthenticated` in the bundled `app/static/powerflow/app.js` so the login screen is never shown regardless of cookie or localStorage state
- Fix Powerwall capacity spec comparison using wrong rated capacity (12.5 kWh → 13.5 kWh per Powerwall unit) (#9)
- Added missing `GET /api/system_status` endpoint (parent route for existing `/soe`, `/grid_status`, `/grid_faults` sub-routes)

**Added:**
- Console System Health panel: **Powerwall Mode** item showing current operating mode (Self-Consumption, Backup, Time-Based, Off-Grid), per feature request (#1)
- Console System Health panel: **Firmware** item displaying gateway firmware version, per feature request (#1)
- Renamed "Uptime" label to **"Server Uptime"** for clarity

**Changed:**
- Suppress verbose uvicorn access log spam: `GET /api/...` lines now only appear at WARNING level and above
- Suppress websocket connection noise (`connection open`, `connection closed`, `WebSocket ... [accepted]`) from logs unless running in debug mode

### [0.1.11] - 2026-02-03

**Fixed:**
- Bug Fix: Refactor POD data extraction to handle missing values gracefully and ensure energy values always overwrite system status - resolves Internal Server Error on `/pod` endpoint (#5)
- Fixed issue where `/pod` endpoint would fail with Internal Server Error when extended info was not available
- POD data extraction now properly handles None values and missing battery block data
- Energy values from battery blocks now correctly populate the vitals section

### [0.1.10] - 2026-01-24

**Fixed:**
- Corrected Backup Reserve display on the console by removing duplicate frontend scaling. The server now returns the Tesla-scaled reserve and the console displays it directly.

**Added:**
- Segmented vertical battery graphic for **Total Capacity** (blue) and **Current Charge** (green) on the `/console` dashboard.
- Gray segmented indicator for **Backup Reserve** in the backup panel.
- Time Remaining clock infographic: an SVG pie-sector that scales its total (12 → 24 → 48…) until the remaining hours fit, with a thicker outline and inset fill.

**Changed:**
- Bumped package version to 0.1.10 and synchronized `SERVER_VERSION` in configuration.
- Removed redundant percent label elements from the console UI and removed the center clock dot for a cleaner look.


### [0.1.9] - 2026-01-23

**Fixed:**
- **Critical:** Grid down error in TEDAPI mode when grid breakers are turned off
  - Fixed `compute_LL_voltage()` function in pypowerwall TEDAPI module
  - Error: "TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'"
  - When no active voltages (all below 100V threshold), function now safely handles None values: `(v1n or 0) + (v2n or 0) + (v3n or 0)`
  - Powerwall API returns None for voltage readings when grid breakers are off
  - Fix allows `/api/meters/aggregates` and other endpoints to work correctly during grid outages
- Pydantic serialization warning for gateway status field
  - Changed `status` field type from `Optional[str]` to `Optional[Union[str, Dict[str, Any]]]` in GatewayData model
  - Allows storing full status dict from `pw.status()` API call without type validation warnings

**Changed:**
- Updated pypowerwall dependency from 0.14.8 to 0.14.9 (includes grid down fix)
- None values from Powerwall API now preserved to indicate missing/unavailable data

---
### [0.1.8] - 2026-01-22

**Fixed:**
- Battery percentage scaling now consistently uses Tesla App formula across all endpoints:
  - `/api/system_status/soe` now applies Tesla scaling: `(raw / 0.95) - (5 / 0.95)` instead of old proxy's `raw * 0.95`
  - Console dashboard battery charge and backup reserve displays use Tesla scaling
  - Scaling properly reserves bottom 5%: raw 5% → 0% displayed, raw 100% → 100% displayed
  - All battery percentage displays now match Tesla App behavior
- Grid status display on console dashboard:
  - Shows "Grid Down" with orange X (✕) when grid is down
  - Grid status checked from cached `grid_status` field before power-based fallback
  - Real-time grid status updates via background polling
- Legacy API endpoint compatibility improvements:
  - `/api/status` returns all required fields (din, git_hash, commission_count, device_type, teg_type, sync_type, cellular_disabled, can_reboot)
  - `/api/site_info` includes complete grid_code structure and energy/power capacity fields
  - `/api/site_info/site_name` returns null instead of fake default
  - `/api/operation` added with direct API call to return raw (unscaled) backup_reserve_percent
  - `/pod` endpoint properly matches TEPOD vitals to battery blocks by serial number
  - `/api/system_status/grid_status` serves from cached grid_status_detail with full API response

**Changed:**
- Background polling now calls `pw.get_reserve(scale=False)` to store raw reserve percentage
- Reserve percentage from API remains unscaled (0-100), only display values are scaled
- Grid status polling enhanced to capture both simplified status and detailed API response

---
### [0.1.7] - 2026-01-18

**Added:**
- Powerwall 3 (PW3) detection support:
  - Cached `pw3` status from pypowerwall TEDAPI connection during polling cycle
  - `/stats` endpoint now correctly reports `pw3: true` for Powerwall 3 systems
  - Console dashboard mode display now indicates PW3 hardware (e.g., "Local (TEDAPI PW3)")
- TEDAPI mode caching for improved performance:
  - `tedapi_mode` cached during polling cycle alongside other gateway metrics
  - Eliminates redundant connection object access in API endpoints

**Fixed:**
- PW3 detection now correctly accesses `pw.tedapi.pw3` attribute (was incorrectly checking `pw.pw3`)
- Console dashboard mode display restructured to show clear connection types:
  - Local, Local (TEDAPI), Local (TEDAPI PW3)
  - Cloud, Cloud (PW3), Cloud (FleetAPI), Cloud (FleetAPI PW3)

**Changed:**
- `sync.sh` deployment script now uses `--copy-links` flag to copy symlink contents instead of just the link
- Updated pypowerwall dependency to newer version with PW3 power reporting bug fix

---
### [0.1.6] - 2026-01-17

**Added:**
- Enhanced console dashboard (`/console`) with comprehensive monitoring panels:
  - Powerwall Status panel with individual Powerwall metrics (capacity, voltage, power, frequency)
  - Power direction indicators (↑ charging, ↓ discharging) on Powerwall power values
  - Total energy storage metrics: capacity, current charge, time remaining, backup reserve
  - Tesla App percentage display alongside actual charge percentage
  - Capacity comparison to spec (12.5 kWh per Powerwall) with color-coded indicators *(corrected to 13.5 kWh in v0.1.12, see #9)*
  - System Health panel with site name, mode, gateways, connection status, uptime, and resource metrics
- Alert sorting by priority (notice → info → warning) in console dashboard

**Fixed:**
- Site name endpoints now return actual Powerwall site name instead of gateway configuration name
  - `/api/site_info/site_name` now includes both site_name and timezone
  - `/api/site_info` returns actual site name from Powerwall
  - `/stats` includes actual site name in response
  - Site name fetched during polling cycle for thread-safe cached access
- Power values in Powerwall Status panel correctly converted to kW units

---
### [0.1.5] - 2026-01-17

**Fixed:**
- `/freq` endpoint now returns comprehensive frequency, current, voltage, and grid status data
  - Returns detailed device data from `system_status` (battery_blocks) and `vitals` (TEPINV, TESYNC, TEMSA)
  - Includes PW device names, frequencies, voltages, package part/serial numbers
  - Includes power output metrics (p_out, q_out, v_out, f_out, i_out)
  - Includes ISLAND and METER metrics from Backup Gateway/Switch
  - Grid status now returns numeric format (1 = UP, 0 = DOWN) matching old proxy behavior
  - Fallback to simple freq value when detailed data unavailable (e.g., Cloud Mode)
  - Note: Full device data requires Local/TEDAPI mode; Cloud Mode has limited data

---
### [0.1.5] - 2026-01-17

**Fixed:**
- `/freq` endpoint now returns comprehensive frequency, current, voltage, and grid status data
  - Returns detailed device data from `system_status` (battery_blocks) and `vitals` (TEPINV, TESYNC, TEMSA)
  - Includes PW device names, frequencies, voltages, package part/serial numbers
  - Includes power output metrics (p_out, q_out, v_out, f_out, i_out)
  - Includes ISLAND and METER metrics from Backup Gateway/Switch
  - Grid status now returns numeric format (1 = UP, 0 = DOWN) matching old proxy behavior
  - Fallback to simple freq value when detailed data unavailable (e.g., Cloud Mode)
  - Note: Full device data requires Local/TEDAPI mode; Cloud Mode has limited data

---

### [0.1.4] - 2026-01-17

**Added:**
- Comprehensive DESIGN.md documentation with Mermaid architecture diagrams
- `/json` endpoint for combined metrics (grid, home, solar, battery, soe, grid_status, reserve, time_remaining, energy data, strings)
- `PW_NEG_SOLAR` environment variable support for negative solar correction

**Improved:**
- Centralized negative solar correction at fetch time in gateway_manager
  - Eliminates duplicate code across `/aggregates`, `/csv`, `/csv/v2`, `/json` endpoints
  - Removes unnecessary `deepcopy` on every request
  - All endpoints now automatically get consistent corrected data
- Moved inline `import json` statements to module-level imports in gateway_manager

---

### [0.1.3] - 2026-01-17

**Added:**
- Color-coded alert categorization in console UI
  - Notice alerts (green ✓): FWUpdateSucceeded, SystemConnectedToGrid, GridCodesWrite, PodCommissionTime
  - Info alerts (blue ℹ): ScheduledIslandContactorOpen, SelfTest
  - Warning alerts (yellow ⚠️): All other alerts
- Improved alert panel scrolling to fill available height

**Fixed:**
- Alert list scroll area now properly fills the panel height

---

### [0.1.2] - 2026-01-17

**Fixed:**
- Alerts panel scroll behavior corrected to use full card height

---

### [0.1.1] - 2026-01-17

**Added:**
- PyPI package support with `pip install pypowerwall-server`
- CLI command `pypowerwall-server` with full argument support
- `--setup` flag for Tesla Cloud authentication setup
- Static files now included in Python package distribution

**Fixed:**
- Package structure to include app/static/* files in distribution
- Authentication setup now uses subprocess to call pypowerwall correctly

---

### [0.1.0] - Initial Release

Initial release of PyPowerwall Server as next-generation evolution of pypowerwall proxy.

**Core Features:**
- Multi-gateway support for monitoring multiple Powerwall installations
- Background polling with intelligent caching (5-second default interval)
- Graceful degradation when gateways are temporarily offline
- WebSocket streaming for real-time updates (1-second intervals)
- Full backward compatibility with pypowerwall proxy endpoints
- TEDAPI, Cloud Mode, and FleetAPI connection support
- Tesla Power Flow animation UI with real-time updates
- Management console for gateway status
- Auto-generated API documentation (Swagger UI and ReDoc)
- Health monitoring endpoint: `/health`
- Comprehensive test suite with pytest
- Docker and docker-compose support
- Configuration via environment variables or YAML file

**API Endpoints:**
- Legacy proxy endpoints (backward compatible): `/vitals`, `/aggregates`, `/soe`, `/csv`, etc.
- Multi-gateway endpoints: `/api/gateways/*`
- Aggregate data endpoints: `/api/aggregate/*`
- WebSocket endpoints: `/ws/gateway/{id}` and `/ws/aggregate`

**Architecture:**
- FastAPI-based async server with sync pypowerwall integration
- ThreadPoolExecutor for non-blocking pypowerwall calls
- Exponential backoff for failed gateway connections
- Lazy initialization of pypowerwall connections
- Stateless server design (historical data in browser localStorage)
- Cached responses for instant API access
- Concurrent gateway polling using asyncio
- Dynamic thread pool sizing: max(10, num_gateways * 3)
- Automatic cleanup of dead WebSocket connections

**Connection Modes:**
- TEDAPI (local gateway access)
- Cloud Mode (remote access)
- FleetAPI support

**Deployment:**
- Docker and docker-compose
- Environment variable configuration
- YAML configuration file support

---

## Planned Features

### Future Releases

**MQTT Integration**
- Publish metrics to MQTT brokers
- Home Assistant MQTT discovery
- Configurable topic patterns and message formats

**Enhanced UI**
- Historical data visualization
- Multi-gateway dashboard
- Gateway comparison views
- Dark/light theme switching

**Performance**
- Configurable polling intervals per gateway
- Advanced caching strategies
- Metrics and monitoring

**Control Features**
- Enhanced control operations
- Batch control across multiple gateways
- Scheduling and automation

---

## Migration Notes

### From pypowerwall proxy

PyPowerwall Server is a drop-in replacement:
- All proxy API endpoints work unchanged
- Same environment variables supported
- No changes needed to Telegraf/Grafana integrations
- Simply change Docker image name

### Breaking Changes

None - Full backward compatibility maintained.

---

## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for development guidelines and how to submit changes.

## Support

- **Issues:** https://github.com/jasonacox/pypowerwall-server/issues
- **Discussions:** https://github.com/jasonacox/pypowerwall-server/discussions
- **Wiki:** https://github.com/jasonacox/pypowerwall-server/wiki
