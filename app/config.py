"""
Configuration Management for PyPowerwall Server

This module handles all server configuration through environment variables and provides
100% backward compatibility with the original pypowerwall proxy server settings.

Configuration Methods:

    1. Multi-Gateway Configuration (recommended for multiple Powerwalls):
        export PW_GATEWAYS='[
          {
            "id": "home",
            "name": "Home Powerwall",
            "host": "192.168.91.1",
            "gw_pwd": "gateway_wifi_password",
            "timezone": "America/Los_Angeles"
          },
          {
            "id": "cabin",
            "name": "Cabin Powerwall",
            "host": "192.168.92.1",
            "gw_pwd": "different_password",
            "timezone": "America/Denver"
          }
        ]'

    2. Single Gateway Configuration (legacy compatibility):
        export PW_HOST=192.168.91.1
        export PW_GW_PWD=gateway_wifi_password
        export PW_TIMEZONE=America/Los_Angeles

Environment Variables (Proxy Compatible):

    Core Connection Settings:
        PW_HOST              - Powerwall IP address (default: none, e.g., 192.168.91.1)
        PW_GW_PWD            - Gateway WiFi password for TEDAPI (default: none)
        PW_PASSWORD          - Customer password for local API access - PW2 local
                               mode or PW3 Basic LAN mode, typically the last 5
                               characters of the gateway password (default: none)
        PW_EMAIL             - Tesla account email for cloud mode (default: none)
        PW_TIMEZONE          - Local timezone (default: "America/Los_Angeles")
        PW_AUTH_PATH         - Path to auth token files (default: none)

    Server Settings:
        PW_BIND_ADDRESS      - Server bind address (default: "0.0.0.0")
        PW_PORT              - Server port (default: 8675)
        PW_DEBUG             - Enable debug logging "yes"/"no" (default: "no")
        PW_HTTPS             - Enable HTTPS mode (default: "no")

    Performance Settings:
        PW_CACHE_EXPIRE      - Polling frequency in seconds (default: 5)
        PW_BROWSER_CACHE     - Browser cache time in seconds (default: 0)
        PW_TIMEOUT           - Pypowerwall timeout in seconds (default: 10)
        PW_POOL_MAXSIZE      - Connection pool size (default: 15)

    Network Robustness:
        PW_SUPPRESS_NETWORK_ERRORS  - Suppress error logs (default: "no")
        PW_NETWORK_ERROR_RATE_LIMIT - Errors per minute limit (default: 5)
        PW_FAIL_FAST                - Return immediately on degraded connection (default: "no")
        PW_GRACEFUL_DEGRADATION     - Use cached data when unavailable (default: "yes")
        PW_HEALTH_CHECK             - Enable health monitoring (default: "yes")
        PW_CACHE_TTL                - Max cached data age in seconds (default: 30)
        PW_TEDAPI_RECOVERY          - Auto-recover TEDAPI SolarOnly fallback (default: "yes")
        PW_TEDAPI_PROBE_INTERVAL    - Seconds between TEDAPI health probes (default: 30)

    Rate Limiting (disabled by default; see README "Rate Limiting" section):
        PW_RATE_LIMIT_ENABLED         - Enable per-IP request rate limiting (default: "no")
        PW_RATE_LIMIT_MAX_REQUESTS    - Requests per window per client IP (default: 1000)
        PW_RATE_LIMIT_WINDOW_SECONDS  - Rate limit window in seconds (default: 60)
        PW_RATE_LIMIT_MAX_BUCKETS     - Max tracked client IPs before pruning (default: 10000)

    UI and Advanced:
        PW_STYLE             - UI style: clear/black/white/grafana/grafana-dark (default: "clear")
        PW_AUTH_MODE         - Auth mode: cookie/token (default: "cookie")
        PW_CACHE_FILE        - Cache file path (default: auto - uses PW_AUTH_PATH/.powerwall or /tmp/.powerwall)
        PW_SITEID            - Tesla site ID for multi-site accounts (default: none)
        PW_CONTROL_SECRET    - Enable control commands (default: none/disabled)
        PW_NEG_SOLAR         - Allow negative solar values "yes"/"no" (default: "no")
        PW_RSA_KEY_PATH      - Path to RSA-4096 private key PEM for TEDAPI v1r LAN access (default: none)
        PW_WIFI_HOST         - WiFi host IP for TEDAPI v1r WiFi fallback (default: none)
        PROXY_BASE_URL       - Base URL for reverse proxy (default: "/")

    Time-Series Storage (Daily Energy Stats):
        PW_TIMESERIES_RETENTION       - Raw 5s sample retention, e.g. "24h" (default), "7d",
                                        "30d", "365d"; "0" = unlimited, "-1" = disable
                                        the subsystem entirely (headless proxy, no SQLite,
                                        no daily stats UI/API)
        PW_TIMESERIES_DAILY_RETENTION - Daily kWh aggregate retention (default: "0" =
                                        unlimited; one tiny row per gateway per day)
        PW_TIMESERIES_PATH            - SQLite database path (default: /data/timeseries.db
                                        when /data exists, else data/timeseries.db).
                                        If pointed at a directory instead of a file,
                                        "timeseries.db" is appended automatically.

Connection Modes:

    TEDAPI (Local Gateway Access):
        • Fastest, most reliable
        • Requires direct connection to gateway WiFi or local network
        • Configuration: PW_HOST + PW_GW_PWD  (password-based)
        •            OR: PW_HOST + PW_GW_PWD + PW_RSA_KEY_PATH  (RSA key + v1r mode)
        • Example: 192.168.91.1 with gateway password (optionally with RSA PEM key for v1r mode)

    Cloud Mode:
        • Remote access from anywhere
        • Requires Tesla account authentication
        • Configuration: PW_EMAIL + PW_AUTH_PATH
        • Setup: Run `python3 -m pypowerwall setup` first

    FleetAPI Mode:
        • Official Tesla API
        • Requires app registration with Tesla
        • Configuration: PW_EMAIL + PW_AUTH_PATH + fleetapi=true

Gateway Configuration Priority:

    1. PW_GATEWAYS JSON (highest priority)
       - Supports multiple gateways with independent settings
       - Each gateway can have different connection modes

    2. Legacy environment variables (fallback)
       - Single gateway using PW_HOST, PW_GW_PWD, etc.
       - Automatically creates "default" gateway
       - 100% compatible with proxy server

Examples:

    # Single gateway (TEDAPI local access)
    PW_HOST=192.168.91.1
    PW_GW_PWD=MyGatewayPassword
    PW_CACHE_EXPIRE=10
    PW_DEBUG=yes

    # Single gateway (Cloud mode)
    PW_EMAIL=user@example.com
    PW_AUTH_PATH=/home/user/.pypowerwall
    PW_CACHE_EXPIRE=30

    # Multiple gateways (mixed modes)
    PW_GATEWAYS='[
      {"id": "home", "host": "192.168.91.1", "gw_pwd": "pass1"},
      {"id": "remote", "email": "user@example.com", "authpath": "/path", "cloud_mode": true}
    ]'
    PW_CACHE_EXPIRE=15

Architecture:

    Settings Class (Pydantic BaseSettings):
        • Automatically loads from environment variables
        • Type validation and conversion
        • Default values for all settings
        • Computed properties (e.g., control_enabled)

    GatewayConfig Class:
        • Defines single gateway configuration
        • Supports TEDAPI, Cloud, and FleetAPI modes
        • Used in both PW_GATEWAYS and legacy modes

    Initialization Flow:
        1. Settings() loads all environment variables
        2. _initialize_gateways() called automatically
        3. Try PW_GATEWAYS first (multi-gateway JSON)
        4. Fall back to legacy PW_HOST/PW_EMAIL (single gateway)
        5. Gateway list stored in settings.gateways

Adding New Settings:

    1. Add field to Settings class with Field() and alias:
        new_setting: int = Field(default=42, alias="PW_NEW_SETTING")

    2. Document in this module docstring

    3. Use in code via settings.new_setting

    4. Update README with new variable

Accessing Configuration:

    from app.config import settings

    # Access settings
    port = settings.server_port
    gateways = settings.gateways
    debug_enabled = settings.debug

    # Check features
    if settings.control_enabled:
        # Control commands available
        pass
"""

import json
import logging
import os
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Server version
SERVER_VERSION = "0.6.2"


class GatewayConfig(BaseModel):
    """Configuration for a single Powerwall gateway.

    Auth modes:
    - TEDAPI: host + gw_pwd (local gateway access via 192.168.91.1)
    - Basic LAN: host + password (PW3 wired LAN core metrics, no RSA key needed)
    - Cloud: email + authpath (uses .pypowerwall.auth and .pypowerwall.site files)

    NOTE: Deliberately a plain BaseModel, not BaseSettings — it is only ever
    constructed from explicit kwargs / PW_GATEWAYS JSON.  As a BaseSettings
    with an empty env_prefix, generic environment variables (PORT, EMAIL,
    NAME, HOST — ubiquitous on container platforms) silently populated any
    field not passed explicitly.
    """

    id: str
    name: Optional[str] = None  # Defaults to id when omitted
    host: Optional[str] = None
    port: Optional[int] = Field(
        default=None, ge=1, le=65535
    )  # Non-standard HTTPS port (e.g. 8443 via travel router)
    gw_pwd: Optional[str] = None  # Gateway Wi-Fi password for TEDAPI mode
    password: Optional[str] = None  # Customer password for local API (PW3 Basic LAN / PW2 local)
    rsa_key_path: Optional[str] = (
        None  # Path to RSA-4096 private key PEM for v1r LAN TEDAPI access
    )
    wifi_host: Optional[str] = None  # WiFi host IP for TEDAPI v1r WiFi fallback
    email: Optional[str] = None
    authpath: Optional[str] = (
        None  # Path to .pypowerwall.auth and .pypowerwall.site files
    )
    timezone: str = "America/Los_Angeles"
    cloud_mode: bool = False
    fleetapi: bool = False
    type: str = "powerwall"  # "powerwall" | "inverter" (solar-only, no batteries)

    @model_validator(mode="after")
    def _default_name_to_id(self):
        if not self.name:
            self.name = self.id
        return self


class Settings(BaseSettings):
    """Application settings - Compatible with pypowerwall proxy environment variables."""

    # Server configuration (maps to proxy settings)
    server_host: str = Field(default="0.0.0.0", alias="PW_BIND_ADDRESS")
    server_port: int = Field(default=8675, alias="PW_PORT")
    debug: bool = Field(default=False, alias="PW_DEBUG")

    # Powerwall connection settings
    pw_host: Optional[str] = Field(default=None, alias="PW_HOST")
    pw_gw_pwd: Optional[str] = Field(default=None, alias="PW_GW_PWD")
    pw_password: Optional[str] = Field(
        default=None, alias="PW_PASSWORD"
    )  # Legacy PW2 local access
    pw_email: Optional[str] = Field(default=None, alias="PW_EMAIL")
    pw_timezone: str = Field(default="America/Los_Angeles", alias="PW_TIMEZONE")

    # Proxy settings
    cache_expire: int = Field(
        default=5, alias="PW_CACHE_EXPIRE"
    )  # Polling frequency in seconds
    browser_cache: int = Field(
        default=0, alias="PW_BROWSER_CACHE"
    )  # Browser cache time in seconds
    timeout: int = Field(
        default=10, alias="PW_TIMEOUT"
    )  # Pypowerwall timeout in seconds
    pool_maxsize: int = Field(
        default=15, alias="PW_POOL_MAXSIZE"
    )  # Connection pool size
    https_mode: bool = Field(default=False, alias="PW_HTTPS")

    # Network robustness settings
    suppress_network_errors: bool = Field(
        default=False, alias="PW_SUPPRESS_NETWORK_ERRORS"
    )
    network_error_rate_limit: int = Field(
        default=5, alias="PW_NETWORK_ERROR_RATE_LIMIT"
    )
    fail_fast: bool = Field(default=False, alias="PW_FAIL_FAST")
    graceful_degradation: bool = Field(default=True, alias="PW_GRACEFUL_DEGRADATION")
    health_check: bool = Field(default=True, alias="PW_HEALTH_CHECK")
    cache_ttl: int = Field(default=30, alias="PW_CACHE_TTL")  # Max age for cached data

    # Rate limiting (disabled by default; see README "Rate Limiting" section)
    rate_limit_enabled: bool = Field(default=False, alias="PW_RATE_LIMIT_ENABLED")
    rate_limit_max_requests: int = Field(
        default=1000, alias="PW_RATE_LIMIT_MAX_REQUESTS"
    )  # Requests per window, per client IP
    rate_limit_window_seconds: int = Field(
        default=60, alias="PW_RATE_LIMIT_WINDOW_SECONDS"
    )
    rate_limit_max_buckets: int = Field(
        default=10000, alias="PW_RATE_LIMIT_MAX_BUCKETS"
    )  # Cap on tracked client IPs before stale/oldest buckets are pruned

    # TEDAPI SolarOnly fallback recovery
    tedapi_recovery: bool = Field(
        default=True, alias="PW_TEDAPI_RECOVERY"
    )  # Auto-recover TEDAPI connection when SolarOnly fallback is detected
    tedapi_probe_interval: int = Field(
        default=30, alias="PW_TEDAPI_PROBE_INTERVAL"
    )  # Seconds between TEDAPI health probes

    # UI and advanced settings
    style: str = Field(default="clear", alias="PW_STYLE")
    pw_authpath: Optional[str] = Field(default=None, alias="PW_AUTH_PATH")
    auth_mode: str = Field(default="cookie", alias="PW_AUTH_MODE")
    cache_file: Optional[str] = Field(default=None, alias="PW_CACHE_FILE")
    siteid: Optional[str] = Field(default=None, alias="PW_SITEID")
    control_secret: Optional[str] = Field(default=None, alias="PW_CONTROL_SECRET")
    proxy_base_url: str = Field(default="/", alias="PROXY_BASE_URL")
    pw_rsa_key_path: Optional[str] = Field(
        default=None, alias="PW_RSA_KEY_PATH"
    )  # RSA-4096 private key PEM path for TEDAPI v1r LAN access
    pw_wifi_host: Optional[str] = Field(
        default=None, alias="PW_WIFI_HOST"
    )  # WiFi host IP for TEDAPI v1r WiFi fallback
    neg_solar: bool = Field(
        default=False, alias="PW_NEG_SOLAR"
    )  # Allow negative solar values (default: no)

    # CORS configuration
    cors_origins: List[str] = Field(default=["*"], alias="CORS_ORIGINS")

    # Time-series storage (TimeSeriesStore — daily energy stats, see app/core/timeseries.py)
    timeseries_retention: str = Field(
        default="24h", alias="PW_TIMESERIES_RETENTION"
    )  # Raw sample retention; "-1" disables the subsystem, "0" = unlimited
    timeseries_daily_retention: str = Field(
        default="0", alias="PW_TIMESERIES_DAILY_RETENTION"
    )  # Daily aggregate retention; "0" = unlimited (default)
    timeseries_path: Optional[str] = Field(
        default=None, alias="PW_TIMESERIES_PATH"
    )  # SQLite file path; resolved in __init__ (/data aware)

    # MQTT settings
    # Set MQTT_HOST to enable MQTT publishing. All other MQTT_ variables are optional.
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

    @property
    def mqtt_enabled(self) -> bool:
        """MQTT publishing is enabled when MQTT_HOST is set."""
        return bool(self.mqtt_host)

    # Gateway configuration
    gateways: List[GatewayConfig] = Field(default_factory=list)

    # Computed properties
    @property
    def control_enabled(self) -> bool:
        """Control features enabled if PW_CONTROL_SECRET is set."""
        return bool(self.control_secret)

    model_config = {"env_prefix": "", "case_sensitive": False}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Set default cache_file based on auth path if not explicitly provided
        if self.cache_file is None:
            if self.pw_authpath:
                # Use auth directory for cache file if it's set
                self.cache_file = os.path.join(self.pw_authpath, ".powerwall")
            else:
                # Fall back to /tmp if no auth path
                self.cache_file = "/tmp/.powerwall"
        # Set default time-series database path: prefer a /data volume mount
        # (the Docker image creates /data) so daily energy history survives
        # container restarts; otherwise keep it local to the working dir.
        if self.timeseries_path is None:
            self.timeseries_path = (
                "/data/timeseries.db"
                if os.path.isdir("/data")
                else "data/timeseries.db"
            )
        self._initialize_gateways()

    def _load_config_file(self) -> bool:
        """Load gateway (and optional server) config from a PW_CONFIG file.

        Supports the YAML format documented in the README (JSON works too —
        it is a subset of YAML).  Expected structure:

            server:            # optional
              host: 0.0.0.0
              port: 8675
              cors_origins: [...]
            gateways:
              - id: home
                host: 192.168.91.1
                gw_pwd: ...

        Returns True when PW_CONFIG is set (the file takes precedence over
        PW_GATEWAYS / legacy env config, even if it fails to parse — a broken
        file must not silently reconfigure the server to a different mode).
        """
        config_path = os.getenv("PW_CONFIG")
        if not config_path:
            return False

        self.gateways = []
        try:
            import yaml

            with open(config_path) as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            logger.error(
                f"Failed to read PW_CONFIG file {config_path}: {e} - "
                "no gateways configured"
            )
            return True

        if not isinstance(doc, dict):
            logger.error(
                f"PW_CONFIG file {config_path} must contain a mapping with a "
                "'gateways' list - no gateways configured"
            )
            return True

        server = doc.get("server")
        if isinstance(server, dict):
            # Parse defensively: a malformed value (e.g. port: "eight")
            # should log an error and keep the default, not crash startup.
            if "host" in server:
                self.server_host = str(server["host"])
            if "port" in server:
                try:
                    port = int(server["port"])
                    if not 1 <= port <= 65535:
                        raise ValueError(f"port out of range: {port}")
                    self.server_port = port
                except (TypeError, ValueError) as e:
                    logger.error(
                        f"Invalid server.port in {config_path} ({e}) - "
                        f"keeping {self.server_port}"
                    )
            if isinstance(server.get("cors_origins"), list):
                self.cors_origins = [str(o) for o in server["cors_origins"]]

        entries = doc.get("gateways")
        if not isinstance(entries, list):
            logger.error(
                f"PW_CONFIG file {config_path} has no 'gateways' list - "
                "no gateways configured"
            )
            return True

        for idx, gw in enumerate(entries):
            try:
                self.gateways.append(GatewayConfig(**gw))
            except Exception as e:
                logger.error(
                    f"Skipping invalid gateway entry {idx} in {config_path} "
                    f"(id={gw.get('id', '?') if isinstance(gw, dict) else '?'}): {e}"
                )
        if not self.gateways:
            logger.error(
                f"PW_CONFIG file {config_path} contained no valid gateway entries"
            )
        return True

    def _initialize_gateways(self):
        """Initialize gateway configurations from PW_CONFIG file or environment variables."""
        # Config file (--config / PW_CONFIG) takes precedence
        if self._load_config_file():
            return

        # Try to load from PW_GATEWAYS JSON
        gateways_json = os.getenv("PW_GATEWAYS")
        if gateways_json:
            # When PW_GATEWAYS is set, never silently fall back to legacy
            # single-gateway mode: a typo in a 5-gateway config used to
            # discard the whole list and quietly reconfigure the server.
            # Validate each entry individually so one bad entry doesn't
            # take out the rest.
            self.gateways = []
            try:
                gateways_data = json.loads(gateways_json)
            except Exception as e:
                logger.error(
                    f"PW_GATEWAYS is not valid JSON ({e}) - " "no gateways configured"
                )
                return
            if not isinstance(gateways_data, list):
                logger.error(
                    "PW_GATEWAYS must be a JSON array of gateway objects - "
                    "no gateways configured"
                )
                return
            for idx, gw in enumerate(gateways_data):
                try:
                    self.gateways.append(GatewayConfig(**gw))
                except Exception as e:
                    logger.error(
                        f"Skipping invalid PW_GATEWAYS entry {idx} "
                        f"(id={gw.get('id', '?') if isinstance(gw, dict) else '?'}): {e}"
                    )
            if not self.gateways:
                logger.error(
                    "PW_GATEWAYS was set but contained no valid gateway entries"
                )
            return

        # Fall back to single gateway mode (legacy compatibility)
        if self.pw_host or self.pw_email:
            self.gateways = [
                GatewayConfig(
                    id="default",
                    name="Default Gateway",
                    host=self.pw_host,
                    gw_pwd=self.pw_gw_pwd,
                    password=self.pw_password,
                    rsa_key_path=self.pw_rsa_key_path,
                    wifi_host=self.pw_wifi_host,
                    email=self.pw_email,
                    authpath=self.pw_authpath,
                    timezone=self.pw_timezone,
                    cloud_mode=bool(self.pw_email and not self.pw_host),
                )
            ]


# Global settings instance
settings = Settings()
