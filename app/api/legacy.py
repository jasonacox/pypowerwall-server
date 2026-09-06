"""
Legacy Proxy-Compatible API Endpoints

This router provides backward compatibility with the original pypowerwall proxy server.
Routes are registered WITHOUT a prefix (included directly at root level in main.py).

Key Routes (all cache-backed for graceful degradation):
    - /aggregates, /api/meters/aggregates -> Power meter data
    - /soe, /api/system_status/soe -> Battery state of energy
    - /csv, /csv/v2 -> CSV formatted data for Telegraf/InfluxDB
    - /vitals -> Detailed system vitals
    - /strings -> Solar string data
    - /temps, /temps/pw -> Temperature data
    - /alerts, /alerts/pw -> System alerts
    - /version -> Firmware version
    - /stats -> Server statistics
    - /api/networks, /api/system/networks -> Network configuration
    - /api/powerwalls -> Powerwall device list
    - /api/system_status -> Full system status (cached)
    - /api/system_status/soe -> Battery state of energy
    - /api/system_status/grid_status -> Grid connection status
    - /api/system_status/grid_faults -> Grid fault events

Auth Routes (powerflow web app compatibility):
    - POST /api/login/Basic -> Fake login; sets long-lived AuthCookie/UserRecord

Control Routes (require authentication):
    - POST /control/{path} -> Control operations (reserve, mode, etc.)

Design Principles:
    1. EXPLICIT ENDPOINTS ONLY - No catch-all /api/{path:path} routes
       Every endpoint is explicitly defined to ensure predictable behavior.
    
    2. CACHE-BACKED DATA - All data comes from background polling cache
       This ensures graceful degradation when gateway is slow/offline.
       No on-demand blocking calls during HTTP requests.
    
    3. SAFE DEFAULTS - Returns empty arrays/nulls on errors
       Keeps UI responsive even during outages.
    
    4. FAST-FAIL - Checks cached gateway status before attempting calls
       Prevents request pile-up during network issues.

Adding New Endpoints:
    If you need a new /api/* endpoint, add it explicitly with cache support.
    Do NOT add catch-all routes - they break graceful degradation.
"""
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import psutil
import pypowerwall
from fastapi import APIRouter, HTTPException, Response, Header

from app.api.auth import verify_control_token
from app.core.gateway_manager import gateway_manager
from app.config import settings, SERVER_VERSION
from app.utils.stats_tracker import stats_tracker

logger = logging.getLogger(__name__)

router = APIRouter()


def _device_serial(value: object) -> Optional[str]:
    """Extract a serial number from a TEDAPI VIN/DIN or plain serial value."""
    if not isinstance(value, str) or not value:
        return None
    return value.rsplit("--", 1)[-1]


def _expansion_parent_indexes(
    system_status: Optional[dict], tedapi_config: Optional[dict]
) -> dict[int, int]:
    """Map battery-expansion block indexes to their leader block indexes.

    TEDAPI config stores expansions under their leader while system status exposes
    each expansion as a separate ``battery_blocks`` entry.  The mapping lets the
    legacy flat ``/pod`` response retain the parent relationship for the Console.
    """
    if not isinstance(system_status, dict) or not isinstance(tedapi_config, dict):
        return {}

    blocks = system_status.get("battery_blocks") or []
    if not isinstance(blocks, list):
        return {}

    serial_to_index = {}
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            continue
        serial = _device_serial(block.get("PackageSerialNumber"))
        if serial:
            serial_to_index[serial] = index

    parent_indexes: dict[int, int] = {}
    config_blocks = tedapi_config.get("battery_blocks") or []
    if not isinstance(config_blocks, list):
        return parent_indexes

    for config_block in config_blocks:
        if not isinstance(config_block, dict):
            continue
        leader_serial = _device_serial(
            config_block.get("vin") or config_block.get("din")
        )
        leader_index = serial_to_index.get(leader_serial)
        if not leader_index:
            continue
        for expansion in config_block.get("battery_expansions") or []:
            if not isinstance(expansion, dict):
                continue
            expansion_serial = _device_serial(
                expansion.get("din") or expansion.get("vin")
            )
            expansion_index = serial_to_index.get(expansion_serial)
            if expansion_index:
                parent_indexes[expansion_index] = leader_index

    return parent_indexes


@router.post("/control/{path:path}")
async def control_api(
    path: str, data: dict, authorization: Optional[str] = Header(None)
):
    """Authenticated control endpoint for POST operations.

    Routes to cloud control connection for write operations (set_reserve, set_mode,
    set_grid_charging) when available, since TEDAPI doesn't support POST/write APIs.
    Falls back to direct post for cloud-mode or FleetAPI gateways.

    Optional companion parameters (ported from pypowerwall PR #308):
    - POST /control/reserve with mode=<mode> calls set_operation(level, mode)
    - POST /control/mode with level=<int> calls set_operation(level, mode)
    """
    verify_control_token(authorization)

    # Validate grid_charging requires an explicit boolean value to prevent
    # silent state changes from malformed or empty payloads.
    if path == "grid_charging":
        if "value" not in data or not isinstance(data["value"], bool):
            raise HTTPException(
                status_code=400,
                detail="'value' must be a boolean (true or false)",
            )

    # Same for reserve and mode: an empty or typoed payload must never
    # silently drain the backup reserve to 0 or flip the operating mode.
    valid_modes = ["self_consumption", "backup", "autonomous"]

    if path == "reserve":
        value = data.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail="'value' must be an integer reserve level (0-100)",
            )
        if not 0 <= value <= 100:
            raise HTTPException(
                status_code=400,
                detail="'value' must be between 0 and 100",
            )

    if path == "mode":
        if data.get("value") not in valid_modes:
            raise HTTPException(
                status_code=400,
                detail="'value' must be a valid mode: " + ", ".join(valid_modes),
            )

    # Optional companion parameters for combined reserve+mode writes.
    # When a caller wants to change both reserve and mode, sending them in a
    # single request avoids duplicate Tesla audit-log entries caused by
    # set_reserve() + set_mode() each calling set_operation() internally.
    # Omitting the companion parameter preserves the original behaviour.
    if path == "reserve" and "mode" in data:
        mode_val = data["mode"]
        if mode_val not in valid_modes:
            raise HTTPException(
                status_code=400,
                detail="Invalid 'mode' companion value. Must be one of: "
                       + ", ".join(valid_modes),
            )
        level = data["value"]
        if gateway_manager._cloud_control:
            result = await gateway_manager.cloud_control(
                "set_operation", level, mode_val, timeout=10.0
            )
            if result is None:
                raise HTTPException(
                    status_code=503,
                    detail="Control operation failed via cloud",
                )
            return result
        # Fallback for local gateways (v1r / Basic LAN): use the library's
        # set_operation() on the gateway's own local connection so the write
        # is translated correctly instead of a raw POST the gateway rejects.
        gateway_id = get_default_gateway()
        result = await gateway_manager.local_control(
            gateway_id, "set_operation", level, mode_val, timeout=10.0
        )
        if result is None:
            raise HTTPException(
                status_code=503,
                detail="Control operation failed or gateway not available",
            )
        return result

    if path == "mode" and "level" in data:
        level_val = data["level"]
        if not isinstance(level_val, int) or isinstance(level_val, bool):
            raise HTTPException(
                status_code=400,
                detail="Invalid 'level' companion value. Must be an integer.",
            )
        mode = data["value"]
        if gateway_manager._cloud_control:
            result = await gateway_manager.cloud_control(
                "set_operation", level_val, mode, timeout=10.0
            )
            if result is None:
                raise HTTPException(
                    status_code=503,
                    detail="Control operation failed via cloud",
                )
            return result
        # Fallback for local gateways (v1r / Basic LAN): set_operation() on
        # the local connection rather than a raw POST to /api/operation.
        gateway_id = get_default_gateway()
        result = await gateway_manager.local_control(
            gateway_id, "set_operation", level_val, mode, timeout=10.0
        )
        if result is None:
            raise HTTPException(
                status_code=503,
                detail="Control operation failed or gateway not available",
            )
        return result

    # Map control paths to pypowerwall control methods. Used for both cloud
    # control (TEDAPI gateways with cloud credentials, hybrid mode) and local
    # control (v1r / Basic LAN gateways without cloud credentials).
    control_map = {
        "reserve": ("set_reserve", lambda d: [d["value"]]),
        "mode": ("set_mode", lambda d: [d["value"]]),
        "grid_charging": ("set_grid_charging", lambda d: [d["value"]]),
    }

    if path in control_map:
        method, args_fn = control_map[path]
        if gateway_manager._cloud_control:
            result = await gateway_manager.cloud_control(
                method, *args_fn(data), timeout=10.0
            )
        else:
            # Local mode (v1r / Basic LAN): call the mapped library method on
            # the gateway's own connection instead of a raw POST, which the
            # gateway's local API rejects with "Unknown API".
            gateway_id = get_default_gateway()
            result = await gateway_manager.local_control(
                gateway_id, method, *args_fn(data), timeout=10.0
            )
        if result is None:
            raise HTTPException(
                status_code=503, detail=f"Control operation failed for {path}"
            )
        return result

    # Fallback: direct post for cloud-mode/FleetAPI gateways or unmapped paths
    gateway_id = get_default_gateway()
    result = await gateway_manager.call_api(
        gateway_id, "post", f"/api/{path}", data, timeout=10.0
    )
    if result is None:
        raise HTTPException(
            status_code=503, detail="Control operation failed or gateway not available"
        )
    return result


def get_default_gateway():
    """Get the default gateway (first one or 'default' id)."""
    if "default" in gateway_manager.gateways:
        return "default"
    if gateway_manager.gateways:
        return list(gateway_manager.gateways.keys())[0]
    raise HTTPException(status_code=503, detail="No gateways configured")


# Login cookie max-age: 10 years for long-running kiosk dashboards
_AUTH_COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60  # 315360000 seconds


@router.post("/api/login/Basic")
async def post_login_basic(response: Response):
    """Fake login endpoint for powerflow web app compatibility (issue #7).

    The Tesla Gateway web app (served from /powerflow) stores auth state in
    localStorage.  When that state expires the app calls POST /api/login/Basic
    to re-authenticate.  Without this endpoint the powerflow UI shows a login
    screen.

    We return a synthetic success payload and set long-lived AuthCookie /
    UserRecord cookies so the browser never hits the login screen again.
    """
    response.set_cookie(
        key="AuthCookie",
        value="1234567890",
        max_age=_AUTH_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
    )
    response.set_cookie(
        key="UserRecord",
        value="1234567890",
        max_age=_AUTH_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
    )
    return {
        "email": "",
        "firstname": "Tesla",
        "lastname": "Energy",
        "roles": ["Home_Owner"],
        "token": "1234567890",
        "provider": "Basic",
        "loginType": "Basic",
    }


@router.get("/vitals")
async def get_vitals():
    """Get vitals data (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns empty object if no data available yet.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.vitals or {}


@router.get("/strings")
async def get_strings():
    """Get strings data (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns empty object if no data available yet.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.strings or {}


@router.get("/aggregates")
async def get_aggregates():
    """Get aggregates data (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns empty object if no data available yet.

    Note: Negative solar correction (PW_NEG_SOLAR) is applied at fetch time in gateway_manager.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates


@router.get("/soe")
async def get_soe():
    """Get state of energy (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns Tesla-scaled percentage plus the preserved raw percentage if available.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"percentage": None, "raw_percentage": None}

    return {
        "percentage": status.data.soe,
        "raw_percentage": status.data.soe_raw,
    }


@router.get("/freq")
async def get_freq():
    """Get frequency, current, voltage and grid status data (legacy proxy endpoint).

    Returns comprehensive data including:
    - PW device names, frequencies, voltages
    - Package part/serial numbers
    - Power output metrics
    - ISLAND and METER metrics
    - Grid status

    Note: Cloud mode may not support all fields. Local/TEDAPI mode provides most data.

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"freq": None}

    fcv = {}
    vitals = status.data.vitals or {}
    system_status = status.data.system_status or {}

    # --- Build serial-keyed lookup maps ---

    # Map PackageSerialNumber → system_status battery block
    ss_block_map: dict = {}
    for block in (system_status.get("battery_blocks") or []):
        serial = block.get("PackageSerialNumber")
        if serial:
            ss_block_map[serial] = block

    # Map PackageSerialNumber → (TEPINV device key, TEPINV vitals data)
    # Key format: TEPINV--{partno}--{serial}  (TEDAPI / PW3)
    #         or: TEPINV--{serial}              (local API / older PW)
    # rsplit on "--" with maxsplit=1 extracts the serial from both formats.
    tepinv_map: dict = {}
    for device, d in vitals.items():
        if device.startswith("TEPINV--"):
            serial = device.rsplit("--", 1)[1]
            tepinv_map[serial] = (device, d)

    # --- Determine authoritative ordering of Powerwalls ---
    # Use tedapi_config (fetched from gateway config.json) as the authoritative source.
    # In TEDAPI full mode (WiFi only), vitals and system_status only cover the primary
    # Powerwall because the follower's TEDAPI endpoint is unreachable without v1r/WiFi.
    # config.json is served by the primary and lists all registered units including followers.
    pw_serials: list = []
    config_part_map: dict = {}  # serial → part number (from VIN field)

    tedapi_config = status.data.tedapi_config
    if isinstance(tedapi_config, dict):
        for cb in (tedapi_config.get("battery_blocks") or []):
            vin = cb.get("vin", "")
            if "--" in vin:
                part, serial = vin.rsplit("--", 1)
                if serial and serial not in pw_serials:
                    pw_serials.append(serial)
                    config_part_map[serial] = part

    # Fall back to system_status ordering (non-TEDAPI modes, or when config unavailable)
    if not pw_serials:
        for block in (system_status.get("battery_blocks") or []):
            serial = block.get("PackageSerialNumber")
            if serial and serial not in pw_serials:
                pw_serials.append(serial)

    # Last resort: derive from vitals TEPINV ordering (tepinv_map already keyed by serial)
    if not pw_serials:
        pw_serials = list(tepinv_map.keys())

    # --- Populate per-Powerwall fields ---
    for idx, serial in enumerate(pw_serials, 1):
        block = ss_block_map.get(serial, {})
        tepinv_device, tepinv_data = tepinv_map.get(serial, (None, {}))

        fcv[f"PW{idx}_name"] = tepinv_device
        # PINV frequency/voltage from vitals (preferred); fall back to system_status f_out
        fcv[f"PW{idx}_PINV_Fout"] = (
            tepinv_data.get("PINV_Fout") if tepinv_data else None
        ) or block.get("f_out")
        fcv[f"PW{idx}_PINV_VSplit1"] = tepinv_data.get("PINV_VSplit1") if tepinv_data else None
        fcv[f"PW{idx}_PINV_VSplit2"] = tepinv_data.get("PINV_VSplit2") if tepinv_data else None
        fcv[f"PW{idx}_PackagePartNumber"] = block.get("PackagePartNumber") or config_part_map.get(serial)
        fcv[f"PW{idx}_PackageSerialNumber"] = serial
        fcv[f"PW{idx}_p_out"] = block.get("p_out")
        fcv[f"PW{idx}_q_out"] = block.get("q_out")
        fcv[f"PW{idx}_v_out"] = block.get("v_out")
        fcv[f"PW{idx}_f_out"] = block.get("f_out")
        fcv[f"PW{idx}_i_out"] = block.get("i_out")

    # ISLAND and METER metrics from Backup Gateway (TESYNC) or Backup Switch (TEMSA)
    for device, d in vitals.items():
        if device.startswith("TESYNC") or device.startswith("TEMSA"):
            for i, value in d.items():
                if i.startswith("ISLAND") or i.startswith("METER"):
                    fcv[i] = value

    # Fallback: if we have freq data but no device-specific data, include it
    if status.data.freq is not None and not any(k.startswith("PW") for k in fcv.keys()):
        fcv["freq"] = status.data.freq

    # Add grid status (numeric: 1 = UP, 0 = DOWN)
    if status.data.grid_status == "UP":
        fcv["grid_status"] = 1
    elif status.data.grid_status == "DOWN":
        fcv["grid_status"] = 0
    else:
        fcv["grid_status"] = 0

    return fcv


@router.get("/csv")
async def get_csv(headers: Optional[str] = None):
    """Get CSV format data (legacy proxy endpoint).

    Returns: Grid,Home,Solar,Battery,BatteryLevel
    Add ?headers (any value) to include CSV headers.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Graceful degradation: return cached data even if offline
    if not status or not status.data:
        # Return zeros for CSV (backwards compatibility)
        csv_data = (
            "Grid,Home,Solar,Battery,BatteryLevel\n" if headers is not None else ""
        )
        csv_data += "0.00,0.00,0.00,0.00,0.00\n"
        return Response(content=csv_data, media_type="text/plain; charset=utf-8")

    # Extract power values from aggregates (neg_solar correction applied at fetch time)
    aggregates = status.data.aggregates or {}
    grid = aggregates.get("site", {}).get("instant_power", 0)
    solar = aggregates.get("solar", {}).get("instant_power", 0)
    battery = aggregates.get("battery", {}).get("instant_power", 0)
    home = aggregates.get("load", {}).get("instant_power", 0)
    level = status.data.soe_raw or 0

    # Build CSV response
    csv_data = ""
    if headers is not None:
        csv_data += "Grid,Home,Solar,Battery,BatteryLevel\n"
    csv_data += f"{grid:.2f},{home:.2f},{solar:.2f},{battery:.2f},{level:.2f}\n"

    return Response(content=csv_data, media_type="text/plain; charset=utf-8")


@router.get("/csv/v2")
async def get_csv_v2(headers: Optional[str] = None):
    """Get CSV v2 format data (legacy proxy endpoint).

    Returns: Grid,Home,Solar,Battery,BatteryLevel,GridStatus,Reserve
    Add ?headers (any value) to include CSV headers.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)
    pw = gateway_manager.get_connection(gateway_id)

    # Graceful degradation: return cached data even if offline
    if not status or not status.data:
        # Return zeros for CSV (backwards compatibility)
        csv_data = (
            "Grid,Home,Solar,Battery,BatteryLevel,GridStatus,Reserve\n"
            if headers is not None
            else ""
        )
        csv_data += "0.00,0.00,0.00,0.00,0.00,0,0\n"
        return Response(content=csv_data, media_type="text/plain; charset=utf-8")

    # Extract power values from aggregates (neg_solar correction applied at fetch time)
    aggregates = status.data.aggregates or {}
    grid = aggregates.get("site", {}).get("instant_power", 0)
    solar = aggregates.get("solar", {}).get("instant_power", 0)
    battery = aggregates.get("battery", {}).get("instant_power", 0)
    home = aggregates.get("load", {}).get("instant_power", 0)
    level = status.data.soe_raw or 0

    # Get grid status from cache (1=UP, 0=DOWN)
    grid_status_str = status.data.grid_status
    gridstatus = 1 if grid_status_str == "UP" else 0

    # Get reserve level from cache
    reserve = status.data.reserve or 0

    # Build CSV response
    csv_data = ""
    if headers is not None:
        csv_data += "Grid,Home,Solar,Battery,BatteryLevel,GridStatus,Reserve\n"
    csv_data += f"{grid:.2f},{home:.2f},{solar:.2f},{battery:.2f},{level:.2f},{gridstatus},{reserve:.0f}\n"

    return Response(content=csv_data, media_type="text/plain; charset=utf-8")


@router.get("/temps")
async def get_temps():
    """Get Powerwall temperatures (legacy proxy endpoint).

    Uses graceful degradation: returns cached temps even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.temps or {}


@router.get("/temps/pw")
async def get_temps_pw():
    """Get Powerwall temperatures with simple keys (legacy proxy endpoint).

    Uses graceful degradation: returns cached temps even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    pwtemp = {}
    if status and status.data and status.data.temps:
        temps = status.data.temps
        idx = 1
        for i in temps:
            key = f"PW{idx}_temp"
            pwtemp[key] = temps[i]
            idx += 1
    return pwtemp


@router.get("/alerts")
async def get_alerts():
    """Get Powerwall alerts (legacy proxy endpoint).

    Uses graceful degradation: returns cached alerts even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return []

    return status.data.alerts or []


@router.get("/alerts/pw")
async def get_alerts_pw():
    """Get Powerwall alerts in dictionary format (legacy proxy endpoint).

    Uses graceful degradation: returns cached alerts even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    pwalerts = {}
    if status and status.data and status.data.alerts:
        for alert in status.data.alerts:
            pwalerts[alert] = 1
    return pwalerts


@router.get("/fans")
async def get_fans():
    """Get fan speeds in raw format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.fan_speeds or {}


@router.get("/fans/pw")
async def get_fans_pw():
    """Get fan speeds in simplified format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    fan_speeds = status.data.fan_speeds or {}
    fans = {}
    for i, (_, value) in enumerate(sorted(fan_speeds.items())):
        key = f"FAN{i+1}"
        fans[f"{key}_actual"] = value.get("PVAC_Fan_Speed_Actual_RPM")
        fans[f"{key}_target"] = value.get("PVAC_Fan_Speed_Target_RPM")
    return fans


@router.get("/tedapi")
@router.get("/tedapi/")
async def get_tedapi_info():
    """Get TEDAPI information (legacy proxy endpoint)."""
    return {
        "error": "Use /tedapi/config, /tedapi/status, /tedapi/components, /tedapi/battery, /tedapi/controller"
    }


@router.get("/tedapi/config")
async def get_tedapi_config():
    """Get TEDAPI config (legacy proxy endpoint).

    Note: This diagnostic endpoint makes on-demand calls and does not use cache.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Fast fail if no connection
    if not status or not status.online:
        return {"error": "Gateway offline - TEDAPI unavailable"}

    config = await gateway_manager.call_tedapi(gateway_id, "get_config", timeout=5.0)
    if config is None:
        return {"error": "TEDAPI not enabled or unavailable"}
    return config


@router.get("/tedapi/status")
async def get_tedapi_status():
    """Get TEDAPI status (legacy proxy endpoint).

    Note: This diagnostic endpoint makes on-demand calls and does not use cache.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Fast fail if no connection
    if not status or not status.online:
        return {"error": "Gateway offline - TEDAPI unavailable"}

    result = await gateway_manager.call_tedapi(gateway_id, "get_status", timeout=5.0)
    if result is None:
        return {"error": "TEDAPI not enabled or unavailable"}
    return result


@router.get("/tedapi/components")
async def get_tedapi_components():
    """Get TEDAPI components (legacy proxy endpoint).

    Note: This diagnostic endpoint makes on-demand calls and does not use cache.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Fast fail if no connection
    if not status or not status.online:
        return {"error": "Gateway offline - TEDAPI unavailable"}

    components = await gateway_manager.call_tedapi(
        gateway_id, "get_components", timeout=5.0
    )
    if components is None:
        return {"error": "TEDAPI not enabled or unavailable"}
    return components


@router.get("/tedapi/battery")
async def get_tedapi_battery():
    """Get TEDAPI battery blocks (legacy proxy endpoint).

    Note: This diagnostic endpoint makes on-demand calls and does not use cache.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Fast fail if no connection
    if not status or not status.online:
        return {"error": "Gateway offline - TEDAPI unavailable"}

    battery = await gateway_manager.call_tedapi(
        gateway_id, "get_battery_blocks", timeout=5.0
    )
    if battery is None:
        return {"error": "TEDAPI not enabled or unavailable"}
    return battery


@router.get("/tedapi/controller")
async def get_tedapi_controller():
    """Get TEDAPI device controller (legacy proxy endpoint).

    Note: This diagnostic endpoint makes on-demand calls and does not use cache.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Fast fail if no connection
    if not status or not status.online:
        return {"error": "Gateway offline - TEDAPI unavailable"}

    controller = await gateway_manager.call_tedapi(
        gateway_id, "get_device_controller", timeout=5.0
    )
    if controller is None:
        return {"error": "TEDAPI not enabled or unavailable"}
    return controller


@router.get("/pod")
async def get_pod():
    """Get Powerwall battery data (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    pod = {}

    # Build a serial-number → block type lookup from cached TEDAPI config.
    # TEDAPI config battery_blocks carry a human-readable "type" field
    # ("Powerwall3", "Powerwall3Follower", etc.) that system_status does not.
    # VIN format: "PARTNUM--SERIAL" (e.g. "1707000-11-M--TG1253370033TB" → "TG1253370033TB")
    tedapi_type_map: dict = {}
    if status.data.tedapi_config:
        for cfg_block in status.data.tedapi_config.get("battery_blocks", []):
            vin = cfg_block.get("vin", "")
            block_type = cfg_block.get("type")
            if vin and block_type and "--" in vin:
                serial = vin.rsplit("--", 1)[1]
                tedapi_type_map[serial] = block_type

    # Get Individual Powerwall Battery Data from cached system_status
    system_status = status.data.system_status
    expansion_parent_indexes = _expansion_parent_indexes(
        system_status, status.data.tedapi_config
    )
    if system_status and "battery_blocks" in system_status:
        idx = 1
        for block in system_status["battery_blocks"]:
            # Initialize with None placeholders
            pod[f"PW{idx}_name"] = None
            pod[f"PW{idx}_POD_ActiveHeating"] = None
            pod[f"PW{idx}_POD_ChargeComplete"] = None
            pod[f"PW{idx}_POD_ChargeRequest"] = None
            pod[f"PW{idx}_POD_DischargeComplete"] = None
            pod[f"PW{idx}_POD_PermanentlyFaulted"] = None
            pod[f"PW{idx}_POD_PersistentlyFaulted"] = None
            pod[f"PW{idx}_POD_enable_line"] = None
            pod[f"PW{idx}_POD_available_charge_power"] = None
            pod[f"PW{idx}_POD_available_dischg_power"] = None
            pod[f"PW{idx}_POD_nom_energy_remaining"] = None
            pod[f"PW{idx}_POD_nom_energy_to_be_charged"] = None
            pod[f"PW{idx}_POD_nom_full_pack_energy"] = None

            parent_idx = expansion_parent_indexes.get(idx)
            if parent_idx:
                pod[f"PW{idx}_attached_to"] = f"PW{parent_idx}"

            # System Status Data
            pod[f"PW{idx}_POD_nom_energy_remaining"] = block.get(
                "nominal_energy_remaining"
            )
            pod[f"PW{idx}_POD_nom_full_pack_energy"] = block.get(
                "nominal_full_pack_energy"
            )
            pod[f"PW{idx}_PackagePartNumber"] = block.get("PackagePartNumber")
            pod[f"PW{idx}_PackageSerialNumber"] = block.get("PackageSerialNumber")
            # Prefer TEDAPI config type ("Powerwall3", "Powerwall3Follower") over
            # system_status Type ("ACPW") which is not useful for model detection.
            serial = block.get("PackageSerialNumber", "")
            pod[f"PW{idx}_Type"] = tedapi_type_map.get(serial) or block.get("Type")
            pod[f"PW{idx}_pinv_state"] = block.get("pinv_state")
            pod[f"PW{idx}_pinv_grid_state"] = block.get("pinv_grid_state")
            pod[f"PW{idx}_p_out"] = block.get("p_out")
            pod[f"PW{idx}_q_out"] = block.get("q_out")
            pod[f"PW{idx}_v_out"] = block.get("v_out")
            pod[f"PW{idx}_f_out"] = block.get("f_out")
            pod[f"PW{idx}_i_out"] = block.get("i_out")
            pod[f"PW{idx}_energy_charged"] = block.get("energy_charged")
            pod[f"PW{idx}_energy_discharged"] = block.get("energy_discharged")
            pod[f"PW{idx}_off_grid"] = int(block.get("off_grid") or 0)
            pod[f"PW{idx}_vf_mode"] = int(block.get("vf_mode") or 0)
            pod[f"PW{idx}_wobble_detected"] = int(block.get("wobble_detected") or 0)
            pod[f"PW{idx}_charge_power_clamped"] = int(
                block.get("charge_power_clamped") or 0
            )
            pod[f"PW{idx}_backup_ready"] = int(block.get("backup_ready") or 0)
            pod[f"PW{idx}_OpSeqState"] = block.get("OpSeqState")
            pod[f"PW{idx}_version"] = block.get("version")
            idx += 1

    # Augment with Vitals Data if available - match POD data to battery blocks by serial number
    if status.data.vitals:
        vitals = status.data.vitals
        
        # Build a map of serial numbers to vitals data.
        # Prefer the serialNumber field; fall back to the serial embedded in the
        # device key (TEPOD--{partno}--{serial}) — live PW3 TEDAPI vitals have
        # no serialNumber field, only the key carries the serial.
        tepod_map = {}
        for device in vitals:
            if device.startswith("TEPOD"):
                v = vitals[device]
                serial = v.get("serialNumber")
                if not serial and "--" in device:
                    serial = device.rsplit("--", 1)[1]
                if serial:
                    tepod_map[serial] = (device, v)
        
        # Match TEPOD vitals to battery blocks by serial number
        if system_status and "battery_blocks" in system_status:
            for idx, block in enumerate(system_status["battery_blocks"], 1):
                serial = block.get("PackageSerialNumber")
                if serial and serial in tepod_map:
                    device_name, v = tepod_map[serial]
                    # Populate POD vitals fields from TEPOD entry
                    pod[f"PW{idx}_name"] = device_name
                    pod[f"PW{idx}_POD_ActiveHeating"] = int(v.get("POD_ActiveHeating") or 0)
                    pod[f"PW{idx}_POD_ChargeComplete"] = int(v.get("POD_ChargeComplete") or 0)
                    pod[f"PW{idx}_POD_ChargeRequest"] = int(v.get("POD_ChargeRequest") or 0)
                    pod[f"PW{idx}_POD_DischargeComplete"] = int(v.get("POD_DischargeComplete") or 0)
                    pod[f"PW{idx}_POD_PermanentlyFaulted"] = int(v.get("POD_PermanentlyFaulted") or 0)
                    pod[f"PW{idx}_POD_PersistentlyFaulted"] = int(v.get("POD_PersistentlyFaulted") or 0)
                    pod[f"PW{idx}_POD_enable_line"] = int(v.get("POD_enable_line") or 0)
                    pod[f"PW{idx}_POD_available_charge_power"] = v.get("POD_available_charge_power")
                    pod[f"PW{idx}_POD_available_dischg_power"] = v.get("POD_available_dischg_power")
                    # Energy values from vitals (always overwrite system_status values per old proxy behavior)
                    pod[f"PW{idx}_POD_nom_energy_remaining"] = v.get("POD_nom_energy_remaining")
                    pod[f"PW{idx}_POD_nom_energy_to_be_charged"] = v.get("POD_nom_energy_to_be_charged")
                    pod[f"PW{idx}_POD_nom_full_pack_energy"] = v.get("POD_nom_full_pack_energy")

    # Aggregate data from cached system_status
    if system_status:
        pod["nominal_full_pack_energy"] = system_status.get("nominal_full_pack_energy")
        pod["nominal_energy_remaining"] = system_status.get("nominal_energy_remaining")

    # Use cached time_remaining and reserve (if available)
    pod["time_remaining_hours"] = status.data.time_remaining if status.data.time_remaining is not None else None
    pod["backup_reserve_percent"] = status.data.reserve if status.data.reserve is not None else None

    return pod


@router.get("/json")
async def get_json():
    """Get combined metrics and status in JSON format (legacy proxy endpoint).

    Returns grid, home, solar, battery power, state of energy, grid status (1/0),
    backup reserve, time remaining, full pack energy, energy remaining, and strings data.

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {
            "grid": None,
            "home": None,
            "solar": None,
            "battery": None,
            "soe": None,
            "soe_raw": None,
            "grid_status": None,
            "reserve": None,
            "time_remaining_hours": None,
            "full_pack_energy": None,
            "energy_remaining": None,
            "strings": None,
        }

    # Extract power values from aggregates (neg_solar correction applied at fetch time)
    aggregates = status.data.aggregates or {}
    grid = aggregates.get("site", {}).get("instant_power", 0)
    solar = aggregates.get("solar", {}).get("instant_power", 0)
    battery = aggregates.get("battery", {}).get("instant_power", 0)
    home = aggregates.get("load", {}).get("instant_power", 0)

    # Get battery level (SOE)
    soe = status.data.soe if status.data.soe is not None else 0
    soe_raw = status.data.soe_raw if status.data.soe_raw is not None else 0

    # Convert grid_status to numeric (1=UP, 0=DOWN)
    grid_status_str = status.data.grid_status or "DOWN"
    grid_status = 1 if "UP" in grid_status_str.upper() else 0

    # Get reserve and time remaining
    reserve = status.data.reserve if status.data.reserve is not None else 0
    time_remaining = (
        status.data.time_remaining if status.data.time_remaining is not None else 0
    )

    # Get full pack energy and energy remaining from system_status
    system_status = status.data.system_status or {}
    full_pack_energy = system_status.get("nominal_full_pack_energy", 0)
    energy_remaining = system_status.get("nominal_energy_remaining", 0)

    # Get strings data
    strings = status.data.strings or {}

    return {
        "grid": grid,
        "home": home,
        "solar": solar,
        "battery": battery,
        "soe": soe,
        "soe_raw": soe_raw,
        "grid_status": grid_status,
        "reserve": reserve,
        "time_remaining_hours": time_remaining,
        "full_pack_energy": full_pack_energy,
        "energy_remaining": energy_remaining,
        "strings": strings,
    }


@router.get("/battery")
async def get_battery_power():
    """Get battery power (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"power": None}

    aggregates = status.data.aggregates or {}
    battery_power = aggregates.get("battery", {}).get("instant_power", 0)

    return {"power": battery_power}


# NOTE: Specific /api/* routes must be defined BEFORE the catch-all /api/{path:path}
# Otherwise FastAPI will match the catch-all first.


@router.get("/api/system_status")
async def get_api_system_status():
    """Get full system status - API format (legacy proxy endpoint).

    Returns the cached system_status data (battery blocks, nominal energy, etc.).
    Data is populated by the background polling task, so this endpoint is
    always non-blocking and safe for concurrent requests.

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns empty object if no data available yet (e.g., during startup).
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.system_status or {}


@router.get("/api/system_status/soe")
async def get_api_soe():
    """Get battery state of energy - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"percentage": None, "raw_percentage": None}

    return {
        "percentage": status.data.soe,
        "raw_percentage": status.data.soe_raw,
    }


@router.get("/api/system_status/grid_status")
async def get_api_grid_status():
    """Get grid status - API format (legacy proxy endpoint).

    Returns the full grid status response from the Powerwall API including
    grid_status and grid_services_active fields.

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"grid_status": "Unknown", "grid_services_active": None}

    # Return cached grid_status_detail if available
    if status.data.grid_status_detail:
        return status.data.grid_status_detail

    # Fallback to simplified grid_status if detailed version not available
    if status.data.grid_status:
        # Map simplified status to API format
        grid_status_map = {
            "UP": "SystemGridConnected",
            "DOWN": "SystemIslandedActive"
        }
        api_status = grid_status_map.get(status.data.grid_status, status.data.grid_status)
        return {"grid_status": api_status, "grid_services_active": None}

    return {"grid_status": "Unknown", "grid_services_active": None}


@router.get("/api/sitemaster")
async def get_api_sitemaster():
    """Get sitemaster status - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    If we have cached data, report as running even if currently offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # If we have data (even stale), report as running
    if status and status.data:
        return {
            "status": "StatusUp",
            "running": True,
            "connected_to_tesla": status.online,  # True only if actually connected
            "power_supply_mode": False,
            "can_reboot": "Yes",
        }

    return {
        "status": "StatusDown",
        "running": False,
        "connected_to_tesla": False,
        "power_supply_mode": False,
        "can_reboot": "Yes",
    }


@router.get("/api/troubleshooting/problems")
async def get_api_problems():
    """Get troubleshooting problems - API format (legacy proxy endpoint)."""
    # Return empty problems list - this endpoint is for Tesla app diagnostics
    return {"problems": []}


@router.get("/api/auth/toggle/supported")
async def get_api_auth_toggle():
    """Get auth toggle support - API format (legacy proxy endpoint)."""
    # This endpoint indicates whether the gateway supports auth toggling
    return {"toggle_auth_supported": False}


@router.get("/api/status")
async def get_api_status():
    """Get API status - API format (legacy proxy endpoint)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if status and status.data:
        # Get DIN from status data
        din = None
        if status.data.status and isinstance(status.data.status, dict):
            din = status.data.status.get("din")
        
        # Format start_time as ISO datetime string if available
        start_time = None
        if status.data.status and isinstance(status.data.status, dict):
            start_time = status.data.status.get("start_time")
        
        # Get uptime as seconds or null
        up_time_seconds = None
        if status.data.status and isinstance(status.data.status, dict):
            up_time_seconds = status.data.status.get("up_time_seconds")
        
        return {
            "din": din or status.data.din,
            "start_time": start_time,
            "up_time_seconds": up_time_seconds,
            "is_new": False,
            "version": status.data.version or "Unknown",
            "git_hash": status.data.status.get("git_hash") if status.data.status and isinstance(status.data.status, dict) else None,
            "commission_count": status.data.status.get("commission_count", 0) if status.data.status and isinstance(status.data.status, dict) else 0,
            "device_type": status.data.device_type,
            "teg_type": status.data.status.get("teg_type", "unknown") if status.data.status and isinstance(status.data.status, dict) else "unknown",
            "sync_type": status.data.status.get("sync_type", "unknown") if status.data.status and isinstance(status.data.status, dict) else "unknown",
            "cellular_disabled": status.data.status.get("cellular_disabled", False) if status.data.status and isinstance(status.data.status, dict) else False,
            "can_reboot": status.data.status.get("can_reboot", True) if status.data.status and isinstance(status.data.status, dict) else True,
        }

    return {
        "din": None,
        "start_time": None,
        "up_time_seconds": None,
        "is_new": False,
        "version": "Unknown",
        "git_hash": None,
        "commission_count": 0,
        "device_type": None,
        "teg_type": "unknown",
        "sync_type": "unknown",
        "cellular_disabled": False,
        "can_reboot": True,
    }


@router.get("/api/site_info")
@router.head("/api/site_info", include_in_schema=False)
async def get_api_site_info():
    """Get site info - API format (legacy proxy endpoint)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    site_name = None
    timezone = None
    
    # Get site_name from cached data
    if status and status.data and status.data.site_name:
        site_name = status.data.site_name
    
    if status and status.gateway:
        timezone = status.gateway.timezone

    # Get system status for energy/power capacity info
    system_status = None
    if status and status.data:
        system_status = status.data.system_status or {}

    return {
        "max_system_energy_kWh": system_status.get("max_system_energy_kWh") if system_status else None,
        "max_system_power_kW": system_status.get("max_system_power_kW") if system_status else None,
        "site_name": site_name,
        "timezone": timezone,
        "max_site_meter_power_kW": system_status.get("max_site_meter_power_kW") if system_status else None,
        "min_site_meter_power_kW": system_status.get("min_site_meter_power_kW") if system_status else None,
        "nominal_system_energy_kWh": system_status.get("nominal_system_energy_kWh") if system_status else None,
        "nominal_system_power_kW": system_status.get("nominal_system_power_kW") if system_status else None,
        "panel_max_current": system_status.get("panel_max_current") if system_status else None,
        "grid_code": {
            "grid_code": None,
            "grid_voltage_setting": None,
            "grid_freq_setting": None,
            "grid_phase_setting": None,
            "country": None,
            "state": None,
            "utility": None,
        },
    }


@router.get("/api/site_info/site_name")
async def get_api_site_name():
    """Get site name - API format (legacy proxy endpoint)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    site_name = None
    timezone = None
    
    # Get site_name from cached data
    if status and status.data and status.data.site_name:
        site_name = status.data.site_name
    
    if status and status.gateway:
        timezone = status.gateway.timezone

    return {"site_name": site_name, "timezone": timezone}


@router.get("/api/operation")
async def get_api_operation():
    """Get operation mode and backup reserve - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.

    The operation mode is polled every cycle via pw.get_mode() (which reads
    /api/operation from the gateway) so that changes made in the Tesla app are
    reflected within one poll interval (fixes issue #14).

    Mode values:
        "self_consumption" - Self-Powered mode
        "backup"           - Backup-Only mode
        "autonomous"       - Time-Based Control mode
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # No fabricated default: when the mode is genuinely unknown (plain Basic
    # LAN has no local mode endpoint, or the hybrid cloud link is down),
    # report null so consumers display "unavailable" instead of a made-up
    # "self_consumption" (nesys, PR #85 — Console showed a mode the system
    # was not in). Cached mode and system_status.default_real_mode remain
    # the real fallbacks for gateways that can provide them.
    real_mode = None
    backup_reserve_percent = None
    stale = False
    last_updated = None

    if status and status.data:
        if status.data.reserve is not None:
            backup_reserve_percent = status.data.reserve

        # Use the cached operation mode polled by the background task.
        # Fall back to system_status.default_real_mode if mode isn't cached yet.
        if status.data.mode:
            real_mode = status.data.mode
        elif status.data.system_status and isinstance(status.data.system_status, dict):
            mode = status.data.system_status.get("default_real_mode")
            if mode:
                real_mode = mode

    # Hybrid stale fallback (issue #87): Basic LAN has no local mode/reserve
    # endpoint, so when the cloud link drops the values above go null. Serve
    # the last known cloud value explicitly marked stale — with its fetch
    # time — instead of either fabricating a default or silently freezing
    # the previous reading. When no cloud value was ever seen, null stands
    # and consumers render "unavailable".
    if real_mode is None or backup_reserve_percent is None:
        cloud_link = gateway_manager.cloud_link_status()
        if cloud_link:
            times = []
            if real_mode is None and cloud_link["last_known_mode"]:
                real_mode = cloud_link["last_known_mode"]
                stale = True
                if cloud_link["last_known_mode_time"]:
                    times.append(cloud_link["last_known_mode_time"])
            if (
                backup_reserve_percent is None
                and cloud_link["last_known_reserve"] is not None
            ):
                backup_reserve_percent = cloud_link["last_known_reserve"]
                stale = True
                if cloud_link["last_known_reserve_time"]:
                    times.append(cloud_link["last_known_reserve_time"])
            if times:
                last_updated = max(times)

    return {
        "real_mode": real_mode,
        "backup_reserve_percent": backup_reserve_percent,
        "stale": stale,
        "last_updated": last_updated,
    }


@router.get("/api/customer/registration")
async def get_api_customer_registration():
    """Get customer registration - API format (legacy proxy endpoint).

    The old proxy deliberately disables this endpoint (it can leak customer
    PII).  Match its disabled response exactly for drop-in compatibility.
    """
    return {"status": "404 Response - API Disabled"}


@router.get("/api/system_status/grid_faults")
async def get_api_grid_faults():
    """Get grid faults - API format (legacy proxy endpoint)."""
    # Return empty faults list
    return []


@router.get("/api/meters/aggregates")
async def get_api_aggregates():
    """Get power aggregates - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    Returns empty object if no data available yet (e.g., during startup).
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    # Graceful degradation: return cached data or empty object
    if not status or not status.data:
        return {}

    return status.data.aggregates or {}


@router.get("/api/meters/site")
async def get_api_meters_site():
    """Get site meter hardware config - API format (legacy proxy endpoint).

    The old proxy returns the synchrometer CT configuration synthesized by
    pypowerwall (id/location/type/cts/Cached_readings).  pypowerwall builds
    this from its internal TEDAPI cache, so an on-demand call is cheap.
    Falls back to empty list when the gateway is offline (like /tedapi/*,
    this diagnostic-style endpoint is exempt from the strict cache-only rule).
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.online:
        return []

    result = await gateway_manager.call_api(
        gateway_id, "poll", "/api/meters/site", timeout=5.0
    )
    if result is None or isinstance(result, str):
        return []
    return result


@router.get("/api/meters/solar")
async def get_api_meters_solar():
    """Get solar meter data - API format (legacy proxy endpoint).

    Returns the cached solar power data from aggregates.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return []

    solar = status.data.aggregates.get("solar", {})
    return [solar] if solar else []


@router.get("/api/meters")
async def get_api_meters():
    """Get meters list - API format (legacy proxy endpoint).

    Returns meter hardware configuration. Data not available in TEDAPI/local
    mode; returns an empty list for API compatibility.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return []

    return status.data.meters or []


@router.get("/api/meters/readings")
async def get_api_meters_readings():
    """Get meter readings - API format (legacy proxy endpoint).

    Detailed CT meter readings are not available in TEDAPI/local mode.
    Returns empty dict for API compatibility.
    """
    return {}


@router.get("/api/solars")
async def get_api_solars():
    """Get solar inverter list - API format (legacy proxy endpoint).

    Returns cached solar inverter data if available, otherwise an empty list.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return []

    return status.data.solars or []


@router.get("/api/solars/brands")
async def get_api_solars_brands():
    """Get solar inverter brands list - API format (legacy proxy endpoint).

    Returns a static list of known solar inverter brands for API compatibility.
    """
    return [
        "ABB", "Advanced Energy Industries", "AEG Power Solutions",
        "AEconversion", "Altair Energy", "Chilicon Power", "ClipperCreek",
        "Darfon Electronics", "Delta Energy Systems", "Diehl AKO Stiftung",
        "Enphase Energy", "FIMER", "Fronius", "Ginlong Technologies",
        "GoodWe", "Growatt New Energy", "Huawei", "iGo", "Ingeteam",
        "Kaco New Energy", "Kostal Solar Electric", "Power Electronics",
        "Powercom", "Premier Power", "SMA Solar Technology", "SolarEdge",
        "Solax Power", "Solectria Renewables", "SunGrow", "Sunnyboy",
        "Sunnyside Solar", "Suntrica", "TMEIC", "Tesla", "Yaskawa Solectria",
    ]


@router.get("/api/customer")
async def get_api_customer():
    """Get customer info - API format (legacy proxy endpoint)."""
    return {"registered": True}


@router.get("/api/installer")
async def get_api_installer():
    """Get installer info - API format (legacy proxy endpoint)."""
    return {
        "company": "Tesla",
        "customer_id": "",
        "phone": "",
        "email": "",
        "location": "",
        "mounting": "",
        "wiring": "",
        "backup_configuration": "Whole Home",
        "solar_installation": "New",
        "solar_installation_type": "PV Panel",
        "run_sitemaster": True,
        "verified_config": True,
        "installation_types": ["Residential"],
    }


@router.get("/api/system/update/status")
async def get_api_system_update_status():
    """Get system firmware update status - API format (legacy proxy endpoint)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    version = None
    if status and status.data:
        version = status.data.version

    return {
        "state": "/update_succeeded",
        "info": {"status": ["nonactionable"]},
        "current_time": int(__import__("time").time() * 1000),
        "last_status_time": int(__import__("time").time() * 1000),
        "version": version or "Unknown",
        "offline_updating": False,
        "offline_update_error": "",
        "estimated_bytes_per_second": None,
    }


@router.get("/api/site_info/grid_codes")
async def get_api_site_info_grid_codes():
    """Get available grid codes - API format (legacy proxy endpoint).

    Returns empty list; grid code enumeration is not available in TEDAPI mode.
    """
    return []


@router.get("/api/synchrometer/ct_voltage_references")
async def get_api_synchrometer_ct_voltage_references():
    """Get CT voltage references - API format (legacy proxy endpoint)."""
    return {"ct1": "Phase1", "ct2": "Phase2", "ct3": "Phase1"}


@router.get("/api/solar_powerwall")
async def get_api_solar_powerwall():
    """Get solar+powerwall combined data - API format (legacy proxy endpoint).

    Returns cached solar_powerwall data if available, otherwise empty dict.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.solar_powerwall or {}


@router.get("/api/networks")
@router.get("/api/system/networks")
async def get_api_networks():
    """Get network configuration - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return []

    return status.data.networks or []


@router.get("/api/powerwalls")
async def get_api_powerwalls():
    """Get powerwalls list - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.powerwalls or {}


# ---------------------------------------------------------------------------
# /pw/* Convenience Endpoints
#
# Shorthand endpoints that map to common library calls for backward
# compatibility with the original pypowerwall proxy.
# All return JSON and read from the in-memory cache (no blocking calls).
# ---------------------------------------------------------------------------


@router.get("/pw/level")
async def pw_level():
    """Battery state of energy (%).

    Old proxy shape: {"level": <raw soe>} — pw.level() returns the raw
    (unscaled) percentage from /api/system_status/soe.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"level": None}

    return {"level": status.data.soe_raw}


@router.get("/pw/power")
async def pw_power():
    """Site, solar, battery, load power (W)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {"site": 0, "solar": 0, "battery": 0, "load": 0}

    aggregates = status.data.aggregates
    return {
        "site": aggregates.get("site", {}).get("instant_power", 0),
        "solar": aggregates.get("solar", {}).get("instant_power", 0),
        "battery": aggregates.get("battery", {}).get("instant_power", 0),
        "load": aggregates.get("load", {}).get("instant_power", 0),
    }


@router.get("/pw/site")
async def pw_site():
    """Site (grid) power data."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("site", {})


@router.get("/pw/solar")
async def pw_solar():
    """Solar power data."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("solar", {})


@router.get("/pw/battery")
async def pw_battery():
    """Battery power data.

    Old proxy shape: pw.battery(verbose=True) — the full battery meter block
    from aggregates (instant_power, voltage, current, energy counters, etc.).
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("battery", {})


# The /pw/battery_blocks endpoint provides block-level detail.


@router.get("/pw/battery_blocks")
async def pw_battery_blocks():
    """Battery block details.

    Old proxy shape: pw.battery_blocks() — dict keyed by PackageSerialNumber
    (serial removed from the block body), augmented with TETHC temperature
    and state data from vitals when available.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.system_status:
        return {}

    result: dict = {}
    for bat in status.data.system_status.get("battery_blocks") or []:
        sn = bat.get("PackageSerialNumber")
        result[sn] = {k: v for k, v in bat.items() if k != "PackageSerialNumber"}

    # Merge TETHC temperature/state from vitals (pw.battery_blocks behavior)
    vitals = status.data.vitals or {}
    for device, d in vitals.items():
        if device.startswith("TETHC--"):
            parts = device.split("--")
            if len(parts) < 3:
                continue
            sn = parts[2]
            if sn not in result:
                result[sn] = {}
            result[sn].update(
                {
                    "THC_State": d.get("THC_State"),
                    "temperature": d.get("THC_AmbientTemp"),
                }
            )

    return result


@router.get("/pw/load")
async def pw_load():
    """Load (home consumption) power data."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("load", {})


@router.get("/pw/grid")
async def pw_grid():
    """Grid (site) power data."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("site", {})


@router.get("/pw/home")
async def pw_home():
    """Home consumption data (same as load)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or not status.data.aggregates:
        return {}

    return status.data.aggregates.get("load", {})


@router.get("/pw/vitals")
async def pw_vitals():
    """Device vitals."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.vitals or {}


@router.get("/pw/temps")
async def pw_temps():
    """Temperature metrics."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.temps or {}


@router.get("/pw/strings")
async def pw_strings():
    """Solar string data.

    Old proxy shape: pw.strings(verbose=True) — dict keyed by PVAC device name
    with PVAC_Pout plus per-string PVAC_PVCurrent/PVMeasuredPower/
    PVMeasuredVoltage/PvState and PVS_String* fields. Derived from cached
    vitals (with PVS string data injected, matching library behavior).
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    vitals = status.data.vitals or {}
    if not vitals:
        # No vitals (e.g. cloud mode) — fall back to the simplified cache
        return status.data.strings or {}

    result: dict = {}
    for device, d in vitals.items():
        if device.split("--")[0] != "PVAC":
            continue
        entry = {"PVAC_Pout": d.get("PVAC_Pout")}
        for field, value in d.items():
            if (
                "PVAC_PVCurrent" in field
                or "PVAC_PVMeasuredPower" in field
                or "PVAC_PVMeasuredVoltage" in field
                or "PVAC_PvState" in field
                or "PVS_String" in field
            ):
                entry[field] = value
        # Inject PVS string data from the matching PVS device (library behavior)
        pvs_key = "PVS" + device[4:]
        for field, value in (vitals.get(pvs_key) or {}).items():
            if "String" in field:
                entry[field] = value
        result[device] = entry

    return result if result else (status.data.strings or {})


@router.get("/pw/din")
async def pw_din():
    """Device identifier."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"din": None}

    return {"din": status.data.din}


@router.get("/pw/uptime")
async def pw_uptime():
    """System uptime string (e.g., '5d 3h 42m')."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"uptime": None}

    return {"uptime": status.data.uptime}


@router.get("/pw/version")
async def pw_version():
    """Firmware version."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    version = None
    if status and status.data:
        version = status.data.version

    if version is None:
        return {"version": "Unknown", "vint": 0}

    vint = 0
    try:
        parts = version.split(".")
        if len(parts) >= 2:
            vint = int(parts[0]) * 100 + int(parts[1])
    except Exception:
        pass

    return {"version": version, "vint": vint}



@router.get("/pw/status")
async def pw_status():
    """Status summary.

    Old proxy shape: pw.status() — the full /api/status payload (din,
    start_time, version, etc.).  Reuses the /api/status builder.
    """
    return await get_api_status()


@router.get("/pw/system_status")
async def pw_system_status():
    """System status."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.system_status or {}


@router.get("/pw/grid_status")
async def pw_grid_status():
    """Grid status.

    Old proxy shape: pw.grid_status("json") — the raw
    /api/system_status/grid_status payload (grid_status +
    grid_services_active).  Reuses the /api/system_status/grid_status builder.
    """
    return await get_api_grid_status()


@router.get("/pw/aggregates")
async def pw_aggregates():
    """Aggregated meter data."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {}

    return status.data.aggregates or {}


@router.get("/pw/site_name")
async def pw_site_name():
    """Site name."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"site_name": None}

    return {"site_name": status.data.site_name}


@router.get("/pw/alerts")
async def pw_alerts():
    """Alerts list.

    Old proxy shape: {"alerts": [...]} — wrapped in a dict.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"alerts": []}

    return {"alerts": status.data.alerts or []}


@router.get("/pw/is_connected")
async def pw_is_connected():
    """Connection boolean."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    return {"is_connected": status.online if status else False}


@router.get("/pw/get_reserve")
async def pw_get_reserve():
    """Current reserve setting (%)."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"reserve": None}

    return {"reserve": status.data.reserve}


@router.get("/pw/get_mode")
async def pw_get_mode():
    """Current operating mode."""
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"mode": None}

    return {"mode": status.data.mode}


@router.get("/pw/get_time_remaining")
async def pw_get_time_remaining():
    """Estimated backup time remaining.

    Old proxy shape: {"time_remaining": <hours>}.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"time_remaining": None}

    return {"time_remaining": status.data.time_remaining}


# NOTE: No catch-all /api/{path:path} routes!
# All API endpoints must be explicitly defined to ensure:
# 1. Graceful degradation - all data comes from cache
# 2. Predictable behavior - documented, testable endpoints
# 3. Security - no passthrough to arbitrary Powerwall endpoints
# 4. Performance - no on-demand blocking calls during requests
#
# If you need a new /api/* endpoint, add it explicitly above with cache support.


@router.get("/stats")
async def get_stats():
    """Get proxy statistics (legacy proxy endpoint)."""
    # Get process info
    process = psutil.Process(os.getpid())

    # Get request stats from tracker
    request_stats = stats_tracker.get_stats()

    # Calculate uptime
    create_time = process.create_time()
    uptime_seconds = int(time.time() - create_time)
    uptime = str(timedelta(seconds=uptime_seconds))

    # Count online/offline gateways and detect modes
    total_gateways = len(gateway_manager.gateways)
    online_count = 0
    gateway_statuses = []
    cloudmode = False
    fleetapi = False
    tedapi = False
    basiclan = False
    cloudcontrol = False
    pw3 = False
    tedapi_mode = None
    siteid = None

    for gateway_id, gw in gateway_manager.gateways.items():
        status = gateway_manager.get_gateway(gateway_id)
        if status and status.online:
            online_count += 1

        # Detect connection modes
        if gw.fleetapi:
            fleetapi = True
        if gw.cloud_mode:
            cloudmode = True
        if gw.host and not gw.basic_lan:
            tedapi = True
        if gw.basic_lan:
            basiclan = True

        # Get site ID if available
        if gw.site_id:
            siteid = gw.site_id

        # Detect PW3 and TEDAPI mode from cached data
        status = gateway_manager.get_gateway(gateway_id)
        if status and status.data:
            if status.data.pw3:
                pw3 = True
            if status.data.tedapi_mode:
                tedapi_mode = status.data.tedapi_mode

        # Get backoff info from gateway_manager
        failures = gateway_manager._consecutive_failures.get(gateway_id, 0)
        next_poll = gateway_manager._next_poll_time.get(gateway_id, 0)
        now = datetime.now().timestamp()
        backoff_remaining = max(0, int(next_poll - now))

        gateway_statuses.append(
            {
                "id": gateway_id,
                "name": gw.name,
                "host": gw.host,
                "online": status.online if status else False,
                "last_error": status.error if status and status.error else None,
                "last_updated": status.last_updated
                if status and status.last_updated
                else None,
                "consecutive_failures": failures,
                "backoff_seconds": backoff_remaining if failures > 0 else 0,
            }
        )

    # Hybrid (local reads + cloud control) connection state. _cloud_control
    # is created/assigned on the event loop by the background init task and
    # only read here on the same loop, so no lock is needed (same access
    # pattern as the poll loop).
    cloudcontrol = gateway_manager._cloud_control is not None

    # Per-link hybrid health (issue #87): the cloud-control connection is a
    # separate link with its own failure tracking. None when hybrid mode is
    # not configured, so non-hybrid /stats output is unchanged.
    cloud_link = gateway_manager.cloud_link_status()
    cloud_healthy = cloud_link["healthy"] if cloud_link else None

    # Determine mode string
    if fleetapi:
        mode = "FleetAPI"
    elif cloudmode:
        mode = "Cloud"
    else:
        mode = "Local"

    # Build configuration section (sanitize sensitive data)
    config = {
        "PW_BIND_ADDRESS": settings.server_host,
        "PW_PASSWORD": "**********" if settings.pw_password else None,
        # PII / infrastructure details are masked: /stats is unauthenticated,
        # so the Tesla account email, site id, and filesystem paths must not
        # be readable by arbitrary LAN (or cross-origin) clients.
        "PW_EMAIL": "**********" if settings.pw_email else "",
        "PW_HOST": settings.pw_host or "",
        "PW_TIMEZONE": settings.pw_timezone,
        "PW_DEBUG": settings.debug,
        "PW_CACHE_EXPIRE": settings.cache_expire,
        "PW_BROWSER_CACHE": settings.browser_cache,
        "PW_TIMEOUT": settings.timeout,
        "PW_POOL_MAXSIZE": settings.pool_maxsize,
        "PW_HTTPS": "yes" if settings.https_mode else "no",
        "PW_PORT": settings.server_port,
        "PW_STYLE": settings.style,
        "PW_SITEID": "**********" if settings.siteid else None,
        "PW_AUTH_PATH": "**********" if settings.pw_authpath else "",
        "PW_AUTH_MODE": settings.auth_mode,
        "PW_CACHE_FILE": "**********" if settings.cache_file else None,
        "PW_CONTROL_SECRET": "**********" if settings.control_secret else None,
        "PW_GW_PWD": "**********" if settings.pw_gw_pwd else None,
        "PW_NEG_SOLAR": True,  # Always enabled in this implementation
        "PW_SUPPRESS_NETWORK_ERRORS": settings.suppress_network_errors,
        "PW_NETWORK_ERROR_RATE_LIMIT": settings.network_error_rate_limit,
        "PW_FAIL_FAST": settings.fail_fast,
        "PW_GRACEFUL_DEGRADATION": settings.graceful_degradation,
        "PW_HEALTH_CHECK": settings.health_check,
        "PW_CACHE_TTL": settings.cache_ttl,
        "PW_TEDAPI_RECOVERY": settings.tedapi_recovery,
        "PW_TEDAPI_PROBE_INTERVAL": settings.tedapi_probe_interval,
    }

    # Build connection health section
    total_failures = sum(gateway_manager._consecutive_failures.values())
    # Local-link degradation only (previous is_degraded semantics).
    local_degraded = any(
        f > 0 for f in gateway_manager._consecutive_failures.values()
    )
    connection_health = {
        "consecutive_failures": gateway_manager._consecutive_failures.get("default", 0),
        "total_failures": total_failures,
        "total_successes": request_stats["gets"]
        + request_stats["posts"]
        - request_stats["errors"],
        # Hybrid-aware overall degradation (issue #87): a down cloud link
        # degrades the *system* even when local monitoring is healthy. For
        # non-hybrid setups cloud_healthy is None and this is exactly the
        # previous local-only semantics.
        "is_degraded": local_degraded or (cloud_healthy is False),
        "local_healthy": not local_degraded,
        "cloud_healthy": cloud_healthy,
        "last_success_time": time.time() if online_count > 0 else 0,
        "cache_size": total_gateways,
    }

    # Build stats response (compatible with old proxy format)
    stats = {
        "pypowerwall": f"{pypowerwall.__version__} Server {SERVER_VERSION}",
        "pypowerwall_version": pypowerwall.__version__,  # Library version only
        "server_version": SERVER_VERSION,  # Server version only
        "mode": mode,
        "gets": request_stats["gets"],
        "posts": request_stats["posts"],
        "errors": request_stats["errors"],
        "timeout": request_stats["timeout"],
        "uri": request_stats["uri"],
        "ts": int(time.time()),
        "start": request_stats["start"],
        "clear": request_stats["clear"],
        "uptime": uptime,
        "mem": int(process.memory_info().rss / 1024),  # Convert to KB like old proxy
        "cloudmode": cloudmode,
        "fleetapi": fleetapi,
        "tedapi": tedapi,
        "basiclan": basiclan,
        "cloudcontrol": cloudcontrol,
        "cloud_control": cloud_link,
        "pw3": pw3,
        "tedapi_mode": tedapi_mode,
        "siteid": siteid,
        "counter": 0,  # Legacy field, not used
        "cf": settings.cache_file,
        "config": config,
        "connection_health": connection_health,
        "gateways": {
            "total": total_gateways,
            "online": online_count,
            "offline": total_gateways - online_count,
        },
        "gateway_statuses": gateway_statuses,
    }

    # Add SolarOnly fallback mode state for each tracked gateway
    stats["fallback_mode"] = gateway_manager.get_all_fallback_states()

    # Add default gateway info for backward compatibility
    if gateway_manager.gateways:
        gateway_id = list(gateway_manager.gateways.keys())[0]
        status = gateway_manager.get_gateway(gateway_id)
        if status:
            # Use cached site_name from Powerwall, fallback to gateway name
            if status.data and status.data.site_name:
                stats["site_name"] = status.data.site_name
            else:
                stats["site_name"] = status.gateway.name

    return stats


@router.get("/version")
async def get_version():
    """Get firmware version (legacy proxy endpoint).

    Uses graceful degradation: returns cached version even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    version = None
    if status and status.data:
        version = status.data.version

    if version is None:
        return {"version": "Unknown", "vint": 0}

    # Parse version string to integer (basic implementation)
    vint = 0
    try:
        # Extract numbers from version string like "23.44.0"
        parts = version.split(".")
        if len(parts) >= 2:
            vint = int(parts[0]) * 100 + int(parts[1])
    except Exception:
        pass

    return {"version": version, "vint": vint}
