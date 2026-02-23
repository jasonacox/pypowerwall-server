"""
PyPowerwall Server - Main FastAPI Application

A modern, high-performance server for monitoring Tesla Powerwall systems
with support for multiple gateways and real-time data streaming.

Standard Configuration (TEDAPI):
    Most users will connect to their Powerwall gateway using TEDAPI at the
    standard IP address 192.168.91.1 with their gateway Wi-Fi password.
    
    Environment variables:
        PW_HOST=192.168.91.1
        PW_GW_PWD=your_gateway_wifi_password
        
    For control operations, authenticate with Tesla Cloud:
        python3 -m pypowerwall setup
        PW_EMAIL=tesla@email.com
        PW_AUTHPATH=/path/to/auth/files

Routing Structure:
    Routes are organized to avoid conflicts:
    
    1. Direct app routes (registered on main app):
       - GET  /              -> Tesla Power Flow animation UI
       - GET  /console       -> Management console UI
       - GET  /example       -> iFrame demo page
       - GET  /example.html  -> Same as /example
       - GET  /favicon-*.png -> Favicon files
    
    2. Legacy proxy compatibility (no prefix):
       - GET  /aggregates, /soe, /csv, /vitals, /strings, etc.
       - GET  /version, /stats, /api/*, etc.
       - POST /control/*     -> Control operations
       
    3. Multi-gateway API (prefix: /api/gateways):
       - GET  /api/gateways/              -> List all gateways
       - GET  /api/gateways/{id}          -> Get gateway status
       - POST /api/gateways/{id}/control  -> Control operations
       
    4. Aggregate data API (prefix: /api/aggregate):
       - GET  /api/aggregate/battery      -> Aggregated battery data
       - GET  /api/aggregate/power        -> Aggregated power data
       
    5. WebSocket streaming (prefix: /ws):
       - WS   /ws/gateway/{id}            -> Real-time gateway data
       - WS   /ws/aggregate               -> Real-time aggregate data
    
    6. Static files:
       - /static/*                        -> Static assets (CSS, JS, images)
    
    Note: FastAPI will raise an error at startup if routes conflict.
    The @app.get("/") route does NOT conflict with router.get("/") 
    because routers use prefixes or have no "/" route defined.
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings, SERVER_VERSION
from app.api import legacy, gateways, aggregates, websockets
from app.core.gateway_manager import gateway_manager
from app.utils.transform import get_static
from app.utils.stats_tracker import stats_tracker

# Configure logging based on PW_DEBUG setting
log_level = logging.DEBUG if settings.debug else logging.INFO
logging.basicConfig(
    level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class _SuppressWebSocketConnectionMessages(logging.Filter):
    """Filter out websocket connection noise on uvicorn.error.

    Uvicorn injects its 'uvicorn.error' logger into the websockets library,
    so these messages all flow through that logger at INFO level:
      - "connection open"
      - "connection closed"
      - '10.x.x.x - "WebSocket /ws/..." [accepted]'

    We suppress only those specific patterns, leaving real errors and all
    other INFO messages untouched.
    """

    _EXACT = frozenset(["connection open", "connection closed"])

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO:
            return True
        msg = record.getMessage()
        if msg in self._EXACT:
            return False
        if 'WebSocket' in msg and msg.endswith('[accepted]'):
            return False
        return True


# Suppress noisy log output unless debug mode is on:
#   uvicorn.access: "GET /api/..." request lines
#   uvicorn.error filter: "connection open", "connection closed", WebSocket [accepted]
if not settings.debug:
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").addFilter(_SuppressWebSocketConnectionMessages())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info(f"Starting PyPowerwall Server v{SERVER_VERSION}...")
    logger.info(f"Configured for {len(settings.gateways)} gateway(s)")
    logger.info(f"Polling interval (PW_CACHE_EXPIRE): {settings.cache_expire}s")
    logger.info(f"Timeout (PW_TIMEOUT): {settings.timeout}s")
    logger.info(f"Server listening on {settings.server_host}:{settings.server_port}")
    if _proxy_base:
        logger.info(f"Reverse proxy base path (PROXY_BASE_URL): {_proxy_base}")

    # Initialize gateway manager
    await gateway_manager.initialize(
        settings.gateways, poll_interval=settings.cache_expire
    )
    logger.info(f"Initialized {len(gateway_manager.gateways)} gateway(s)")

    for gateway_id, gateway in gateway_manager.gateways.items():
        # Show appropriate connection info based on mode
        if gateway.fleetapi:
            mode_info = f"FleetAPI: {gateway.site_id or gateway.email}"
        elif gateway.cloud_mode:
            mode_info = f"Cloud Mode: {gateway.site_id or gateway.email}"
        else:
            mode_info = gateway.host or "TEDAPI"
        logger.info(f"  - {gateway_id}: {gateway.name} ({mode_info})")

    yield

    # Shutdown
    logger.info("Shutting down PyPowerwall Server...")
    await gateway_manager.shutdown()


# Normalize PROXY_BASE_URL: strip trailing slash, keep leading slash (or empty string)
# e.g. "/" -> "", "/powerwall" -> "/powerwall", "/powerwall/" -> "/powerwall"
_proxy_base = settings.proxy_base_url.rstrip("/") if settings.proxy_base_url != "/" else ""

# Create FastAPI application
# NOTE: Do NOT set root_path=_proxy_base here. FastAPI's __call__ injects root_path
# into scope["root_path"] for every request. Starlette 0.46+ uses root_path in
# Mount.matches() child_scope AND in StaticFiles.get_path() → get_route_path().
# When nginx strips the proxy prefix before forwarding (trailing-slash proxy_pass),
# the path arrives WITHOUT the prefix (e.g. "/static/powerflow/app.css").
# If root_path="/pypowerwall" is also in scope, Mount sets child root_path to
# "/pypowerwall/static", then get_route_path returns the un-stripped
#  "/static/powerflow/app.css", and StaticFiles tries to serve
# "static/powerflow/app.css" relative to app/static/ → "app/static/static/..."
# which doesn't exist → 404.  Removing root_path here keeps scope["root_path"]
# empty so the Mount child scope is just "/static" and get_route_path correctly
# strips that prefix, yielding "powerflow/app.css" → correct file.
app = FastAPI(
    title="PyPowerwall Server",
    description="Modern FastAPI server for Tesla Powerwall monitoring with multi-gateway support",
    version=SERVER_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    # When behind a proxy sub-path, set openapi_url so the Swagger/ReDoc UIs
    # fetch the spec from the prefixed path rather than a root-relative /openapi.json
    # that would escape the proxy base (e.g. route to Grafana at /).
    openapi_url=f"{_proxy_base}/openapi.json" if _proxy_base else "/openapi.json",
    lifespan=lifespan,
)

# Configure CORS.
# The CORS spec forbids allow_credentials=True combined with allow_origins=["*"].
# However, the powerflow app.js runs in iframes on different origins and sends
# cookies (AuthCookie/UserRecord injected by track_requests), making every request
# credentialed.  Credentialed requests require the exact origin reflected back —
# not a wildcard — plus Access-Control-Allow-Credentials: true.
#
# Solution: when CORS_ORIGINS is the default wildcard, use allow_origin_regex=".*"
# instead.  Starlette then reflects the actual request Origin and sets credentials,
# satisfying the browser for both plain and credentialed cross-origin requests.
# When specific origins are configured via CORS_ORIGINS, use those directly.
_cors_wildcard = "*" in settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if _cors_wildcard else settings.cors_origins,
    allow_origin_regex=".*" if _cors_wildcard else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Cookie max-age for powerflow web app auth compatibility (issue #7)
# Use a 10-year lifetime so the cookie never expires on long-running kiosk dashboards.
_AUTH_COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60  # 10 years


# Reverse proxy path prefix stripping (PROXY_BASE_URL)
# Mirrors the old pypowerwall proxy behavior: strip the base path prefix from every
# incoming request before routing so that e.g. /powerwall/aggregates is handled
# identically to /aggregates.  Only active when PROXY_BASE_URL is set to a non-root
# value (i.e. anything other than "/").
if _proxy_base:
    _proxy_base_bytes = _proxy_base.encode("latin-1")

    @app.middleware("http")
    async def strip_proxy_prefix(request: Request, call_next):
        """Strip PROXY_BASE_URL prefix from request paths for reverse proxy support."""
        path = request.scope["path"]
        if path == _proxy_base or path.startswith(_proxy_base + "/"):
            new_path = path[len(_proxy_base):] or "/"
            request.scope["path"] = new_path
            # Slice the original raw_path bytes to preserve percent-encoding
            # rather than re-encoding the decoded string, which can corrupt
            # non-ASCII or already-encoded characters.
            raw = request.scope["raw_path"]
            request.scope["raw_path"] = raw[len(_proxy_base_bytes):] or b"/"
        return await call_next(request)


# Add request tracking middleware
@app.middleware("http")
async def track_requests(request: Request, call_next):
    """Track request statistics and inject auth cookies for powerflow compatibility.

    Injects AuthCookie and UserRecord on every successful response so the Tesla
    Gateway web app (served from /powerflow) never shows the login screen.
    This mirrors the behavior of the original pypowerwall proxy (issue #7).
    """
    try:
        response = await call_next(request)

        # Record the request with status code
        # URIs are only tracked for successful requests (200-399)
        # This prevents memory exhaustion from DDOS attacks with random URLs
        stats_tracker.record_request(
            request.method, request.url.path, response.status_code
        )

        # Record errors (4xx, 5xx status codes)
        if response.status_code >= 400:
            stats_tracker.record_error()

        # Inject auth cookies for powerflow web app compatibility (issue #7).
        # The Tesla Gateway web UI checks for AuthCookie and shows a login screen
        # when it is absent or expired.  We set it here (matching the original proxy)
        # so the cookie is always present with a fresh max-age.  Only injected on
        # successful responses (2xx/3xx) and only when the endpoint has not already
        # set it (e.g. POST /api/login/Basic sets its own Set-Cookie header).
        if response.status_code < 400:
            existing_set_cookie = [
                v
                for k, v in response.headers.items()
                if k.lower() == "set-cookie" and "AuthCookie" in v
            ]
            if not existing_set_cookie:
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

        return response
    except Exception as e:
        # Record error and re-raise
        stats_tracker.record_error()
        raise


# Static files path (used by mounts and prefixed static route below)
static_path = Path(__file__).parent / "static"

# Include API routers
# NOTE: Order matters! More specific routers (gateways, aggregates) must be
# included BEFORE legacy router so that /api/gateways/* and /api/aggregate/*
# routes are not shadowed by legacy endpoints that share the /api/* path prefix.
app.include_router(gateways.router, prefix="/api/gateways", tags=["Gateways"])
app.include_router(aggregates.router, prefix="/api/aggregate", tags=["Aggregates"])
app.include_router(websockets.router, prefix="/ws", tags=["WebSockets"])

app.include_router(legacy.router, tags=["Legacy Proxy Compatibility"])

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Serve favicon.ico from static files."""
    favicon_path = static_path / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/x-icon")
    # Fall back to 32x32 PNG if .ico doesn't exist
    png_path = static_path / "favicon-32x32.png"
    if png_path.exists():
        return FileResponse(png_path, media_type="image/png")
    return Response(status_code=404)


@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def root(request: Request, style: str = None):
    """Serve the Power Flow animation (Tesla Powerwall interface).

    Args:
        style: Optional style override (e.g., ?style=clear). If not provided,
               uses PW_STYLE environment variable setting.
    """
    # Use powerflow directory for Power Flow animation
    web_root = str(Path(__file__).parent / "static" / "powerflow")

    # Use style from query parameter or fall back to settings (PW_STYLE environment variable)
    # Options: clear, grafana, grafana-dark, solar, white, black, dakboard
    if style:
        style_name = style
        style_file = f"{style}.js"
    else:
        style_name = settings.style
        style_file = f"{settings.style}.js"

    # Get the index.html using get_static
    request_path = "/index.html"
    fcontent, ftype = get_static(web_root, request_path)

    if fcontent:
        # Get gateway status for variable replacement
        gateway_id = None
        if gateway_manager.gateways:
            gateway_id = list(gateway_manager.gateways.keys())[0]

        status_data = {"version": "", "git_hash": ""}
        if gateway_id:
            status = gateway_manager.get_gateway(gateway_id)
            if status and status.data:
                status_data = {
                    "version": status.data.version or "",
                    "git_hash": "",
                }

        # Convert fcontent to string for replacements
        content = fcontent.decode("utf-8")

        # Replace template variables
        content = content.replace("{VERSION}", status_data.get("version", ""))
        content = content.replace("{HASH}", status_data.get("git_hash", ""))
        content = content.replace("{EMAIL}", "")
        content = content.replace("{THEME_CLASS}", f"pypowerwall-theme-{style_name}")

        # Build absolute API base URL from request.
        # When behind an HTTPS reverse proxy (e.g. nginx), the backend sees
        # requests as plain HTTP.  Honour X-Forwarded-Proto / X-Forwarded-Host
        # so the injected {API_BASE_URL} uses the correct scheme, avoiding
        # Mixed Content errors in the browser.
        #
        # nginx's $host variable strips the port; $http_host preserves it.
        # If nginx sends X-Forwarded-Host without a port (common with $host),
        # check X-Forwarded-Port and re-attach the port so the powerflow app.js
        # calls back through the same proxy rather than the bare origin port.
        scheme = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
        )
        host = (
            request.headers.get("x-forwarded-host")
            or request.url.netloc
        )
        # Re-attach non-standard port when X-Forwarded-Host was set without one
        fwd_port = request.headers.get("x-forwarded-port")
        if fwd_port and ":" not in host:
            standard = ("443" if scheme == "https" else "80")
            if fwd_port != standard:
                host = f"{host}:{fwd_port}"
        api_base_url = f"{scheme}://{host}{_proxy_base}/api"

        # Set up asset prefix for static files - needs trailing slash for webpack chunk loading.
        # Prepend proxy base so webpack public path (s.p = window.appPrefix) resolves chunks
        # correctly when the server is mounted under a sub-path (PROXY_BASE_URL).
        static_asset_prefix = f"{_proxy_base}/static/powerflow/"
        content = content.replace("{STYLE}", static_asset_prefix + style_file)
        content = content.replace("{THEME_NAME}", style_name)
        content = content.replace("{ASSET_PREFIX}", static_asset_prefix)
        content = content.replace("{API_BASE_URL}", api_base_url)
        content = content.replace("{PROXY_BASE}", _proxy_base)

        # When running under a proxy sub-path (PROXY_BASE_URL), inject a fetch
        # monkey-patch so app.js root-relative calls like /stats and /version
        # get the prefix prepended automatically.
        if _proxy_base:
            pf_proxy_script = f"""<script>
(function() {{
    var _BASE = "{_proxy_base}";
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (typeof url === 'string' && url.charAt(0) === '/' && url.indexOf(_BASE) !== 0)
            url = _BASE + url;
        return _origFetch.apply(this, opts !== undefined ? [url, opts] : [url]);
    }};
}})();
</script>"""
        else:
            pf_proxy_script = ""
        content = content.replace("{PROXY_BASE_SCRIPT}", pf_proxy_script)

        return HTMLResponse(content=content)

    # Fallback if proxy web files not found
    b = _proxy_base
    return HTMLResponse(
        content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>PyPowerwall Server</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                }}
                h1 {{ color: #e31937; }}
                a {{ color: #0066cc; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <h1>PyPowerwall Server</h1>
            <p>Power Flow animation not available. Install pypowerwall proxy web files.</p>
            <h2>Quick Links</h2>
            <ul>
                <li><a href="{b}/console">Management Console</a></li>
                <li><a href="{b}/docs">API Documentation (Swagger UI)</a></li>
                <li><a href="{b}/redoc">API Documentation (ReDoc)</a></li>
            </ul>
        </body>
        </html>
    """
    )


@app.get("/console", response_class=HTMLResponse, tags=["UI"])
async def console():
    """Serve the management console UI."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        content = index_path.read_text()
        # When running under a proxy sub-path (PROXY_BASE_URL), inject a fetch
        # monkey-patch so all root-relative API calls get the prefix prepended
        # automatically, without modifying every call site in index.html.
        if _proxy_base:
            proxy_base_script = f"""<script>
(function() {{
    var _BASE = "{_proxy_base}";
    window._BASE = _BASE;
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (typeof url === 'string' && url.charAt(0) === '/' && url.indexOf(_BASE) !== 0)
            url = _BASE + url;
        return _origFetch.apply(this, opts !== undefined ? [url, opts] : [url]);
    }};
}})();
</script>"""
            # Fix WebSocket URL which is built in JS as a template literal
            content = content.replace(
                "/ws/aggregate",
                f"{_proxy_base}/ws/aggregate",
            )
        else:
            proxy_base_script = ""
        content = content.replace("{PROXY_BASE_SCRIPT}", proxy_base_script)
        content = content.replace("{PROXY_BASE}", _proxy_base)
        return HTMLResponse(content=content)
    b = _proxy_base
    return HTMLResponse(
        content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>PyPowerwall Server</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                }}
                h1 {{ color: #e31937; }}
                a {{ color: #0066cc; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <h1>PyPowerwall Server</h1>
            <p>Welcome to PyPowerwall Server - A modern FastAPI-based monitoring solution for Tesla Powerwall.</p>
            <h2>Quick Links</h2>
            <ul>
                <li><a href="{b}/docs">API Documentation (Swagger UI)</a></li>
                <li><a href="{b}/redoc">API Documentation (ReDoc)</a></li>
                <li><a href="{b}/api/gateways">List Gateways</a></li>
                <li><a href="{b}/vitals">Vitals (Legacy)</a></li>
                <li><a href="{b}/aggregates">Aggregates (Legacy)</a></li>
            </ul>
            <p><a href="{b}/">← Back to Power Flow</a></p>
        </body>
        </html>
    """
    )


@app.get("/example", response_class=HTMLResponse, tags=["UI"])
@app.get("/example.html", response_class=HTMLResponse, tags=["UI"])
async def example():
    """Serve the Power Flow iFrame example page."""
    example_path = Path(__file__).parent / "static" / "example.html"
    if example_path.exists():
        content = example_path.read_text()
        content = content.replace("{PROXY_BASE}", _proxy_base)
        return HTMLResponse(content=content)
    return HTMLResponse(
        content="""
        <!DOCTYPE html>
        <html>
        <head><title>Example Not Found</title></head>
        <body>
            <h1>Example page not found</h1>
            <p><a href="/">← Back to Power Flow</a></p>
        </body>
        </html>
    """
    )


@app.get("/favicon-32x32.png", tags=["Static"])
@app.get("/favicon-16x16.png", tags=["Static"])
async def favicon_png(request: Request):
    """Serve favicon files."""
    filename = request.url.path.lstrip("/")
    favicon_path = Path(__file__).parent / "static" / filename
    if favicon_path.exists():
        return FileResponse(favicon_path)
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/%5Bobject%20Object%5D", include_in_schema=False)
@app.get("/[object Object]", include_in_schema=False)
async def handle_malformed_object_url():
    """Handle malformed [object Object] URLs from vendor.js bug.

    Returns empty object to prevent 404 errors in logs.
    """
    return {}


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint with actual gateway status.

    Returns:
        - healthy: All gateways online
        - degraded: Some gateways online, some offline
        - unhealthy: All gateways offline
    """
    total = len(gateway_manager.gateways)

    if total == 0:
        return {
            "status": "no_gateways",
            "version": SERVER_VERSION,
            "gateways": 0,
            "gateway_ids": [],
        }

    # Count online gateways
    online_count = 0
    gateway_details = []

    for gateway_id in gateway_manager.gateways.keys():
        status = gateway_manager.get_gateway(gateway_id)
        is_online = status.online if status else False
        if is_online:
            online_count += 1

        gateway_details.append(
            {
                "id": gateway_id,
                "online": is_online,
                "error": status.error if status and status.error else None,
            }
        )

    # Determine overall health
    if online_count == total:
        health_status = "healthy"
    elif online_count > 0:
        health_status = "degraded"
    else:
        health_status = "unhealthy"

    return {
        "status": health_status,
        "version": SERVER_VERSION,
        "gateways": total,
        "gateways_online": online_count,
        "gateways_offline": total - online_count,
        "gateway_ids": list(gateway_manager.gateways.keys()),
        "gateway_details": gateway_details,
    }


def cli():
    """Command-line interface for pypowerwall-server.

    Supports environment variables and command-line arguments for configuration.
    Command-line arguments override environment variables.

    Examples:
        pypowerwall-server
        pypowerwall-server --host 192.168.91.1 --gw-pwd mypassword
        pypowerwall-server --port 8080 --debug
        pypowerwall-server --config /path/to/config.json
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="PyPowerwall Server - Monitor and manage Tesla Powerwall systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  PW_HOST            Powerwall gateway IP (default: 192.168.91.1)
  PW_GW_PWD          Gateway Wi-Fi password (required for TEDAPI)
  PW_EMAIL           Tesla account email (for Cloud/FleetAPI)
  PW_PASSWORD        Tesla account password (deprecated, use setup)
  PW_AUTHPATH        Path to store authentication files (default: .)
  PW_STYLE           Theme style (default: clear)
  PW_SITEID          Specific site ID (for multiple sites)
  PW_CACHE_EXPIRE    Polling interval in seconds (default: 5)
  PW_TIMEOUT         Request timeout in seconds (default: 5)
  PW_DEBUG           Enable debug logging (default: false)
  PW_PORT            Server port (default: 8675)
  PW_BIND_ADDRESS    Server bind address (default: 0.0.0.0)
  PW_CONFIG          Path to JSON configuration file
  
For more information, visit: https://github.com/jasonacox/pypowerwall-server
        """,
    )

    parser.add_argument(
        "--version", action="version", version=f"pypowerwall-server {SERVER_VERSION}"
    )
    parser.add_argument(
        "--setup", action="store_true", help="Run Tesla Cloud authentication setup"
    )
    parser.add_argument("--host", help="Powerwall gateway IP address")
    parser.add_argument("--gw-pwd", dest="gw_pwd", help="Gateway Wi-Fi password")
    parser.add_argument("--email", help="Tesla account email")
    parser.add_argument("--password", help="Tesla account password (deprecated)")
    parser.add_argument("--authpath", help="Path to authentication files")
    parser.add_argument("--style", help="UI theme style")
    parser.add_argument("--siteid", help="Specific site ID")
    parser.add_argument("--cache-expire", type=int, help="Polling interval in seconds")
    parser.add_argument("--timeout", type=int, help="Request timeout in seconds")
    parser.add_argument("--port", type=int, help="Server port (default: 8675)")
    parser.add_argument(
        "--bind-address",
        dest="bind_address",
        help="Server bind address (default: 0.0.0.0)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--config", help="Path to JSON configuration file")
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload for development"
    )

    args = parser.parse_args()

    # Handle setup mode
    if args.setup:
        print(f"PyPowerwall Server v{SERVER_VERSION} - Cloud Authentication Setup")
        print()
        print("This will authenticate with Tesla Cloud and generate auth token files.")
        print()
        try:
            import subprocess

            # Call python -m pypowerwall setup
            result = subprocess.run(
                [sys.executable, "-m", "pypowerwall", "setup"], check=True
            )
            print()
            print("✓ Setup complete!")
            print()
            print("Auth files created. You can now use PW_EMAIL and PW_AUTHPATH")
            print("to enable Cloud mode control operations.")
            sys.exit(result.returncode)
        except subprocess.CalledProcessError as e:
            print()
            print(f"✗ Setup failed with exit code {e.returncode}")
            sys.exit(e.returncode)
        except Exception as e:
            print()
            print(f"✗ Setup failed: {e}")
            sys.exit(1)

    # Override environment variables with command-line arguments
    if args.host:
        os.environ["PW_HOST"] = args.host
    if args.gw_pwd:
        os.environ["PW_GW_PWD"] = args.gw_pwd
    if args.email:
        os.environ["PW_EMAIL"] = args.email
    if args.password:
        os.environ["PW_PASSWORD"] = args.password
    if args.authpath:
        os.environ["PW_AUTHPATH"] = args.authpath
    if args.style:
        os.environ["PW_STYLE"] = args.style
    if args.siteid:
        os.environ["PW_SITEID"] = args.siteid
    if args.cache_expire:
        os.environ["PW_CACHE_EXPIRE"] = str(args.cache_expire)
    if args.timeout:
        os.environ["PW_TIMEOUT"] = str(args.timeout)
    if args.port:
        os.environ["PW_PORT"] = str(args.port)
    if args.bind_address:
        os.environ["PW_BIND_ADDRESS"] = args.bind_address
    if args.debug:
        os.environ["PW_DEBUG"] = "true"
    if args.config:
        os.environ["PW_CONFIG"] = args.config

    # Reload settings to pick up CLI overrides
    from app.config import settings

    settings.__init__()

    # Start server
    import uvicorn

    print(f"Starting PyPowerwall Server v{SERVER_VERSION}")
    print(f"Server will listen on http://{settings.server_host}:{settings.server_port}")
    print(f"Console UI: http://{settings.server_host}:{settings.server_port}/console")
    print(f"API Docs: http://{settings.server_host}:{settings.server_port}/docs")
    print()

    try:
        uvicorn.run(
            "app.main:app",
            host=settings.server_host,
            port=settings.server_port,
            reload=args.reload,
        )
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)


if __name__ == "__main__":
    cli()
