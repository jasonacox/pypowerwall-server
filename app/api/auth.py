"""
Shared authentication helpers for API routers.

Control (write) operations across all routers are gated by PW_CONTROL_SECRET.
Keeping the check in one place ensures every write surface enforces the same
rules — see the "no unauthenticated writes" principle in AGENTS.md.
"""
import hmac
from typing import Optional

from fastapi import Header, HTTPException

from app.config import settings


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    """The token in an Authorization header: "Bearer <token>" or the bare token."""
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]
    return authorization


def token_matches(token: Optional[str]) -> bool:
    """Constant-time check of a control token against PW_CONTROL_SECRET.

    Compares bytes: hmac.compare_digest raises TypeError on non-ASCII str,
    which would turn a bad token into an HTTP 500.
    """
    secret = settings.control_secret
    if not token or not secret:
        return False
    return hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))


def verify_control_token(authorization: Optional[str] = Header(None)):
    """Verify control token for authenticated operations."""
    if not settings.control_enabled or not settings.control_secret:
        raise HTTPException(status_code=403, detail="Control features not enabled")

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    if not token_matches(bearer_token(authorization)):
        raise HTTPException(status_code=401, detail="Invalid control token")

    return True
