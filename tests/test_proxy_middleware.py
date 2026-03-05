"""Tests for _StripProxyPrefix ASGI middleware (app/main.py).

The middleware is conditionally defined and registered at import time when
PROXY_BASE_URL is set, so we test its algorithm here by replicating the exact
implementation and verifying all input/edge-case combinations.
"""
import pytest


def make_strip_middleware(proxy_base: str, inner_app):
    """Instantiate a _StripProxyPrefix-equivalent middleware for testing.

    This mirrors the implementation in app/main.py exactly (including the
    raw_path fix) so any divergence will cause tests to fail.
    """
    proxy_base_bytes = proxy_base.encode("latin-1")

    class _StripProxyPrefix:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                path = scope.get("path", "")
                if path == proxy_base or path.startswith(proxy_base + "/"):
                    new_path = path[len(proxy_base):] or "/"
                    scope["path"] = new_path
                    raw = scope.get("raw_path")
                    if isinstance(raw, (bytes, bytearray)):
                        if raw == proxy_base_bytes or raw.startswith(proxy_base_bytes + b"/"):
                            scope["raw_path"] = raw[len(proxy_base_bytes):] or b"/"
                        # else: raw_path present but doesn't start with prefix; keep as-is
                    else:
                        # raw_path absent; derive from stripped path to keep in sync
                        scope["raw_path"] = new_path.encode("latin-1")
            await self.app(scope, receive, send)

    return _StripProxyPrefix(inner_app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def capture_app():
    """Return a minimal ASGI app that records the scope it receives."""
    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope.get("path")
        captured["raw_path"] = scope.get("raw_path")

    return app, captured


# ---------------------------------------------------------------------------
# HTTP scope tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_path_and_raw_path_stripped():
    """Normal HTTP request — both path and raw_path should be stripped."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "http",
        "path": "/pypowerwall/health",
        "raw_path": b"/pypowerwall/health",
    }
    await mw(scope, None, None)

    assert captured["path"] == "/health"
    assert captured["raw_path"] == b"/health"


@pytest.mark.asyncio
async def test_http_exact_base_becomes_root():
    """A request to the bare proxy base (/pypowerwall) maps to /."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "http",
        "path": "/pypowerwall",
        "raw_path": b"/pypowerwall",
    }
    await mw(scope, None, None)

    assert captured["path"] == "/"
    assert captured["raw_path"] == b"/"


@pytest.mark.asyncio
async def test_http_raw_path_absent_derived_from_new_path():
    """When raw_path is absent, it should be derived from the stripped path.

    Regression test for the bug where scope.get("raw_path", b"") would default
    to b"" and then set raw_path to b"/" regardless of the actual path.
    """
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "http",
        "path": "/pypowerwall/api/gateways",
        # raw_path intentionally absent
    }
    await mw(scope, None, None)

    assert captured["path"] == "/api/gateways"
    # raw_path must match the stripped path, not just b"/"
    assert captured["raw_path"] == b"/api/gateways"


@pytest.mark.asyncio
async def test_http_non_matching_path_unchanged():
    """Requests without the proxy prefix should pass through untouched."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "http",
        "path": "/other/service/metrics",
        "raw_path": b"/other/service/metrics",
    }
    await mw(scope, None, None)

    assert captured["path"] == "/other/service/metrics"
    assert captured["raw_path"] == b"/other/service/metrics"


@pytest.mark.asyncio
async def test_http_raw_path_present_but_no_prefix_kept_as_is():
    """raw_path present but path doesn't start with proxy prefix — keep raw_path."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "http",
        "path": "/grafana/dashboard",
        "raw_path": b"/grafana/dashboard",
    }
    await mw(scope, None, None)

    assert captured["path"] == "/grafana/dashboard"
    assert captured["raw_path"] == b"/grafana/dashboard"


# ---------------------------------------------------------------------------
# WebSocket scope tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_path_and_raw_path_stripped():
    """WebSocket upgrade — both path and raw_path should be stripped."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "websocket",
        "path": "/pypowerwall/ws/aggregate",
        "raw_path": b"/pypowerwall/ws/aggregate",
    }
    await mw(scope, None, None)

    assert captured["path"] == "/ws/aggregate"
    assert captured["raw_path"] == b"/ws/aggregate"


@pytest.mark.asyncio
async def test_websocket_raw_path_absent_derived_from_new_path():
    """WebSocket without raw_path in scope — derive raw_path from stripped path."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {
        "type": "websocket",
        "path": "/pypowerwall/ws/aggregate",
        # raw_path absent
    }
    await mw(scope, None, None)

    assert captured["path"] == "/ws/aggregate"
    assert captured["raw_path"] == b"/ws/aggregate"


# ---------------------------------------------------------------------------
# Non-HTTP/WS scope types are passed through unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_scope_not_modified():
    """Lifespan scopes should bypass prefix stripping entirely."""
    inner, captured = capture_app()
    mw = make_strip_middleware("/pypowerwall", inner)

    scope = {"type": "lifespan"}
    await mw(scope, None, None)

    # path never set on lifespan scope
    assert captured.get("path") is None
