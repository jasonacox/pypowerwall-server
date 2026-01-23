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

from app.core.gateway_manager import gateway_manager
from app.config import settings, SERVER_VERSION
from app.utils.stats_tracker import stats_tracker

logger = logging.getLogger(__name__)

router = APIRouter()


def verify_control_token(authorization: Optional[str] = Header(None)):
    """Verify control token for authenticated operations."""
    if not settings.control_enabled or not settings.control_secret:
        raise HTTPException(status_code=403, detail="Control features not enabled")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Support both "Bearer token" and plain token
    token = (
        authorization.replace("Bearer ", "")
        if authorization.startswith("Bearer ")
        else authorization
    )

    if token != settings.control_secret:
        raise HTTPException(status_code=401, detail="Invalid control token")

    return True


@router.post("/control/{path:path}")
async def control_api(
    path: str, data: dict, authorization: Optional[str] = Header(None)
):
    """Authenticated control endpoint for POST operations."""
    verify_control_token(authorization)

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
    Returns null percentage if no data available yet.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data or status.data.soe is None:
        return {"percentage": None}

    return {"percentage": status.data.soe}


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
    idx = 1

    # Pull freq, current, voltage of each Powerwall via system_status
    system_status = status.data.system_status or {}
    if "battery_blocks" in system_status:
        for block in system_status["battery_blocks"]:
            fcv[f"PW{idx}_name"] = None  # Placeholder for vitals
            fcv[f"PW{idx}_PINV_Fout"] = block.get("f_out")
            fcv[f"PW{idx}_PINV_VSplit1"] = None  # Placeholder for vitals
            fcv[f"PW{idx}_PINV_VSplit2"] = None  # Placeholder for vitals
            fcv[f"PW{idx}_PackagePartNumber"] = block.get("PackagePartNumber")
            fcv[f"PW{idx}_PackageSerialNumber"] = block.get("PackageSerialNumber")
            fcv[f"PW{idx}_p_out"] = block.get("p_out")
            fcv[f"PW{idx}_q_out"] = block.get("q_out")
            fcv[f"PW{idx}_v_out"] = block.get("v_out")
            fcv[f"PW{idx}_f_out"] = block.get("f_out")
            fcv[f"PW{idx}_i_out"] = block.get("i_out")
            idx += 1

    # Pull freq, current, voltage of each Powerwall via vitals if available
    vitals = status.data.vitals or {}
    idx = 1
    for device, d in vitals.items():
        if device.startswith("TEPINV"):
            # PW freq
            fcv[f"PW{idx}_name"] = device
            fcv[f"PW{idx}_PINV_Fout"] = d.get("PINV_Fout")
            fcv[f"PW{idx}_PINV_VSplit1"] = d.get("PINV_VSplit1")
            fcv[f"PW{idx}_PINV_VSplit2"] = d.get("PINV_VSplit2")
            idx += 1
        if device.startswith("TESYNC") or device.startswith("TEMSA"):
            # Island and Meter Metrics from Backup Gateway or Backup Switch
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
    level = status.data.soe or 0

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
    level = status.data.soe or 0

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

    # Get Individual Powerwall Battery Data from cached system_status
    system_status = status.data.system_status
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

            # System Status Data
            pod[f"PW{idx}_POD_nom_energy_remaining"] = block.get(
                "nominal_energy_remaining"
            )
            pod[f"PW{idx}_POD_nom_full_pack_energy"] = block.get(
                "nominal_full_pack_energy"
            )
            pod[f"PW{idx}_PackagePartNumber"] = block.get("PackagePartNumber")
            pod[f"PW{idx}_PackageSerialNumber"] = block.get("PackageSerialNumber")
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
        
        # Build a map of serial numbers to vitals data
        tepod_map = {}
        for device in vitals:
            if device.startswith("TEPOD"):
                v = vitals[device]
                serial = v.get("serialNumber")
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
                    pod[f"PW{idx}_POD_ActiveHeating"] = int(v.get("POD_ActiveHeating", 0))
                    pod[f"PW{idx}_POD_ChargeComplete"] = int(v.get("POD_ChargeComplete", 0))
                    pod[f"PW{idx}_POD_ChargeRequest"] = int(v.get("POD_ChargeRequest", 0))
                    pod[f"PW{idx}_POD_DischargeComplete"] = int(v.get("POD_DischargeComplete", 0))
                    pod[f"PW{idx}_POD_PermanentlyFaulted"] = int(v.get("POD_PermanentlyFaulted", 0))
                    pod[f"PW{idx}_POD_PersistentlyFaulted"] = int(v.get("POD_PersistentlyFaulted", 0))
                    pod[f"PW{idx}_POD_enable_line"] = int(v.get("POD_enable_line", 0))
                    pod[f"PW{idx}_POD_available_charge_power"] = v.get("POD_available_charge_power")
                    pod[f"PW{idx}_POD_available_dischg_power"] = v.get("POD_available_dischg_power")
                    # Energy values from vitals (may override system_status values)
                    if v.get("POD_nom_energy_remaining") is not None:
                        pod[f"PW{idx}_POD_nom_energy_remaining"] = v.get("POD_nom_energy_remaining")
                    if v.get("POD_nom_energy_to_be_charged") is not None:
                        pod[f"PW{idx}_POD_nom_energy_to_be_charged"] = v.get("POD_nom_energy_to_be_charged")
                    if v.get("POD_nom_full_pack_energy") is not None:
                        pod[f"PW{idx}_POD_nom_full_pack_energy"] = v.get("POD_nom_full_pack_energy")

    # Aggregate data from cached system_status
    if system_status:
        pod["nominal_full_pack_energy"] = system_status.get("nominal_full_pack_energy")
        pod["nominal_energy_remaining"] = system_status.get("nominal_energy_remaining")

    # Use cached time_remaining and reserve
    pod["time_remaining_hours"] = status.data.time_remaining
    pod["backup_reserve_percent"] = status.data.reserve

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
            "grid": 0,
            "home": 0,
            "solar": 0,
            "battery": 0,
            "soe": 0,
            "grid_status": 0,
            "reserve": 0,
            "time_remaining_hours": 0,
            "full_pack_energy": 0,
            "energy_remaining": 0,
            "strings": {},
        }

    # Extract power values from aggregates (neg_solar correction applied at fetch time)
    aggregates = status.data.aggregates or {}
    grid = aggregates.get("site", {}).get("instant_power", 0)
    solar = aggregates.get("solar", {}).get("instant_power", 0)
    battery = aggregates.get("battery", {}).get("instant_power", 0)
    home = aggregates.get("load", {}).get("instant_power", 0)

    # Get battery level (SOE)
    soe = status.data.soe if status.data.soe is not None else 0

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
        return {"power": 0}

    aggregates = status.data.aggregates or {}
    battery_power = aggregates.get("battery", {}).get("instant_power", 0)

    return {"power": battery_power}


# NOTE: Specific /api/* routes must be defined BEFORE the catch-all /api/{path:path}
# Otherwise FastAPI will match the catch-all first.


@router.get("/api/system_status/soe")
async def get_api_soe():
    """Get battery state of energy - API format (legacy proxy endpoint).

    Uses graceful degradation: returns cached data even if gateway is temporarily offline.
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    if not status or not status.data:
        return {"percentage": None}

    level = status.data.soe
    if level is not None:
        # Scale using Tesla App formula: reserves bottom 5%, maps 5%->0% and 100%->100%
        level = (level / 0.95) - (5 / 0.95)

    return {"percentage": level}


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
        }

    return {"status": "StatusDown", "running": False, "connected_to_tesla": False}


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
            "din": din or status.gateway.din if status.gateway else None,
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
    """
    gateway_id = get_default_gateway()
    status = gateway_manager.get_gateway(gateway_id)

    real_mode = "self_consumption"  # Default mode
    backup_reserve_percent = 0.0
    
    # Get reserve from cached data
    if status and status.data:
        if status.data.reserve is not None:
            backup_reserve_percent = status.data.reserve
        
        # Try to get operation mode from system_status if available
        if status.data.system_status and isinstance(status.data.system_status, dict):
            mode = status.data.system_status.get("default_real_mode")
            if mode:
                real_mode = mode
    
    return {
        "real_mode": real_mode,
        "backup_reserve_percent": backup_reserve_percent,
    }


@router.get("/api/customer/registration")
async def get_api_customer_registration():
    """Get customer registration - API format (legacy proxy endpoint)."""
    return {
        "privacy_notice": True,
        "limited_warranty": True,
        "grid_services": False,
        "marketing": False,
        "registered": True,
        "timed_out_registration": False,
    }


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
        if gw.host:
            tedapi = True

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
        "PW_EMAIL": settings.pw_email or "",
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
        "PW_SITEID": settings.siteid,
        "PW_AUTH_PATH": settings.pw_authpath or "",
        "PW_AUTH_MODE": settings.auth_mode,
        "PW_CACHE_FILE": settings.cache_file,
        "PW_CONTROL_SECRET": "**********" if settings.control_secret else None,
        "PW_GW_PWD": "**********" if settings.pw_gw_pwd else None,
        "PW_NEG_SOLAR": True,  # Always enabled in this implementation
        "PW_SUPPRESS_NETWORK_ERRORS": settings.suppress_network_errors,
        "PW_NETWORK_ERROR_RATE_LIMIT": settings.network_error_rate_limit,
        "PW_FAIL_FAST": settings.fail_fast,
        "PW_GRACEFUL_DEGRADATION": settings.graceful_degradation,
        "PW_HEALTH_CHECK": settings.health_check,
        "PW_CACHE_TTL": settings.cache_ttl,
    }

    # Build connection health section
    total_failures = sum(gateway_manager._consecutive_failures.values())
    connection_health = {
        "consecutive_failures": gateway_manager._consecutive_failures.get("default", 0),
        "total_failures": total_failures,
        "total_successes": request_stats["gets"]
        + request_stats["posts"]
        - request_stats["errors"],
        "is_degraded": any(
            f > 0 for f in gateway_manager._consecutive_failures.values()
        ),
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
