# pypowerwall-server Design Document

This document describes the architecture, design patterns, and implementation details of pypowerwall-server.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Component Diagram](#component-diagram)
3. [Data Flow](#data-flow)
4. [Module Reference](#module-reference)
5. [Key Design Patterns](#key-design-patterns)
6. [Threading & Async Model](#threading--async-model)
7. [Graceful Degradation](#graceful-degradation)
8. [Configuration System](#configuration-system)
9. [Improvements & Recommendations](#improvements--recommendations)

---

## Architecture Overview

pypowerwall-server is a FastAPI-based HTTP proxy server that provides a unified REST and WebSocket API for Tesla Powerwall systems. It wraps the `pypowerwall` library to handle:

- **Multi-gateway support**: Monitor multiple Powerwall installations simultaneously
- **Background polling**: Non-blocking data collection at configurable intervals  
- **Graceful degradation**: Serve cached data during network outages
- **Legacy compatibility**: Drop-in replacement for the original pypowerwall proxy

```mermaid
graph TB
    subgraph "Clients"
        UI[Web UI / Dashboard]
        API[API Consumers]
        WS[WebSocket Clients]
    end
    
    subgraph "pypowerwall-server"
        FW[FastAPI Web Server]
        GM[Gateway Manager<br/>Singleton]
        CFG[Settings<br/>Pydantic BaseSettings]
        
        subgraph "API Routers"
            LR[Legacy Router<br/>/aggregates, /soe, /csv...]
            GR[Gateways Router<br/>/api/gateways/*]
            AR[Aggregates Router<br/>/api/aggregate/*]
            WSR[WebSocket Router<br/>/ws/*]
        end
        
        subgraph "Background Tasks"
            POLL[Polling Loop<br/>asyncio.gather]
            EXEC[ThreadPoolExecutor<br/>Blocking pypowerwall calls]
        end
        
        CACHE[(In-Memory Cache<br/>Dict: gateway_id to GatewayStatus)]
    end
    
    subgraph "External Systems"
        PW1[Powerwall Gateway 1<br/>TEDAPI/Local]
        PW2[Powerwall Gateway 2<br/>Cloud API]
        PW3[Powerwall Gateway N<br/>FleetAPI]
    end
    
    UI --> FW
    API --> FW
    WS --> WSR
    
    FW --> LR
    FW --> GR
    FW --> AR
    
    LR --> GM
    GR --> GM
    AR --> GM
    WSR --> GM
    
    GM --> CACHE
    GM --> POLL
    POLL --> EXEC
    EXEC --> PW1
    EXEC --> PW2
    EXEC --> PW3
    
    CFG --> GM
    CFG --> FW
```

---

## Component Diagram

```mermaid
classDiagram
    class FastAPIApp {
        +lifespan context manager
        +middleware: request tracking
        +routers: legacy, gateways, aggregates, websockets
        +static file serving
        +CLI entry point
    }
    
    class GatewayManager {
        -gateways: Dict~str, Gateway~
        -connections: Dict~str, Powerwall~
        -cache: Dict~str, GatewayStatus~
        -_executor: ThreadPoolExecutor
        -_consecutive_failures: Dict~str, int~
        -_next_poll_time: Dict~str, float~
        -_last_successful_data: Dict~str, PowerwallData~
        +initialize() void
        +shutdown() void
        +get_gateway(id) GatewayStatus
        +get_all_gateways() Dict
        +call_api(id, method) Any
        +call_tedapi(id, method) Any
        +get_aggregate_data() AggregateData
        -_poll_gateways() void
        -_poll_gateway(id) void
    }
    
    class Settings {
        +server_host: str
        +server_port: int
        +cache_expire: int
        +timeout: int
        +graceful_degradation: bool
        +cache_ttl: int
        +neg_solar: bool
        +gateways: List~GatewayConfig~
        +_initialize_gateways()
    }
    
    class Gateway {
        +id: str
        +name: str
        +host: str
        +gw_pwd: str
        +email: str
        +cloud_mode: bool
        +fleetapi: bool
        +online: bool
        +last_error: str
    }
    
    class PowerwallData {
        +aggregates: Dict
        +vitals: Dict
        +strings: Dict
        +alerts: List
        +soe: float
        +freq: float
        +version: str
        +timestamp: float
    }
    
    class GatewayStatus {
        +gateway: Gateway
        +data: PowerwallData
        +online: bool
        +last_updated: float
        +error: str
    }
    
    class AggregateData {
        +total_battery_percent: float
        +total_site_power: float
        +total_solar_power: float
        +num_gateways: int
        +num_online: int
        +gateways: Dict
    }
    
    class StatsTracker {
        -_gets: int
        -_posts: int
        -_errors: int
        -_uri_counts: Dict
        +record_request()
        +get_stats()
    }
    
    FastAPIApp --> GatewayManager : uses
    FastAPIApp --> Settings : configured by
    GatewayManager --> Gateway : manages
    GatewayManager --> GatewayStatus : caches
    GatewayManager --> AggregateData : computes
    GatewayStatus --> Gateway : contains
    GatewayStatus --> PowerwallData : contains
    Settings --> Gateway : creates
    FastAPIApp --> StatsTracker : uses
```

---

## Data Flow

### Request Flow (REST API)

```mermaid
sequenceDiagram
    participant C as Client
    participant M as Middleware
    participant R as Router
    participant GM as GatewayManager
    participant Cache as Cache Dict
    
    C->>M: GET /aggregates
    M->>M: Track request (stats_tracker)
    M->>R: Forward request
    R->>GM: get_gateway("default")
    GM->>Cache: Lookup cached status
    
    alt Cache Hit + Online
        Cache-->>GM: GatewayStatus (online=true)
        GM-->>R: Return data
    else Cache Hit + Offline (Graceful Degradation)
        Cache-->>GM: GatewayStatus (online=false)
        GM->>GM: Check TTL
        alt Within TTL
            GM-->>R: Return stale data
        else Expired
            GM-->>R: Return null data
        end
    else No Data
        GM-->>R: Return empty {}
    end
    
    R-->>M: JSON Response
    M-->>C: HTTP 200
```

### Background Polling Flow

```mermaid
sequenceDiagram
    participant Loop as Polling Loop
    participant GM as GatewayManager
    participant Exec as ThreadPoolExecutor
    participant PW as pypowerwall.Powerwall
    participant Cache as Cache Dict
    
    loop Every 5s
        Loop->>GM: _poll_gateways()
        
        par For each gateway
            GM->>GM: Check backoff time
            alt Backoff expired
                GM->>Exec: run_in_executor(pw.poll)
                Exec->>PW: poll("/api/meters/aggregates")
                PW-->>Exec: aggregates data
                Exec-->>GM: aggregates result
                
                GM->>Exec: run_in_executor(pw.level)
                PW-->>GM: SOE value
                
                Note over GM: Fetch vitals, strings,<br/>alerts, temps... (optional)
                
                GM->>Cache: Update GatewayStatus
                GM->>GM: Reset backoff counters
            else Within backoff
                Note over GM: Skip this gateway
            end
        end
    end
```

### WebSocket Streaming Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant WS as WebSocket Handler
    participant CM as ConnectionManager
    participant GM as GatewayManager
    
    C->>WS: Connect /ws/aggregate
    WS->>CM: connect(websocket)
    CM->>CM: Add to active_connections
    WS-->>C: Connection Accepted
    
    loop Every 1s
        WS->>GM: get_aggregate_data()
        GM-->>WS: AggregateData
        WS->>C: send_json(data)
    end
    
    alt Client Disconnects
        C--xWS: Disconnect
        WS->>CM: disconnect(websocket)
    else Error
        WS->>CM: disconnect(websocket)
    end
```

---

## Module Reference

### Core Modules

| Module | Purpose | Key Exports |
|--------|---------|-------------|
| `app/main.py` | FastAPI app, lifespan, CLI | `app`, `cli()` |
| `app/config.py` | Configuration via env vars | `settings`, `Settings`, `GatewayConfig` |
| `app/core/gateway_manager.py` | Connection pooling, polling | `gateway_manager` (singleton) |

### API Routers

| Router | Prefix | Purpose |
|--------|--------|---------|
| `app/api/legacy.py` | None (root) | Backward-compatible proxy endpoints |
| `app/api/gateways.py` | `/api/gateways` | Multi-gateway management |
| `app/api/aggregates.py` | `/api/aggregate` | Combined data from all gateways |
| `app/api/websockets.py` | `/ws` | Real-time streaming |

### Models

| Model | Purpose |
|-------|---------|
| `Gateway` | Connection configuration (host, credentials, mode) |
| `PowerwallData` | Telemetry snapshot (aggregates, vitals, soe, etc.) |
| `GatewayStatus` | Gateway config + data + online status |
| `AggregateData` | Combined metrics from all gateways |

### Utilities

| Module | Purpose |
|--------|---------|
| `app/utils/transform.py` | Static file serving, JS injection |
| `app/utils/stats_tracker.py` | Request statistics for /stats endpoint |

---

## Key Design Patterns

### 1. Singleton Gateway Manager

The `GatewayManager` is a global singleton that centralizes all pypowerwall interactions:

```python
# gateway_manager.py
gateway_manager = GatewayManager()  # Module-level singleton

# Usage everywhere
from app.core.gateway_manager import gateway_manager
status = gateway_manager.get_gateway("default")
```

**Rationale**: Ensures single connection pool, unified cache, and coordinated polling.

### 2. Cache-First Architecture

All API endpoints read from an in-memory cache populated by background polling:

```python
@router.get("/aggregates")
async def get_aggregates():
    status = gateway_manager.get_gateway(gateway_id)
    return status.data.aggregates or {}  # Always returns from cache
```

**Benefits**:
- Instant response times (no blocking I/O during requests)
- Graceful degradation when gateways are offline
- Protects pypowerwall from request storms

### 3. Exponential Backoff

Failed connections use progressive retry delays to prevent hammering:

```python
backoff_intervals = [5, 10, 30, 60, 120]  # seconds
failure_count = self._consecutive_failures[gateway_id]
backoff_seconds = backoff_intervals[min(failure_count - 1, 4)]
```

### 4. ThreadPoolExecutor Bridge

pypowerwall uses blocking I/O, but FastAPI is async. The executor bridges this:

```python
result = await asyncio.wait_for(
    loop.run_in_executor(self._executor, lambda: pw.poll(path)),
    timeout=5.0
)
```

### 5. Pydantic Settings

Configuration uses Pydantic BaseSettings for validation and env var binding:

```python
class Settings(BaseSettings):
    server_port: int = Field(default=8675, validation_alias=AliasChoices('PW_PORT', 'PORT'))
```

---

## Threading & Async Model

```mermaid
graph LR
    subgraph "Main Thread (asyncio)"
        EVL[Event Loop]
        FW[FastAPI Routes]
        BG[Background Polling Task]
    end
    
    subgraph "ThreadPoolExecutor"
        T1[Worker Thread 1]
        T2[Worker Thread 2]
        TN[Worker Thread N]
    end
    
    EVL --> FW
    EVL --> BG
    BG -->|run_in_executor| T1
    BG -->|run_in_executor| T2
    BG -->|run_in_executor| TN
    
    T1 -->|pypowerwall.poll| PW1[Gateway 1]
    T2 -->|pypowerwall.poll| PW2[Gateway 2]
    TN -->|pypowerwall.poll| PWN[Gateway N]
```

**Thread Pool Sizing**:
```python
max_workers = max(4, len(self._pending_configs) * 2)
```

**Why This Model**:
- pypowerwall is synchronous (uses `requests` library)
- FastAPI/uvicorn is async (must not block event loop)
- `run_in_executor` allows async code to call blocking functions

---

## Graceful Degradation

When a gateway goes offline, the system continues serving data:

```mermaid
stateDiagram-v2
    [*] --> Online: Gateway Connected
    Online --> Offline: Connection Lost
    Offline --> Degraded: PW_GRACEFUL_DEGRADATION=yes
    Offline --> NoData: PW_GRACEFUL_DEGRADATION=no
    Degraded --> Online: Reconnected
    Degraded --> NoData: TTL Expired
    NoData --> Online: Reconnected
    
    state Degraded {
        [*] --> ServingStale
        ServingStale: Return last good data
        ServingStale: Track data age
    }
    
    state NoData {
        [*] --> ReturningEmpty
        ReturningEmpty: Return {} or null
        ReturningEmpty: Indicate offline status
    }
```

**Implementation**:
```python
def get_gateway(self, gateway_id: str):
    status = self.cache.get(gateway_id)
    if status.online:
        return status
    
    if settings.graceful_degradation:
        last_success = self._last_successful_data.get(gateway_id)
        if last_success and (now - last_success.timestamp) <= settings.cache_ttl:
            return GatewayStatus(data=last_success, online=False)
    
    return status  # No data
```

---

## Configuration System

```mermaid
graph TD
    subgraph "Environment Variables"
        PW_HOST[PW_HOST]
        PW_GW_PWD[PW_GW_PWD]
        PW_GATEWAYS[PW_GATEWAYS JSON]
        PW_PORT[PW_PORT]
        PW_TIMEOUT[PW_TIMEOUT]
    end
    
    subgraph "Pydantic Settings"
        Settings[Settings Class]
        GC[GatewayConfig List]
    end
    
    subgraph "Gateway Manager"
        GM[GatewayManager]
        GW[Gateway Objects]
    end
    
    PW_HOST --> Settings
    PW_GW_PWD --> Settings
    PW_GATEWAYS --> Settings
    PW_PORT --> Settings
    PW_TIMEOUT --> Settings
    
    Settings -->|_initialize_gateways| GC
    GC --> GM
    GM --> GW
```

**Gateway Configuration Methods**:

1. **Legacy Single Gateway** (backward compatible):
   ```bash
   PW_HOST=192.168.91.1
   PW_GW_PWD=password
   ```

2. **Multi-Gateway JSON**:
   ```bash
   PW_GATEWAYS='[{"id":"home","host":"192.168.91.1","gw_pwd":"pw1"},{"id":"cabin","host":"10.0.0.5","gw_pwd":"pw2"}]'
   ```

---

## Improvements & Recommendations

### Critical Issues

#### 1. ⚠️ No Rate Limiting on Control Endpoints

**Location**: [app/api/legacy.py](app/api/legacy.py#L73-L87)

**Issue**: The `/control/{path}` endpoint accepts any POST with valid auth token but has no rate limiting. A compromised token could flood the Powerwall with commands.

**Recommendation**:
```python
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@router.post("/control/{path:path}")
@limiter.limit("10/minute")
async def control_api(path: str, ...):
```

#### 2. ⚠️ Import Statement Inside Function

**Location**: [app/core/gateway_manager.py](app/core/gateway_manager.py#L400-L420)

**Issue**: `import json` appears inside the polling loop, executed on every poll cycle:

```python
elif networks_result and isinstance(networks_result, str):
    import json  # <-- Repeated import
    try:
        data.networks = json.loads(networks_result)
```

**Recommendation**: Move to top of file with other imports.

#### 3. ⚠️ Thread Safety Concern in StatsTracker

**Location**: [app/utils/stats_tracker.py](app/utils/stats_tracker.py)

**Issue**: While `_lock` is used for counter updates, the `_uri_counts` defaultdict could theoretically have race conditions during dictionary growth. This is low-risk due to Python's GIL but worth noting.

**Recommendation**: Consider using `collections.Counter` or ensure lock is held during dict operations.

### Architectural Improvements

#### 1. Consider Connection Health Monitoring

**Current**: Failures are tracked but not exposed via health endpoint.

**Recommendation**: Add `/health/gateways` endpoint that returns per-gateway health metrics:
```json
{
  "gateways": {
    "home": {"status": "healthy", "latency_ms": 45, "consecutive_failures": 0},
    "cabin": {"status": "degraded", "latency_ms": null, "consecutive_failures": 3}
  }
}
```

#### 2. Structured Logging

**Current**: Uses Python's basic logging with string formatting.

**Recommendation**: Use structured logging (JSON format) for better log aggregation:
```python
import structlog
logger = structlog.get_logger()
logger.info("gateway_poll_complete", gateway_id=gw_id, latency_ms=elapsed)
```

### Suboptimal Patterns

#### 1. Repeated Default Gateway Lookup

**Location**: Every legacy endpoint calls `get_default_gateway()`:

```python
@router.get("/vitals")
async def get_vitals():
    gateway_id = get_default_gateway()  # Called every request
```

**Recommendation**: Cache the default gateway ID or use dependency injection:
```python
def get_default_gateway_dep() -> str:
    return get_default_gateway()

@router.get("/vitals")
async def get_vitals(gateway_id: str = Depends(get_default_gateway_dep)):
```

#### 2. Deep Copy on Every Aggregates Request

**Location**: [app/api/legacy.py](app/api/legacy.py#L128)

```python
aggregates = deepcopy(status.data.aggregates)
```

**Issue**: `deepcopy` is expensive; called on every `/aggregates` request.

**Recommendation**: Only copy if modifying (neg_solar correction):
```python
aggregates = status.data.aggregates
if not settings.neg_solar and aggregates.get('solar', {}).get('instant_power', 0) < 0:
    aggregates = deepcopy(aggregates)  # Copy only when needed
    # ... modify
```

#### 3. Polling Loop Runs Even With No Gateways

**Location**: [app/core/gateway_manager.py](app/core/gateway_manager.py#L192-L205)

**Issue**: The polling loop starts at server boot even if no gateways are configured.

**Recommendation**: Check for gateways before starting loop:
```python
if not self.gateways:
    logger.warning("No gateways configured, polling disabled")
    return
```

### Unused Code

#### 1. `counter` Field in Stats

**Location**: [app/api/legacy.py](app/api/legacy.py#L918)

```python
"counter": 0,  # Legacy field, not used
```

This field exists for compatibility but is never populated. Consider documenting why or removing if legacy support isn't needed.

#### 2. `din` Field Type Variance

**Location**: [app/models/gateway.py](app/models/gateway.py#L109)

```python
din: Optional[Union[str, Dict[str, Any]]] = None
```

The `din` field accepts both `str` and `Dict`, but it's unclear when a Dict would be returned. This may be defensive typing that could be simplified.

### Testing Gaps

1. **No WebSocket Tests**: The `websockets.py` router has no corresponding test file.

2. **No Integration Tests**: All tests use mocked pypowerwall; no tests against real or simulated gateways.

3. **No Load Tests**: No benchmarks for concurrent request handling or WebSocket scaling.

---

## Summary

pypowerwall-server is a well-structured FastAPI application with solid async architecture and thoughtful graceful degradation. The main areas for improvement are:

| Priority | Area | Effort |
|----------|------|--------|
| High | Rate limiting on control endpoints | Low |
| Medium | Move imports to top of file | Trivial |
| Medium | Structured logging | Medium |
| Low | Optimize deepcopy usage | Low |
| Low | Add WebSocket tests | Medium |

The codebase demonstrates good practices:
- ✅ Comprehensive docstrings
- ✅ Type hints throughout
- ✅ Pydantic models for validation
- ✅ Singleton pattern for shared state
- ✅ Graceful degradation for resilience
- ✅ Exponential backoff for reliability
