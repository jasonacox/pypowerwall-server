"""Control helpers shared by the HTTP API, the MQTT controls and the gateway
manager (kept here so none of them imports another's module for them)."""
from typing import Any


def is_error_result(result: object) -> bool:
    """True when a library response reports an error instead of a value."""
    return isinstance(result, dict) and ("error" in result or "ERROR" in result)


def is_v1r_gateway(gateway: Any, data: Any) -> bool:
    """True when a gateway uses the v1r transport (signed-command capable).

    Fail-closed like the Console gate (``tedapi_mode === 'v1r'``): the RSA
    key marks a v1r connection, but an unknown/unresolved mode (cold start,
    cloud failover) must NOT pass — otherwise islanding commands could be
    dispatched on a transport that cannot sign them. The library's signed
    islanding command works on Powerwall 2 and 3 alike, so hardware must
    NOT gate it — otherwise PW2 v1r users lose a control they have today.
    Shared by discovery (which buttons to announce) and the control loop
    (which islanding commands to accept).
    """
    try:
        if gateway is None or not getattr(gateway, "rsa_key_configured", False):
            return False
        if data is None:
            return False
        return getattr(data, "tedapi_mode", None) == "v1r"
    except Exception:
        return False
