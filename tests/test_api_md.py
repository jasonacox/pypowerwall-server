"""
Keep API.md honest: every setting and every route must be documented.

API.md is the reference for all environment variables and URI routes. These
tests fail when a new Settings field or route is added without an API.md
entry, or when API.md names an environment variable the server doesn't have.
"""

import re
from pathlib import Path

from app.config import Settings
from app.main import app

API_MD = (Path(__file__).resolve().parent.parent / "API.md").read_text()

# Read with os.getenv in app/config.py rather than declared on Settings
EXTRA_ENV_VARS = {"PW_CONFIG", "PW_GATEWAYS"}

# Legacy proxy variables API.md names as not read by the server
PROXY_ONLY_ENV_VARS = {
    "PW_FIRMWARE_CHECK_INTERVAL",
    "PW_SITE_ZERO_THRESHOLD",
    "PW_TEDAPI_API_VERSION",
    "PW_TEDAPI_AUTH_MODE",
}


def _setting_names():
    """Environment variable name of every Settings field that has one."""
    return {f.alias for f in Settings.model_fields.values() if f.alias}


def _route_paths():
    """Every registered path, with `{name:path}` converters shown as `{name}`."""
    return {re.sub(r"\{(\w+):\w+\}", r"{\1}", route.path) for route in app.routes}


def test_every_setting_documented():
    """Each Settings alias and extra env var appears in API.md in backticks."""
    missing = sorted(
        name for name in _setting_names() | EXTRA_ENV_VARS if f"`{name}`" not in API_MD
    )
    assert not missing, f"Settings missing from API.md: {missing}"


def test_every_route_documented():
    """Each registered route path appears in API.md in backticks."""
    missing = sorted(path for path in _route_paths() if f"`{path}`" not in API_MD)
    assert not missing, f"Routes missing from API.md: {missing}"


def test_no_unknown_env_vars_documented():
    """API.md names no PW_/MQTT_ variable the server doesn't know about."""
    documented = set(re.findall(r"`((?:PW|MQTT)_[A-Z0-9_]+)`", API_MD))
    known = _setting_names() | EXTRA_ENV_VARS | PROXY_ONLY_ENV_VARS
    unknown = sorted(documented - known)
    assert not unknown, f"API.md documents unknown variables: {unknown}"
