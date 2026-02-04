# Release Notes

## Version History

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
  - Capacity comparison to spec (12.5 kWh per Powerwall) with color-coded indicators
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
