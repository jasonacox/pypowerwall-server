# Plan: Standalone Inverter & Multi-Gateway Console Improvements

Tracking issue: pypowerwall/issues#254 (to be ported to pypowerwall-server repo)

This plan covers four features to support sites that have one or more standalone Tesla
inverters (no Powerwall batteries) connected alongside — or instead of — a full gateway.

---

## Feature 1: Gateway Type Declaration

**Problem:** The server treats all configured gateways identically. An inverter-only
gateway has no battery data — `soe`, `system_status.battery_blocks`, and `reserve` will
always be empty — but the console still renders Powerwall Status and battery graphics for
it, showing blank panels.

**Approach:** Add an explicit `type` field to the gateway configuration. Auto-detection
from polling data is brittle (empty battery data could also mean a temporarily offline
unit); explicit declaration is cleaner and more reliable.

### Changes

**`app/models/gateway.py`** — `Gateway` model:
```python
# New field with default so existing configs need no changes
type: str = "powerwall"  # "powerwall" | "inverter"
```
`GatewayStatus` should surface this field so API consumers and the console can read it.

**`gateways.yaml`** — add `type: inverter` example:
```yaml
  # Example: Standalone inverter (no Powerwall batteries)
  - id: inverter-south
    name: South Array Inverter
    host: 192.168.91.1       # or travel-router IP
    gw_pwd: your_gateway_password
    type: inverter           # Omit Powerwall battery panels in console
    timezone: America/Los_Angeles
```

**`app/core/gateway_manager.py`** — pass `type` through to `GatewayStatus` so it is
visible in `/api/gateways` responses.

**Tests:** Add a `type: inverter` fixture gateway and assert that `/api/gateways/{id}`
returns the correct type.

---

## Feature 2: Per-Gateway Labeled Sections in Console Panels

**Problem:** When multiple gateways are configured, the **Alerts**, **Solar Strings**, and
**Powerwall Status** panels show data from the "default" gateway only. Users with an
inverter on a separate gateway lose visibility into its strings and alerts.

**Approach:** Add new aggregate API endpoints that return per-gateway data. The console
detects multiple gateways at load time and conditionally renders labeled sub-sections.
Single-gateway installs see no visual change.

### New API Endpoints (`app/api/aggregates.py`)

```
GET /api/aggregate/strings   → { "home": {"A": {...}, "B": {...}}, "inverter-south": {"A": {...}} }
GET /api/aggregate/alerts    → { "home": ["FWUpdateSucceeded", ...], "inverter-south": [...] }
GET /api/aggregate/vitals    → { "home": {...}, "inverter-south": {...} }
```

Each endpoint iterates `gateway_manager.list_gateways()` and returns data keyed by
gateway ID, skipping offline gateways (or including them with `null` data, TBD by design
preference). These read from the existing per-gateway cache — no new polling needed.

### Console Changes (`app/static/index.html`)

The console already fetches `/api/gateways` on load to build the System Health panel.
Extend startup to also load the gateway list and count.

**When `gateways.length === 1`:** Current behavior, no labels.

**When `gateways.length > 1`:**
- **Alerts panel:** Render one `<div class="gateway-section">` per gateway with a bold
  gateway name header, then the existing color-coded alert list inside it.
- **Solar Strings panel:** One labeled section per gateway with its own strings table.
- **Powerwall Status panel:** One labeled section per gateway that has `type === "powerwall"`.
  Inverter-only gateways are **entirely omitted** from this panel (they have no batteries).
  The Total Energy / Backup Reserve / Time Remaining block at the top of this panel
  aggregates only across powerwall-type gateways.

Battery graphics and the System Health panel are unaffected (they already aggregate or
are gateway-aware).

### Tests

- New tests in `tests/test_api_aggregates.py` for the three new endpoints.
- Mock a two-gateway fixture (one `powerwall`, one `inverter`) and assert correct shape.

---

## Feature 3: String Namespace Disambiguation

**Problem:** Both a full gateway and a standalone inverter may return a solar string named
`"A"`. When two gateways both report `"A"`, the aggregate `/strings` endpoint (or a
Telegraf/InfluxDB pipeline that merges them) will silently discard one of them.

**Approach:** The new `/api/aggregate/strings` endpoint (Feature 2) returns strings
*nested under the gateway ID key*, which is enough disambiguation for API consumers.
For the legacy `/strings` endpoint, nothing changes (it serves only the default gateway,
no collision possible).

For users who push data to InfluxDB via the aggregate endpoint, the gateway-keyed
structure (`home/A`, `inverter-south/A`) makes it trivial to add a `gateway` tag in
Telegraf without needing `[[processor.rename.replace]]` workarounds.

**Console:** The Solar Strings section (Feature 2) already solves this for the UI by
showing strings inside a per-gateway labeled section.

**No changes needed to `legacy.py` `/strings`.** Changes are entirely in
`app/api/aggregates.py` (the new endpoint from Feature 2).

---

## Feature 4: Non-Standard Port Support

**Problem:** Users who bridge their Powerwall's Tesla Wi-Fi using a travel router with
port-forwarding (e.g., `192.168.1.50:8443 → 192.168.91.1:443`) cannot configure
pypowerwall-server to reach the gateway at a non-standard port because the `host` field
accepts only a hostname/IP and `pypowerwall.Powerwall()` has no separate `port` kwarg.

### Investigation Result

URL construction in both `pypowerwall/local/pypowerwall_local.py` and
`pypowerwall/tedapi/__init__.py` interpolates `self.host` / `self.gw_ip` directly into
HTTPS URLs (`"https://%s%s" % (self.host, api)`), so `host:port` strings produce valid
URLs like `https://192.168.1.50:8443/api/...` without any changes to those modules.

**The only blocker was input validation** in `pypowerwall/__init__.py`
`_validate_init_configuration()`: `IPV4_6_REGEX` and `HOST_REGEX` only match bare
hosts. Fixed in the local copy by stripping any `:<digits>` port suffix before the
regex check. **This change has already been applied to `pypowerwall/__init__.py`.**

### Changes

**`app/models/gateway.py`** — `Gateway` model:
```python
port: Optional[int] = None  # Non-standard port (default: 443 / pypowerwall default)
```

**`gateways.yaml`** — add example:
```yaml
  # Example: Gateway behind a travel router on a custom port
  - id: garage
    name: Garage Gateway (travel router)
    host: 192.168.1.50
    port: 8443
    gw_pwd: your_gateway_password
    timezone: America/Los_Angeles
```

**`app/core/gateway_manager.py`** — when building TEDAPI kwargs:
```python
effective_host = f"{config.host}:{config.port}" if config.port else config.host
tedapi_kwargs = {"host": effective_host, "gw_pwd": config.gw_pwd, ...}
```

**Tests:** Mock gateway with `port: 8443` and assert the correct host string is passed
to `pypowerwall.Powerwall()`.

### Fallback

If `host:port` does not work inside pypowerwall, document the limitation in the README
with a note that users can configure their travel router to forward port 443 to the
gateway, making a custom port unnecessary at the pypowerwall level.

---

## Implementation Order

| Step | Feature | Files | Status |
|------|---------|-------|--------|
| 1 | Gateway type field | `models/gateway.py`, `gateways.yaml`, `gateway_manager.py` | ✅ Done (v0.1.13) |
| 2 | Investigate `host:port` in pypowerwall | `pypowerwall/__init__.py` | ✅ Done — stripped port suffix before regex validation |
| 3 | `/api/aggregate/strings`, `/alerts`, `/vitals` endpoints | `api/aggregates.py` | ✅ Done (v0.1.13) |
| 4 | Per-gateway labeled console sections | `static/index.html` | ✅ Done (v0.1.13) |
| 5 | Non-standard port config | `models/gateway.py`, `gateway_manager.py`, `gateways.yaml` | ✅ Done (v0.1.13) |

---

## Out of Scope

- mDNS/auto-discovery of gateways on the local network
- Built-in NAT/iptables management for multiple `192.168.91.1` addresses
- Inverter start/stop control buttons (separate feature, tracked elsewhere)
