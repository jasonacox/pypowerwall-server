"""
Gateway Manager - Manages connections to multiple Powerwall gateways.

This is the central hub of the server that manages all pypowerwall connections,
performs background polling, caches data, and provides fast API responses.

Architecture:
    - Singleton pattern (single gateway_manager instance)
    - Background polling every PW_CACHE_EXPIRE seconds (default: 5s)
    - One independent poll task per gateway (slow gateways don't delay others)
    - Cached data for instant API responses without blocking
    - Automatic reconnection on failure

Connection Modes:
    TEDAPI (Local Gateway):
        pw = pypowerwall.Powerwall(
            host="192.168.91.1",
            gw_pwd="gateway_wifi_password",
            timeout=3
        )
    
    Cloud Mode:
        pw = pypowerwall.Powerwall(
            email="user@example.com",
            authpath="/path/to/auth/files",
            cloudmode=True
        )
    
    FleetAPI:
        pw = pypowerwall.Powerwall(
            email="user@example.com",
            authpath="/path/to/auth/files",
            fleetapi=True
        )

Data Flow:
    1. Background task calls _poll_gateway() for each gateway every N seconds
    2. _poll_gateway() makes blocking pypowerwall calls in executor with timeouts
    3. Results cached in self.cache[gateway_id] as GatewayStatus objects
    4. API endpoints read from cache (instant response, no blocking)
    5. Failed polls update gateway status to offline (automatic retry next cycle)

Error Handling:
    - Connection failures logged but don't crash server
    - Timeouts on pypowerwall calls (3-10s depending on operation)
    - Offline gateways excluded from aggregates
    - Cached data remains available during outages
    - Automatic reconnection every poll cycle

Thread Safety:
    - All operations use asyncio (no threads/locks needed for reads)
    - Write operations serialized via _write_lock (held until the library call
      finishes, even past a timeout) to prevent set_operation() races
    - Single event loop handles all concurrency
    - Background task coordinated via asyncio.create_task()
    - Graceful shutdown via task cancellation

Performance:
    - Concurrent gateway polling for speed
    - Short timeouts prevent blocking
    - Cached responses for instant API access
    - Minimal memory footprint (only latest data cached)
"""
import asyncio
import json
import logging
import math
import os
import time
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pypowerwall
from pypowerwall.tedapi import GW_IP as TEDAPI_GW_IP
from app.models.gateway import Gateway, GatewayStatus, PowerwallData, AggregateData
from app.core.scaling import raw_to_tesla_battery_percent
from app.config import GatewayConfig
from app.utils.control import is_v1r_gateway

logger = logging.getLogger(__name__)

# Methods that write gateway state and must not run concurrently.
# set_operation() always writes backup_reserve_percent + real_mode together,
# reading the field it wasn't given from the 5-second poll cache. Two
# concurrent writes both see the same stale cache value and the last one to
# land on the gateway clobbers whichever field it didn't own.
_WRITE_METHODS = frozenset(
    {
        "set_reserve",
        "set_mode",
        "set_operation",
        "set_grid_charging",
        "set_grid_export",
        "go_off_grid",
        "reconnect_grid",
        "set_tariff",
        "schedule_max_backup",
        "cancel_max_backup",
        "dispatch_battery_power",
        "resume_battery_operation",
        # Raw POST is the control fallback for v1r/cloud-mode/FleetAPI
        # gateways (e.g. post("/api/operation", ...)). It targets the same
        # Tesla site as the set_* methods, so it must hold the same lock or
        # the read-modify-write race the lock prevents stays open on the
        # fallback path.
        "post",
    }
)


_ISLANDING_METHODS = frozenset({"go_off_grid", "reconnect_grid"})
# Reads that take the write lock: the backup-events read must wait for a
# timed-out schedule or cancel that is still running, or a cleanup could
# decide on stale events and cancel the event that just got scheduled.
_LOCKED_READS = frozenset({"get_backup_events"})


# pypowerwall client modes whose top-level get_grid_charging()/get_grid_export()
# are actually implemented. On the plain local client (hybrid TEDAPI or
# password-only local mode) those two getters are stubs that log an ERROR on
# every single call and return None (issue #114) — the library is right to
# complain when an unsupported function is called, so the server must simply
# not call them there and use the hybrid cloud-control fallback instead.
_GRID_CONTROL_MODES = frozenset({"v1r", "full"})


def _grid_controls_supported(pw: Any) -> bool:
    """True when pw.get_grid_charging()/get_grid_export() are implemented.

    Availability follows the pypowerwall library client routing:
      - cloudmode / fleetapi connections: implemented (site config API)
      - TEDAPI v1r / full connections: implemented (gateway config read)
      - local client (hybrid TEDAPI or password-only): stubs that log an
        ERROR per call — caller must not invoke them (issue #114)
    """
    if getattr(pw, "cloudmode", False) or getattr(pw, "fleetapi", False):
        return True
    return getattr(pw, "tedapi_mode", None) in _GRID_CONTROL_MODES


def _is_pw3_hardware(tedapi_config: Any) -> Optional[bool]:
    """Classify the battery hardware from a TEDAPI config.

    Args:
        tedapi_config: Cached TEDAPI config (``pw.tedapi.get_config()``).

    Returns:
        True for Powerwall 3 hardware (a battery block of type
        ``Powerwall3*``/``LFPV``, or a ``1707000`` part number, which the
        ``vin`` field starts with), False for other hardware, and None when
        the config has no battery blocks (unknown).
    """
    if not isinstance(tedapi_config, dict):
        return None
    blocks = tedapi_config.get("battery_blocks")
    if not isinstance(blocks, list):
        return None
    blocks = [b for b in blocks if isinstance(b, dict)]
    if not blocks:
        return None
    for block in blocks:
        block_type = str(block.get("type") or "")
        part = str(
            block.get("PackagePartNumber")
            or block.get("partNumber")
            or block.get("PartNumber")
            or block.get("vin")
            or ""
        )
        if (
            "Powerwall3" in block_type
            or block_type == "LFPV"
            or part.startswith("1707000")
        ):
            return True
    return False


class IslandingCommandInProgressError(RuntimeError):
    """Raised when an earlier islanding command is still running after timeout."""


class IslandingCooldownError(RuntimeError):
    """Raised when an islanding command arrives inside the server-side cooldown.

    The cooldown rate-limits physical grid-contactor operations per gateway,
    independent of any client-side (browser) lockout — API callers and
    automations are bound by it too.
    """

    def __init__(self, retry_after: int):
        super().__init__(
            f"Islanding is rate limited; retry in {retry_after} seconds"
        )
        self.retry_after = retry_after


def _effective_host(config: GatewayConfig) -> Optional[str]:
    """The host pypowerwall connects to: with a non-standard port appended
    (e.g. host="192.168.1.50", port=8443 -> "192.168.1.50:8443")."""
    return f"{config.host}:{config.port}" if config.port else config.host


def _expects_tedapi(config: GatewayConfig, default_password: Optional[str]) -> bool:
    """Whether pypowerwall will run TEDAPI for a local configuration: an RSA key
    (v1r) or a gateway password without a customer password (full TEDAPI) at
    any host; with both passwords (hybrid) only at the gateway's WiFi address,
    the one place pypowerwall's local client starts TEDAPI."""
    if not config.host or config.cloud_mode or config.fleetapi:
        return False
    if config.rsa_key_path:
        return True
    if not config.gw_pwd:
        return False
    if not (config.password or default_password):
        return True
    return _effective_host(config) in (TEDAPI_GW_IP, f"{TEDAPI_GW_IP}:443")


def _siteid_kwarg(siteid) -> Any:
    """PW_SITEID for pypowerwall: the cloud client compares it with integer
    site ids, so a numeric value is passed as an int."""
    text = str(siteid).strip()
    return int(text) if text.isdigit() else text


def _connection_mode_label(pw, basic_lan: bool) -> Optional[str]:
    """The connection pypowerwall is using now, which failover can change
    (FleetAPI -> Cloud, local -> Cloud); None when the client doesn't say."""
    mode = getattr(pw, "mode", None)
    if mode == "fleetapi":
        return "FleetAPI"
    if mode == "cloud":
        return "Cloud"
    if mode == "local":
        tedapi_mode = getattr(pw, "tedapi_mode", None)
        if tedapi_mode == "v1r":
            return "TEDAPI v1r"
        if tedapi_mode in ("full", "hybrid"):
            return "TEDAPI"
        return "Basic LAN" if basic_lan else "Local"
    return None


class GatewayManager:
    """Manages multiple Powerwall gateway connections."""

    # Consecutive TEDAPI probe failures before entering fallback mode.
    _TEDAPI_FALLBACK_THRESHOLD = 3
    # Recovery retry intervals (seconds) — exponential backoff.
    _TEDAPI_RECOVERY_INITIAL_INTERVAL = 60
    _TEDAPI_RECOVERY_MAX_INTERVAL = 300
    # Minimum timeout for constructing a fresh local connection (lazy init
    # and TEDAPI recovery share it); raised to PW_TIMEOUT + 5 s when higher.
    _CONNECTION_BUILD_TIMEOUT = 15.0
    # Cloud-control init retry backoff (seconds) when the connection could
    # not be established at startup: a Tesla cloud that is unreachable at
    # boot must not leave control permanently unavailable.
    _CLOUD_CONTROL_RETRY_INITIAL = 60
    _CLOUD_CONTROL_RETRY_MAX = 300
    # /api/site_info changes rarely and local gateways rate-limit: refresh it at most
    # this often, and retry a failed fetch no sooner than _SITE_INFO_RETRY
    _SITE_INFO_REFRESH = 300
    _SITE_INFO_RETRY = 60
    # Basic LAN detected on connect is provisional: re-check /api/status so a
    # Powerwall 2 whose status probe failed once gets full polling. A real PW3
    # answers 404 (pypowerwall logs it), so the interval doubles after each
    # failed check, from 5 minutes up to 6 hours.
    _BASIC_LAN_RECHECK = 300
    _BASIC_LAN_RECHECK_MAX = 6 * 3600

    def __init__(self):
        self.gateways: Dict[str, Gateway] = {}
        self.connections: Dict[str, pypowerwall.Powerwall] = {}
        self.cache: Dict[str, GatewayStatus] = {}
        # One independent poll task per gateway so a slow/degraded gateway
        # never delays polling of the healthy ones.
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        self._poll_interval = 5  # Default, will be set from config during initialize()

        # Strong references to in-flight MQTT publish tasks (one per gateway).
        # The event loop only holds weak refs to tasks, so fire-and-forget
        # tasks could be garbage-collected mid-publish; keeping them here also
        # lets us coalesce (latest-wins) and cancel them on shutdown.
        self._mqtt_tasks: Dict[str, asyncio.Task] = {}

        # Background cloud-control connection task (created in initialize()).
        self._cloud_control_task: Optional[asyncio.Task] = None

        # Exponential backoff tracking per gateway
        self._consecutive_failures: Dict[str, int] = {}  # Track failure count
        self._next_poll_time: Dict[
            str, float
        ] = {}  # Track when to poll next (Unix timestamp)
        # Battery hardware per gateway (True = Powerwall 3), learned from
        # tedapi_config; hardware can't change while the server runs.
        self._hw_pw3: Dict[str, bool] = {}
        self._last_successful_data: Dict[
            str, PowerwallData
        ] = {}  # Keep last good data for graceful degradation
        self._firmware_seen: Dict[
            str, str
        ] = {}  # Last logged firmware per gateway (#854) — survives transient fetch misses
        self._pending_configs: Dict[
            str, GatewayConfig
        ] = {}  # Gateways waiting for lazy initialization
        # Original per-gateway configuration, kept for TEDAPI recovery: a
        # replacement connection must be built exactly like startup builds
        # the first one. (Lives on after _pending_configs is drained.)
        self._gateway_configs: Dict[str, GatewayConfig] = {}
        self._preserve_stale_count: Dict[str, int] = {}  # Multi-PW snapshot preservation staleness tracker
        # Gateways whose current vitals were copied from the previous poll by
        # the multi-PW guard: not fresh readings, so not recorded as history.
        self._vitals_preserved: Set[str] = set()

        # TEDAPI SolarOnly fallback tracking (per gateway).
        # Distinct from _consecutive_failures: is_degraded = transient transport
        # failures; is_fallback_mode = TEDAPI has fallen back to SolarOnly mode
        # and needs explicit reconnection.
        self._fallback_state: Dict[str, Dict] = {}
        self._tedapi_probe_failures: Dict[str, int] = {}
        self._probe_tasks: Dict[str, asyncio.Task] = {}  # Per-gateway TEDAPI probe tasks

        # Dedicated thread pool for blocking pypowerwall operations
        # Will be sized during initialize() based on gateway count
        self._executor: Optional[ThreadPoolExecutor] = None

        # Serializes concurrent write operations to prevent set_operation()
        # from reading stale cache when two control calls race.
        self._write_lock: asyncio.Lock = asyncio.Lock()

        # Executor threads cannot be cancelled after a timeout. Keep each
        # islanding call visible until its thread actually finishes so an
        # opposite grid-contactor command can be rejected rather than queued.
        self._islanding_futures: Dict[str, asyncio.Future] = {}

        # Server-side islanding cooldown: monotonic timestamp of the last
        # dispatched contactor command per gateway. Recorded at dispatch
        # (not completion) and consulted for every islanding request,
        # regardless of client — the browser lockout is UX, this is the
        # enforcement.
        self._islanding_last_dispatch: Dict[str, float] = {}

        # Last good /api/site_info per gateway, and (monotonic time, succeeded) of the last fetch
        self._site_info: Dict[str, Dict[str, Any]] = {}
        self._site_info_attempt: Dict[str, Tuple[float, bool]] = {}
        # Basic LAN /api/status re-check per gateway: (monotonic due time, interval)
        self._basic_lan_recheck: Dict[str, Tuple[float, float]] = {}

        # Cloud connection for control operations (set_reserve, set_mode).
        # TEDAPI doesn't support POST/write APIs, so a separate cloud-mode
        # pypowerwall instance is created when cloud credentials are available
        # alongside a TEDAPI gateway. This enables hybrid operation:
        # TEDAPI for fast local reads, cloud for control writes.
        self._cloud_control: Optional[pypowerwall.Powerwall] = None
        # Gateway the shared cloud connection writes for, set only when its
        # Tesla site is unambiguous (see _cloud_site_unambiguous). MQTT
        # controls use the shared connection for this gateway and no other.
        self._cloud_control_gateway_id: Optional[str] = None

        # Hybrid cloud-link health (issue #87): the shared cloud connection
        # is a second link with its own failure profile, tracked separately
        # from local gateway poll failures. A WAN outage leaves local
        # monitoring healthy while cloud control degrades, and the Console
        # needs to show both links independently.
        self._cloud_control_configured = False  # hybrid credentials present
        self._cloud_failures = 0  # consecutive cloud call failures
        self._cloud_last_success: Optional[float] = None  # last successful call
        # Last known cloud-sourced operation values + fetch timestamps. Used
        # to serve stale-marked (not silently frozen) mode/reserve when the
        # cloud link drops after having been up (issue #87).
        self._cloud_mode: Optional[str] = None
        self._cloud_mode_time: Optional[float] = None
        self._cloud_reserve: Optional[float] = None
        self._cloud_reserve_time: Optional[float] = None
        self._cloud_grid_charging: Optional[bool] = None
        self._cloud_grid_charging_time: Optional[float] = None
        self._cloud_grid_export: Optional[str] = None
        self._cloud_grid_export_time: Optional[float] = None

    @staticmethod
    def _expected_battery_block_count(data: Optional[PowerwallData]) -> int:
        """Return expected battery block count from cached TEDAPI config when available."""
        if not data or not isinstance(data.tedapi_config, dict):
            return 0
        battery_blocks = data.tedapi_config.get("battery_blocks") or []
        return len(battery_blocks) if isinstance(battery_blocks, list) else 0

    @staticmethod
    def _count_tepinv_devices(vitals: Optional[Dict[str, Any]]) -> int:
        """Count Powerwall inverter entries in vitals payload."""
        if not isinstance(vitals, dict):
            return 0
        return sum(1 for key in vitals if key.startswith("TEPINV--"))

    @staticmethod
    def _count_system_status_blocks(system_status: Optional[Dict[str, Any]]) -> int:
        """Count battery blocks in cached system status payload."""
        if not isinstance(system_status, dict):
            return 0
        battery_blocks = system_status.get("battery_blocks") or []
        return len(battery_blocks) if isinstance(battery_blocks, list) else 0

    # Max consecutive polls a preserved snapshot is promoted before letting
    # partial data through.  Prevents stale follower data from living forever.
    _PRESERVE_STALENESS_CAP = 3

    # Consecutive cloud-control call failures before the hybrid cloud link
    # is reported "unavailable" rather than "degraded" (issue #87). Mirrors
    # the TEDAPI fallback threshold so both links use the same convention.
    _CLOUD_UNAVAILABLE_THRESHOLD = 3

    def _preserve_complete_multi_pw_snapshot(
        self, gateway_id: str, data: PowerwallData
    ) -> PowerwallData:
        """Avoid downgrading a complete multi-PW TEDAPI snapshot with a partial one.

        TEDAPI multi-PW data is synthesized in pypowerwall from follower vitals.
        If a single poll cycle drops follower data, the server can otherwise cache a
        one-PW snapshot even though the gateway config still reports multiple blocks.

        The guard is capped at ``_PRESERVE_STALENESS_CAP`` consecutive polls — after
        that, the partial data passes through so downstream consumers see reality.
        """
        self._vitals_preserved.discard(gateway_id)
        previous = self._last_successful_data.get(gateway_id)
        if not previous:
            return data

        current_config_count = self._expected_battery_block_count(data)
        previous_config_count = self._expected_battery_block_count(previous)

        # Trust the current TEDAPI config when it is present and non-zero.
        # Only fall back to the previous (larger) count when the current config
        # is missing — that indicates a transient read failure, not a legitimate
        # transition from multi-PW to single-PW.
        if current_config_count > 0:
            expected_blocks = current_config_count
        else:
            expected_blocks = previous_config_count

        if expected_blocks < 2:
            # Not a multi-PW system (or no longer one) — reset staleness counter.
            self._preserve_stale_count.pop(gateway_id, None)
            return data

        # --- Staleness cap ---
        stale_key = gateway_id
        stale_count = self._preserve_stale_count.get(stale_key, 0)
        if stale_count >= self._PRESERVE_STALENESS_CAP:
            logger.warning(
                "Preservation guard for %s hit staleness cap (%d polls) — "
                "letting partial data through",
                gateway_id,
                self._PRESERVE_STALENESS_CAP,
            )
            self._preserve_stale_count.pop(stale_key, None)
            return data

        preserved_any = False

        current_vitals_count = self._count_tepinv_devices(data.vitals)
        previous_vitals_count = self._count_tepinv_devices(previous.vitals)
        current_status_count = self._count_system_status_blocks(data.system_status)
        previous_status_count = self._count_system_status_blocks(previous.system_status)

        if (
            current_vitals_count < expected_blocks
            and previous_vitals_count >= expected_blocks
        ):
            logger.warning(
                "Preserving prior complete vitals snapshot for %s: "
                "expected %d TEPINV blocks, got %d in current poll (stale %d/%d)",
                gateway_id,
                expected_blocks,
                current_vitals_count,
                stale_count + 1,
                self._PRESERVE_STALENESS_CAP,
            )
            data.vitals = deepcopy(previous.vitals)
            self._vitals_preserved.add(gateway_id)
            preserved_any = True

        if (
            current_status_count < expected_blocks
            and previous_status_count >= expected_blocks
        ):
            logger.warning(
                "Preserving prior complete system_status snapshot for %s: "
                "expected %d battery blocks, got %d in current poll (stale %d/%d)",
                gateway_id,
                expected_blocks,
                current_status_count,
                stale_count + 1,
                self._PRESERVE_STALENESS_CAP,
            )
            data.system_status = deepcopy(previous.system_status)
            preserved_any = True

        if not data.tedapi_config and previous.tedapi_config:
            data.tedapi_config = deepcopy(previous.tedapi_config)

        if preserved_any:
            self._preserve_stale_count[stale_key] = stale_count + 1
        else:
            # Current poll was complete — reset staleness.
            self._preserve_stale_count.pop(stale_key, None)

        return data

    async def initialize(
        self, gateway_configs: List[GatewayConfig], poll_interval: int = 5
    ):
        """Initialize gateway manager - non-blocking.

        This method sets up gateways for lazy initialization. Actual pypowerwall
        connections are created during the first poll cycle to ensure the server
        starts accepting connections immediately.

        Args:
            gateway_configs: List of gateway configurations
            poll_interval: Polling frequency in seconds (from PW_CACHE_EXPIRE, default: 5)
        """
        self._poll_interval = poll_interval

        from app.config import settings

        # Size thread pool based on gateway count
        # Formula: max(10, num_gateways * 3) to support concurrent API calls
        num_gateways = len(gateway_configs)
        pool_size = max(10, num_gateways * 3)
        self._executor = ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="pypowerwall"
        )
        logger.info(
            f"Thread pool initialized with {pool_size} workers for {num_gateways} gateway(s)"
        )

        for config in gateway_configs:
            try:
                # Validate configuration
                # TEDAPI mode: need host + (gw_pwd OR rsa_key_path)
                # host + password only: the local API. A Powerwall 2 serves the
                #   full local API; a Powerwall 3 on its wired LAN serves only
                #   core metrics (Basic LAN). Which one is decided on connect.
                # Cloud mode: need email (authpath is optional, pypowerwall has defaults)
                basic_lan = bool(
                    config.host
                    and (config.password or settings.pw_password)
                    and not (config.gw_pwd or config.rsa_key_path)
                )
                has_local = config.host and (
                    config.gw_pwd
                    or config.rsa_key_path
                    or config.password
                    or settings.pw_password
                )
                # cloud_mode is auto-set; email (Cloud) or fleetapi is sufficient
                has_cloud = config.email or config.fleetapi

                if not (has_local or has_cloud):
                    logger.error(
                        f"Invalid configuration for gateway {config.id}: need host+gw_pwd or host+rsa_key_path (TEDAPI), host+password (Basic LAN), or email (Cloud)"
                    )
                    continue

                # Warn when both gw_pwd and rsa_key_path are set alongside host.
                # pypowerwall selects TEDAPI v1r (RSA auth) in this case, which
                # limits follower Powerwall data to primary-only unless a wifi_host
                # is also provided for the follower WiFi fallback path.
                if config.host and config.gw_pwd and config.rsa_key_path and not config.wifi_host:
                    logger.warning(
                        "Gateway %s: PW_HOST + PW_GW_PWD + PW_RSA_KEY_PATH are "
                        "all set — TEDAPI v1r mode is active but follower "
                        "Powerwall data will be limited to the primary unit only. "
                        "To see all Powerwalls, either: "
                        "(a) set PW_WIFI_HOST to the gateway's WiFi access point "
                        "(normally 192.168.91.1, which this host must be able to "
                        "reach) for follower queries while keeping v1r, or "
                        "(b) remove PW_RSA_KEY_PATH to use TEDAPI WiFi mode.",
                        config.id,
                    )

                # Auto-enable cloud_mode if email (or FleetAPI) is set but no host
                if (config.email or config.fleetapi) and not config.host:
                    config.cloud_mode = True

                gateway = Gateway(
                    id=config.id,
                    name=config.name,
                    host=config.host,
                    port=config.port,
                    gw_pwd=config.gw_pwd,
                    rsa_key_path=config.rsa_key_path,
                    rsa_key_configured=bool(config.rsa_key_path),
                    wifi_host=config.wifi_host,
                    email=config.email,
                    timezone=config.timezone,
                    basic_lan=basic_lan,
                    tedapi_expected=_expects_tedapi(config, settings.pw_password),
                    cloud_mode=config.cloud_mode,
                    fleetapi=config.fleetapi,
                    type=config.type,
                )

                # Store gateway - connection will be created lazily on first poll
                self.gateways[config.id] = gateway
                self._pending_configs[config.id] = config  # All start as pending
                # Keep the configuration for TEDAPI recovery (see
                # _create_local_connection): a replacement connection is
                # built the same way the lazy init builds the first one.
                self._gateway_configs[config.id] = config

                self.cache[config.id] = GatewayStatus(
                    gateway=gateway, online=False, error="Initializing..."
                )

                # Initialize backoff tracking
                self._consecutive_failures[config.id] = 0
                self._next_poll_time[config.id] = 0  # Poll immediately

                # Determine and log connection mode
                if config.fleetapi:
                    mode = "FleetAPI"
                elif config.cloud_mode:
                    mode = "Cloud"
                elif basic_lan:
                    mode = "Local or Basic LAN (detected on connect)"
                else:
                    mode = "TEDAPI"

                logger.info(
                    f"Registered gateway: {config.id} ({config.name}) - {mode} mode - connection pending"
                )
            except Exception as e:
                logger.error(f"Failed to initialize gateway {config.id}: {e}")

        # Initialize cloud control connection for local gateways (TEDAPI or
        # Basic LAN) with cloud credentials. This enables hybrid operation:
        # local reads + cloud control writes.
        # Runs as a background task so a slow/flaky Tesla cloud connection (up
        # to 15s) doesn't block server startup and container health checks.
        # Like the proxy, the cloud login starts only when it is wanted:
        # PW_CONTROL_SECRET (writes need it) or PW_CLOUD_HYBRID=yes (read-only
        # values such as Basic LAN mode/reserve). PW_EMAIL alone isn't enough:
        # PW2 local logins need an email and never asked for a cloud session.
        local_with_email = [
            config
            for config in gateway_configs
            if config.host
            and (
                config.gw_pwd
                or config.rsa_key_path
                or config.password
                or settings.pw_password
            )
            and config.email
            and not config.cloud_mode
        ]
        hybrid_configs = (
            local_with_email
            if settings.control_enabled or settings.cloud_hybrid
            else []
        )
        if local_with_email and not hybrid_configs:
            logger.info(
                "Hybrid cloud connection off: PW_EMAIL is set for a local gateway, "
                "but neither PW_CONTROL_SECRET nor PW_CLOUD_HYBRID=yes is"
            )
        # Hybrid configured => the cloud link exists as a tracked link even
        # while the connection itself is down (issue #87 per-link health).
        self._cloud_control_configured = bool(hybrid_configs)
        if hybrid_configs:
            self._cloud_control_task = asyncio.create_task(
                self._init_cloud_control(hybrid_configs), name="cloud-control-init"
            )

        # Start one independent polling task per gateway.  A degraded gateway
        # (each optional fetch timing out) can take a long time per cycle;
        # with a shared cycle barrier that used to stall data for the healthy
        # gateways too.
        for gateway_id in self.gateways:
            self._poll_tasks[gateway_id] = asyncio.create_task(
                self._poll_gateway_loop(gateway_id), name=f"poll-{gateway_id}"
            )

        # Start TEDAPI probe/recovery tasks for TEDAPI gateways.
        # Probes pw.version() on an interval; after N consecutive failed
        # probes (or a connection that left TEDAPI mode), enters fallback
        # mode and recovers with a fresh connection on a backoff.
        if settings.tedapi_recovery:
            for gateway_id, gw in self.gateways.items():
                if (
                    gw.host
                    and not gw.cloud_mode
                    and not gw.fleetapi
                    and gw.tedapi_expected
                ):
                    self._fallback_state[gateway_id] = self._new_fallback_state()
                    self._tedapi_probe_failures[gateway_id] = 0
                    self._probe_tasks[gateway_id] = asyncio.create_task(
                        self._tedapi_probe_loop(gateway_id),
                        name=f"tedapi-probe-{gateway_id}",
                    )
                    logger.info(
                        "TEDAPI probe/recovery task started for gateway %s "
                        "(interval=%ds, threshold=%d)",
                        gateway_id,
                        settings.tedapi_probe_interval,
                        self._TEDAPI_FALLBACK_THRESHOLD,
                    )

        if self.gateways:
            logger.info(
                f"Gateway manager ready - {len(self.gateways)} gateway(s) will connect on first poll"
            )

        # Start the time-series store's background maintenance loop (no-op
        # when PW_TIMESERIES_RETENTION=-1).
        from app.core.timeseries import get_timeseries_store

        await get_timeseries_store().start()

    async def _init_cloud_control(self, hybrid_configs: List[GatewayConfig]):
        """Create the hybrid cloud-control connection in the background.

        Retries with exponential backoff (_CLOUD_CONTROL_RETRY_INITIAL up
        to _CLOUD_CONTROL_RETRY_MAX) while no connection exists: a Tesla
        cloud that is unreachable at boot (or during a first-setup token
        exchange) must not leave control permanently unavailable. Stops
        once a connection is established; the task is cancelled at
        shutdown.
        """
        from app.config import settings

        retry_interval = self._CLOUD_CONTROL_RETRY_INITIAL
        while self._cloud_control is None:
            for config in hybrid_configs:
                try:
                    authpath = config.authpath or settings.pw_authpath or ""
                    loop = asyncio.get_running_loop()
                    cloud_kwargs = {
                        "email": config.email,
                        "authpath": authpath,
                        "cachefile": "/tmp/.powerwall.cloud",
                        "timezone": config.timezone,
                        "fleetapi": config.fleetapi,
                        "auto_select": True,
                        "failover": settings.failover,
                    }
                    if settings.siteid:
                        # PW_SITEID picks the site on multi-site accounts. The
                        # cloud client compares it with integer site ids.
                        cloud_kwargs["siteid"] = _siteid_kwarg(settings.siteid)
                    pw = await asyncio.wait_for(
                        loop.run_in_executor(
                            self._executor,
                            lambda kw=cloud_kwargs: pypowerwall.Powerwall(**kw),
                        ),
                        timeout=15.0,
                    )
                    # pypowerwall's constructor doesn't raise when it can't
                    # connect: it logs, and its failover may even land in
                    # local mode. Accept only a connected cloud/FleetAPI one.
                    connected = getattr(pw, "cloudmode", False) and await (
                        asyncio.wait_for(
                            loop.run_in_executor(self._executor, pw.is_connected),
                            timeout=10.0,
                        )
                    )
                    if not connected:
                        raise ConnectionError(
                            "no Tesla cloud connection "
                            f"(mode={getattr(pw, 'mode', 'unknown')})"
                        )
                    self._cloud_control = pw
                    if await self._cloud_site_unambiguous(config.fleetapi):
                        self._cloud_control_gateway_id = config.id
                    logger.info(
                        "Cloud control connection established for write operations"
                    )
                    return  # Only need one cloud control connection
                except Exception as e:
                    logger.warning(
                        "Cloud control connection failed for %s: %s", config.id, e
                    )
            logger.warning(
                "Cloud control connection not established; retrying in %.0fs",
                retry_interval,
            )
            await asyncio.sleep(retry_interval)
            retry_interval = min(
                retry_interval * 2, self._CLOUD_CONTROL_RETRY_MAX
            )

    async def _cloud_site_unambiguous(self, fleetapi: bool) -> bool:
        """True when the shared cloud connection's Tesla site is certain.

        That is when PW_SITEID is set, a site was chosen with pypowerwall
        setup (the site file in the auth path), the connection is FleetAPI
        (its site comes from its own setup), or the account has one site.
        Otherwise the cloud client defaults to the account's first site,
        which may not be this gateway's, so MQTT controls don't use it.
        Only MQTT controls use the binding, so without them nothing is checked.
        """
        from app.config import settings

        if not settings.mqtt_controls_available:
            return False
        client = getattr(self._cloud_control, "client", None)
        sitefile = getattr(client, "sitefile", None)
        if settings.siteid or fleetapi or (sitefile and os.path.exists(sitefile)):
            return True
        getsites = getattr(client, "getsites", None)
        sites = None
        if getsites is not None:
            try:
                loop = asyncio.get_running_loop()
                sites = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, getsites), timeout=15.0
                )
            except Exception:
                pass
        if isinstance(sites, list) and len(sites) == 1:
            return True
        logger.warning(
            "Hybrid cloud connection: the Tesla account has %s sites and no "
            "site is selected, so MQTT controls won't use it. Set PW_SITEID "
            "to this gateway's energy site id.",
            len(sites) if isinstance(sites, list) else "an unknown number of",
        )
        return False

    async def _cancel_task_with_retry(
        self,
        task: asyncio.Task,
        name: str,
        attempt_timeout: float = 0.5,
        max_attempts: int = 6,
    ):
        """Cancel a task, re-issuing cancel() until it actually stops.

        A single Task.cancel() call is not always sufficient: if the task is
        currently awaiting a run_in_executor() future whose underlying
        concurrent.futures.Future has already started running in a worker
        thread, that future's cancel() is a no-op (can't interrupt a running
        thread), and asyncio silently drops the cancellation request instead
        of retrying it. The task then keeps running past its next await
        point as if nothing happened. Re-issuing cancel() on a short timer
        catches it the next time the task is at a genuinely cancellable
        suspension point (e.g. asyncio.sleep()).
        """
        for attempt in range(max_attempts):
            if task.done():
                break
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=attempt_timeout)
                break
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Task '{name}' error during shutdown: {e}")
                break
        else:
            logger.warning(
                f"Task '{name}' did not stop after {max_attempts} cancellation "
                f"attempts (~{max_attempts * attempt_timeout:.1f}s); abandoning it"
            )

    async def shutdown(self):
        """Shutdown gateway manager and cleanup resources."""
        # Stop polling first so no new MQTT publishes are scheduled,
        # then cancel any in-flight publish tasks.
        named_tasks = [(t, f"poll-{gid}") for gid, t in self._poll_tasks.items()]
        named_tasks.extend(
            (t, f"tedapi-probe-{gid}") for gid, t in self._probe_tasks.items()
        )
        if self._cloud_control_task:
            named_tasks.append((self._cloud_control_task, "cloud-control-init"))
        named_tasks.extend(
            (t, f"mqtt-{gid}") for gid, t in self._mqtt_tasks.items()
        )

        # Cancel all tasks concurrently so one slow-to-cancel task doesn't
        # delay cancellation of the others.
        await asyncio.gather(
            *(
                self._cancel_task_with_retry(task, name)
                for task, name in named_tasks
            ),
            return_exceptions=True,
        )

        self._poll_tasks.clear()
        self._probe_tasks.clear()
        self._mqtt_tasks.clear()

        # Stop the time-series store (maintenance task + SQLite close).
        from app.core.timeseries import get_timeseries_store, reset_timeseries_store

        await get_timeseries_store().stop()
        reset_timeseries_store()

        # Shutdown thread pool executor
        if self._executor:
            self._executor.shutdown(wait=False)
        logger.info("Gateway manager shutdown complete")

    async def _poll_gateway_loop(self, gateway_id: str):
        """Fixed-tick polling loop for a single gateway.

        Each gateway gets its own task so one slow gateway never delays the
        others.  The loop records the monotonic clock time at the start of
        each cycle and only sleeps for the *remaining* time after the poll,
        so the next cycle starts as close to the configured interval as
        possible (a sleep-after-poll approach would drift by poll duration).
        """
        while True:
            try:
                loop = asyncio.get_running_loop()
                loop_start = loop.time()
                await self._poll_gateway(gateway_id)
                elapsed = loop.time() - loop_start
                await asyncio.sleep(max(0, self._poll_interval - elapsed))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in polling task for {gateway_id}: {e}")
                await asyncio.sleep(self._poll_interval)

    async def _fetch_gateway_data(self, gateway_id: str, pw) -> PowerwallData:
        """Fetch all data fields from a connected gateway.

        Runs every blocking pypowerwall call in the dedicated executor with a
        per-step timeout.  Aggregates is required (raises on failure); all
        other fields are optional and degrade to None.

        Per-step timeouts must exceed pypowerwall's internal request timeout
        (settings.timeout) so the library times out first and returns
        cleanly.  When wait_for gives up before the library does, the thread
        keeps running and holds pypowerwall's per-function API lock, which
        cascades into lock-wait timeouts on every subsequent call.
        """
        from app.config import settings

        step_timeout = max(5.0, settings.timeout + 2.0)
        long_timeout = max(10.0, settings.timeout + 2.0)

        # PW3 Basic LAN (host + password) exposes a limited local API:
        # /api/meters/aggregates, /api/system_status/soe and
        # /api/system_status/grid_status. Most other /api/* endpoints return
        # 404 on the wired LAN interface, so skip the fetches below in that
        # mode - avoids per-cycle 404 log noise and wasted request timeouts.
        gateway = self.gateways.get(gateway_id)
        basic_lan = bool(gateway.basic_lan) if gateway else False

        # Run blocking pypowerwall calls in dedicated executor with timeout protection
        loop = asyncio.get_running_loop()

        # Basic LAN is decided on connect from a failed /api/status, which a
        # Powerwall 2 can also hit transiently. Re-check now and then: once
        # the gateway answers, it gets the full local API from then on.
        now = time.monotonic()
        due, interval = self._basic_lan_recheck.setdefault(
            gateway_id, (now + self._BASIC_LAN_RECHECK, self._BASIC_LAN_RECHECK)
        )  # the clock starts at the first poll
        if basic_lan and now >= due:
            try:
                full_api = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.is_connected),
                    timeout=step_timeout,
                )
            except (asyncio.TimeoutError, Exception):
                full_api = False
            interval = min(interval * 2, self._BASIC_LAN_RECHECK_MAX)
            self._basic_lan_recheck[gateway_id] = (now + interval, interval)
            if full_api is True:
                gateway.basic_lan = basic_lan = False
                logger.info(
                    f"Gateway {gateway_id} now answers /api/status: polling the "
                    "full local API (not Basic LAN)"
                )

        # Fetch core data - aggregates is required, vitals/strings are optional
        # Use asyncio.wait_for to timeout if pypowerwall hangs
        try:
            aggregates = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor, pw.poll, "/api/meters/aggregates"
                ),
                timeout=long_timeout,
            )
        except asyncio.TimeoutError:
            raise Exception(f"Timeout fetching aggregates from {gateway_id}")
        except Exception as e:
            # If we can't get aggregates, this is a real connection failure
            raise Exception(f"Failed to fetch aggregates: {e}")

        # pypowerwall signals connection failure by RETURNING None (or an
        # ERROR payload), not by raising.  Without this check a dead
        # gateway would be marked online with empty data, resetting the
        # backoff and clobbering the last-known-good snapshot.
        if not aggregates or (
            isinstance(aggregates, dict) and "ERROR" in aggregates
        ):
            raise Exception(f"No aggregates data from {gateway_id}")

        # Apply negative solar correction if configured (PW_NEG_SOLAR=no; default yes)
        # This is done at fetch time so all endpoints get consistent data
        from app.config import settings

        if aggregates and not settings.neg_solar:
            # A meter can report instant_power as None (data gap); leave it alone
            solar = aggregates.get("solar") or {}
            solar_power = solar.get("instant_power")
            if solar_power is not None and solar_power < 0:
                # Shift negative solar energy to load
                load = aggregates.get("load") or {}
                if load.get("instant_power") is not None:
                    load["instant_power"] -= solar_power
                # Clamp solar to 0
                solar["instant_power"] = 0
                logger.debug(
                    f"Applied neg_solar correction for {gateway_id}: solar clamped to 0"
                )

        # Build PowerwallData with required aggregates
        data = PowerwallData(
            aggregates=aggregates, timestamp=datetime.now().timestamp()
        )

        # Try to get optional vitals and strings (don't fail if these aren't available)
        if not basic_lan:
            try:
                data.vitals = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.vitals), timeout=long_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Vitals not available for {gateway_id}: {e}")

            try:
                data.strings = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.strings), timeout=long_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Strings not available for {gateway_id}: {e}")

        # Try to get additional data
        try:
            data.soe_raw = await asyncio.wait_for(
                loop.run_in_executor(self._executor, pw.level), timeout=step_timeout
            )
            data.soe = raw_to_tesla_battery_percent(data.soe_raw)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"SOE not available for {gateway_id}: {e}")

        if not basic_lan:
            try:
                data.freq = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.freq), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Frequency not available for {gateway_id}: {e}")

            try:
                data.status = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.status), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Status not available for {gateway_id}: {e}")

            try:
                data.version = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.version), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Version not available for {gateway_id}: {e}")

            try:
                data.din = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.din), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"DIN not available for {gateway_id}: {e}")

            try:
                data.uptime = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.uptime), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Uptime not available for {gateway_id}: {e}")

        logger.debug(f"Gateway {gateway_id} aggregates: {data.aggregates}")

        if not basic_lan:
            # Try to get alerts (for caching)
            try:
                data.alerts = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.alerts), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Alerts not available for {gateway_id}: {e}")

            # Try to get temps (for caching)
            try:
                data.temps = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.temps), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Temps not available for {gateway_id}: {e}")

            # Try to get site name (for caching)
            try:
                data.site_name = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.site_name), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Site name not available for {gateway_id}: {e}")

            # /api/site_info from the library (the gateway's own shape in every
            # mode). Throttled whether or not the last fetch worked; the last good
            # copy is kept.
            attempt = self._site_info_attempt.get(gateway_id)
            if attempt is None or time.monotonic() - attempt[0] >= (
                    self._SITE_INFO_REFRESH if attempt[1] else self._SITE_INFO_RETRY):
                ok = False
                try:
                    site_info = await asyncio.wait_for(
                        loop.run_in_executor(self._executor, pw.poll, "/api/site_info"), timeout=step_timeout
                    )
                    if isinstance(site_info, dict):
                        self._site_info[gateway_id] = site_info
                        ok = True
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(f"Site info not available for {gateway_id}: {e}")
                self._site_info_attempt[gateway_id] = (time.monotonic(), ok)
            data.site_info = self._site_info.get(gateway_id)

        data.connection_mode = _connection_mode_label(pw, basic_lan)

        # Cache tedapi_mode and the transport pw3 flag; pw3 itself is
        # resolved from the hardware after tedapi_config is fetched below.
        transport_pw3 = None
        try:
            if hasattr(pw, "tedapi") and pw.tedapi:
                transport_pw3 = getattr(pw.tedapi, "pw3", None)
                if hasattr(pw, "tedapi_mode"):
                    data.tedapi_mode = pw.tedapi_mode
        except Exception:
            pass

        # Cache TEDAPI config for battery block type enrichment (PW3 systems)
        # battery_blocks[].type gives "Powerwall3" / "Powerwall3Follower" etc.,
        # which is more useful for model detection than system_status Type ("ACPW").
        try:
            if hasattr(pw, "tedapi") and pw.tedapi and hasattr(pw.tedapi, "get_config"):
                tedapi_config = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.tedapi.get_config),
                    timeout=long_timeout,
                )
                if tedapi_config and isinstance(tedapi_config, dict):
                    data.tedapi_config = tedapi_config
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"TEDAPI config not available for {gateway_id}: {e}")

        # Resolve pw3 from the battery hardware in tedapi_config: tedapi.pw3
        # describes the transport (the library sets it for every v1r
        # connection, PW2 included). Hardware can't change at runtime, so the
        # last known answer is kept per gateway and used whenever a poll's
        # config read fails. Until the hardware is known, non-v1r connections
        # keep the transport flag and v1r stays None (unknown).
        hw_pw3 = _is_pw3_hardware(data.tedapi_config)
        if hw_pw3 is not None:
            self._hw_pw3[gateway_id] = hw_pw3
        else:
            hw_pw3 = self._hw_pw3.get(gateway_id)
        if hw_pw3 is not None:
            data.pw3 = hw_pw3
        elif transport_pw3 is not None and data.tedapi_mode != "v1r":
            data.pw3 = bool(transport_pw3)

        # Try to get grid status (for caching)
        try:
            data.grid_status = await asyncio.wait_for(
                loop.run_in_executor(self._executor, pw.grid_status), timeout=step_timeout
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Grid status not available for {gateway_id}: {e}")

        # Try to get detailed grid status from API (for /api/system_status/grid_status endpoint)
        try:
            grid_status_response = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor, pw.poll, "/api/system_status/grid_status"
                ),
                timeout=step_timeout,
            )
            if isinstance(grid_status_response, str):
                data.grid_status_detail = json.loads(grid_status_response)
            else:
                data.grid_status_detail = grid_status_response
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Grid status detail not available for {gateway_id}: {e}")

        # Try to get operation mode (for /api/operation endpoint)
        # Mode can be "self_consumption", "backup", or "autonomous" (time-based control).
        # This is fetched from /api/operation on the gateway via pw.get_mode() and
        # must be polled on every cycle so that mode changes made in the Tesla app
        # are reflected promptly (fixes issue #14).
        # Pre-fill from last known good value so a transient failure doesn't wipe the cache.
        # Skipped for Basic LAN: that mode has no local mode/reserve endpoint, so a
        # pre-filled value would go stale forever (issue reported by nesys on #85 -
        # Console kept showing a mode from a previous control write). Hybrid mode
        # refreshes from the cloud control connection below instead.
        last_data = self._last_successful_data.get(gateway_id)
        # Grid charging/export are deliberately NOT pre-filled here: unlike
        # mode (locally re-polled every cycle), these can be cloud-sourced on
        # plain local/hybrid clients (library getters are stubs, #114), so a
        # pre-filled value would serve an old cloud reading as fresh once the
        # cloud link drops. /api/operation
        # already serves the timestamped _cloud_grid_* fallback stale-marked
        # instead — same no-silent-freeze contract as mode/reserve (#87).
        if last_data and last_data.mode and not basic_lan:
            data.mode = last_data.mode
        if basic_lan:
            # Basic LAN local API does not expose operation mode/reserve. When a
            # hybrid cloud-control connection is available, refresh mode/reserve
            # from the cloud so the Console reflects the real system state.
            if self._cloud_control:
                try:
                    cloud_mode = await self.cloud_control(
                        "get_mode", timeout=step_timeout
                    )
                    if cloud_mode:
                        data.mode = cloud_mode
                        # Remember the last cloud-sourced value so /api/operation
                        # can serve it stale-marked (not silently frozen) if the
                        # cloud link later drops (issue #87).
                        self._cloud_mode = cloud_mode
                        self._cloud_mode_time = time.time()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(
                        f"Operation mode not available via cloud control for {gateway_id}: {e}"
                    )
                try:
                    cloud_reserve = await self.cloud_control(
                        "get_reserve", timeout=step_timeout, scale=True
                    )
                    if cloud_reserve is not None:
                        data.reserve = cloud_reserve
                        self._cloud_reserve = cloud_reserve
                        self._cloud_reserve_time = time.time()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(
                        f"Reserve not available via cloud control for {gateway_id}: {e}"
                    )
                try:
                    # Supplementary read: must not disturb the cloud-link
                    # health counters (issue #87) — those stay driven by the
                    # mode/reserve path so failure thresholds keep their
                    # 2-calls-per-cycle semantics.
                    grid_func = (
                        getattr(self._cloud_control, "get_grid_charging", None)
                        if _grid_controls_supported(self._cloud_control)
                        else None
                    )
                    if grid_func is not None:
                        cloud_grid_charging = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, grid_func),
                            timeout=step_timeout,
                        )
                        if isinstance(cloud_grid_charging, bool):
                            data.grid_charging = cloud_grid_charging
                            self._cloud_grid_charging = cloud_grid_charging
                            self._cloud_grid_charging_time = time.time()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(
                        f"Grid charging not available via cloud control for {gateway_id}: {e}"
                    )
                try:
                    # Same supplementary-read contract as grid charging above.
                    export_func = (
                        getattr(self._cloud_control, "get_grid_export", None)
                        if _grid_controls_supported(self._cloud_control)
                        else None
                    )
                    if export_func is not None:
                        cloud_grid_export = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, export_func),
                            timeout=step_timeout,
                        )
                        if isinstance(cloud_grid_export, str) and cloud_grid_export:
                            data.grid_export = cloud_grid_export
                            self._cloud_grid_export = cloud_grid_export
                            self._cloud_grid_export_time = time.time()
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug(
                        f"Grid export unavailable via cloud for {gateway_id}: {e}"
                    )
        if not basic_lan:
            try:
                data.mode = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.get_mode),
                    timeout=step_timeout,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Operation mode not available for {gateway_id}: {e}")

            # Try to get reserve and time remaining (for caching)
            try:
                # Request the Tesla App scaled reserve setting (scale=True)
                data.reserve = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, lambda: pw.get_reserve(scale=True)), timeout=step_timeout
                )
                data.time_remaining = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.get_time_remaining),
                    timeout=step_timeout,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(
                    f"Reserve/time remaining not available for {gateway_id}: {e}"
                )

            # Grid charging: only clients that implement the getter provide a
            # local value (cloud/FleetAPI, TEDAPI v1r/full). The plain local
            # client's getter is a stub that logs an ERROR per call
            # (issue #114), so it is skipped entirely — hybrid setups fall back to
            # the cloud connection below instead. Read directly (not via
            # cloud_control()) so this supplementary read leaves the
            # cloud-link health counters untouched.
            try:
                local_grid_charging = None
                if _grid_controls_supported(pw):
                    local_grid_charging = await asyncio.wait_for(
                        loop.run_in_executor(self._executor, pw.get_grid_charging),
                        timeout=step_timeout,
                    )
                if isinstance(local_grid_charging, bool):
                    data.grid_charging = local_grid_charging
                elif (
                    local_grid_charging is None
                    and self._cloud_control is not None
                    and _grid_controls_supported(self._cloud_control)
                ):
                    grid_func = getattr(self._cloud_control, "get_grid_charging", None)
                    if grid_func is not None:
                        cloud_grid_charging = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, grid_func),
                            timeout=step_timeout,
                        )
                        if isinstance(cloud_grid_charging, bool):
                            data.grid_charging = cloud_grid_charging
                            self._cloud_grid_charging = cloud_grid_charging
                            self._cloud_grid_charging_time = time.time()
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(
                    f"Grid charging not available for {gateway_id}: {e}"
                )

            # Grid export policy: same availability rule and hybrid fallback
            # as grid charging above. Only real strings are cached — the
            # library contract is str | None.
            try:
                local_grid_export = None
                if _grid_controls_supported(pw):
                    local_grid_export = await asyncio.wait_for(
                        loop.run_in_executor(self._executor, pw.get_grid_export),
                        timeout=step_timeout,
                    )
                if isinstance(local_grid_export, str) and local_grid_export:
                    data.grid_export = local_grid_export
                elif (
                    local_grid_export is None
                    and self._cloud_control is not None
                    and _grid_controls_supported(self._cloud_control)
                ):
                    export_func = getattr(self._cloud_control, "get_grid_export", None)
                    if export_func is not None:
                        cloud_grid_export = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, export_func),
                            timeout=step_timeout,
                        )
                        if isinstance(cloud_grid_export, str) and cloud_grid_export:
                            data.grid_export = cloud_grid_export
                            self._cloud_grid_export = cloud_grid_export
                            self._cloud_grid_export_time = time.time()
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(
                    f"Grid export not available for {gateway_id}: {e}"
                )

            # Try to get system status for /pod endpoint (for caching)
            try:
                data.system_status = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.system_status), timeout=step_timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"System status not available for {gateway_id}: {e}")

        # Try to get fan speeds for /fans endpoint (TEDAPI only)
        # get_fan_speeds() lives on the TEDAPI client (pw.tedapi),
        # not on the top-level Powerwall object itself
        try:
            if hasattr(pw, "tedapi") and pw.tedapi and hasattr(pw.tedapi, "get_fan_speeds"):
                data.fan_speeds = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, pw.tedapi.get_fan_speeds),
                    timeout=step_timeout,
                )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Fan speeds not available for {gateway_id}: {e}")

        if not basic_lan:
            # Try to get networks for /api/system/networks endpoint
            try:
                networks_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: pw.poll("/api/networks")
                    ),
                    timeout=step_timeout,
                )
                if networks_result and isinstance(networks_result, list):
                    data.networks = networks_result
                elif networks_result and isinstance(networks_result, str):
                    try:
                        data.networks = json.loads(networks_result)
                    except json.JSONDecodeError:
                        pass
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Networks not available for {gateway_id}: {e}")

            # Try to get powerwalls for /api/powerwalls endpoint
            try:
                powerwalls_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: pw.poll("/api/powerwalls")
                    ),
                    timeout=step_timeout,
                )
                if powerwalls_result and isinstance(powerwalls_result, dict):
                    data.powerwalls = powerwalls_result
                elif powerwalls_result and isinstance(powerwalls_result, str):
                    try:
                        data.powerwalls = json.loads(powerwalls_result)
                    except json.JSONDecodeError:
                        pass
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug(f"Powerwalls not available for {gateway_id}: {e}")

        # Guard against partial TEDAPI follower snapshots replacing a complete
        # multi-Powerwall view for a single poll cycle.
        return self._preserve_complete_multi_pw_snapshot(gateway_id, data)

    async def _poll_gateway(self, gateway_id: str) -> None:
        """Poll a single gateway for data with exponential backoff on failures."""
        try:
            # Check if we're in backoff period
            now = datetime.now().timestamp()
            next_poll = self._next_poll_time.get(gateway_id, 0)

            if now < next_poll:
                # Skip this poll cycle - in backoff period
                logger.debug(
                    f"Gateway {gateway_id} in backoff, skipping poll (next poll at {next_poll - now:.0f}s)"
                )
                return

            # Check for lazy initialization - create connection if pending
            if (
                gateway_id in self._pending_configs
                and gateway_id not in self.connections
            ):
                config = self._pending_configs[gateway_id]
                logger.info(f"Attempting lazy initialization of gateway {gateway_id}")

                from app.config import settings

                loop = asyncio.get_running_loop()

                try:
                    # Keep pypowerwall's internal cache alive for the whole
                    # poll cycle: with the library default of 5s, a cycle that
                    # takes longer re-fetches mid-cycle data it already has.
                    pwcacheexpire = max(5, int(self._poll_interval))

                    if config.cloud_mode and (config.email or config.fleetapi):
                        cloud_kwargs = {
                            "cloudmode": True,
                            "fleetapi": config.fleetapi,
                            "timezone": config.timezone,
                            "pwcacheexpire": pwcacheexpire,
                            "failover": settings.failover,
                        }
                        if config.email:
                            cloud_kwargs["email"] = config.email
                        if config.authpath:
                            cloud_kwargs["authpath"] = config.authpath
                        if settings.siteid:
                            # PW_SITEID picks the site on multi-site accounts.
                            # The cloud client compares it with integer ids.
                            cloud_kwargs["siteid"] = _siteid_kwarg(settings.siteid)
                        pw = await asyncio.wait_for(
                            loop.run_in_executor(
                                self._executor,
                                lambda kw=cloud_kwargs: pypowerwall.Powerwall(**kw),
                            ),
                            timeout=15.0,
                        )
                        connected = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, pw.is_connected),
                            timeout=10.0,
                        )
                        if not connected:
                            raise Exception(
                                f"pypowerwall failed to connect to gateway {gateway_id} (cloud mode)"
                            )
                    else:
                        # Same construction path as TEDAPI recovery
                        # (_create_local_connection) so a replacement
                        # connection is built exactly like this one.
                        pw = await self._create_local_connection(config)
                        connected = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, pw.is_connected),
                            timeout=10.0,
                        )
                        password_only = bool(
                            config.host
                            and (config.password or settings.pw_password)
                            and not (config.gw_pwd or config.rsa_key_path)
                        )
                        if password_only:
                            # A Powerwall 2 answers /api/status (what
                            # is_connected() probes) and serves the full local
                            # API, like the proxy's local mode. PW3 Basic LAN
                            # doesn't expose /api/status, so is_connected()
                            # reports a false negative there; probe an
                            # endpoint that mode does serve and limit polling
                            # to it.
                            basic_lan = not connected
                            if basic_lan:
                                grid_probe = await asyncio.wait_for(
                                    loop.run_in_executor(self._executor, pw.grid_status),
                                    timeout=10.0,
                                )
                                connected = grid_probe is not None
                            if connected:
                                self.gateways[gateway_id].basic_lan = basic_lan
                        if not connected:
                            mode_hint = "local API" if password_only else "TEDAPI mode"
                            raise Exception(
                                f"pypowerwall failed to connect to gateway {gateway_id} ({mode_hint})"
                            )

                    self.connections[gateway_id] = pw
                    del self._pending_configs[gateway_id]

                    # Try to get site_id for cloud mode gateways
                    gateway = self.gateways[gateway_id]
                    if gateway.fleetapi:
                        mode_label = "FleetAPI"
                    elif gateway.cloud_mode:
                        mode_label = "Cloud"
                    elif config.rsa_key_path and config.wifi_host:
                        mode_label = "TEDAPI v1r + WiFi"
                    elif config.rsa_key_path:
                        mode_label = "TEDAPI v1r"
                    elif config.gw_pwd:
                        mode_label = "TEDAPI WiFi"
                    elif gateway.basic_lan:
                        mode_label = "Basic LAN"
                    else:
                        mode_label = "Local"
                    # Failover can land elsewhere (e.g. local -> Cloud): say so
                    live = _connection_mode_label(pw, gateway.basic_lan)
                    if live and not mode_label.startswith(live):
                        mode_label = f"{live} (configured {mode_label})"

                    if gateway.cloud_mode or gateway.fleetapi:
                        try:
                            site_id = getattr(pw, "siteid", None) or getattr(
                                pw, "site_id", None
                            )
                            if site_id:
                                gateway.site_id = str(site_id)
                                logger.info(
                                    f"Connected to gateway {gateway_id} - {mode_label} mode (Site ID: {site_id}, Email: {gateway.email})"
                                )
                            else:
                                logger.info(
                                    f"Connected to gateway {gateway_id} - {mode_label} mode (Email: {gateway.email})"
                                )
                        except Exception:
                            logger.info(
                                f"Connected to gateway {gateway_id} - {mode_label} mode"
                            )
                    else:
                        logger.info(
                            f"Connected to gateway {gateway_id} - {mode_label} mode ({gateway.host})"
                        )
                        if gateway.basic_lan:
                            logger.info(
                                "Gateway %s: Basic LAN mode serves a limited local "
                                "API - skipping endpoints not exposed in this mode "
                                "(vitals, strings, status, firmware, DIN, uptime, "
                                "alerts, temps, site name, operation mode/reserve, "
                                "system status, networks, powerwalls). Monitoring is "
                                "limited to power flows, battery SoC and grid status.",
                                gateway_id,
                            )

                except asyncio.TimeoutError:
                    logger.warning(
                        f"Lazy initialization timeout for gateway {gateway_id} - will retry next cycle"
                    )
                    raise Exception("Connection initialization timeout")
                except Exception as e:
                    logger.warning(
                        f"Lazy initialization failed for gateway {gateway_id}: {e}"
                    )
                    raise

            pw = self.connections.get(gateway_id)
            if not pw:
                logger.debug(
                    f"No connection object for gateway {gateway_id} - waiting for lazy init"
                )
                raise Exception("Connection not yet initialized")

            # Cap the whole fetch with an overall budget.  ~20 sequential
            # per-step timeouts can otherwise sum to minutes, and a degraded
            # gateway that answers aggregates but crawls through the optional
            # fields would stretch its poll cycle indefinitely without ever
            # triggering backoff.  Exceeding the budget is a poll failure.
            from app.config import settings

            poll_budget = max(
                30.0, self._poll_interval * 3.0, (settings.timeout + 2.0) * 4.0
            )
            try:
                data = await asyncio.wait_for(
                    self._fetch_gateway_data(gateway_id, pw), timeout=poll_budget
                )
            except asyncio.TimeoutError:
                raise Exception(
                    f"Poll of {gateway_id} exceeded overall budget of {poll_budget:.0f}s"
                )


            # Update cache
            gateway = self.gateways[gateway_id]

            # Log connection success on first connection or reconnection
            was_offline = not gateway.online
            gateway.online = True
            gateway.last_error = None

            # Reset backoff on success
            previous_failures = self._consecutive_failures.get(gateway_id, 0)
            self._consecutive_failures[gateway_id] = 0
            self._next_poll_time[gateway_id] = 0  # Poll normally next cycle

            # Firmware change tracking (Powerwall-Dashboard#854): log the
            # gateway firmware once at first successful poll and a dated line
            # on every change, so `docker logs pypowerwall-server` answers
            # "when did my firmware update?". Uses _firmware_seen (not the
            # last poll snapshot) so a transient version-fetch miss doesn't
            # re-log the same version. Basic LAN skips the version fetch
            # (data.version stays None) — no noise.
            new_firmware = data.version
            if new_firmware is not None:
                # Sanitize external input — collapse
                # whitespace and drop control chars (log-forging guard)
                new_firmware = " ".join(new_firmware.split())
                new_firmware = "".join(c for c in new_firmware if c.isprintable())
            if new_firmware:
                seen_firmware = self._firmware_seen.get(gateway_id)
                if seen_firmware is None:
                    logger.info(
                        f"Gateway {gateway_id} firmware: {new_firmware}"
                    )
                elif new_firmware != seen_firmware:
                    logger.info(
                        f"Gateway {gateway_id} firmware changed: "
                        f"{seen_firmware} -> {new_firmware}"
                    )
                self._firmware_seen[gateway_id] = new_firmware

            # Store successful data for graceful degradation
            self._last_successful_data[gateway_id] = data

            if was_offline:
                # Sanitize site_name (external input) — strip CR/LF and other
                # control chars to prevent log forging/injection.
                raw_site = data.site_name or ""
                site_clean = " ".join(raw_site.split())
                site_label = f" - Site: {site_clean}" if site_clean else ""
                logger.info(
                    f"Successfully connected to gateway {gateway_id} ({gateway.host}){site_label}"
                )
                if previous_failures > 0:
                    logger.debug(
                        f"Exponential backoff reset for {gateway_id} after {previous_failures} failures"
                    )

            self.cache[gateway_id] = GatewayStatus(
                gateway=gateway, data=data, online=True, last_updated=data.timestamp
            )

            # Queue the sample for daily energy statistics (never raises or
            # waits; no-op when the store is disabled).
            self._record_timeseries_sample(gateway_id, gateway, data)
            self._record_signal_sample(gateway_id, gateway, data)

            # Publish to MQTT after the cache is updated (never raises here).
            self._schedule_mqtt_publish(gateway_id)

        except Exception as e:
            gateway = self.gateways[gateway_id]

            # Increment failure count and calculate exponential backoff
            self._consecutive_failures[gateway_id] = (
                self._consecutive_failures.get(gateway_id, 0) + 1
            )
            failure_count = self._consecutive_failures[gateway_id]

            # Exponential backoff: 5s, 10s, 30s, 60s, 120s (max 2 minutes)
            backoff_intervals = [5, 10, 30, 60, 120]
            backoff_index = min(failure_count - 1, len(backoff_intervals) - 1)
            backoff_seconds = backoff_intervals[backoff_index]

            now = datetime.now().timestamp()
            self._next_poll_time[gateway_id] = now + backoff_seconds

            logger.debug(
                f"Exponential backoff for {gateway_id}: failure #{failure_count}, waiting {backoff_seconds}s before retry"
            )

            # Log connection failures with full context
            if gateway.online:
                # Just went offline
                logger.error(
                    f"Lost connection to gateway {gateway_id} ({gateway.host}): {e}"
                )
                logger.info(
                    f"Will retry gateway {gateway_id} in {backoff_seconds}s (failure #{failure_count})"
                )
            else:
                # Still offline, attempting to reconnect
                logger.warning(
                    f"Unable to connect to gateway {gateway_id} ({gateway.host}): {e} - backoff {backoff_seconds}s (failure #{failure_count})"
                )

            gateway.online = False
            gateway.last_error = str(e)

            self.cache[gateway_id] = GatewayStatus(
                gateway=gateway, online=False, error=str(e), last_updated=now
            )

            # Publish the offline status to MQTT so HA reflects gateway going offline.
            self._schedule_mqtt_publish(gateway_id)

    def _record_timeseries_sample(
        self, gateway_id: str, gateway, data: PowerwallData
    ) -> None:
        """Queue one poll result for the TimeSeriesStore.

        Runs after the cache update on every successful poll cycle and never
        waits for the write: a stalled disk must not hold up polling or MQTT
        (DESIGN.md §6). The store runs writes in FIFO order, so each
        gateway's samples stay ordered for trapezoidal integration, bounds
        its backlog, and logs storage failures.
        """
        try:
            aggregates = data.aggregates or {}
            solar = (aggregates.get("solar") or {}).get("instant_power")
            load = (aggregates.get("load") or {}).get("instant_power")
            battery = (aggregates.get("battery") or {}).get("instant_power")
            site = (aggregates.get("site") or {}).get("instant_power")
            if None in (solar, load, battery, site):
                return  # Partial aggregates — skip rather than store zeros
            ts = data.timestamp
            if ts is None or ts < 1e9:  # Bogus/missing clock — use server time
                ts = datetime.now().timestamp()
            from app.core.timeseries import get_timeseries_store

            get_timeseries_store().submit_sample(
                gateway_id=gateway_id,
                ts=ts,
                solar_w=solar,
                home_w=load,
                battery_w=battery,
                site_w=site,
                soe=data.soe,
                timezone=gateway.timezone,
            )
        except Exception as e:
            logger.debug(f"Time-series sample recording failed for {gateway_id}: {e}")

    def _record_signal_sample(
        self, gateway_id: str, gateway: Gateway, data: PowerwallData
    ) -> None:
        """Queue this poll's Powerwall temperatures and fan readings for the
        TimeSeriesStore.

        Signals come from the vitals and fan_speeds already fetched this
        cycle (no extra gateway calls). The store keeps at most one snapshot
        per PW_TIMESERIES_SIGNAL_INTERVAL, so most calls queue nothing. Like
        power samples, the write isn't awaited and failures are logged.

        Args:
            gateway_id: Gateway identifier.
            gateway: Gateway config (its timezone keys the daily rollups).
            data: This poll's data (vitals, fan_speeds, timestamp).
        """
        try:
            from app.core.timeseries import (
                extract_device_metrics,
                get_timeseries_store,
            )

            store = get_timeseries_store()
            if not store.signals_enabled:
                return
            # Vitals copied forward from the previous poll by the multi-PW
            # guard are not fresh readings: record only this poll's fans
            vitals = None if gateway_id in self._vitals_preserved else data.vitals
            metrics = extract_device_metrics(vitals, data.fan_speeds)
            if not metrics:
                return
            ts = data.timestamp
            if ts is None or ts < 1e9:
                ts = datetime.now().timestamp()
            store.submit_signal_sample(
                gateway_id, ts, metrics, timezone=gateway.timezone
            )
        except Exception as e:
            logger.debug(f"Device signal recording failed for {gateway_id}: {e}")

    def _schedule_mqtt_publish(self, gateway_id: str) -> None:
        """Schedule an MQTT publish of the current cached status for a gateway.

        Importing here (late import) avoids a circular dependency at module
        load time (publisher.py → config → gateway_manager), and create_task()
        ensures MQTT failures never raise into the poll path.

        At most one publish task is in flight per gateway (latest-wins): when
        the broker is slower than the poll interval, spawning a new unbounded
        task every cycle piles up tasks and interleaves stale-then-fresh
        retained topic writes.  The dict also holds a strong reference — the
        event loop keeps only weak refs, so an unreferenced task could be
        garbage-collected mid-publish.
        """
        from app.mqtt.publisher import mqtt_publisher

        if not mqtt_publisher.enabled:
            return
        previous = self._mqtt_tasks.get(gateway_id)
        if previous and not previous.done():
            # Replace the in-flight publish: the new task republishes every
            # topic with fresher data, so cancelling mid-publish is safe.
            previous.cancel()
        self._mqtt_tasks[gateway_id] = asyncio.create_task(
            mqtt_publisher.publish_gateway(gateway_id, self.cache[gateway_id]),
            name=f"mqtt-publish-{gateway_id}",
        )

    def _new_fallback_state(self) -> Dict:
        """Create a fresh fallback state dict."""
        return {
            "is_fallback_mode": False,
            "fallback_since": None,
            "recovery_attempts": 0,
            "last_recovery_attempt": None,
        }

    def _enter_fallback_mode(self, gateway_id: str, reason: str = "TEDAPI data unavailable"):
        """Signal that a gateway has entered SolarOnly fallback mode."""
        state = self._fallback_state.get(gateway_id)
        if not state:
            return
        if not state["is_fallback_mode"]:
            state["is_fallback_mode"] = True
            state["fallback_since"] = time.time()
            state["recovery_attempts"] = 0
            state["last_recovery_attempt"] = None
            logger.warning(
                "Gateway %s entering SolarOnly fallback mode: %s. "
                "Background recovery will retry periodically.",
                gateway_id,
                reason,
            )

    def _exit_fallback_mode(self, gateway_id: str):
        """Signal that a gateway has recovered from SolarOnly fallback mode."""
        state = self._fallback_state.get(gateway_id)
        if not state or not state["is_fallback_mode"]:
            return
        duration = time.time() - (state["fallback_since"] or time.time())
        attempts = state["recovery_attempts"]
        state["is_fallback_mode"] = False
        state["fallback_since"] = None
        state["recovery_attempts"] = 0
        state["last_recovery_attempt"] = None
        logger.info(
            "Gateway %s recovered from SolarOnly fallback mode after "
            "%.0fs and %d recovery attempt(s).",
            gateway_id,
            duration,
            attempts,
        )

    def get_fallback_state(self, gateway_id: str) -> Optional[Dict]:
        """Get fallback state for a gateway, or None if not tracked."""
        state = self._fallback_state.get(gateway_id)
        if not state:
            return None
        snapshot = dict(state)
        if snapshot["fallback_since"]:
            snapshot["fallback_duration_seconds"] = round(
                time.time() - snapshot["fallback_since"], 1
            )
        else:
            snapshot["fallback_duration_seconds"] = None
        from app.config import settings
        snapshot["recovery_enabled"] = settings.tedapi_recovery
        return snapshot

    def get_all_fallback_states(self) -> Dict[str, Dict]:
        """Get fallback state snapshots for all tracked gateways.

        Returns:
            Dict mapping gateway_id → fallback state snapshot (see
            get_fallback_state).  Empty dict when no gateways are tracked.
        """
        return {
            gw_id: self.get_fallback_state(gw_id)
            for gw_id in self._fallback_state
        }

    def reset_fallback_state(self, gateway_id: Optional[str] = None):
        """Reset fallback state for one or all gateways."""
        ids = [gateway_id] if gateway_id else list(self._fallback_state.keys())
        for gid in ids:
            state = self._fallback_state.get(gid)
            if state:
                state["is_fallback_mode"] = False
                state["fallback_since"] = None
                state["recovery_attempts"] = 0
                state["last_recovery_attempt"] = None
            self._tedapi_probe_failures[gid] = 0
            logger.info("Reset fallback state for gateway %s", gid)

    def _local_connection_kwargs(self, config: GatewayConfig) -> Dict[str, Any]:
        """Keyword arguments for a local pypowerwall connection.

        Shared by lazy initialization and TEDAPI recovery so a replacement
        connection is built exactly like the one startup builds.
        """
        from app.config import settings

        # Keep pypowerwall's internal cache alive for the whole poll cycle:
        # with the library default of 5s, a cycle that takes longer
        # re-fetches mid-cycle data it already has.
        pwcacheexpire = max(5, int(self._poll_interval))
        effective_host = _effective_host(config)
        tedapi_kwargs = {
            "host": effective_host,
            "gw_pwd": config.gw_pwd,
            "timezone": config.timezone,
            "timeout": settings.timeout,
            "poolmaxsize": settings.pool_maxsize,
            "failover": settings.failover,
            "pwcacheexpire": pwcacheexpire,
        }
        # Per-gateway password (PW_GATEWAYS/config file) or the legacy
        # PW_PASSWORD env var. Without gw_pwd/rsa_key this selects
        # pypowerwall's local client (PW3 Basic LAN / PW2 local mode);
        # alongside gw_pwd it selects hybrid.
        local_password = config.password or settings.pw_password
        if local_password:
            tedapi_kwargs["password"] = local_password
        if config.email:
            tedapi_kwargs["email"] = config.email
        if config.authpath:
            tedapi_kwargs["authpath"] = config.authpath
        if settings.cache_file:
            tedapi_kwargs["cachefile"] = settings.cache_file
        if settings.siteid:
            tedapi_kwargs["siteid"] = _siteid_kwarg(settings.siteid)
        if config.rsa_key_path:
            tedapi_kwargs["rsa_key_path"] = config.rsa_key_path
        if config.wifi_host:
            tedapi_kwargs["wifi_host"] = config.wifi_host
        return tedapi_kwargs

    async def _create_local_connection(
        self, config: GatewayConfig
    ) -> pypowerwall.Powerwall:
        """Create a fresh local pypowerwall connection.

        The blocking constructor runs in the shared executor under
        max(_CONNECTION_BUILD_TIMEOUT, PW_TIMEOUT + 5 s), so a slow gateway
        with a raised PW_TIMEOUT isn't abandoned mid-connect.  Used by the
        lazy init and by TEDAPI recovery, so both paths build the
        connection the same way.
        """
        from app.config import settings

        loop = asyncio.get_running_loop()
        tedapi_kwargs = self._local_connection_kwargs(config)
        return await asyncio.wait_for(
            loop.run_in_executor(
                self._executor,
                lambda kw=tedapi_kwargs: pypowerwall.Powerwall(**kw),
            ),
            timeout=max(self._CONNECTION_BUILD_TIMEOUT, settings.timeout + 5.0),
        )

    async def _tedapi_probe_loop(self, gateway_id: str):
        """Background task: probe TEDAPI health and recover from SolarOnly fallback.

        Probes pw.version() every PW_TEDAPI_PROBE_INTERVAL seconds.  After
        _TEDAPI_FALLBACK_THRESHOLD consecutive failed probes, enters fallback
        mode and attempts recovery with exponential backoff (60s → max 300s).
        On success, exits fallback mode and resets backoff.  On failure, stays
        in SolarOnly — the gateway keeps serving whatever data is available.

        The probe is driven by the gateway's configuration, not by the
        connection's current mode: a gateway configured for local TEDAPI is
        always probed, and a connection that is no longer in TEDAPI mode
        (the library's failover can switch it to cloud inside a timed-out
        connect(), #155) counts as a failed probe instead of skipping
        silently.  Recovery builds a fresh connection the same way startup
        does (_create_local_connection) and swaps it in only if it comes up
        in TEDAPI mode and reports a version, so the existing connection
        keeps serving until a verified replacement exists (reconnecting the
        drifted object in place would leave nothing serving meanwhile).  The
        replaced connection is dropped, not closed: the local client's
        close_session() logs its token out at the gateway, and the fresh
        connection reuses that token from the shared auth cache file.

        v1r + WiFi note: that connection has pw.tedapi set, so its probes
        run against the wired LAN; a WiFi TEDAPI outage might not be
        detected.  Monitoring is best-effort there; TEDAPI over WiFi alone
        gets full coverage.
        """
        from app.config import settings

        probe_interval = max(5, settings.tedapi_probe_interval)
        recovery_interval = self._TEDAPI_RECOVERY_INITIAL_INTERVAL

        while True:
            try:
                await asyncio.sleep(probe_interval)

                pw = self.connections.get(gateway_id)
                if not pw:
                    # Connection not yet established; skip probe
                    continue

                state = self._fallback_state.get(gateway_id)
                if not state:
                    continue

                if not state["is_fallback_mode"]:
                    # Probe based on configuration: a connection that is no
                    # longer in TEDAPI mode counts as a failed probe instead
                    # of skipping silently — skipping once held a gateway on
                    # cloud reads for hours after a firmware update (#155).
                    if not getattr(pw, "tedapi", None):
                        failures = self._tedapi_probe_failures.get(gateway_id, 0) + 1
                        self._tedapi_probe_failures[gateway_id] = failures
                        logger.warning(
                            "TEDAPI probe for %s: connection is in %s mode, "
                            "not TEDAPI — counting as failed probe (%d/%d)",
                            gateway_id,
                            getattr(pw, "mode", "unknown"),
                            failures,
                            self._TEDAPI_FALLBACK_THRESHOLD,
                        )
                        if failures >= self._TEDAPI_FALLBACK_THRESHOLD:
                            self._enter_fallback_mode(
                                gateway_id,
                                "connection left TEDAPI mode "
                                f"(mode={getattr(pw, 'mode', 'unknown')})",
                            )
                        continue

                    # Healthy path: probe TEDAPI
                    try:
                        loop = asyncio.get_running_loop()
                        version = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, pw.version),
                            timeout=max(5.0, settings.timeout + 2.0),
                        )
                    except Exception as probe_exc:
                        logger.debug(
                            "TEDAPI probe exception for %s (counts as failure): %s",
                            gateway_id, probe_exc,
                        )
                        version = None

                    if version is not None:
                        self._tedapi_probe_failures[gateway_id] = 0
                        recovery_interval = self._TEDAPI_RECOVERY_INITIAL_INTERVAL
                    else:
                        failures = self._tedapi_probe_failures.get(gateway_id, 0) + 1
                        self._tedapi_probe_failures[gateway_id] = failures
                        if failures >= self._TEDAPI_FALLBACK_THRESHOLD:
                            self._enter_fallback_mode(
                                gateway_id,
                                f"TEDAPI returned no data for {failures} consecutive probes",
                            )
                else:
                    # Fallback path: wait the backoff interval then attempt reconnect
                    extra_wait = recovery_interval - probe_interval
                    if extra_wait > 0:
                        await asyncio.sleep(extra_wait)

                    # Re-check after sleep: reset may have cleared state
                    if not state["is_fallback_mode"]:
                        self._tedapi_probe_failures[gateway_id] = 0
                        recovery_interval = self._TEDAPI_RECOVERY_INITIAL_INTERVAL
                        continue

                    state["recovery_attempts"] += 1
                    state["last_recovery_attempt"] = time.time()
                    attempt_num = state["recovery_attempts"]

                    logger.info(
                        "TEDAPI recovery attempt #%d for %s (interval=%ds)...",
                        attempt_num, gateway_id, recovery_interval,
                    )

                    recovered = False
                    try:
                        config = self._gateway_configs.get(gateway_id)
                        if config is None:
                            raise RuntimeError("no stored configuration")
                        # Fresh connection, built the same way the lazy init
                        # builds the first one.  Swap it in only if it comes
                        # up in TEDAPI mode and reports a version, so the
                        # current connection keeps serving otherwise.
                        fresh_pw = await self._create_local_connection(config)
                        verify_version = None
                        if getattr(fresh_pw, "tedapi", None):
                            loop = asyncio.get_running_loop()
                            verify_version = await asyncio.wait_for(
                                loop.run_in_executor(
                                    self._executor, fresh_pw.version
                                ),
                                timeout=max(5.0, settings.timeout + 2.0),
                            )
                        if verify_version is not None:
                            self.connections[gateway_id] = fresh_pw
                            recovered = True
                            logger.info(
                                "TEDAPI recovery attempt #%d for %s: fresh "
                                "connection in TEDAPI mode (version=%s), "
                                "replacing connection",
                                attempt_num, gateway_id, verify_version,
                            )
                        elif getattr(fresh_pw, "tedapi", None):
                            logger.warning(
                                "TEDAPI recovery attempt #%d for %s: fresh "
                                "connection reported no version — discarded",
                                attempt_num, gateway_id,
                            )
                        else:
                            logger.warning(
                                "TEDAPI recovery attempt #%d for %s: fresh "
                                "connection is not in TEDAPI mode (mode=%s) "
                                "— discarded",
                                attempt_num,
                                gateway_id,
                                getattr(fresh_pw, "mode", "unknown"),
                            )
                    except Exception as exc:
                        logger.warning(
                            "TEDAPI recovery attempt #%d for %s exception: %r",
                            attempt_num, gateway_id, exc,
                        )

                    if recovered:
                        self._exit_fallback_mode(gateway_id)
                        self._tedapi_probe_failures[gateway_id] = 0
                        recovery_interval = self._TEDAPI_RECOVERY_INITIAL_INTERVAL
                    else:
                        next_interval = min(
                            recovery_interval * 2,
                            self._TEDAPI_RECOVERY_MAX_INTERVAL,
                        )
                        logger.warning(
                            "TEDAPI recovery attempt #%d for %s failed — "
                            "staying in SolarOnly, next retry in %ds",
                            attempt_num, gateway_id, next_interval,
                        )
                        recovery_interval = next_interval

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "TEDAPI probe/recovery task unexpected error for %s: %s",
                    gateway_id, exc,
                )

    def get_last_data(self, gateway_id: str) -> Optional[PowerwallData]:
        """Last successfully polled data for a gateway, however old.

        Unlike get_gateway(), this ignores PW_CACHE_TTL, so it suits facts
        that don't change while the gateway is offline (e.g. which battery
        is PW1). Read-only: callers must not modify it.

        Args:
            gateway_id: Gateway identifier

        Returns:
            The last successful PowerwallData, or None before the first one.
        """
        return self._last_successful_data.get(gateway_id)

    def get_gateway(self, gateway_id: str) -> Optional[GatewayStatus]:
        """Get status for a specific gateway with graceful degradation support.

        Graceful Degradation (PW_GRACEFUL_DEGRADATION=yes):
            - If gateway is offline but went offline recently (within PW_CACHE_TTL seconds)
            - Return cached data with last_updated timestamp
            - After PW_CACHE_TTL expires, return status with data=None

        This allows UI to remain responsive during brief network outages while
        indicating stale data, and eventually showing "offline" after extended downtime.
        """
        from app.config import settings

        status = self.cache.get(gateway_id)
        if not status:
            return None

        # If gateway is online, return current status
        if status.online:
            return status

        # Gateway is offline - check graceful degradation settings
        if not settings.graceful_degradation:
            # Graceful degradation disabled - return offline status with no data
            logger.debug(
                f"Gateway {gateway_id} offline, graceful degradation disabled (PW_GRACEFUL_DEGRADATION=no)"
            )
            return status

        # Check if we have cached data that's still fresh
        last_success_data = self._last_successful_data.get(gateway_id)
        if not last_success_data or not last_success_data.timestamp:
            # No cached data available
            logger.debug(
                f"Gateway {gateway_id} offline, no cached data available for graceful degradation"
            )
            return status

        # Calculate age of cached data
        now = datetime.now().timestamp()
        data_age = now - last_success_data.timestamp

        # If cached data is within TTL, return it with offline status
        if data_age <= settings.cache_ttl:
            logger.debug(
                f"Graceful degradation active for {gateway_id}: serving stale data (age: {data_age:.0f}s / TTL: {settings.cache_ttl}s)"
            )
            return GatewayStatus(
                gateway=status.gateway,
                data=last_success_data,  # Return last good data
                online=False,  # Still indicate gateway is offline
                last_updated=last_success_data.timestamp,
                error=status.error,
            )

        # Cached data too old - return offline status with no data
        logger.debug(
            f"Graceful degradation expired for {gateway_id}: cached data too old (age: {data_age:.0f}s > TTL: {settings.cache_ttl}s), returning null"
        )
        return status

    def get_all_gateways(self) -> Dict[str, GatewayStatus]:
        """Get status for all gateways with graceful degradation applied."""
        result = {}
        for gateway_id in self.gateways.keys():
            status = self.get_gateway(
                gateway_id
            )  # Use get_gateway for graceful degradation
            if status:
                result[gateway_id] = status
        return result

    def get_connection(self, gateway_id: str) -> Optional[pypowerwall.Powerwall]:
        """Get pypowerwall connection for a gateway."""
        return self.connections.get(gateway_id)

    async def _locked_call(
        self,
        func: Callable[[], Any],
        timeout: float,
        on_dispatch: Optional[Callable[[asyncio.Future], None]] = None,
    ) -> Any:
        """Run a write (or a locked read) in the executor under _write_lock.

        The lock is released when the library call finishes, not when we stop
        waiting: after a timeout, or a cancelled request, the call keeps
        running in its worker thread, and the next write must not overlap it
        (two writes to the same site would race). ``timeout`` covers waiting
        for the lock, for a free worker and for the call: a write whose time
        runs out before its worker starts it raises asyncio.TimeoutError and
        never reaches the site after its caller gave up. ``on_dispatch`` sees
        the future right after submission (islanding's cooldown bookkeeping).
        """
        deadline = time.monotonic() + timeout

        def run_if_in_time():
            # The shared executor may be busy with reads: check again when
            # a worker picks the call up
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError("expired before a worker started it")
            return func()

        await asyncio.wait_for(self._write_lock.acquire(), timeout=timeout)
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, run_if_in_time
            )
        except BaseException:
            self._write_lock.release()
            raise
        future.add_done_callback(lambda _done: self._write_lock.release())
        if on_dispatch is not None:
            on_dispatch(future)
        return await asyncio.wait_for(
            asyncio.shield(future), timeout=deadline - time.monotonic()
        )

    async def call_api(
        self,
        gateway_id: str,
        method: str,
        *args,
        timeout: float = 5.0,
        fail_if_offline: bool = True,
        **kwargs,
    ) -> Optional[Any]:
        """Safely call a pypowerwall API method with timeout protection.

        This wraps blocking pypowerwall calls in the dedicated executor to prevent
        blocking the FastAPI event loop. All direct pypowerwall calls from API
        endpoints should use this method.

        Fast-Fail Behavior:
            By default, returns None immediately if gateway is offline (fail_if_offline=True).
            This prevents wasting time on connections that will likely fail.
            Set fail_if_offline=False for operations that should attempt connection
            regardless of cached status (e.g., reconnection attempts).

        Args:
            gateway_id: Gateway identifier
            method: Method name to call on pypowerwall object (e.g., 'grid_status', 'get_reserve')
            *args: Positional arguments to pass to method
            timeout: Timeout in seconds (default: 5.0)
            fail_if_offline: Return None immediately if gateway offline (default: True)
            **kwargs: Keyword arguments to pass to method

        Returns:
            Result of the pypowerwall method call, or None on error/timeout/offline

        Example:
            grid_status = await gateway_manager.call_api('default', 'grid_status', timeout=3.0)
            reserve = await gateway_manager.call_api('default', 'get_reserve')
        """
        # Fast-fail if gateway is offline
        if fail_if_offline:
            status = self.cache.get(gateway_id)
            if status and not status.online:
                logger.debug(
                    f"[{gateway_id}] call_api({method}) fast-fail: gateway offline"
                )
                return None

        pw = self.connections.get(gateway_id)
        if not pw:
            logger.warning(f"[{gateway_id}] call_api({method}): no connection object")
            return None

        try:
            method_func = getattr(pw, method)
            loop = asyncio.get_running_loop()
            logger.debug(
                f"[{gateway_id}] call_api({method}) starting (timeout={timeout}s)"
            )
            if method in _WRITE_METHODS:
                result = await self._locked_call(
                    lambda: method_func(*args, **kwargs), timeout
                )
            else:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: method_func(*args, **kwargs)
                    ),
                    timeout=timeout,
                )
            logger.debug(f"[{gateway_id}] call_api({method}) completed successfully")
            return result
        except asyncio.TimeoutError:
            logger.warning(
                f"[{gateway_id}] call_api({method}) timeout after {timeout}s"
            )
            return None
        except AttributeError:
            logger.error(f"[{gateway_id}] call_api({method}): method not found")
            return None
        except Exception as e:
            logger.warning(f"[{gateway_id}] call_api({method}) error: {e}")
            return None

    def cloud_link_status(self) -> Optional[Dict[str, Any]]:
        """Per-link health for the shared hybrid cloud-control connection (#87).

        Returns None when hybrid mode is not configured, so non-hybrid
        setups keep their previous /stats shape and semantics untouched.

        States:
            healthy     - connection live, no consecutive failures
            degraded    - connection live but recent calls failing
            unavailable - never connected, or failures >= threshold

        Also carries the last known cloud-sourced mode/reserve/grid-charging/
        grid-export values with their fetch timestamps, so consumers can
        serve stale-marked values when the cloud link drops after having
        been up.
        """
        if not self._cloud_control_configured:
            return None
        if self._cloud_control is None:
            state = "unavailable"
        elif self._cloud_failures >= self._CLOUD_UNAVAILABLE_THRESHOLD:
            state = "unavailable"
        elif self._cloud_failures > 0:
            state = "degraded"
        else:
            state = "healthy"
        return {
            "configured": True,
            "connected": self._cloud_control is not None,
            "state": state,
            "healthy": state == "healthy",
            "consecutive_failures": self._cloud_failures,
            "last_success_time": self._cloud_last_success,
            "last_known_mode": self._cloud_mode,
            "last_known_mode_time": self._cloud_mode_time,
            "last_known_reserve": self._cloud_reserve,
            "last_known_reserve_time": self._cloud_reserve_time,
            "last_known_grid_charging": self._cloud_grid_charging,
            "last_known_grid_charging_time": self._cloud_grid_charging_time,
            "last_known_grid_export": self._cloud_grid_export,
            "last_known_grid_export_time": self._cloud_grid_export_time,
        }

    async def cloud_control(
        self, method: str, *args, timeout: float = 10.0, **kwargs
    ) -> Optional[Any]:
        """Call a control method via the cloud connection.

        Local gateways (TEDAPI / v1r / Basic LAN) don't support write
        operations on their local API. When cloud credentials are configured
        alongside a local gateway, a separate cloud-mode pypowerwall
        connection is created for control operations like set_reserve() and
        set_mode().

        Args:
            method: Method name on pypowerwall (e.g., 'set_reserve', 'set_mode')
            *args: Positional arguments for the method
            timeout: Timeout in seconds (default: 10.0)
            **kwargs: Keyword arguments for the method

        Returns:
            Result of the method call, or None on error/timeout
        """
        if not self._cloud_control:
            logger.error(f"cloud_control({method}): no cloud connection available")
            return None
        try:
            method_func = getattr(self._cloud_control, method)
            loop = asyncio.get_running_loop()
            if method in _WRITE_METHODS:
                result = await self._locked_call(
                    lambda: method_func(*args, **kwargs), timeout
                )
            else:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: method_func(*args, **kwargs)
                    ),
                    timeout=timeout,
                )
            # Reads (get_mode/get_reserve polling, etc.) log at DEBUG so the
            # hybrid poll loop doesn't flood INFO every cycle (nesys, PR #85);
            # writes are rare and user-initiated, so they stay visible at INFO.
            if method in _WRITE_METHODS:
                logger.info(f"cloud_control({method}) completed successfully")
            else:
                logger.debug(f"cloud_control({method}) completed successfully")
            # Cloud link health (issue #87): a completed call means the link
            # is alive - clear the consecutive-failure counter and stamp the
            # last-success time. Same event-loop-only access as the poll loop.
            self._cloud_failures = 0
            self._cloud_last_success = time.time()
            return result
        except asyncio.TimeoutError:
            # Reads poll on a fixed cadence, so a cloud-link outage would
            # otherwise flood WARNING every cycle; writes stay at WARNING
            # because they are user-initiated (mirrors the success-path split).
            if method in _WRITE_METHODS:
                logger.warning(
                    f"cloud_control({method}) timeout after {timeout}s"
                )
            else:
                logger.debug(
                    f"cloud_control({method}) timeout after {timeout}s"
                )
            self._cloud_failures += 1
            return None
        except AttributeError:
            logger.error(f"cloud_control({method}): method not found")
            return None
        except Exception as e:
            if method in _WRITE_METHODS:
                logger.warning(f"cloud_control({method}) error: {e}")
            else:
                logger.debug(f"cloud_control({method}) error: {e}")
            self._cloud_failures += 1
            return None

    async def local_control(
        self,
        gateway_id: str,
        method: str,
        *args,
        timeout: float = 10.0,
        **kwargs,
    ) -> Optional[Any]:
        """Safely call a control method on a gateway's local pypowerwall connection.

        Mirrors cloud_control() but targets the gateway's own local connection
        (v1r/TEDAPI/Basic LAN), so mapped library methods like set_mode() and
        set_reserve() work without any cloud credentials.

        Args:
            gateway_id: Gateway identifier
            method: Method name on pypowerwall (e.g., 'set_mode', 'set_operation')
            *args: Positional arguments for the method
            timeout: Timeout in seconds (default: 10.0)
            **kwargs: Keyword arguments for the method

        Returns:
            Result of the method call, or None on error/timeout
        """
        if method in _ISLANDING_METHODS:
            # "In progress" means the dispatched future hasn't finished. Check
            # done() rather than relying on the clear-on-completion callback
            # below: asyncio runs done-callbacks on a later loop iteration, and
            # on Python 3.13 local_control() returns before that has happened.
            in_flight = self._islanding_futures.get(gateway_id)
            if in_flight is not None and not in_flight.done():
                raise IslandingCommandInProgressError(
                    "An islanding command is still in progress"
                )
            # Server-enforced cooldown between contactor commands. Late
            # import per repo convention (avoids circular dependency).
            from app.config import settings

            cooldown = settings.islanding_cooldown
            last_dispatch = self._islanding_last_dispatch.get(gateway_id)
            if cooldown > 0 and last_dispatch is not None:
                elapsed = time.monotonic() - last_dispatch
                if elapsed < cooldown:
                    raise IslandingCooldownError(
                        retry_after=max(1, math.ceil(cooldown - elapsed))
                    )

        pw = self.connections.get(gateway_id)
        if not pw:
            logger.error(
                f"local_control({method}): no local connection for {gateway_id}"
            )
            return None
        try:
            method_func = getattr(pw, method)
            loop = asyncio.get_running_loop()
            if method in _WRITE_METHODS or method in _LOCKED_READS:
                on_dispatch = None
                if method in _ISLANDING_METHODS:

                    def on_dispatch(future):
                        # Cooldown clock starts at dispatch — a failed or
                        # timed-out contactor command still counts, because
                        # the gateway may have acted on it.
                        self._islanding_last_dispatch[gateway_id] = time.monotonic()
                        self._islanding_futures[gateway_id] = future

                        def clear_after_completion(completed_future):
                            if (
                                self._islanding_futures.get(gateway_id)
                                is completed_future
                            ):
                                self._islanding_futures.pop(gateway_id, None)

                        future.add_done_callback(clear_after_completion)

                result = await self._locked_call(
                    lambda: method_func(*args, **kwargs), timeout, on_dispatch
                )
            else:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor, lambda: method_func(*args, **kwargs)
                    ),
                    timeout=timeout,
                )
            # Same read/write split as cloud_control(): polled reads stay
            # quiet at DEBUG, writes remain INFO.
            if method in _WRITE_METHODS:
                logger.info(
                    f"[{gateway_id}] local_control({method}) completed successfully"
                )
            else:
                logger.debug(
                    f"[{gateway_id}] local_control({method}) completed successfully"
                )
            return result
        except asyncio.TimeoutError:
            logger.warning(
                f"[{gateway_id}] local_control({method}) timeout after {timeout}s"
            )
            return None
        except AttributeError:
            logger.error(
                f"[{gateway_id}] local_control({method}): method not found"
            )
            return None
        except Exception as e:
            logger.warning(f"[{gateway_id}] local_control({method}) error: {e}")
            return None

    def _is_confirmed_v1r(self, gateway_id: str) -> bool:
        """True when the gateway is confirmed on the v1r transport.

        Same fail-closed rule as the MQTT controls (RSA key configured and
        ``tedapi_mode == 'v1r'`` from a live poll): an unknown mode (cold
        start, cloud failover) is not v1r.
        """
        try:
            status = self.get_gateway(gateway_id)
            return is_v1r_gateway(
                self.gateways.get(gateway_id),
                status.data if status else None,
            )
        except Exception:
            return False

    def _is_lan_failed(self, gateway_id: str) -> bool:
        """True when the library tripped its LAN to WiFi failover.

        Reads the same ``pw.tedapi`` handle the poll loop already
        dereferences: ``lan_failed`` means LAN requests fail and the WiFi
        host is serving, so a local v1r write (LAN-only) cannot dispatch.
        Fail-closed: anything unknown means the LAN is assumed up.
        """
        try:
            pw = self.connections.get(gateway_id)
            return bool(getattr(getattr(pw, "tedapi", None), "lan_failed", False))
        except Exception:
            return False

    async def control_write(
        self, gateway_id: str, method: str, *args, timeout: float = 10.0, **kwargs
    ) -> Tuple[Optional[str], Optional[Any]]:
        """Write a reserve/mode/grid control locally when PW_LOCAL_FIRST is on.

        Returns ``(path_label, result)`` when this helper takes the write,
        else ``(None, None)`` — the caller then runs its historical dispatch
        verbatim. The helper only takes the write with
        ``PW_LOCAL_FIRST=yes`` on a confirmed v1r transport whose LAN is up
        and which has a local connection object; in every other case (flag
        off, non-v1r, LAN failed, no local connection) behavior is
        byte-identical to before, because the historical HTTP and MQTT
        dispatch rules genuinely differ (HTTP: any cloud connection wins,
        even an ambiguous-site one; MQTT: strict ``_write_path`` order) and
        no single shared default could reproduce both.

        Local v1r writes are LAN-only, so with the library's LAN to WiFi
        failover tripped (``lan_failed``, e.g. the ESP32 bridge serving) or
        no local connection object there is nothing this helper could
        dispatch — it declines and the historical dispatch (cloud first
        when configured) runs unchanged. Any dispatched attempt — success,
        timeout (None) or a library error dict — is final and never
        followed by a second write: a timed-out write may still have been
        applied, and blindly failing over would risk double-applying or
        racing two async command paths (issue #149). A failed write returns
        HTTP 503 (or an MQTT failed-warning) so the caller can retry
        deliberately. Absolute-value controls (reserve, mode, grid
        settings) converge on the same target state either way.

        Islanding methods must not go through here (always local v1r).
        """
        # Late import per repo convention (avoids circular dependency).
        from app.config import settings

        if not settings.local_first or not self._is_confirmed_v1r(gateway_id):
            return None, None
        status = self.cache.get(gateway_id)
        if (
            self._is_lan_failed(gateway_id)
            or self.connections.get(gateway_id) is None
            # lan_failed only trips with a WiFi host and failover on; a
            # failed last poll covers a dead LAN without them (cached state)
            or status is None
            or not status.online
        ):
            # Nothing dispatchable locally (local v1r writes are LAN-only):
            # decline and let the caller's historical dispatch run unchanged.
            return None, None
        logger.info(
            f"[{gateway_id}] control_write({method}): trying local v1r first"
        )
        result = await self.local_control(
            gateway_id, method, *args, timeout=timeout, **kwargs
        )
        # Final, whatever came back: a timeout (None) may still have been
        # applied, and an error dict means the gateway answered. Either
        # way a second write on another path could race it.
        return "local v1r", result

    async def call_tedapi(
        self,
        gateway_id: str,
        method: str,
        *args,
        timeout: float = 5.0,
        fail_if_offline: bool = True,
        **kwargs,
    ) -> Optional[Any]:
        """Safely call a TEDAPI method with timeout protection.

        Args:
            gateway_id: Gateway identifier
            method: Method name to call on tedapi object (e.g., 'get_config', 'get_status')
            timeout: Timeout in seconds (default: 5.0)
            fail_if_offline: Return None immediately if gateway offline (default: True)

        Returns:
            Result of the TEDAPI method call, or None if TEDAPI not available/offline
        """
        # Fast-fail if gateway is offline
        if fail_if_offline:
            status = self.cache.get(gateway_id)
            if status and not status.online:
                logger.debug(
                    f"[{gateway_id}] call_tedapi({method}) fast-fail: gateway offline"
                )
                return None

        pw = self.connections.get(gateway_id)
        if not pw or not hasattr(pw, "tedapi") or not pw.tedapi:
            logger.debug(f"[{gateway_id}] call_tedapi({method}): TEDAPI not available")
            return None

        try:
            method_func = getattr(pw.tedapi, method)
            loop = asyncio.get_running_loop()
            logger.debug(
                f"[{gateway_id}] call_tedapi({method}) starting (timeout={timeout}s)"
            )
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor, lambda: method_func(*args, **kwargs)
                ),
                timeout=timeout,
            )
            logger.debug(f"[{gateway_id}] call_tedapi({method}) completed successfully")
            return result
        except asyncio.TimeoutError:
            logger.warning(
                f"[{gateway_id}] call_tedapi({method}) timeout after {timeout}s"
            )
            return None
        except AttributeError:
            logger.error(f"[{gateway_id}] call_tedapi({method}): method not found")
            return None
        except Exception as e:
            logger.warning(f"[{gateway_id}] call_tedapi({method}) error: {e}")
            return None

    def get_aggregate_data(self) -> AggregateData:
        """Get aggregated data from all gateways.

        SMART AGGREGATION NOTES:
        This is a first-pass implementation that will need tuning as we get real-world
        multi-gateway deployments. Current approach:

        - Battery %: Simple average (TODO: weight by capacity when available)
        - Power flows: Simple sum (works for most cases)
        - Grid power: Calculated as site - solar

        Future considerations:
        - Different aggregation strategies per metric type
        - Weighted averages based on system capacity
        - Handling mixed local/cloud gateways
        - Time synchronization across gateways
        - Outlier detection and handling
        """
        aggregate = AggregateData(timestamp=datetime.now().timestamp())

        # Battery % must be averaged over the gateways that actually reported
        # SOE — solar-only inverters (type: "inverter") and transient SOE
        # fetch failures would otherwise dilute the average toward zero.
        num_soe = 0
        num_soe_raw = 0

        # Use get_all_gateways() so graceful degradation applies here the same
        # way it does for the legacy endpoints — otherwise a brief outage makes
        # aggregate totals dip to zero while /aggregates still shows steady
        # power from the stale-within-TTL snapshot.
        for gateway_id, status in self.get_all_gateways().items():
            aggregate.num_gateways += 1

            if not status.data:
                continue

            if status.online:
                aggregate.num_online += 1
            else:
                aggregate.num_degraded += 1
            data = status.data

            # Aggregate battery percentage
            # TODO: Weight by capacity when battery capacity info is available
            if data.soe_raw is not None:
                aggregate.total_battery_percent_raw += data.soe_raw
                num_soe_raw += 1
            if data.soe is not None:
                aggregate.total_battery_percent += data.soe
                num_soe += 1

            # Aggregate power flows (simple sum - works well for separate systems)
            if data.aggregates:
                # A meter block or its instant_power can be None (data gap);
                # count it as 0 in the totals and leave the snapshot as is
                def power(meter: str) -> float:
                    return (data.aggregates.get(meter) or {}).get("instant_power") or 0

                site_power = power("site")
                battery_power = power("battery")
                load_power = power("load")
                solar_power = power("solar")

                logger.debug(
                    f"Gateway {gateway_id} power: site={site_power}, battery={battery_power}, load={load_power}, solar={solar_power}"
                )

                aggregate.total_site_power += site_power
                aggregate.total_battery_power += battery_power
                aggregate.total_load_power += load_power
                aggregate.total_solar_power += solar_power

            aggregate.gateways[gateway_id] = status

        # Calculate average battery percentage (simple average for now)
        if num_soe_raw > 0:
            aggregate.total_battery_percent_raw /= num_soe_raw
        if num_soe > 0:
            aggregate.total_battery_percent /= num_soe

        # Grid power is the site power (positive = importing, negative = exporting)
        # The "site" meter in aggregates measures grid interaction directly
        aggregate.total_grid_power = aggregate.total_site_power

        # Get grid status from the primary gateway: "default" if configured,
        # otherwise the first configured gateway (not everyone names one "default")
        primary_id = "default" if "default" in self.gateways else next(
            iter(self.gateways), None
        )
        if primary_id:
            primary = aggregate.gateways.get(primary_id) or self.cache.get(primary_id)
            if primary and primary.data:
                aggregate.grid_status = primary.data.grid_status

        return aggregate


# Global gateway manager instance
gateway_manager = GatewayManager()
