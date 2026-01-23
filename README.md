# PyPowerwall Server

[![License](https://img.shields.io/github/license/jasonacox/pypowerwall-server)](https://img.shields.io/github/license/jasonacox/pypowerwall-server)
[![PyPI version](https://badge.fury.io/py/pypowerwall-server.svg)](https://badge.fury.io/py/pypowerwall-server)
[![Python Version](https://img.shields.io/pypi/pyversions/pypowerwall-server)](https://img.shields.io/pypi/pyversions/pypowerwall-server)
[![PyPI Downloads](https://static.pepy.tech/badge/pypowerwall-server/month)](https://static.pepy.tech/badge/pypowerwall-server/month)

A high-performance FastAPI-based server for monitoring and managing Tesla Powerwall systems. Designed as the next-generation evolution of the [pypowerwall proxy](https://github.com/jasonacox/pypowerwall/tree/main/proxy#pypowerwall-proxy-server) with multi-gateway support, real-time monitoring, and a built-in status console.

<img alt="PyPowerwall Server Console" src="https://github.com/user-attachments/assets/5e64d456-b8fc-4f54-9ba8-e0922e1fea7d" />

## Features

- **Multi-Gateway Support** - Monitor multiple Powerwall installations from a single server with per-gateway configuration and aggregated metrics
- **Connection Modes** - TEDAPI (local), Cloud Mode (remote), and FleetAPI support with automatic failover and graceful degradation
- **Real-Time Updates** - WebSocket streaming with 1-second updates and background polling with intelligent caching
- **Complete API** - Full backward compatibility with pypowerwall proxy plus new multi-gateway and aggregate endpoints
- **Modern Web UI** - Tesla Power Flow animation, management console, and auto-generated API documentation at /docs
- **Container Ready** - Docker and docker-compose support with configurable deployment via environment variables or YAML
- **Planned: MQTT Integration** - Publish metrics to MQTT brokers for Home Assistant and other automation systems

## Quick Start

### Requirements

* You need the Powerwall/Gateway Password (typically found on the QR sticker - behind front panel of PW3 - see [picture](https://github.com/user-attachments/assets/6cf11830-fa70-4ebb-9be7-7d0a5e2db4dc))
* Your computer must be connected to the Powerwall WiFi Access point (it will be IP address 192.168.91.1)

### Docker (Recommended)

```bash
# TEDAPI Mode (Local Gateway) - requires host network to access gateway at 192.168.91.1
docker run -d \
  --name pypowerwall-server \
  --network host \
  -e PW_HOST=192.168.91.1 \
  -e PW_GW_PWD=your_gateway_password \
  jasonacox/pypowerwall-server

# Cloud Mode - connects via internet, includes persistent auth storage
docker run -d \
  --name pypowerwall-server \
  -p 8675:8675 \
  -v ~/.pypowerwall:/auth \
  -e PW_EMAIL="your@email.com" \
  -e PW_AUTHPATH=/auth \
  jasonacox/pypowerwall-server

# Complete Cloud Mode setup (one-time authentication)
docker exec -it pypowerwall-server python -m pypowerwall setup
```

Visit http://localhost:8675

### Multiple Powerwalls

```bash
# Multiple local gateways - requires host network
docker run -d \
  --name pypowerwall-server \
  --network host \
  -e PW_GATEWAYS='[
    {"id": "home", "name": "Home Gateway", "host": "192.168.91.1", "gw_pwd": "gateway_password_1"},
    {"id": "cabin", "name": "Cabin Gateway", "host": "192.168.91.2", "gw_pwd": "gateway_password_2"}
  ]' \
  jasonacox/pypowerwall-server
```

### Command Line

```bash
# Install
pip install pypowerwall-server

# TEDAPI Mode
pypowerwall-server --host 192.168.91.1 --gw-pwd your_gateway_password

# Multiple Powerwalls
pypowerwall-server --config gateways.yaml

# Cloud Mode
pypowerwall-server --setup # one-time setup
pypowerwall-server --email "your@email.com"
```

## Configuration

> **Note**: Most users will use **TEDAPI** to connect to their Powerwall gateway, which is accessible at the standard IP address `192.168.91.1` on your local network. You'll need your gateway password (found in the Tesla app under your gateway settings).

### Cloud Authentication Setup (Optional, for Control Operations)

If you want to control your Powerwall (set reserve level, operating mode, etc.), you'll need Tesla Cloud authentication:

**One-time setup:**
```bash
pip install pypowerwall-server
pypowerwall-server --setup 
```

This will:
1. Open your browser to authenticate with Tesla
2. Generate `.pypowerwall.auth` and `.pypowerwall.site` token files
3. Store them in the default location or a specified directory

### Environment Variables

**Single Gateway Mode (Read-Only via TEDAPI):**
```bash
PW_HOST=192.168.91.1
PW_GW_PWD=your_gateway_password
PW_TIMEZONE=America/Los_Angeles
PW_PORT=8675              # Default port (proxy-compatible)
PW_BIND_ADDRESS=0.0.0.0  # Listen on all interfaces
```

**Single Gateway Mode (With Cloud Control):**
```bash
PW_HOST=192.168.91.1
PW_GW_PWD=your_gateway_password          # For TEDAPI data reads
PW_EMAIL=your-tesla-account@email.com
PW_AUTHPATH=/path/to/auth/files            # Directory with .pypowerwall.auth/.site
PW_TIMEZONE=America/Los_Angeles
```

**Multi-Gateway Mode:**
```bash
PW_GATEWAYS='[
  {
    "id": "home",
    "name": "Home System", 
    "host": "192.168.91.1",
    "gw_pwd": "gw_pwd_1",
    "email": "tesla@email.com",
    "authpath": "/auth"
  },
  {
    "id": "cabin",
    "name": "Cabin System",
    "host": "192.168.91.1",
    "gw_pwd": "gw_pwd_2",
    "email": "tesla@email.com",
    "authpath": "/auth"
  }
]'
```

### Configuration File (gateways.yaml)

```yaml
server:
  host: 0.0.0.0
  port: 8675
  cors_origins:
    - http://localhost:3000

gateways:
  - id: home
    name: Home System
    host: 192.168.91.1
    gw_pwd: gw_pwd_1
    email: tesla@email.com
    authpath: /auth
    timezone: America/Los_Angeles
    
  - id: cabin
    name: Cabin System
    host: 192.168.91.1
    gw_pwd: gw_pwd_2
    email: tesla@email.com
    authpath: /auth
    timezone: America/Denver
    
  - id: cloud-site
    name: Cloud Mode Site
    email: user@example.com
    authpath: /auth
    cloud_mode: true
```

**Authentication:**
- `gw_pwd`: For TEDAPI local gateway access
- `email` + `authpath`: For Tesla Cloud API (control operations)
  - Run `pypowerwall-server --setup` to authenticate and generate auth files
  - Specify directory containing `.pypowerwall.auth` and `.pypowerwall.site` files

## API Endpoints

### Legacy Proxy Compatibility

All existing proxy endpoints work unchanged:

**Core Data Endpoints:**
- `GET /vitals` - Detailed system vitals
- `GET /aggregates` - Power meter aggregates
- `GET /soe` - State of energy (battery %)
- `GET /freq` - Grid frequency data
- `GET /pod` - Battery pod details
- `GET /strings` - Solar string data
- `GET /battery` - Battery information
- `GET /json` - Combined metrics and status (JSON)

**Temperature & Environment:**
- `GET /temps` - All temperature sensors
- `GET /temps/pw` - Powerwall temperatures only

**Alerts & Status:**
- `GET /alerts` - System alerts
- `GET /alerts/pw` - Powerwall alerts only

**Fan Information:**
- `GET /fans` - All fan status
- `GET /fans/pw` - Powerwall fans only

**Data Export:**
- `GET /csv` - CSV format for Telegraf/InfluxDB
- `GET /csv/v2` - Enhanced CSV format

**TEDAPI Raw Access:**
- `GET /tedapi` - TEDAPI endpoint list
- `GET /tedapi/config` - Gateway configuration
- `GET /tedapi/status` - System status
- `GET /tedapi/components` - Component details
- `GET /tedapi/battery` - Battery information
- `GET /tedapi/controller` - Controller data

**Tesla API Endpoints:**
- `GET /api/system_status/soe` - State of energy
- `GET /api/system_status/grid_status` - Grid connection status
- `GET /api/system_status/grid_faults` - Grid fault log
- `GET /api/sitemaster` - Sitemaster information
- `GET /api/meters/aggregates` - Power meters
- `GET /api/status` - System status
- `GET /api/site_info` - Site information
- `GET /api/site_info/site_name` - Site name
- `GET /api/customer/registration` - Customer registration info
- `GET /api/troubleshooting/problems` - Problem list
- `GET /api/auth/toggle/supported` - Auth toggle support
- `GET /api/networks` - Network configuration
- `GET /api/system/networks` - System networks
- `GET /api/powerwalls` - Powerwall device list

**Server Status:**
- `GET /version` - Server and firmware versions
- `GET /stats` - Server statistics (uptime, requests, errors)

**Control Operations (requires authentication):**
- `POST /control/{path}` - Control operations (reserve, mode, etc.)

### Multi-Gateway Endpoints

**Gateway Selection:**
- `GET /api/gateways` - List all configured gateways
- `GET /api/gateways/{id}` - Gateway details
- `GET /api/gateways/{id}/vitals` - Gateway-specific vitals
- `GET /api/gateways/{id}/aggregates` - Gateway-specific power data

**Aggregated Data:**
- `GET /api/aggregate/power` - Combined power across all gateways
- `GET /api/aggregate/soe` - Total battery capacity and charge
- `GET /api/aggregate/status` - Health status of all gateways

**WebSocket Endpoints:**
- `WS /ws/gateway/{id}` - Real-time data stream for specific gateway
- `WS /ws/aggregate` - Real-time aggregated data stream

### Interactive API Documentation

- Swagger UI: http://localhost:8675/docs
- ReDoc: http://localhost:8675/redoc
- OpenAPI JSON: http://localhost:8675/openapi.json

## Design Decisions

### Cloud Authentication with Auth Tokens

The server supports **Tesla Cloud authentication** for control operations:

**TEDAPI (Local)**: For fast data reads from `192.168.91.1`
- Requires: `host` + `password` (gateway password)
- Fast response times (local network)
- No internet dependency
- Used for monitoring metrics

**Cloud (Control)**: For control operations via Tesla API
- Requires: `email` + `authpath`
- Setup: Run `pypowerwall-server --setup` to authenticate
- Generates: `.pypowerwall.auth` and `.pypowerwall.site` token files
- Used for: Setting reserve level, operating mode, etc.

**Configuration:**
```bash
# TEDAPI only (monitoring)
PW_HOST=192.168.91.1
PW_GW_PWD=gateway_password

# TEDAPI + Cloud (monitoring + control)
PW_HOST=192.168.91.1
PW_GW_PWD=gateway_password
PW_EMAIL=tesla@email.com
PW_AUTHPATH=/path/to/auth  # Directory with .pypowerwall.auth/.site files
```

### Async + Sync Library Integration
FastAPI is async, but pypowerwall is synchronous. This is handled using `asyncio.run_in_executor()` to run blocking pypowerwall calls in thread pools, preventing event loop blocking. This is the standard pattern for integrating sync libraries with async frameworks and works perfectly.

### Stateless Server Architecture
The server maintains no persistent state or historical data. All historical data for graphs is stored in **browser localStorage**, allowing:
- Server restarts without data loss (data persists in browser)
- Horizontal scaling (no shared state required)
- Minimal server resource usage
- Simple deployment model

### Control Features & Security
**Default: Read-only** - The server operates in monitoring mode by default.

**Optional Control Mode**: Enable with environment variables:
```bash
CONTROL_ENABLED=true
CONTROL_TOKEN=your-secure-random-token
```

When control is enabled:
- All control operations require authentication via token
- Token must be sent in `Authorization` header
- Legacy POST endpoints are disabled (redirect to `/control` endpoint)
- UI requires authentication for control features

Similar to the proxy server's auth model but token-based for better security.

### Data Aggregation Strategy
Multi-gateway aggregation uses **smart aggregation** that will evolve over time:

Current implementation (v0.1.0):
- Battery %: Simple average (TODO: weighted by capacity)
- Power flows: Simple sum (works for independent systems)
- Grid power: Calculated as site - solar

Future considerations documented in code:
- Capacity-weighted averages
- Different strategies per metric type
- Handling mixed local/cloud gateways
- Time synchronization across gateways
- Outlier detection

This area is expected to need tuning as real-world multi-gateway deployments provide feedback.

### Performance & Caching
- **Polling interval**: 5 seconds (configurable later if needed)
- **WebSocket updates**: Real-time to UI (1-second interval)
- **No server-side caching**: Fresh data on every request
- **Browser caching**: Historical data in localStorage

### UI Framework
Vanilla JavaScript - lightweight, no build step, fast loading. Charts and advanced features can be added incrementally without framework overhead.

## Architecture

```
pypowerwall-server/
├── app/
│   ├── main.py                 # FastAPI application entry point
│   ├── config.py               # Configuration management
│   ├── api/
│   │   ├── __init__.py
│   │   ├── legacy.py           # Legacy proxy endpoints
│   │   ├── gateways.py         # Multi-gateway endpoints
│   │   ├── aggregates.py       # Aggregated data endpoints
│   │   └── websockets.py       # WebSocket handlers
│   ├── core/
│   │   ├── __init__.py
│   │   └── gateway_manager.py  # Connection manager with caching
│   ├── models/
│   │   ├── __init__.py
│   │   └── gateway.py          # All data models
│   ├── utils/
│   │   ├── __init__.py
│   │   └── transform.py        # UI data transformations
│   └── static/
│       ├── index.html          # Management console
│       ├── example.html        # iFrame demo
│       └── powerflow/          # Power flow UI assets
├── tests/
│   ├── conftest.py
│   ├── test_api_aggregates.py
│   ├── test_api_gateways.py
│   ├── test_api_legacy.py
│   ├── test_basic.py
│   ├── test_config.py
│   ├── test_edge_cases.py
│   └── test_gateway_manager.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## Development

### Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements-dev.txt

# Run development server with auto-reload
uvicorn app.main:app --reload --port 8675
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/test_api.py -v
```

### Building Docker Image

```bash
docker build -t pypowerwall-server .
docker run -p 8675:8675 pypowerwall-server
```

## Performance

The server is designed for efficiency with background polling and caching:

- **Cached Responses** - API endpoints return instantly from cache (no pypowerwall blocking)
- **Background Polling** - Default 5-second interval (configurable via PW_CACHE_EXPIRE)
- **Thread Pool** - Sized dynamically: max(10, num_gateways * 3) workers
- **WebSocket Updates** - Push data every 1 second to connected clients
- **Graceful Degradation** - Serves last known good data when gateways are offline
- **Concurrent Gateway Polling** - All gateways polled in parallel using asyncio

## Technology Stack

- **FastAPI** - Modern, fast web framework
- **Uvicorn** - Lightning-fast ASGI server
- **Pydantic** - Data validation and settings management
- **pypowerwall** - Core Powerwall communication library
- **aiohttp** - Async HTTP client for concurrent gateway polling
- **WebSockets** - Real-time data streaming
- **Modern UI** - HTML5, CSS3, Vanilla JavaScript

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - See [LICENSE](LICENSE) for details.

## Support

- **Issues:** https://github.com/jasonacox/pypowerwall-server/issues
- **Discussions:** https://github.com/jasonacox/pypowerwall-server/discussions
- **Wiki:** https://github.com/jasonacox/pypowerwall-server/wiki

