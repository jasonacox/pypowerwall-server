"""Setup wizard service: status, validation, and atomic configuration."""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, available_timezones

import pypowerwall
import yaml
from pydantic import SecretStr

from app.config import (
    GatewayConfig,
    _SETTINGS_BLOCK_EXCLUDED,
    Settings,
    default_config_path,
    settings,
)
from app.models.setup import (
    SetupCloudAuthCompleteRequest,
    SetupCloudAuthCompleteResult,
    SetupCloudAuthStartResult,
    SetupCloudAuthUseRequest,
    SetupConfigureRequest,
    SetupConfigureResult,
    SetupAuthFileRequest,
    SetupAuthFileResult,
    SetupAuthTokensRequest,
    SetupAuthTokensResult,
    SetupDeleteConfigRequest,
    SetupDeleteConfigResult,
    SetupDiagnosticResult,
    SetupDiagnosticStep,
    SetupEnvImportRequest,
    SetupEnvImportResult,
    SetupFleetCompleteRequest,
    SetupFleetCompleteResult,
    SetupFleetKeyRequest,
    SetupFleetKeyResult,
    SetupFleetSite,
    SetupFleetSiteRequest,
    SetupFleetSiteResult,
    SetupFleetStartRequest,
    SetupFleetStartResult,
    SetupGatewayInput,
    SetupScanGateway,
    SetupScanRequest,
    SetupScanResult,
    SetupGatewaySummary,
    SetupCloudSiteResult,
    SetupStatus,
    SetupV1rAuthFileRequest,
    SetupV1rAuthFileResult,
    SetupV1rRegisterRequest,
    SetupV1rResult,
    SetupV1rSite,
    SetupV1rStatusRequest,
    SetupValidationIssue,
    SetupValidationResult,
)

logger = logging.getLogger(__name__)

_CONFIG_LOCK = threading.Lock()
_SETUP_TOKEN: Optional[str] = None
_CLOUD_AUTH_LOCK = threading.Lock()
_CLOUD_AUTH_STATES: Dict[str, Dict[str, Any]] = {}
_FLEET_AUTH_LOCK = threading.Lock()
_FLEET_AUTH_STATES: Dict[str, Dict[str, Any]] = {}
_SERVER_OPTION_MAP = {
    "server_host": "host",
    "server_port": "port",
    "cors_origins": "cors_origins",
}
_WRITABLE_OPTION_FIELDS = (set(Settings.model_fields) - _SETTINGS_BLOCK_EXCLUDED) | set(
    _SERVER_OPTION_MAP
)


def initialize_setup_auth() -> None:
    """Generate the bootstrap token once when no control secret exists."""
    global _SETUP_TOKEN
    if settings.control_secret:
        _SETUP_TOKEN = None
        return
    if _SETUP_TOKEN is None:
        _SETUP_TOKEN = secrets.token_urlsafe(32)
        logger.warning(
            "Setup wizard access token: %s (set PW_CONTROL_SECRET to use a "
            "persistent admin token instead)",
            _SETUP_TOKEN,
        )


def setup_auth_token() -> Optional[str]:
    """Current bootstrap token, if control auth is not configured."""
    global _SETUP_TOKEN
    if settings.control_secret:
        return None
    if _SETUP_TOKEN is None:
        _SETUP_TOKEN = secrets.token_urlsafe(32)
        logger.warning(
            "Setup wizard access token: %s (set PW_CONTROL_SECRET to use a "
            "persistent admin token instead)",
            _SETUP_TOKEN,
        )
    return _SETUP_TOKEN


def verify_setup_token(token: Optional[str]) -> bool:
    """Constant-time check against PW_CONTROL_SECRET or the bootstrap token."""
    if not token:
        return False
    expected = settings.control_secret or setup_auth_token()
    if not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def _secret(value: Optional[SecretStr]) -> Optional[str]:
    if value is None:
        return None
    text = value.get_secret_value()
    return text or None


def _masked_error(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text[:300]


def _diagnostic_detail(exc: Exception) -> str:
    """Return a safe diagnostic label without raw upstream exception text."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    suffix = f" (HTTP {status})" if isinstance(status, int) else ""
    return f"{type(exc).__name__}{suffix}: connection test failed"


def _read_doc(path: str) -> Tuple[Dict[str, Any], Optional[bytes]]:
    raw = Path(path).read_bytes()
    doc = yaml.safe_load(raw)
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError("config file must contain a mapping")
    return doc, raw


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_writable(path: str) -> bool:
    parent = Path(path).parent
    if parent.exists():
        return os.access(parent, os.W_OK)
    ancestor = parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    return ancestor.exists() and os.access(ancestor, os.W_OK)


def _effective_path() -> Optional[str]:
    if settings.config_source == "pw_config":
        return settings.config_path
    if settings.config_source == "default_file":
        return settings.config_path
    if settings.config_source == "none":
        return default_config_path()
    return None


def _is_auth_error(message: Optional[str]) -> bool:
    if not message:
        return False
    text = message.lower()
    words = (
        "unauthorized",
        "forbidden",
        "access denied",
        "login failed",
        "invalid password",
        "check your password",
        "authentication",
        "401",
        "403",
    )
    return any(word in text for word in words)


def _derive_mode(config: GatewayConfig, data_mode: Optional[str] = None) -> str:
    if data_mode:
        return data_mode
    if config.fleetapi:
        return "Hybrid FleetAPI" if config.host else "FleetAPI"
    if config.cloud_mode:
        return "Cloud"
    if config.rsa_key_path:
        return "TEDAPI v1r"
    if config.gw_pwd and config.password:
        return "Hybrid TEDAPI"
    if config.gw_pwd:
        return "Hybrid" if config.email and config.authpath else "TEDAPI"
    if config.password:
        return "Hybrid" if config.email and config.authpath else "Local/Basic LAN"
    if config.email:
        return "Hybrid"
    return "Unknown"


def _setup_mode(config: GatewayConfig) -> str:
    if config.fleetapi and config.host and not config.cloud_mode:
        return "hybrid_fleetapi"
    if config.fleetapi:
        return "fleetapi"
    if config.cloud_mode:
        return "cloud"
    if config.rsa_key_path:
        return "tedapi_v1r"
    if config.gw_pwd and config.password:
        return "hybrid_tedapi"
    if config.gw_pwd:
        return "hybrid" if config.email and config.authpath else "tedapi"
    if config.password:
        return "hybrid" if config.email and config.authpath else "local"
    if config.email:
        return "hybrid"
    return "tedapi"


def _editable_gateway(config: GatewayConfig) -> Dict[str, Any]:
    data = config.model_dump(exclude={"gw_pwd", "password", "cloud_mode", "fleetapi"})
    data["mode"] = _setup_mode(config)
    return data


def get_setup_status() -> SetupStatus:
    """Return the current state without performing network requests."""
    from app.core.gateway_manager import gateway_manager

    source = settings.config_source or "none"
    gateway_summaries: List[SetupGatewaySummary] = []
    online = 0
    auth_failed = False
    for config in settings.gateways:
        status = gateway_manager.cache.get(config.id)
        data_mode = None
        if status and status.data:
            data_mode = status.data.connection_mode
        online_here = bool(status and status.online)
        if online_here:
            online += 1
        error = _masked_error(status.error if status else None)
        if error and _is_auth_error(error):
            auth_failed = True
        gateway_summaries.append(
            SetupGatewaySummary(
                id=config.id,
                name=config.name or config.id,
                host=config.host,
                port=config.port,
                type=config.type,
                mode=_derive_mode(config, data_mode),
                online=online_here,
                last_error=error,
                siteid_configured=bool(settings.siteid),
                rsa_key_configured=bool(config.rsa_key_path),
                authpath_configured=bool(config.authpath),
            )
        )

    path = _effective_path()
    current_revision = None
    current_config_error = settings.config_error
    config_changed = False
    if path and source in {"none", "default_file", "pw_config"}:
        try:
            current_revision = _revision(Path(path).read_bytes())
            config_changed = current_revision != settings.config_revision
            if config_changed:
                # The startup error describes the old file. Parse the current
                # file so a repaired configuration does not stay "invalid".
                current_config_error = None
                if Path(path).exists():
                    try:
                        _read_doc(path)
                    except Exception as exc:
                        current_config_error = (
                            f"Unable to read config file ({type(exc).__name__})"
                        )
        except OSError:
            if not Path(path).exists() and settings.config_revision is not None:
                config_changed = True
                current_config_error = None

    state = "unconfigured"
    if source == "none":
        state = "unconfigured"
    elif current_config_error and not settings.gateways:
        state = "invalid"
    elif source in {"pw_gateways", "legacy_env"}:
        state = "env_managed"
    elif settings.config_partial and not config_changed:
        state = "partial"
    elif not settings.gateways:
        state = "unconfigured" if source in {"default_file", "pw_config"} else "invalid"
    elif settings.gateways and len(gateway_summaries) == online:
        state = "configured"
    elif auth_failed:
        state = "auth_failed"
    else:
        state = "unreachable"
    if config_changed:
        state = "restart_required"

    writable = False
    if path and source in {"none", "default_file", "pw_config"}:
        writable = _is_writable(path)
    can_write = bool(
        path and writable and source in {"none", "default_file", "pw_config"}
    )
    setup_recommended = state in {
        "unconfigured",
        "invalid",
        "unreachable",
        "auth_failed",
    }

    masked_settings: Dict[str, Any] = {}
    for name in _WRITABLE_OPTION_FIELDS:
        value = getattr(settings, name, None)
        if name in {"control_secret", "mqtt_password"}:
            value = "***" if value else None
        if isinstance(value, (str, int, float, bool, list)) or value is None:
            masked_settings[name] = value

    settings_keys: List[str] = []
    if path and source in {"default_file", "pw_config"}:
        try:
            doc, _ = _read_doc(path)
            block = doc.get("settings")
            if isinstance(block, dict):
                settings_keys = sorted(str(key) for key in block)
        except Exception:
            # A malformed file must not make the repair page fail to load.
            pass

    warnings = [] if config_changed else list(settings.config_warnings)
    if source == "none" and not os.path.isdir("/data"):
        warnings.append(
            "No persistent /data volume was detected. Mount /data to keep the "
            "configuration across container replacements."
        )
    if config_changed:
        warnings.append(
            "The configuration file changed since startup; restart the "
            "server/container to activate it."
        )
    if settings.control_secret:
        warnings.append("The setup API uses PW_CONTROL_SECRET as the admin token.")
    else:
        warnings.append(
            "No PW_CONTROL_SECRET is set. Use the bootstrap token printed once "
            "in the server log. The bootstrap token is per process; set "
            "PW_CONTROL_SECRET for multiple workers or server instances."
        )

    return SetupStatus(
        state=state,
        config_source=source,
        config_path=path,
        config_writable=writable,
        config_revision=settings.config_revision,
        config_file_revision=current_revision,
        config_error=current_config_error,
        config_partial=settings.config_partial and not config_changed,
        warnings=warnings,
        gateway_count=len(settings.gateways),
        gateways=gateway_summaries,
        gateway_configs=[_editable_gateway(config) for config in settings.gateways],
        setup_recommended=setup_recommended,
        can_write=can_write,
        can_test=bool(settings.gateways),
        restart_required=config_changed,
        control_enabled=bool(settings.control_secret),
        settings_source=source,
        settings_keys=settings_keys,
        settings=masked_settings,
    )


def _validate_host(host: Optional[str], issues: List[SetupValidationIssue]) -> None:
    if not host:
        issues.append(SetupValidationIssue(field="host", message="Host is required."))
        return
    if any(ch.isspace() for ch in host):
        issues.append(
            SetupValidationIssue(field="host", message="Host contains spaces.")
        )
    if "://" in host or "/" in host or "\\" in host:
        issues.append(
            SetupValidationIssue(
                field="host", message="Host must be an IP address or hostname."
            )
        )
        return


def _validate_path(
    field: str,
    path: Optional[str],
    issues: List[SetupValidationIssue],
    must_exist: bool = False,
    directory: bool = False,
) -> None:
    if not path:
        return
    candidate = Path(os.path.expanduser(path))
    if must_exist and not candidate.exists():
        issues.append(
            SetupValidationIssue(field=field, message=f"{field} does not exist")
        )
    elif candidate.exists() and directory and not candidate.is_dir():
        issues.append(
            SetupValidationIssue(field=field, message=f"{field} is not a directory")
        )
    elif candidate.exists() and not directory and not candidate.is_file():
        issues.append(
            SetupValidationIssue(field=field, message=f"{field} is not a file")
        )


def _validate_gateway(
    item: SetupGatewayInput,
    existing: Dict[str, Any],
    issues: List[SetupValidationIssue],
) -> Optional[GatewayConfig]:
    before = len(issues)
    if item.mode not in {"cloud", "fleetapi"}:
        _validate_host(item.host, issues)
    try:
        ZoneInfo(item.timezone)
    except Exception:
        issues.append(
            SetupValidationIssue(field="timezone", message="unknown timezone")
        )
    provided = item.model_fields_set
    gw_pwd = (
        None
        if "gw_pwd" in provided and item.gw_pwd is None
        else (_secret(item.gw_pwd) or existing.get("gw_pwd") or settings.pw_gw_pwd)
    )
    password = (
        None
        if "password" in provided and item.password is None
        else (
            _secret(item.password) or existing.get("password") or settings.pw_password
        )
    )
    rsa_key = (
        None
        if "rsa_key_path" in provided and item.rsa_key_path is None
        else (
            item.rsa_key_path
            or existing.get("rsa_key_path")
            or settings.pw_rsa_key_path
        )
    )
    authpath = (
        None
        if "authpath" in provided and item.authpath is None
        else (item.authpath or existing.get("authpath") or settings.pw_authpath)
    )
    wifi_host = (
        None
        if "wifi_host" in provided and item.wifi_host is None
        else (item.wifi_host or existing.get("wifi_host") or settings.pw_wifi_host)
    )
    email = (
        None
        if "email" in provided and item.email is None
        else (
            (item.email or existing.get("email") or settings.pw_email or "").strip()
            or None
        )
    )

    if item.mode in {"tedapi", "hybrid_tedapi"} and not gw_pwd:
        issues.append(
            SetupValidationIssue(
                field="gw_pwd", message="Gateway password is required."
            )
        )
    if item.mode in {"local", "basic_lan", "hybrid_tedapi"} and not password:
        issues.append(
            SetupValidationIssue(
                field="password", message="Customer password is required."
            )
        )
    if item.mode in {"hybrid", "hybrid_fleetapi"} and not (
        password or gw_pwd or rsa_key
    ):
        issues.append(
            SetupValidationIssue(
                field="password",
                message=(
                    "A local password, gateway password or RSA key is required "
                    "for hybrid mode."
                ),
            )
        )
    if item.mode == "tedapi_v1r" and not rsa_key:
        issues.append(
            SetupValidationIssue(
                field="rsa_key_path", message="RSA key is required for TEDAPI v1r."
            )
        )
    if item.mode in {"tedapi_v1r"} and not (gw_pwd or password):
        issues.append(
            SetupValidationIssue(
                field="gw_pwd",
                message="Gateway or customer password is required for TEDAPI v1r.",
            )
        )
    if (
        email
        and not authpath
        and item.mode
        in {
            "cloud",
            "fleetapi",
            "hybrid",
            "hybrid_fleetapi",
        }
    ):
        issues.append(
            SetupValidationIssue(
                field="authpath",
                message="Auth path is required for Cloud, FleetAPI, and hybrid control.",
            )
        )
    elif (
        email
        and not authpath
        and (
            settings.control_enabled
            or settings.cloud_hybrid
            or settings.cloud_read_fallback
        )
    ):
        issues.append(
            SetupValidationIssue(
                field="authpath",
                message=(
                    "Hybrid control is enabled, but no auth path is configured. "
                    "Set Auth path or PW_AUTH_PATH so the cloud connection can "
                    "find the auth files."
                ),
                severity="warning",
            )
        )
    if item.mode in {"cloud", "hybrid", "hybrid_fleetapi"} and not email:
        issues.append(
            SetupValidationIssue(
                field="email", message="Tesla account email is required."
            )
        )
    if item.mode in {"cloud", "fleetapi"} and not authpath:
        issues.append(
            SetupValidationIssue(
                field="authpath",
                message="Auth path is required for Cloud and FleetAPI modes.",
            )
        )
    _validate_path("rsa_key_path", rsa_key, issues, must_exist=True)
    _validate_path(
        "authpath",
        authpath,
        issues,
        must_exist=bool(email) or item.mode in {"cloud", "fleetapi", "hybrid"},
        directory=True,
    )

    if any(issue.severity == "error" for issue in issues[before:]):
        return None

    kwargs = {
        "id": item.id,
        "name": item.name or item.id,
        "host": item.host,
        "port": item.port,
        "timezone": item.timezone,
        "type": item.type,
    }
    if email:
        kwargs["email"] = email
    if authpath:
        kwargs["authpath"] = authpath
    if item.mode == "cloud":
        kwargs.update(
            host=None,
            port=None,
            email=email,
            authpath=authpath,
            cloud_mode=True,
        )
    elif item.mode == "fleetapi":
        kwargs.update(
            host=None,
            port=None,
            email=email,
            authpath=authpath,
            cloud_mode=True,
            fleetapi=True,
        )
    elif item.mode in {"hybrid", "hybrid_fleetapi"}:
        kwargs.update(
            email=email,
            authpath=authpath,
            cloud_mode=False,
            fleetapi=item.mode == "hybrid_fleetapi",
        )
        if password:
            kwargs["password"] = password
        if gw_pwd:
            kwargs["gw_pwd"] = gw_pwd
        if rsa_key:
            kwargs["rsa_key_path"] = rsa_key
        if wifi_host:
            kwargs["wifi_host"] = wifi_host
    elif item.mode in {"local", "basic_lan"}:
        kwargs.update(password=password)
    elif item.mode == "hybrid_tedapi":
        kwargs.update(gw_pwd=gw_pwd, password=password)
    elif item.mode == "tedapi":
        kwargs.update(gw_pwd=gw_pwd)
    else:
        kwargs.update(gw_pwd=gw_pwd, password=password, rsa_key_path=rsa_key)
        if wifi_host:
            kwargs["wifi_host"] = wifi_host

    try:
        return GatewayConfig(**kwargs)
    except Exception as exc:
        issues.append(
            SetupValidationIssue(
                field="gateway",
                message=f"invalid gateway configuration: {type(exc).__name__}",
            )
        )
        return None


def load_existing_config() -> Dict[str, Any]:
    """Read the effective config file for secret-preserving validation."""
    path = _effective_path()
    if not path or not Path(path).exists():
        return {}
    try:
        doc, _ = _read_doc(path)
    except Exception:
        return {}
    return doc


def get_setup_schema() -> Dict[str, Any]:
    """Describe writable settings and connection modes for the UI."""
    fields = []
    for name in _WRITABLE_OPTION_FIELDS:
        field = Settings.model_fields[name]
        fields.append(
            {
                "name": name,
                "type": str(field.annotation),
                "default": field.default,
                "secret": name in {"control_secret", "mqtt_password"},
            }
        )
    return {
        "modes": [
            "tedapi",
            "tedapi_v1r",
            "local",
            "basic_lan",
            "hybrid_tedapi",
            "cloud",
            "fleetapi",
            "hybrid",
            "hybrid_fleetapi",
        ],
        "timezones": sorted(available_timezones()),
        "gateway_defaults": {
            "mode": "tedapi",
            "type": "powerwall",
            "port": 443,
            "timezone": settings.pw_timezone,
        },
        "settings": fields,
    }


def validate_setup_request(
    request: SetupConfigureRequest,
    existing_doc: Optional[Dict[str, Any]] = None,
) -> Tuple[SetupValidationResult, List[GatewayConfig], Dict[str, Any]]:
    """Validate gateways and options without writing anything."""
    issues: List[SetupValidationIssue] = []
    existing_doc = existing_doc or {}
    existing_entries = existing_doc.get("gateways", [])
    if not isinstance(existing_entries, list):
        issues.append(
            SetupValidationIssue(
                field="gateways",
                message=(
                    "The existing 'gateways' value is not a list; it will be "
                    "replaced when the configuration is saved."
                ),
                severity="warning",
            )
        )
        existing_entries = []
    existing_by_id = {
        str(gw.get("id")): gw
        for gw in existing_entries
        if isinstance(gw, dict) and gw.get("id")
    }
    if not request.gateways:
        issues.append(
            SetupValidationIssue(
                field="gateways",
                message=(
                    "No gateways configured; monitoring will be off until one "
                    "is added."
                ),
                severity="warning",
            )
        )
    seen = set()
    configs: List[GatewayConfig] = []
    for item in request.gateways:
        if item.id in seen:
            issues.append(
                SetupValidationIssue(
                    field="id", message=f"Duplicate gateway ID {item.id!r}."
                )
            )
            continue
        seen.add(item.id)
        existing = {}
        if item.original_id:
            existing = existing_by_id.get(item.original_id, {})
        if not existing:
            existing = existing_by_id.get(item.id, {})
        config = _validate_gateway(item, existing, issues)
        if config is not None:
            configs.append(config)

    validated_options: Dict[str, Any] = {}
    for key, value in request.options.items():
        name = str(key)
        if name not in _WRITABLE_OPTION_FIELDS:
            issues.append(
                SetupValidationIssue(
                    field=f"options.{name}",
                    message="Unknown or non-writable setting.",
                )
            )
            continue
        try:
            validated = settings._coerce_setting(name, value)
        except Exception:
            issues.append(
                SetupValidationIssue(
                    field=f"options.{name}", message="Invalid setting value."
                )
            )
            continue
        alias = Settings.model_fields[name].alias or name
        if os.getenv(alias) is not None:
            issues.append(
                SetupValidationIssue(
                    field=f"options.{name}",
                    message=(
                        "The environment or command line sets this value and "
                        "takes precedence over the config file."
                    ),
                    severity="warning",
                )
            )
        validated_options[name] = validated

    numeric_bounds = {
        "server_port": (1, 65535),
        "https_port": (1, 65535),
        "mqtt_port": (1, 65535),
        "cache_expire": (1, None),
        "timeout": (1, None),
        "pool_maxsize": (0, None),
        "browser_cache": (0, None),
        "cache_ttl": (0, None),
        "rate_limit_max_requests": (1, None),
        "rate_limit_window_seconds": (1, None),
        "rate_limit_max_buckets": (1, None),
        "mqtt_qos": (0, 2),
        "mqtt_controls": (0, 63),
        "islanding_cooldown": (0, None),
        "tedapi_probe_interval": (5, None),
    }
    for name, (minimum, maximum) in numeric_bounds.items():
        if name not in validated_options:
            continue
        value = validated_options[name]
        if (
            not isinstance(value, int)
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            issues.append(
                SetupValidationIssue(
                    field=f"options.{name}",
                    message=(
                        f"must be between {minimum} and {maximum}"
                        if maximum is not None
                        else f"must be at least {minimum}"
                    ),
                )
            )
            validated_options.pop(name, None)

    # Cross-field checks must see the complete effective snapshot, not only
    # the fields changed in this browser session.
    effective_options = {
        name: getattr(settings, name, None) for name in _WRITABLE_OPTION_FIELDS
    }
    effective_options.update(validated_options)

    if effective_options.get("https_mode"):
        cert = effective_options.get("https_cert")
        key = effective_options.get("https_key")
        if bool(cert) != bool(key):
            issues.append(
                SetupValidationIssue(
                    field="options.https",
                    message="https_cert and https_key must both be set or both be empty",
                )
            )
    if effective_options.get("mqtt_controls"):
        if not effective_options.get("control_secret"):
            issues.append(
                SetupValidationIssue(
                    field="options.mqtt_controls",
                    message="MQTT controls require a control token.",
                    severity="warning",
                )
            )
    if effective_options.get("battery_dispatch") and not effective_options.get(
        "control_secret"
    ):
        issues.append(
            SetupValidationIssue(
                field="options.battery_dispatch",
                message="Battery dispatch requires a control token.",
                severity="warning",
            )
        )
    modes = {_setup_mode(config) for config in configs}
    if effective_options.get("cloud_read_fallback") and not (
        modes & {"cloud", "fleetapi", "hybrid"}
    ):
        issues.append(
            SetupValidationIssue(
                field="options.cloud",
                message="Cloud read fallback and cloud energy require a Cloud, FleetAPI, or hybrid gateway.",
                severity="warning",
            )
        )
    if effective_options.get("wifi_v1r_fallback") and not any(
        config.rsa_key_path and config.wifi_host for config in configs
    ):
        issues.append(
            SetupValidationIssue(
                field="options.wifi_v1r_fallback",
                message="WiFi v1r fallback requires a v1r gateway with wifi_host.",
                severity="warning",
            )
        )
    if effective_options.get("local_first") and not (
        modes & {"tedapi_v1r", "hybrid", "hybrid_tedapi"}
    ):
        issues.append(
            SetupValidationIssue(
                field="options.local_first",
                message="Local-first requires a v1r or hybrid gateway.",
                severity="warning",
            )
        )
    for path_field in ("https_cert", "https_key", "mqtt_tls_ca_cert"):
        path_value = effective_options.get(path_field)
        if path_value and not Path(os.path.expanduser(str(path_value))).exists():
            issues.append(
                SetupValidationIssue(
                    field=f"options.{path_field}",
                    message=f"{path_field} does not exist",
                    severity="warning",
                )
            )

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    return (
        SetupValidationResult(valid=not errors, errors=errors, warnings=warnings),
        configs,
        validated_options,
    )


def _gateway_dict(config: GatewayConfig) -> Dict[str, Any]:
    return config.model_dump(exclude_none=True)


def _gateway_dict_with_unknown(
    config: GatewayConfig, existing: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Serialize a gateway and keep fields this version does not understand."""
    data = _gateway_dict(config)
    if not isinstance(existing, dict):
        return data
    known = set(GatewayConfig.model_fields)
    for key, value in existing.items():
        if isinstance(key, str) and key not in known and key != "original_id":
            data[key] = value
    return data


def _write_atomic(path: str, doc: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    directory = Path(path).parent
    directory.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{Path(path).name}.", suffix=".tmp", dir=str(directory)
    )
    backup = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        if Path(path).exists():
            backup = f"{path}.bak"
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return _revision(Path(path).read_bytes()), backup


def apply_setup_request(
    request: SetupConfigureRequest,
) -> SetupConfigureResult:
    """Validate and atomically persist a full setup configuration."""
    if settings.config_source in {"pw_gateways", "legacy_env"}:
        raise ValueError(
            "Configuration is managed by PW_GATEWAYS or legacy environment "
            "variables. Remove that source before using the wizard."
        )
    path = _effective_path()
    if not path:
        raise ValueError("no writable configuration path is available")

    malformed_existing = False
    with _CONFIG_LOCK:
        existing_doc: Dict[str, Any] = {}
        raw = None
        if Path(path).exists():
            raw = Path(path).read_bytes()
            try:
                existing_doc, _ = _read_doc(path)
            except Exception:
                if not settings.config_error:
                    raise ValueError(
                        "existing configuration cannot be merged; repair the file "
                        "manually or reload after fixing it"
                    )
                malformed_existing = True
                existing_doc = {}
            actual_revision = _revision(raw)
            if request.expected_revision != actual_revision:
                raise ValueError("configuration changed; reload the setup page")
        elif request.expected_revision:
            raise ValueError("configuration changed; reload the setup page")

        result, configs, options = validate_setup_request(request, existing_doc)
        if not result.valid:
            raise ValueError("; ".join(issue.message for issue in result.errors))

        existing_entries = existing_doc.get("gateways", [])
        if not isinstance(existing_entries, list):
            existing_entries = []
        existing_by_id = {
            str(gw.get("id")): gw
            for gw in existing_entries
            if isinstance(gw, dict) and gw.get("id")
        }
        existing_for_id: Dict[str, Dict[str, Any]] = {}
        for item in request.gateways:
            previous = existing_by_id.get(str(item.original_id or ""))
            if not previous:
                previous = existing_by_id.get(item.id)
            if previous:
                existing_for_id[item.id] = previous

        doc = dict(existing_doc)
        doc["gateways"] = [
            _gateway_dict_with_unknown(config, existing_for_id.get(config.id))
            for config in configs
        ]
        server_options = {
            target: options[name]
            for name, target in _SERVER_OPTION_MAP.items()
            if name in options
        }
        if server_options:
            server_doc = doc.get("server")
            if not isinstance(server_doc, dict):
                server_doc = {}
            server_doc.update(server_options)
            doc["server"] = server_doc
        settings_options = {
            key: value
            for key, value in options.items()
            if key not in _SERVER_OPTION_MAP
        }
        existing_options = doc.get("settings")
        if not isinstance(existing_options, dict):
            existing_options = {}
        merged_options = dict(existing_options)
        merged_options.update(settings_options)
        if merged_options:
            doc["settings"] = merged_options
        else:
            doc.pop("settings", None)

        revision, backup = _write_atomic(path, doc)
        warnings = [issue.message for issue in result.warnings]
        if malformed_existing:
            warnings.append(
                "The existing configuration file could not be parsed and was "
                f"replaced; a backup was written to {backup}."
            )
        return SetupConfigureResult(
            saved=True,
            restart_required=True,
            config_path=path,
            backup_path=backup,
            revision=revision,
            warnings=warnings,
        )


def delete_setup_config(
    request: SetupDeleteConfigRequest,
) -> SetupDeleteConfigResult:
    """Delete the wizard-managed default config file, keeping a backup."""
    if settings.config_source in {"pw_gateways", "legacy_env"}:
        raise ValueError(
            "Configuration is managed by environment variables; remove that "
            "source instead of deleting the file."
        )
    if settings.config_source == "pw_config":
        raise ValueError(
            "PW_CONFIG selects an explicit file; remove PW_CONFIG or edit the "
            "file manually instead."
        )
    path = _effective_path()
    if not path or not Path(path).exists():
        raise ValueError("no configuration file to delete")
    with _CONFIG_LOCK:
        raw = Path(path).read_bytes()
        if request.expected_revision != _revision(raw):
            raise ValueError("configuration changed; reload the setup page")
        backup = f"{path}.bak"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        os.remove(path)
    return SetupDeleteConfigResult(
        deleted=True,
        config_path=path,
        backup_path=backup,
        message=("Configuration file deleted; restart the server/container to apply."),
    )


def test_gateway_connection(
    item: SetupGatewayInput,
    existing: Optional[Dict[str, Any]] = None,
) -> SetupDiagnosticResult:
    """Run bounded read-only checks for one wizard gateway entry."""
    issues: List[SetupValidationIssue] = []
    existing = existing or {}
    if not existing:
        # Environment/PW_GATEWAYS configurations have no YAML document. Use
        # the matching runtime gateway so masked secrets can still be tested.
        for runtime_config in settings.gateways:
            if runtime_config.id == item.id:
                existing = runtime_config.model_dump(exclude_none=True)
                break
    config = _validate_gateway(item, existing, issues)
    if config is None or any(issue.severity == "error" for issue in issues):
        message = "; ".join(issue.message for issue in issues)
        raise ValueError(message or "invalid gateway configuration")

    # Use the merged configuration for the live check. The browser omits
    # unchanged secrets and paths, and _validate_gateway resolves them from
    # the existing YAML or the global Settings fallbacks.
    item = SetupGatewayInput(
        id=config.id,
        name=config.name,
        mode=item.mode,
        host=config.host,
        port=config.port,
        gw_pwd=config.gw_pwd,
        password=config.password,
        rsa_key_path=config.rsa_key_path,
        wifi_host=config.wifi_host,
        email=config.email,
        authpath=config.authpath,
        timezone=config.timezone,
        type=config.type,
    )

    steps: List[SetupDiagnosticStep] = []
    warnings: List[str] = []

    steps.append(
        SetupDiagnosticStep(
            key="config",
            label="Configuration",
            status="success",
            detail="Required fields are present",
        )
    )

    if item.mode in {"cloud", "fleetapi"}:
        authpath_value = item.authpath or (existing or {}).get("authpath") or ""
        authpath = Path(os.path.expanduser(authpath_value))
        fleet = item.mode == "fleetapi"
        filename = ".pypowerwall.fleetapi" if fleet else ".pypowerwall.auth"
        auth_file = authpath / filename
        ok = auth_file.is_file()
        steps.append(
            SetupDiagnosticStep(
                key="auth_file",
                label="Tesla auth file",
                status="success" if ok else "failed",
                detail=str(auth_file) if ok else f"{auth_file} not found",
            )
        )
        if not ok:
            return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)
        if fleet:
            try:
                fleet_config = _fleet_read_config(authpath)
                siteid = (
                    settings.siteid or str(fleet_config.get("site_id") or "")
                ).strip()
                if not siteid:
                    sites = _fleet_sites_with_refresh(authpath, fleet_config)
                    if len(sites) != 1:
                        steps.append(
                            SetupDiagnosticStep(
                                key="api",
                                label="FleetAPI live access",
                                status="warning",
                                detail="Select an energy site before testing",
                            )
                        )
                        return SetupDiagnosticResult(
                            ok=False,
                            steps=steps,
                            detected_mode="FleetAPI",
                            warnings=warnings,
                        )
                    siteid = sites[0].siteid
                live = _fleet_live_check(authpath, siteid)
                steps.append(
                    SetupDiagnosticStep(
                        key="api",
                        label="FleetAPI live access",
                        status="success",
                        detail=(
                            f"Site {siteid} returned site info and live status"
                            + (
                                f" ({live.get('site_name')})"
                                if live.get("site_name")
                                else ""
                            )
                        ),
                    )
                )
                return SetupDiagnosticResult(
                    ok=True,
                    steps=steps,
                    detected_mode="FleetAPI",
                    site_name=live.get("site_name"),
                    warnings=warnings,
                )
            except Exception as exc:
                steps.append(
                    SetupDiagnosticStep(
                        key="api",
                        label="FleetAPI live access",
                        status="failed",
                        detail=_diagnostic_detail(exc),
                    )
                )
                return SetupDiagnosticResult(
                    ok=False,
                    steps=steps,
                    detected_mode="FleetAPI",
                    warnings=warnings,
                )
        try:
            from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

            accounts = json.loads(auth_file.read_text(encoding="utf-8"))
            if not isinstance(accounts, dict) or not accounts:
                raise ValueError("the Cloud auth file has no account")
            cloud_email = (item.email or str(next(iter(accounts)))).strip()
            if cloud_email not in accounts:
                raise ValueError("the requested account is not in the auth file")
            site_file = authpath / ".pypowerwall.site"
            siteid = (settings.siteid or "").strip()
            if not siteid and site_file.is_file():
                siteid = site_file.read_text(encoding="utf-8").strip()
            cloud = PyPowerwallCloud(
                cloud_email,
                authpath=str(authpath),
                siteid=int(siteid) if siteid.isdigit() else None,
            )
            if not cloud.connect():
                raise ValueError("unable to connect to Tesla Cloud")
            sites = cloud.getsites() or []
            if not sites:
                raise ValueError("no Tesla Cloud sites were returned")
            if siteid and not any(
                str(site.get("energy_site_id")) == siteid
                for site in sites
                if isinstance(site, dict)
            ):
                raise ValueError("the selected site is not available on this account")
            steps.append(
                SetupDiagnosticStep(
                    key="api",
                    label="Cloud API access",
                    status="success",
                    detail=f"Tesla Cloud returned {len(sites)} site(s)",
                )
            )
            return SetupDiagnosticResult(
                ok=True,
                steps=steps,
                detected_mode="Cloud",
                warnings=warnings,
            )
        except Exception as exc:
            steps.append(
                SetupDiagnosticStep(
                    key="api",
                    label="Cloud API access",
                    status="failed",
                    detail=_diagnostic_detail(exc),
                )
            )
            return SetupDiagnosticResult(
                ok=False,
                steps=steps,
                detected_mode="Cloud",
                warnings=warnings,
            )

    host = item.host or ""
    port = item.port or 443
    address = f"{host}:{port}" if ":" in host and not host.startswith("[") else host
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        resolved_addresses: List[str] = []
        for info in infos:
            resolved = info[4][0]
            if (
                info[0] == socket.AF_INET6
                and len(info[4]) > 3
                and info[4][3]
                and "%" not in resolved
            ):
                # Keep the scope id for link-local IPv6 addresses.
                resolved = f"{resolved}%{info[4][3]}"
            if resolved not in resolved_addresses:
                resolved_addresses.append(resolved)
        if not resolved_addresses:
            raise socket.gaierror("no addresses returned")
        addresses = set(resolved_addresses)
        if any(
            not (
                ipaddress.ip_address(address).is_private
                or ipaddress.ip_address(address).is_loopback
                or ipaddress.ip_address(address).is_link_local
            )
            for address in addresses
        ):
            steps.append(
                SetupDiagnosticStep(
                    key="dns",
                    label="Address resolution",
                    status="failed",
                    detail="Gateway address is not private/loopback/link-local",
                )
            )
            return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)
        steps.append(
            SetupDiagnosticStep(
                key="dns",
                label="Address resolution",
                status="success",
                detail=", ".join(sorted(addresses))[:200],
            )
        )
    except socket.gaierror as exc:
        steps.append(
            SetupDiagnosticStep(
                key="dns",
                label="Address resolution",
                status="failed",
                detail=str(exc)[:200],
            )
        )
        return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)

    try:
        last_error: Optional[OSError] = None
        for resolved in resolved_addresses:
            try:
                # Use the validated IP, not the hostname, so a changed DNS
                # answer cannot redirect this TCP check after validation.
                with socket.create_connection((resolved, port), timeout=4.0):
                    pass
                break
            except OSError as exc:
                last_error = exc
        else:
            raise last_error or OSError("no resolved address")
        steps.append(
            SetupDiagnosticStep(
                key="tcp",
                label="TCP connection",
                status="success",
                detail=address,
            )
        )
    except OSError as exc:
        steps.append(
            SetupDiagnosticStep(
                key="tcp",
                label="TCP connection",
                status="failed",
                detail=str(exc)[:200],
            )
        )
        return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)

    if item.mode == "tedapi_v1r":
        try:
            from pypowerwall.tedapi.tedapi_v1r import TEDAPIv1r

            secret = _secret(item.password)
            if not secret and item.gw_pwd:
                secret = _secret(item.gw_pwd)
                secret = secret[-5:] if secret else None
            if not secret:
                raise ValueError("customer password is required for v1r")
            v1r = TEDAPIv1r(
                item.host,
                secret,
                item.rsa_key_path,
                timeout=5,
                poolmaxsize=5,
            )
            if not v1r.login():
                steps.append(
                    SetupDiagnosticStep(
                        key="auth",
                        label="Authentication",
                        status="failed",
                        detail="v1r customer login failed",
                    )
                )
                return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)
            din = v1r.get_din()
            if not din:
                raise ValueError("v1r DIN request failed")
            config = v1r.get_config_v1r(din)
            if not isinstance(config, dict) or not config:
                raise ValueError(
                    "signed v1r config request returned no data; the key may "
                    "be unregistered or v1r may be unsupported for this gateway"
                )
            steps.append(
                SetupDiagnosticStep(
                    key="auth",
                    label="Authentication",
                    status="success",
                    detail=f"DIN {din} accepted; v1r key fingerprint verified",
                )
            )
            steps.append(
                SetupDiagnosticStep(
                    key="data",
                    label="Gateway data",
                    status="success",
                    detail="Signed v1r config received",
                )
            )
            return SetupDiagnosticResult(
                ok=True,
                steps=steps,
                detected_mode="TEDAPI v1r",
                detected_type=item.type,
                warnings=warnings,
            )
        except Exception as exc:
            steps.append(
                SetupDiagnosticStep(
                    key="auth",
                    label="Authentication and data",
                    status="failed",
                    detail=_diagnostic_detail(exc),
                )
            )
            return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)
    cachefile = None
    tempdir = None
    try:
        tempdir = tempfile.TemporaryDirectory(prefix="pws-setup-")
        cachefile = os.path.join(tempdir.name, ".powerwall")
        kwargs: Dict[str, Any] = {
            "host": f"{host}:{port}" if item.port else host,
            "timezone": item.timezone,
            "timeout": 5,
            "poolmaxsize": 5,
            "failover": False,
            "retry_modes": False,
            "cachefile": cachefile,
            "pwcacheexpire": 5,
        }
        if item.gw_pwd:
            kwargs["gw_pwd"] = _secret(item.gw_pwd)
        if item.password:
            kwargs["password"] = _secret(item.password)
        if item.rsa_key_path:
            kwargs["rsa_key_path"] = item.rsa_key_path
        if item.wifi_host:
            kwargs["wifi_host"] = item.wifi_host
        if item.email:
            kwargs["email"] = item.email
        if item.authpath:
            kwargs["authpath"] = item.authpath

        pw = pypowerwall.Powerwall(**kwargs)
        connected = bool(pw.is_connected())
        steps.append(
            SetupDiagnosticStep(
                key="auth",
                label="Authentication",
                status="success" if connected else "failed",
                detail=(
                    "Gateway accepted the credentials"
                    if connected
                    else "Gateway did not confirm the connection"
                ),
            )
        )
        if not connected:
            return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)

        aggregates = pw.poll("/api/meters/aggregates", force=True)
        if isinstance(aggregates, dict) and aggregates.get("error"):
            raise ValueError(str(aggregates["error"]))
        if not isinstance(aggregates, dict) or not aggregates:
            raise ValueError("empty meter response")
        steps.append(
            SetupDiagnosticStep(
                key="data",
                label="Gateway data",
                status="success",
                detail="Meter aggregates received",
            )
        )
        firmware = None
        site_name = None
        try:
            firmware = pw.version()
        except Exception:
            pass
        try:
            site_name = pw.site_name()
        except Exception:
            pass
        mode = getattr(pw, "tedapi_mode", None)
        if not mode:
            mode = _derive_mode(config)
        return SetupDiagnosticResult(
            ok=True,
            steps=steps,
            detected_mode=str(mode),
            detected_type=item.type,
            firmware=str(firmware) if firmware else None,
            site_name=str(site_name) if site_name else None,
            warnings=warnings,
        )
    except Exception as exc:
        steps.append(
            SetupDiagnosticStep(
                key="auth",
                label="Authentication and data",
                status="failed",
                detail=_diagnostic_detail(exc),
            )
        )
        return SetupDiagnosticResult(ok=False, steps=steps, warnings=warnings)
    finally:
        if tempdir is not None:
            tempdir.cleanup()


def auth_status(mode: str, authpath: Optional[str] = None) -> Dict[str, Any]:
    """Report external Tesla auth files without reading token contents."""
    fleet = mode == "fleetapi"
    path = Path(os.path.expanduser(authpath or settings.pw_authpath or ""))
    names = {
        "auth": ".pypowerwall.auth",
        "site": ".pypowerwall.site",
        "fleetapi": ".pypowerwall.fleetapi",
    }
    files = {key: (path / name).is_file() for key, name in names.items()}
    ready = (
        files["fleetapi"]
        if fleet
        else bool(files["auth"] and (files["site"] or settings.siteid))
    )
    command = (
        "python -m pypowerwall setup -fleetapi"
        if fleet
        else "python -m pypowerwall setup"
    )
    notes = [
        "Run the command in the server container or host with the auth path mounted.",
        "The wizard verifies the files but never refreshes Tesla tokens.",
    ]
    return {
        "mode": "fleetapi" if fleet else "cloud",
        "authpath": str(path) if authpath or settings.pw_authpath else None,
        "files": files,
        "ready": ready,
        "command": command,
        "notes": notes,
    }


def _v1r_path(authpath: Optional[str] = None) -> Path:
    raw = authpath or settings.pw_authpath
    root = (
        Path(os.path.expanduser(raw))
        if raw
        else (Path("/data") if Path("/data").is_dir() else Path("data"))
    )
    if root.exists() and not root.is_dir():
        raise ValueError("authpath is not a directory")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _v1r_key_material(root: Path) -> Tuple[bytes, str, Path, Path]:
    from pypowerwall.v1r_register import generate_rsa_key

    _, public_der = generate_rsa_key(str(root))
    fingerprint = hashlib.sha256(public_der).hexdigest()
    return (
        public_der,
        fingerprint,
        root / "tedapi_rsa_private.pem",
        root / "tedapi_rsa_public.der",
    )


def v1r_key(authpath: Optional[str] = None) -> Dict[str, Any]:
    """Generate or inspect the canonical v1r key."""
    root = _v1r_path(authpath)
    private_key_path = root / "tedapi_rsa_private.pem"
    created = not private_key_path.is_file()
    _, fingerprint, private_key_path, public_key_path = _v1r_key_material(root)
    return {
        "created": created,
        "private_key_path": str(private_key_path),
        "public_key_path": str(public_key_path),
        "fingerprint": fingerprint,
        "registration_required": True,
    }


def _v1r_refresh_auth(root: Path, email: Optional[str] = None) -> Tuple[str, str]:
    """Refresh the cached Owner API access token, matching v1r_register.py."""
    from pypowerwall.tesla_auth import _refresh_access_token

    auth_file = root / ".pypowerwall.auth"
    try:
        accounts = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unable to read the Tesla auth file") from exc
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("the Tesla auth file has no account")
    if email and email not in accounts:
        raise ValueError(
            "the requested Tesla account is not in the auth file; sign in again"
        )
    account_email = email or next(iter(accounts))
    account = accounts.get(account_email)
    if not isinstance(account, dict):
        raise ValueError("the Tesla auth file has no usable account")
    sso = account.get("sso")
    if not isinstance(sso, dict) or not sso.get("refresh_token"):
        raise ValueError("the Tesla access token has expired; sign in again")
    try:
        new_data = _refresh_access_token(sso["refresh_token"])
    except Exception as exc:
        raise ValueError(
            "the Tesla access token has expired and could not be refreshed; "
            "sign in again"
        ) from exc
    if not isinstance(new_data, dict) or not new_data.get("access_token"):
        raise ValueError(
            "the Tesla access token has expired and could not be refreshed; "
            "sign in again"
        )
    sso.update(new_data)
    sso["expires_at"] = int(time.time() + new_data.get("expires_in", 28800))
    account["sso"] = sso
    accounts[account_email] = account
    _backup_file(auth_file)
    _write_private_text(auth_file, json.dumps(accounts, indent=2))
    return str(new_data["access_token"]), str(account_email)


def _v1r_auth_token(root: Path, email: Optional[str] = None) -> Tuple[str, str]:
    """Return the code-exchange Owner API access token and its account email."""
    auth_file = root / ".pypowerwall.auth"
    if not auth_file.is_file():
        raise ValueError(
            "Tesla login required for v1r key registration. Use the Tesla "
            "login buttons in this panel, or choose an existing "
            ".pypowerwall.auth file under 'Use an existing Tesla login'."
        )
    try:
        accounts = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unable to read the Tesla auth file") from exc
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("the Tesla auth file has no account")
    if email and email not in accounts:
        raise ValueError(
            "the requested Tesla account is not in the auth file; sign in again"
        )
    account_email = email or next(iter(accounts))
    sso = (accounts.get(account_email) or {}).get("sso") or {}
    token = sso.get("access_token")
    expires_at = int(sso.get("expires_at") or 0)
    if not token or (expires_at and expires_at <= int(time.time()) + 30):
        return _v1r_refresh_auth(root, account_email)
    return str(token), str(account_email)


def v1r_auth_file(request: SetupV1rAuthFileRequest) -> SetupV1rAuthFileResult:
    """Import a user-selected .pypowerwall.auth file into the selected auth path."""
    root = _v1r_path(request.authpath)
    content = (request.content or "").lstrip("\ufeff").strip()
    if not content:
        raise ValueError("the selected auth file is empty")
    if len(content.encode("utf-8")) > 256 * 1024:
        raise ValueError("the selected auth file is too large")
    try:
        accounts = json.loads(content)
    except ValueError as exc:
        raise ValueError(
            "the selected file is not valid .pypowerwall.auth JSON"
        ) from exc
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("the selected auth file has no Tesla account")
    account_email = None
    for email, account in accounts.items():
        if not isinstance(account, dict):
            continue
        sso = account.get("sso")
        if isinstance(sso, dict) and (
            sso.get("access_token") or sso.get("refresh_token")
        ):
            account_email = str(email)
            break
    if not account_email:
        raise ValueError(
            "the selected file has no usable Tesla access or refresh token"
        )
    target = root / ".pypowerwall.auth"
    _backup_file(target)
    merged = _merge_auth_file(target, accounts)
    return SetupV1rAuthFileResult(
        saved=True,
        authpath=str(root),
        auth_file=str(target),
        email=account_email,
        accounts=len(merged),
        message=(
            f"Saved Tesla login for {account_email}. Continue with step 2 to "
            "register the v1r key."
        ),
    )


def _v1r_sites(token: str) -> List[SetupV1rSite]:
    from pypowerwall.v1r_register import OWNER_API_BASE, api_call

    code, response = api_call(f"{OWNER_API_BASE}/api/1/products", token=token)
    if code in (401, 403):
        raise ValueError(
            "Tesla rejected the access token; sign in again and retry the "
            "v1r key registration"
        )
    if code != 200 or not isinstance(response, dict):
        raise ValueError(f"Tesla site lookup failed ({code})")
    sites = []
    for product in response.get("response", []) or []:
        if not isinstance(product, dict) or "energy_site_id" not in product:
            continue
        sites.append(
            SetupV1rSite(
                siteid=str(product["energy_site_id"]),
                name=product.get("site_name") or None,
                gateway_din=product.get("gateway_id") or None,
            )
        )
    if not sites:
        raise ValueError("no Tesla energy sites were found on this account")
    return sites


def _v1r_proof_hint() -> str:
    from pypowerwall.v1r_register import PHYSICAL_PROOF_HINT

    return "\n".join(line.strip() for line in PHYSICAL_PROOF_HINT.splitlines())


def _v1r_result(
    *,
    root: Path,
    fingerprint: str,
    private_key_path: Path,
    public_key_path: Path,
    siteid: Optional[str],
    email: Optional[str],
    state: Optional[int],
    needs_site: bool = False,
    sites: Optional[List[SetupV1rSite]] = None,
) -> SetupV1rResult:
    from pypowerwall.v1r_register import key_state_name

    state_name = key_state_name(state) if state is not None else None
    verified = state == 3
    pending = state == 1
    timed_out = state == 2
    removed = state == 4
    if needs_site:
        message = (
            "Select the Tesla energy site for this gateway, then register the key."
        )
    elif verified:
        message = "Key is VERIFIED and ready for use."
    elif pending:
        message = (
            "Key registered. Give the physical proof within about 10 minutes, "
            "then check verification."
        )
    elif timed_out:
        message = (
            "The verification window has closed. Register the same key again, "
            "then give the physical proof within about 10 minutes."
        )
    elif removed:
        message = "The key was removed from the gateway. Register it again."
    elif state is None:
        message = "No key state was reported. Check verification again."
    else:
        message = f"Key state: {state_name}."
    return SetupV1rResult(
        state=state,
        state_name=state_name,
        verified=verified,
        pending=pending,
        timed_out=timed_out,
        removed=removed,
        needs_site=needs_site,
        sites=sites or [],
        siteid=siteid,
        email=email,
        private_key_path=str(private_key_path),
        public_key_path=str(public_key_path),
        fingerprint=fingerprint,
        proof_hint=_v1r_proof_hint() if pending or not needs_site else None,
        message=message,
    )


def _v1r_rejected(exc: Exception) -> bool:
    text = str(exc).lower()
    return "rejected" in text or "401" in text or "403" in text


def v1r_register(request: SetupV1rRegisterRequest) -> SetupV1rResult:
    """Register the canonical v1r public key using Owner or FleetAPI."""
    from pypowerwall.v1r_register import _check_key_state, api_call, OWNER_API_BASE

    root = _v1r_path(request.authpath)
    public_der, fingerprint, private_key_path, public_key_path = _v1r_key_material(root)
    if request.method == "fleetapi":
        config = _fleet_read_config(root)
        if not config.get("access_token") and config.get("refresh_token"):
            config = _fleet_v1r_refresh(root, config)
        siteid = (request.siteid or "").strip() or str(config.get("site_id") or "")
        fleet_sites = []
        if not siteid:
            fleet_sites = _fleet_sites_with_refresh(root, config)
            if len(fleet_sites) == 1:
                siteid = fleet_sites[0].siteid
            else:
                return _v1r_result(
                    root=root,
                    fingerprint=fingerprint,
                    private_key_path=private_key_path,
                    public_key_path=public_key_path,
                    siteid=None,
                    email=None,
                    state=None,
                    needs_site=True,
                    sites=[
                        SetupV1rSite(
                            siteid=site.siteid,
                            name=site.name,
                        )
                        for site in fleet_sites
                    ],
                )
        result_sites = [
            SetupV1rSite(siteid=site.siteid, name=site.name) for site in fleet_sites
        ]
        response = _fleet_v1r_command(
            root, siteid, _v1r_registration_payload(public_der)
        )
        state = _check_key_state(response, pubkey_der=public_der)
        return _v1r_result(
            root=root,
            fingerprint=fingerprint,
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            siteid=siteid,
            email=None,
            state=state,
            sites=result_sites,
        )

    token, email = _v1r_auth_token(root, request.email)
    try:
        sites = _v1r_sites(token)
    except ValueError as exc:
        if not _v1r_rejected(exc):
            raise
        token, email = _v1r_refresh_auth(root, email)
        sites = _v1r_sites(token)
    siteid = (request.siteid or "").strip() or None
    if not siteid:
        if len(sites) == 1:
            siteid = sites[0].siteid
        else:
            return _v1r_result(
                root=root,
                fingerprint=fingerprint,
                private_key_path=private_key_path,
                public_key_path=public_key_path,
                siteid=None,
                email=email,
                state=None,
                needs_site=True,
                sites=sites,
            )
    if not any(site.siteid == siteid for site in sites):
        raise ValueError("the selected site is not available on this Tesla account")
    payload = _v1r_registration_payload(public_der)
    code, response = api_call(
        f"{OWNER_API_BASE}/api/1/energy_sites/{siteid}/command",
        method="POST",
        data=payload,
        token=token,
    )
    if code in (401, 403):
        token, email = _v1r_refresh_auth(root, email)
        code, response = api_call(
            f"{OWNER_API_BASE}/api/1/energy_sites/{siteid}/command",
            method="POST",
            data=payload,
            token=token,
        )
    if code in (401, 403):
        raise ValueError(
            "Tesla rejected the access token; sign in again and retry the "
            "v1r key registration"
        )
    if code != 200:
        raise ValueError(f"v1r key registration failed ({code})")
    state = _check_key_state(response, pubkey_der=public_der)
    return _v1r_result(
        root=root,
        fingerprint=fingerprint,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        siteid=siteid,
        email=email,
        state=state,
        sites=sites,
    )


def v1r_registration_status(request: SetupV1rStatusRequest) -> SetupV1rResult:
    """Check the gateway's verification state for the canonical v1r key."""
    from pypowerwall.v1r_register import _check_key_state, api_call, OWNER_API_BASE

    root = _v1r_path(request.authpath)
    public_der, fingerprint, private_key_path, public_key_path = _v1r_key_material(root)
    siteid = (request.siteid or "").strip()
    if not siteid.isdigit():
        raise ValueError("a numeric Tesla siteid is required")
    payload = _v1r_status_payload()
    if request.method == "fleetapi":
        state = _check_key_state(
            _fleet_v1r_command(root, siteid, payload),
            pubkey_der=public_der,
        )
        return _v1r_result(
            root=root,
            fingerprint=fingerprint,
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            siteid=siteid,
            email=None,
            state=state,
        )
    token, email = _v1r_auth_token(root, request.email)
    code, response = api_call(
        f"{OWNER_API_BASE}/api/1/energy_sites/{siteid}/command",
        method="POST",
        data=payload,
        token=token,
    )
    if code in (401, 403):
        token, email = _v1r_refresh_auth(root, email)
        code, response = api_call(
            f"{OWNER_API_BASE}/api/1/energy_sites/{siteid}/command",
            method="POST",
            data=payload,
            token=token,
        )
    if code in (401, 403):
        raise ValueError(
            "Tesla rejected the access token; sign in again and retry the "
            "v1r key status check"
        )
    if code != 200:
        raise ValueError(f"v1r key status check failed ({code})")
    state = _check_key_state(response, pubkey_der=public_der)
    return _v1r_result(
        root=root,
        fingerprint=fingerprint,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        siteid=siteid,
        email=email,
        state=state,
    )


def _cloud_auth_path(authpath: str) -> Path:
    if not authpath:
        raise ValueError("authpath is required")
    path = Path(os.path.expanduser(authpath))
    if path.exists() and not path.is_dir():
        raise ValueError("authpath is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cloud_auth_start(
    email: str, authpath: str, region: str = "us"
) -> SetupCloudAuthStartResult:
    """Build a Tesla PKCE URL using pypowerwall's existing auth helper."""
    email = (email or "").strip()
    if "@" not in email:
        raise ValueError("a valid Tesla email is required")
    if region not in {"us", "cn"}:
        raise ValueError("unknown Tesla auth region")
    path = _cloud_auth_path(authpath)
    from pypowerwall import tesla_auth

    auth_url, verifier, state = tesla_auth._build_auth_url(region)
    now = time.monotonic()
    with _CLOUD_AUTH_LOCK:
        for old_state, item in list(_CLOUD_AUTH_STATES.items()):
            if item["expires"] <= now:
                _CLOUD_AUTH_STATES.pop(old_state, None)
        _CLOUD_AUTH_STATES[state] = {
            "verifier": verifier,
            "email": email,
            "authpath": str(path),
            "region": region,
            "expires": now + 600,
        }
    return SetupCloudAuthStartResult(auth_url=auth_url, expires_in=600)


def cloud_auth_complete(
    request: SetupCloudAuthCompleteRequest,
) -> SetupCloudAuthCompleteResult:
    """Exchange the pasted callback code and save auth/site files."""
    parsed = urllib.parse.urlparse(request.callback_url.strip())
    params = urllib.parse.parse_qs(parsed.query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code or not state:
        raise ValueError("callback URL must contain code and state")
    with _CLOUD_AUTH_LOCK:
        pending = _CLOUD_AUTH_STATES.pop(state, None)
    if not pending or pending["expires"] < time.monotonic():
        raise ValueError("login state expired; start the Tesla login again")
    chosen = (request.siteid or "").strip() or None
    if chosen and not chosen.isdigit():
        raise ValueError("siteid must be numeric")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    token_data = tesla_auth._exchange_code(code, pending["verifier"], pending["region"])
    path = Path(pending["authpath"])
    path.mkdir(parents=True, exist_ok=True)
    auth_file = path / ".pypowerwall.auth"
    site_file = path / ".pypowerwall.site"
    with tempfile.TemporaryDirectory(prefix="pws-cloud-auth-") as temp_dir:
        temp_auth = Path(temp_dir) / ".pypowerwall.auth"
        if auth_file.is_file():
            shutil.copy2(auth_file, temp_auth)
            os.chmod(temp_auth, 0o600)
        tesla_auth.save_token(
            token_data,
            str(temp_auth),
            pending["email"],
            pending["region"],
        )
        cloud = PyPowerwallCloud(
            pending["email"],
            authpath=temp_dir,
            siteid=int(chosen) if chosen else None,
        )
        if not cloud.connect():
            raise ValueError("Tesla token is valid but the cloud connection failed")
        sites = cloud.getsites() or []
        if not sites:
            raise ValueError("Tesla Cloud returned no sites")
        safe_sites = [
            {
                "siteid": str(site.get("energy_site_id")),
                "name": site.get("site_name"),
                "resource_type": site.get("resource_type"),
            }
            for site in sites
            if isinstance(site, dict) and site.get("energy_site_id") is not None
        ]
        if not safe_sites:
            raise ValueError("Tesla Cloud returned no energy sites")
        if chosen:
            if not any(site["siteid"] == str(chosen) for site in safe_sites):
                raise ValueError("selected siteid is not available on this account")
            selected = str(chosen)
        elif len(safe_sites) == 1:
            selected = safe_sites[0]["siteid"]
        else:
            selected = None
        validated_accounts = _read_auth_accounts(temp_auth)
        if not validated_accounts:
            raise ValueError("Tesla token could not be validated")
        existing_accounts = _read_auth_accounts(auth_file)
        merged_accounts = dict(existing_accounts)
        merged_accounts.update(validated_accounts)
        auth_text = json.dumps(merged_accounts, indent=2)

    _commit_auth_and_site(
        auth_file,
        auth_text,
        site_file,
        selected,
        update_site=True,
    )
    return SetupCloudAuthCompleteResult(
        saved=True,
        email=pending["email"],
        authpath=str(path),
        sites=safe_sites,
        siteid=selected,
        needs_site=selected is None,
        notes=[
            "Tokens are written with owner-only permissions by pypowerwall.",
            "The setup test never refreshes or prints token values.",
        ],
    )


def cloud_auth_use(
    request: SetupCloudAuthUseRequest,
) -> SetupCloudAuthCompleteResult:
    """Use an existing Cloud auth cache and select or confirm its site."""
    path = _cloud_auth_path(request.authpath)
    auth_file = path / ".pypowerwall.auth"
    if not auth_file.is_file():
        raise ValueError(
            "Cloud auth file not found; sign in or import an auth file first"
        )
    try:
        accounts = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unable to read the Cloud auth file") from exc
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("the Cloud auth file has no account")
    email = (request.email or "").strip() or str(next(iter(accounts)))
    if email not in accounts:
        raise ValueError(
            "the requested Tesla account is not in the auth file; sign in again"
        )
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    site_file = path / ".pypowerwall.site"
    existing_site = ""
    if site_file.is_file():
        try:
            existing_site = site_file.read_text(encoding="utf-8").strip()
        except OSError:
            existing_site = ""
    # Connect without a site so a stale .pypowerwall.site left by an
    # interrupted login cannot make the whole existing auth unusable.
    cloud = PyPowerwallCloud(
        email,
        authpath=str(path),
        siteid=None,
    )
    # PyPowerwallCloud reads .pypowerwall.site in __init__ even when siteid is
    # None. Clear it again so connect() can list sites and recover the match.
    cloud.siteid = None
    if not cloud.connect():
        raise ValueError("unable to connect to Tesla Cloud with the existing auth file")
    sites = cloud.getsites() or []
    safe_sites = [
        {
            "siteid": str(site.get("energy_site_id")),
            "name": site.get("site_name"),
            "resource_type": site.get("resource_type"),
        }
        for site in sites
        if isinstance(site, dict) and site.get("energy_site_id") is not None
    ]
    selected = None
    if existing_site and any(site["siteid"] == existing_site for site in safe_sites):
        selected = existing_site
    elif len(safe_sites) == 1:
        selected = safe_sites[0]["siteid"]
        _write_private_text(site_file, selected, newline=False)
    if selected is None and site_file.is_file():
        # A stale site does not belong to the selected account.
        os.remove(site_file)
    return SetupCloudAuthCompleteResult(
        saved=True,
        email=email,
        authpath=str(path),
        sites=safe_sites,
        siteid=selected,
        needs_site=selected is None,
        notes=[
            "Using the existing Cloud auth file; no new Tesla login was started.",
            "Select a site if it is not already set.",
        ],
    )


def cloud_auth_site(email: str, authpath: str, siteid: str) -> SetupCloudSiteResult:
    """Persist a site selection after an ambiguous multi-site login."""
    path = _cloud_auth_path(authpath)
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    cloud = PyPowerwallCloud(email, authpath=str(path), siteid=int(siteid))
    if not cloud.connect():
        raise ValueError("unable to connect to Tesla Cloud for site selection")
    sites = cloud.getsites() or []
    if not any(
        isinstance(site, dict) and str(site.get("energy_site_id")) == str(siteid)
        for site in sites
    ):
        raise ValueError("siteid is not available on this account")
    sitefile = path / ".pypowerwall.site"
    _write_private_text(sitefile, str(siteid), newline=False)
    return SetupCloudSiteResult(saved=True, siteid=str(siteid), sitefile=str(sitefile))


def _environment_override_names() -> List[str]:
    """Environment/CLI variables that still override the config file."""
    env_names = {name.upper() for name in os.environ}
    names: List[str] = []
    for name in _WRITABLE_OPTION_FIELDS:
        # The server block is loaded after the environment (existing behavior),
        # so PW_BIND_ADDRESS/PW_PORT/CORS_ORIGINS do not need removal.
        if name in _SERVER_OPTION_MAP:
            continue
        field = Settings.model_fields.get(name)
        alias = getattr(field, "alias", None) if field is not None else None
        env_name = str(alias or name)
        if env_name.upper() in env_names:
            names.append(env_name)
    return sorted(names)


def _env_import_requires_removal() -> bool:
    return settings.config_source in {"pw_gateways", "legacy_env"} or bool(
        _environment_override_names()
    )


def _env_import_precedence_notes() -> List[str]:
    notes: List[str] = []
    overrides = _environment_override_names()
    if overrides:
        notes.append(
            "These environment/CLI values still take precedence until they are "
            "removed, then restart: " + ", ".join(overrides) + "."
        )
    if settings.config_source in {"pw_gateways", "legacy_env"}:
        notes.append(
            "PW_GATEWAYS or legacy PW_* gateway variables still take precedence "
            "until they are removed, then restart."
        )
    return notes


def _env_import_document() -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "server": {
            "host": settings.server_host,
            "port": settings.server_port,
            "cors_origins": settings.cors_origins,
        },
        "gateways": [_gateway_dict(config) for config in settings.gateways],
    }
    options = {}
    for name in _WRITABLE_OPTION_FIELDS:
        if name in _SERVER_OPTION_MAP:
            continue
        value = getattr(settings, name, None)
        if value is not None:
            options[name] = value
    if options:
        doc["settings"] = options
    return doc


def env_import_preview() -> Dict[str, Any]:
    """Return a masked YAML preview for migrating env config to PW_CONFIG."""
    doc = _env_import_document()
    for gateway in doc["gateways"]:
        for key in ("gw_pwd", "password"):
            if gateway.get(key):
                gateway[key] = "***"
    block = doc.get("settings")
    if isinstance(block, dict):
        for key in ("control_secret", "mqtt_password"):
            if block.get(key):
                block[key] = "***"
    target = _effective_path() or default_config_path()
    revision = None
    if Path(target).exists():
        revision = _revision(Path(target).read_bytes())
    return {
        "yaml": yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
        "source": settings.config_source,
        "target_path": target,
        "revision": revision,
        "requires_removal": _env_import_requires_removal(),
        "notes": [
            "Secrets are masked in this preview; the write action uses the "
            "server's current values."
        ]
        + _env_import_precedence_notes(),
    }


def env_import_activate(request: SetupEnvImportRequest) -> SetupEnvImportResult:
    """Write the effective environment configuration to the setup YAML file."""
    source = settings.config_source
    requires_removal = _env_import_requires_removal()
    path = _effective_path() or default_config_path()
    malformed_existing = False
    with _CONFIG_LOCK:
        existing_doc: Dict[str, Any] = {}
        if Path(path).exists():
            raw = Path(path).read_bytes()
            if request.expected_revision != _revision(raw):
                raise ValueError("configuration changed; preview again")
            try:
                existing_doc, _ = _read_doc(path)
            except Exception:
                if not settings.config_error:
                    raise ValueError(
                        "existing configuration cannot be merged; repair the file manually"
                    )
                malformed_existing = True
                existing_doc = {}
        elif request.expected_revision:
            raise ValueError("configuration changed; preview again")

        doc = dict(existing_doc)
        current = _env_import_document()
        doc["gateways"] = current["gateways"]
        server_doc = doc.get("server")
        if not isinstance(server_doc, dict):
            server_doc = {}
        server_doc.update(current["server"])
        doc["server"] = server_doc
        existing_options = doc.get("settings")
        if not isinstance(existing_options, dict):
            existing_options = {}
        merged_options = dict(existing_options)
        merged_options.update(current.get("settings", {}))
        if merged_options:
            doc["settings"] = merged_options
        else:
            doc.pop("settings", None)
        revision, backup = _write_atomic(path, doc)
    notes = _env_import_precedence_notes()
    if malformed_existing:
        notes.append(
            "The existing configuration file could not be parsed and was "
            f"replaced; a backup was written to {backup}."
        )
    notes.append("Restart the server/container to activate the saved configuration.")
    return SetupEnvImportResult(
        saved=True,
        config_path=path,
        backup_path=backup,
        revision=revision,
        source=source,
        requires_removal=requires_removal,
        notes=notes,
    )


# Fleet API setup


def _fleet_path(authpath: Optional[str]) -> Path:
    return _v1r_path(authpath)


def _fleet_public_domain(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "/" in domain or "://" in domain or "\\" in domain:
        raise ValueError("domain must be a public hostname without a scheme or path")
    if ":" in domain:
        raise ValueError("domain must not include a port")
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("domain must resolve to a public address") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise ValueError("domain must resolve to a public address")
    return domain


def _fleet_redirect_uri(domain: str, redirect_uri: Optional[str]) -> str:
    value = (redirect_uri or "").strip() or f"https://{domain}/access"
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("redirect URI must be an absolute https URL")
    return value


def _fleet_config_file(path: Path) -> Path:
    return path / ".pypowerwall.fleetapi"


def _fleet_read_config(path: Path) -> Dict[str, Any]:
    config_file = _fleet_config_file(path)
    if not config_file.is_file():
        raise ValueError("FleetAPI configuration does not exist; start the setup first")
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unable to read the FleetAPI configuration") from exc
    if not isinstance(data, dict):
        raise ValueError("the FleetAPI configuration is invalid")
    return data


def _fleet_write_config(path: Path, config: Dict[str, Any]) -> Path:
    config_file = _fleet_config_file(path)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=".pypowerwall.fleetapi.", suffix=".tmp", dir=str(path)
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(config, indent=4))
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, config_file)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return config_file


def _fleet_response_json(response: Any) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        return None


def _fleet_sites(config: Dict[str, Any]) -> List[SetupFleetSite]:
    from pypowerwall.fleetapi.fleetapi import _http2_request

    access_token = config.get("access_token")
    audience = config.get("AUDIENCE")
    if not access_token or not audience:
        raise ValueError("FleetAPI tokens are missing; complete the Tesla login first")
    response = _http2_request(
        "GET",
        f"{audience}/api/1/products",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if response is None:
        raise ValueError("no response from Tesla FleetAPI while listing sites")
    if response.status_code in (401, 403):
        raise ValueError("Tesla rejected the FleetAPI token; sign in again")
    if response.status_code != 200:
        raise ValueError(f"FleetAPI site lookup failed ({response.status_code})")
    payload = _fleet_response_json(response)
    products = payload.get("response", []) if isinstance(payload, dict) else []
    sites = []
    for product in products or []:
        if not isinstance(product, dict) or product.get("energy_site_id") is None:
            continue
        sites.append(
            SetupFleetSite(
                siteid=str(product["energy_site_id"]),
                name=product.get("site_name") or None,
                resource_type=product.get("resource_type") or None,
            )
        )
    if not sites:
        raise ValueError("no Tesla energy sites were found on this account")
    return sites


def _fleet_fetch_public_key(url: str) -> Optional[int]:
    """Fetch the hosted public key without following redirects."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=15) as response:
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def _fleet_sites_with_refresh(
    root: Path, config: Dict[str, Any]
) -> List[SetupFleetSite]:
    """List sites and refresh an expired access token once when needed."""
    try:
        return _fleet_sites(config)
    except ValueError as exc:
        text = str(exc).lower()
        if not config.get("refresh_token") or not (
            "rejected" in text or "401" in text or "403" in text
        ):
            raise
        refreshed = _fleet_v1r_refresh(root, config)
        return _fleet_sites(refreshed)


def _fleet_live_check(path: Path, siteid: Optional[str] = None) -> Dict[str, Any]:
    """Read site info and live status exactly like the FleetAPI setup tool."""
    from pypowerwall.fleetapi.fleetapi import FleetAPI

    config = _fleet_read_config(path)
    selected = (siteid or str(config.get("site_id") or "")).strip()
    if not selected.isdigit():
        raise ValueError("a numeric Tesla siteid is required")
    fleet = FleetAPI(
        configfile=str(_fleet_config_file(path)),
        site_id=int(selected),
    )
    info = fleet.get_site_info(force=True)
    live = fleet.get_live_status(force=True)
    if not isinstance(info, dict) or not info:
        raise ValueError("FleetAPI site info returned no data")
    if not isinstance(live, dict) or not live:
        raise ValueError("FleetAPI live status returned no data")
    return {
        "site_name": info.get("site_name"),
        "battery_percent": live.get("percentage_charged"),
        "grid_status": live.get("grid_status"),
    }


def fleet_setup_start(request: SetupFleetStartRequest) -> SetupFleetStartResult:
    """Verify the FleetAPI public key, register the partner and build login URL."""
    from pypowerwall.fleetapi.fleetapi import (
        SCOPE,
        _http2_request,
        fleet_api_urls,
    )

    path = _fleet_path(request.authpath)
    domain = _fleet_public_domain(request.domain)
    redirect_uri = _fleet_redirect_uri(domain, request.redirect_uri)
    client_id = (request.client_id or "").strip()
    client_secret = request.client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise ValueError("FleetAPI Client ID and Client Secret are required")
    audience = list(fleet_api_urls.values())[
        {"na": 0, "eu": 1, "cn": 2}[request.region]
    ]
    public_key_url = (
        f"https://{domain}/.well-known/appspecific/com.tesla.3p.public-key.pem"
    )
    status = _fleet_fetch_public_key(public_key_url)
    if status != 200:
        code = status if status is not None else "no response"
        raise ValueError(
            f"could not verify the public key at {public_key_url} ({code}); "
            "upload the PEM public key from create_pem_key.py first"
        )
    try:
        existing = _fleet_read_config(path)
    except ValueError:
        existing = {}
    partner_token = existing.get("partner_token") or ""
    partner_account = existing.get("partner_account") or {}
    reuse_partner = (
        existing.get("CLIENT_ID") == client_id
        and existing.get("DOMAIN") == domain
        and bool(partner_account)
    )
    if not reuse_partner:
        token_response = _http2_request(
            "POST",
            "https://auth.tesla.com/oauth2/v3/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": SCOPE,
                "audience": audience,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_data = _fleet_response_json(token_response)
        partner_token = (
            token_data.get("access_token") if isinstance(token_data, dict) else None
        )
        if (
            token_response is None
            or token_response.status_code != 200
            or not partner_token
        ):
            code = getattr(token_response, "status_code", "no response")
            raise ValueError(f"unable to obtain a FleetAPI partner token ({code})")
        partner_response = _http2_request(
            "POST",
            f"{audience}/api/1/partner_accounts",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {partner_token}",
            },
            data=json.dumps({"domain": domain}),
            timeout=15,
        )
        if partner_response is None or partner_response.status_code not in (200, 201):
            code = getattr(partner_response, "status_code", "no response")
            raise ValueError(
                f"unable to register the FleetAPI partner account ({code})"
            )
        partner_account = _fleet_response_json(partner_response) or {}
    same_app = (
        existing.get("CLIENT_ID") == client_id and existing.get("DOMAIN") == domain
    )
    config = {
        "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret,
        "DOMAIN": domain,
        "REDIRECT_URI": redirect_uri,
        "AUDIENCE": audience,
        "partner_token": partner_token,
        "partner_account": partner_account,
        "access_token": (existing.get("access_token") or "") if same_app else "",
        "refresh_token": ((existing.get("refresh_token") or "") if same_app else ""),
        "site_id": existing.get("site_id") if same_app else None,
    }
    _backup_file(_fleet_config_file(path))
    config_file = _fleet_write_config(path, config)
    state = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _FLEET_AUTH_LOCK:
        for old_state, item in list(_FLEET_AUTH_STATES.items()):
            if item["expires"] <= now:
                _FLEET_AUTH_STATES.pop(old_state, None)
        _FLEET_AUTH_STATES[state] = {
            "authpath": str(path),
            "expires": now + 600,
        }
    scope = urllib.parse.quote(SCOPE)
    auth_url = (
        "https://auth.tesla.com/oauth2/v3/authorize?"
        "&client_id="
        + urllib.parse.quote(client_id)
        + "&locale=en-US&prompt=login&redirect_uri="
        + urllib.parse.quote(redirect_uri, safe=":/")
        + "&response_type=code&scope="
        + scope
        + "&state="
        + urllib.parse.quote(state)
    )
    return SetupFleetStartResult(
        auth_url=auth_url,
        expires_in=600,
        public_key_url=public_key_url,
        config_file=str(config_file),
        message=(
            "FleetAPI credentials saved and partner registered. Sign in with "
            "the Tesla account that owns this Powerwall."
        ),
    )


def fleet_setup_complete(
    request: SetupFleetCompleteRequest,
) -> SetupFleetCompleteResult:
    """Exchange the FleetAPI callback code, save tokens and list sites."""
    from pypowerwall.fleetapi.fleetapi import SCOPE, _http2_request

    path = _fleet_path(request.authpath)
    config = _fleet_read_config(path)
    parsed = urllib.parse.urlparse(request.callback_url.strip())
    params = urllib.parse.parse_qs(parsed.query)
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code or not state:
        raise ValueError("paste the full callback URL containing code and state")
    with _FLEET_AUTH_LOCK:
        pending = _FLEET_AUTH_STATES.pop(state, None)
    if (
        not pending
        or pending["expires"] < time.monotonic()
        or pending["authpath"] != str(path)
    ):
        raise ValueError("FleetAPI login state expired; start the setup again")
    token_response = _http2_request(
        "POST",
        "https://auth.tesla.com/oauth2/v3/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config.get("CLIENT_ID"),
            "client_secret": config.get("CLIENT_SECRET"),
            "code": code,
            "audience": config.get("AUDIENCE"),
            "redirect_uri": config.get("REDIRECT_URI"),
            "scope": SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    token_data = _fleet_response_json(token_response)
    if (
        token_response is None
        or token_response.status_code != 200
        or not isinstance(token_data, dict)
    ):
        code_status = getattr(token_response, "status_code", "no response")
        raise ValueError(f"FleetAPI authorization exchange failed ({code_status})")
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token or not refresh_token:
        raise ValueError("Tesla did not return FleetAPI access and refresh tokens")
    candidate = dict(config)
    candidate["access_token"] = access_token
    candidate["refresh_token"] = refresh_token
    sites = _fleet_sites(candidate)
    siteid = None
    needs_site = len(sites) > 1
    if not needs_site:
        siteid = sites[0].siteid
        candidate["site_id"] = int(siteid) if siteid.isdigit() else siteid
    else:
        # Do not keep a site from the previous login when the new account
        # has several sites and the user has not selected one yet.
        candidate["site_id"] = None
    config_file = _fleet_config_file(path)
    _backup_file(config_file)
    _fleet_write_config(path, candidate)
    live_message = ""
    if not needs_site:
        try:
            _fleet_live_check(path, siteid)
            live_message = " Live site info and status verified."
        except Exception as exc:
            live_message = (
                " Site was selected, but the live check failed "
                f"({type(exc).__name__}); use Test connection."
            )
    return SetupFleetCompleteResult(
        saved=True,
        authpath=str(path),
        config_file=str(_fleet_config_file(path)),
        sites=sites,
        siteid=siteid,
        needs_site=needs_site,
        message=(
            "FleetAPI login saved. Select the energy site for this gateway."
            if needs_site
            else "FleetAPI login saved and the only site was selected." + live_message
        ),
    )


def fleet_setup_site(request: SetupFleetSiteRequest) -> SetupFleetSiteResult:
    """Persist the selected FleetAPI site after verifying it belongs to the account."""
    path = _fleet_path(request.authpath)
    config = _fleet_read_config(path)
    sites = _fleet_sites_with_refresh(path, config)
    config = _fleet_read_config(path)
    siteid = (request.siteid or "").strip()
    if not any(site.siteid == siteid for site in sites):
        raise ValueError("the selected site is not available on this Tesla account")
    config["site_id"] = int(siteid) if siteid.isdigit() else siteid
    config_file = _fleet_write_config(path, config)
    try:
        _fleet_live_check(path, siteid)
        message = "FleetAPI site selected. Site info and live status verified."
    except Exception as exc:
        message = (
            "FleetAPI site selected, but the live check failed "
            f"({type(exc).__name__}); use Test connection."
        )
    return SetupFleetSiteResult(
        saved=True,
        siteid=siteid,
        config_file=str(config_file),
        message=message,
    )


def fleet_generate_key(request: SetupFleetKeyRequest) -> SetupFleetKeyResult:
    """Generate the FleetAPI EC key pair used for Tesla app public-key hosting."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    path = _v1r_path(request.authpath)
    private_path = path / "com.tesla.3p.private-key.pem"
    public_path = path / "com.tesla.3p.public-key.pem"
    created = False
    if private_path.is_file():
        try:
            private_key = serialization.load_pem_private_key(
                private_path.read_bytes(), password=None
            )
        except Exception as exc:
            raise ValueError(
                "existing FleetAPI private key could not be read; move it away "
                "before generating a new key"
            ) from exc
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(
            os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
        ) as handle:
            handle.write(private_pem)
        os.chmod(private_path, 0o600)
        created = True
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not public_path.is_file() or public_path.read_bytes() != public_pem:
        public_path.write_bytes(public_pem)
        try:
            os.chmod(public_path, 0o644)
        except OSError:
            pass
    return SetupFleetKeyResult(
        created=created,
        private_key_path=str(private_path),
        public_key_path=str(public_path),
        public_key_pem=public_pem.decode("ascii"),
        message=(
            "FleetAPI key pair created. Host the public key at "
            "https://{DOMAIN}/.well-known/appspecific/com.tesla.3p.public-key.pem "
            "and keep the private key on the server."
            if created
            else "Existing FleetAPI key reused. Host its public key at "
            "https://{DOMAIN}/.well-known/appspecific/com.tesla.3p.public-key.pem."
        ),
    )


def scan_gateways(request: SetupScanRequest) -> SetupScanResult:
    """Scan a private /24 (or smaller) for Tesla Energy Gateways."""
    from pypowerwall.scan import get_my_ip, scan

    network_text = None
    if request.cidr and request.cidr.strip():
        try:
            network = ipaddress.IPv4Network(request.cidr.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("CIDR must be a valid IPv4 network") from exc
    else:
        try:
            network = ipaddress.IPv4Network(get_my_ip() + "/24", strict=False)
        except Exception as exc:
            raise ValueError(
                "unable to determine the local network; enter a CIDR"
            ) from exc
    if network.prefixlen < 24:
        raise ValueError("GUI scans are limited to /24 or smaller private networks")
    if not (network.is_private or network.is_loopback or network.is_link_local):
        raise ValueError("CIDR must be a private, loopback or link-local network")
    network_text = str(network)
    results = scan(
        cidr=network_text,
        timeout=0.5,
        max_threads=30,
        json_output=True,
    )
    gateways = [
        SetupScanGateway(
            ip=str(item.get("ip")),
            din=item.get("din") or None,
            firmware=item.get("firmware") or None,
        )
        for item in results or []
        if isinstance(item, dict) and item.get("ip")
    ]
    return SetupScanResult(
        ok=True,
        network=network_text,
        gateways=gateways,
        message=(
            f"Found {len(gateways)} Tesla gateway(s) on {network_text}."
            if gateways
            else f"No Tesla gateway found on {network_text}."
        ),
    )


def _write_private_text(path: Path, content: str, newline: bool = True) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if newline and not content.endswith("\n"):
                handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _write_private_bytes(path: Path, content: bytes) -> None:
    """Atomically restore private bytes without changing their line endings."""
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _backup_file(path: Path) -> Optional[str]:
    """Keep a private .bak copy before replacing user-provided auth material."""
    if not path.is_file():
        return None
    backup = Path(f"{path}.bak")
    shutil.copy2(path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return str(backup)


def _read_auth_accounts(path: Path) -> Dict[str, Any]:
    """Read a Tesla auth cache, treating a missing or broken file as empty."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _commit_auth_and_site(
    auth_path: Path,
    auth_text: str,
    site_path: Path,
    site_text: Optional[str],
    update_site: bool,
) -> None:
    """Replace auth and site files together, restoring the old state on error."""
    old_auth = auth_path.read_bytes() if auth_path.is_file() else None
    old_site = site_path.read_bytes() if update_site and site_path.is_file() else None
    _backup_file(auth_path)
    try:
        # Write the site first. If the process stops before the auth replace,
        # the old auth remains and a later cloud_auth_use() can validate or
        # discard the site against that account.
        if update_site:
            if site_text is None:
                if site_path.exists():
                    os.remove(site_path)
            else:
                _write_private_text(site_path, site_text, newline=False)
        _write_private_text(auth_path, auth_text)
    except Exception:
        try:
            if old_auth is None:
                auth_path.unlink(missing_ok=True)
            else:
                _write_private_bytes(auth_path, old_auth)
            if update_site:
                if old_site is None:
                    site_path.unlink(missing_ok=True)
                else:
                    _write_private_bytes(site_path, old_site)
        except Exception:
            logger.exception("Failed to restore the previous Cloud auth state")
        raise


def _merge_auth_file(path: Path, incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merge imported Tesla accounts into an existing auth cache."""
    existing: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (OSError, ValueError):
            existing = {}
    merged = dict(existing)
    merged.update(incoming)
    _write_private_text(path, json.dumps(merged, indent=2))
    return merged


def import_auth_tokens(request: SetupAuthTokensRequest) -> SetupAuthTokensResult:
    """Create a Cloud auth file from refresh/access tokens pasted in the GUI."""
    from pypowerwall.tesla_auth import save_token

    email = (request.email or "").strip()
    refresh_token = request.refresh_token.get_secret_value().strip()
    access_token = (
        request.access_token.get_secret_value().strip() if request.access_token else ""
    )
    if not email and access_token:
        from pypowerwall.tesla_auth import _extract_email_from_token

        email = _extract_email_from_token(access_token).strip()
    if "@" not in email:
        raise ValueError(
            "a valid Tesla email is required (or provide an access token containing it)"
        )
    if not refresh_token:
        raise ValueError("a Tesla refresh token is required")
    if any(character.isspace() for character in refresh_token + access_token):
        raise ValueError("tokens must not contain whitespace")
    if len(refresh_token) > 16384 or len(access_token) > 16384:
        raise ValueError("the pasted token is too large")
    siteid = (request.siteid or "").strip() or None
    if siteid and not siteid.isdigit():
        raise ValueError("siteid must be numeric")
    path = _cloud_auth_path(request.authpath)
    auth_file = path / ".pypowerwall.auth"
    site_file = path / ".pypowerwall.site"
    with tempfile.TemporaryDirectory(prefix="pws-cloud-token-") as temp_dir:
        temp_auth = Path(temp_dir) / ".pypowerwall.auth"
        if auth_file.is_file():
            shutil.copy2(auth_file, temp_auth)
            os.chmod(temp_auth, 0o600)
        save_token(
            {
                "refresh_token": refresh_token,
                "access_token": access_token,
                "expires_in": 28800,
                "token_type": "Bearer",
            },
            path=str(temp_auth),
            email=email,
        )
        validated_accounts = _read_auth_accounts(temp_auth)
        if not validated_accounts:
            raise ValueError("the Cloud tokens could not be saved")
        existing_accounts = _read_auth_accounts(auth_file)
        merged_accounts = dict(existing_accounts)
        merged_accounts.update(validated_accounts)
        auth_text = json.dumps(merged_accounts, indent=2)
    _commit_auth_and_site(
        auth_file,
        auth_text,
        site_file,
        siteid,
        update_site=bool(siteid),
    )
    return SetupAuthTokensResult(
        saved=True,
        authpath=str(path),
        auth_file=str(auth_file),
        email=email,
        siteid=siteid,
        site_file=str(site_file) if siteid else None,
        message=(
            f"Cloud tokens saved for {email}."
            + (
                " Site " + siteid + " saved."
                if siteid
                else " Set the Site ID if needed."
            )
        ),
    )


def import_auth_file(request: SetupAuthFileRequest) -> SetupAuthFileResult:
    """Import a Cloud auth cache or FleetAPI config selected in the browser."""
    root = _v1r_path(request.authpath)
    content = (request.content or "").lstrip("\ufeff").strip()
    if not content:
        raise ValueError("the selected auth file is empty")
    if len(content.encode("utf-8")) > 256 * 1024:
        raise ValueError("the selected auth file is too large")
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError("the selected file is not valid auth JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("the selected auth file has no usable entries")
    if request.mode == "cloud":
        account_email = None
        for email, account in data.items():
            if not isinstance(account, dict):
                continue
            sso = account.get("sso")
            if isinstance(sso, dict) and (
                sso.get("access_token") or sso.get("refresh_token")
            ):
                account_email = str(email)
                break
        if not account_email:
            raise ValueError(
                "the selected file has no usable Tesla access or refresh token"
            )
        siteid = (request.siteid or "").strip() or None
        if siteid and not siteid.isdigit():
            raise ValueError("siteid must be numeric")
        auth_file = root / ".pypowerwall.auth"
        site_file = root / ".pypowerwall.site"
        existing_accounts = _read_auth_accounts(auth_file)
        merged_accounts = dict(existing_accounts)
        merged_accounts.update(data)
        _commit_auth_and_site(
            auth_file,
            json.dumps(merged_accounts, indent=2),
            site_file,
            siteid,
            update_site=bool(siteid),
        )
        files = {"auth": str(auth_file)}
        if siteid:
            files["site"] = str(site_file)
        return SetupAuthFileResult(
            saved=True,
            authpath=str(root),
            files=files,
            email=account_email,
            siteid=siteid,
            message=(
                f"Cloud auth file imported for {account_email}."
                + (
                    " Site " + siteid + " saved."
                    if siteid
                    else " Set the Site ID if needed."
                )
            ),
        )
    required = {"CLIENT_ID", "CLIENT_SECRET", "DOMAIN", "AUDIENCE"}
    missing = sorted(key for key in required if not data.get(key))
    if missing or not (data.get("access_token") or data.get("refresh_token")):
        raise ValueError(
            "the selected file is not a usable .pypowerwall.fleetapi configuration"
        )
    _backup_file(_fleet_config_file(root))
    config_file = _fleet_write_config(root, data)
    return SetupAuthFileResult(
        saved=True,
        authpath=str(root),
        files={"fleetapi": str(config_file)},
        siteid=str(data.get("site_id")) if data.get("site_id") is not None else None,
        message="FleetAPI configuration imported. Use Test connection to verify access.",
    )


def _v1r_registration_payload(public_der: bytes) -> Dict[str, Any]:
    return {
        "command_properties": {
            "message": {
                "authorization": {
                    "add_authorized_client_request": {
                        "key_type": 1,
                        "public_key": base64.b64encode(public_der).decode(),
                        "authorized_client_type": 1,
                        "description": "Powerwall LAN Client",
                    }
                }
            },
            "identifier_type": 1,
        },
        "command_type": "grpc_command",
    }


def _fleet_v1r_refresh(root: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    from pypowerwall.fleetapi.fleetapi import _http2_request

    refresh_token = config.get("refresh_token")
    client_id = config.get("CLIENT_ID")
    if not refresh_token or not client_id:
        raise ValueError(
            "the FleetAPI config has no refresh token; run Fleet API setup again"
        )
    response = _http2_request(
        "POST",
        "https://auth.tesla.com/oauth2/v3/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    payload = _fleet_response_json(response)
    if (
        response is None
        or response.status_code != 200
        or not isinstance(payload, dict)
        or not payload.get("access_token")
    ):
        code = getattr(response, "status_code", "no response")
        raise ValueError(f"unable to refresh the FleetAPI token ({code})")
    config["access_token"] = payload["access_token"]
    config["refresh_token"] = payload.get("refresh_token") or refresh_token
    _fleet_write_config(root, config)
    return config


def _fleet_v1r_command(
    root: Path,
    siteid: str,
    payload: Dict[str, Any],
) -> Any:
    from pypowerwall.fleetapi.fleetapi import _http2_request

    siteid = str(siteid).strip()
    if not siteid.isdigit():
        raise ValueError("a numeric Tesla siteid is required")
    config = _fleet_read_config(root)
    if not config.get("access_token"):
        config = _fleet_v1r_refresh(root, config)

    def request():
        return _http2_request(
            "POST",
            f"{config['AUDIENCE']}/api/1/energy_sites/{siteid}/command",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.get('access_token')}",
            },
            data=json.dumps(payload),
            timeout=15,
        )

    response = request()
    if response is None:
        raise ValueError("no response from Tesla FleetAPI")
    if response.status_code in (401, 403):
        config = _fleet_v1r_refresh(root, config)
        response = request()
        if response is None:
            raise ValueError("no response from Tesla FleetAPI")
    if response.status_code != 200:
        raise ValueError(f"FleetAPI v1r registration failed ({response.status_code})")
    return _fleet_response_json(response)


def _v1r_status_payload() -> Dict[str, Any]:
    return {
        "command_properties": {
            "message": {"authorization": {"list_authorized_clients_request": {}}},
            "identifier_type": 1,
        },
        "command_type": "grpc_command",
    }
