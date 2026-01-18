# CLAUDE.md - AI Agent Guidelines for pypowerwall-server

This document provides guidelines for AI code agents (Claude, Copilot, etc.) working on this codebase.

## Quick Reference

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=app

# Run server locally
PW_EMAIL="user@example.com" ./run.sh

# Build package
python -m build

# Upload to PyPI
./upload.sh
```

## Code Style

This project follows **Black** formatting conventions:

- **Line length**: 88 characters max
- **Quotes**: Double quotes for strings (`"string"` not `'string'`)
- **Indentation**: 4 spaces (no tabs)
- **Trailing commas**: Use in multi-line structures
- **Blank lines**: 2 between top-level definitions, 1 between methods

### Import Order

```python
# 1. Standard library imports
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

# 2. Third-party imports
import pypowerwall
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# 3. Local imports
from app.config import settings
from app.core.gateway_manager import gateway_manager
from app.models.gateway import Gateway, GatewayStatus
```

### Type Hints

Always use type hints for function signatures:

```python
def get_gateway(self, gateway_id: str) -> Optional[GatewayStatus]:
    """Get status for a specific gateway."""
    ...

async def get_vitals() -> dict:
    """Get vitals data."""
    ...
```

## Architecture Patterns

### 1. Singleton Gateway Manager

The `GatewayManager` is a module-level singleton. Never instantiate it directly:

```python
# ✅ Correct
from app.core.gateway_manager import gateway_manager
status = gateway_manager.get_gateway("default")

# ❌ Wrong
manager = GatewayManager()
```

### 2. Cache-First Architecture

All API endpoints read from the in-memory cache populated by background polling. **Never make blocking pypowerwall calls during HTTP requests**:

```python
# ✅ Correct - read from cache
@router.get("/aggregates")
async def get_aggregates():
    status = gateway_manager.get_gateway(gateway_id)
    return status.data.aggregates or {}

# ❌ Wrong - blocking call during request
@router.get("/aggregates")
async def get_aggregates():
    pw = gateway_manager.get_connection(gateway_id)
    return pw.poll("/api/meters/aggregates")  # BLOCKS!
```

### 3. Late Imports for Circular Dependency Avoidance

Import `settings` inside functions when needed to avoid circular imports:

```python
# ✅ Correct - late import
async def _poll_gateway(self, gateway_id: str):
    from app.config import settings  # Import inside function
    if not settings.neg_solar:
        ...

# ❌ Wrong - top-level import causes circular dependency
from app.config import settings  # At module level in gateway_manager.py
```

### 4. ThreadPoolExecutor Bridge

pypowerwall is synchronous. Use the executor to avoid blocking the async event loop:

```python
# ✅ Correct
loop = asyncio.get_running_loop()
result = await asyncio.wait_for(
    loop.run_in_executor(self._executor, pw.poll, "/api/meters/aggregates"),
    timeout=10.0
)

# ❌ Wrong - blocks event loop
result = pw.poll("/api/meters/aggregates")
```

### 5. Explicit API Endpoints

Define all endpoints explicitly. **No catch-all routes**:

```python
# ✅ Correct - explicit endpoints
@router.get("/api/networks")
async def get_networks():
    ...

@router.get("/api/powerwalls")
async def get_powerwalls():
    ...

# ❌ Wrong - catch-all breaks graceful degradation
@router.get("/api/{path:path}")
async def catch_all(path: str):
    return await gateway_manager.call_api("default", "poll", f"/api/{path}")
```

### 6. Safe Defaults

Return empty collections on errors, not exceptions:

```python
# ✅ Correct - safe default
if not status or not status.data:
    return {}
return status.data.vitals or {}

# ❌ Wrong - raises exception
if not status:
    raise HTTPException(status_code=503, detail="Gateway offline")
```

### 7. Data Transformations at Fetch Time

Apply data transformations (like `neg_solar` correction) when data is fetched, not on every request:

```python
# ✅ Correct - transform once at fetch time (in gateway_manager._poll_gateway)
if aggregates and not settings.neg_solar:
    if aggregates.get("solar", {}).get("instant_power", 0) < 0:
        aggregates["load"]["instant_power"] -= aggregates["solar"]["instant_power"]
        aggregates["solar"]["instant_power"] = 0

# ❌ Wrong - transform on every request (wasteful, requires deepcopy)
@router.get("/aggregates")
async def get_aggregates():
    aggregates = deepcopy(status.data.aggregates)  # Expensive!
    if not settings.neg_solar and solar < 0:
        ...
```

## Configuration

### Environment Variables

All configuration uses `PW_` prefix for consistency with pypowerwall proxy:

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_HOST` | None | Gateway IP address |
| `PW_GW_PWD` | None | Gateway WiFi password |
| `PW_EMAIL` | None | Tesla account email |
| `PW_PORT` | 8675 | Server port |
| `PW_CACHE_EXPIRE` | 5 | Polling interval (seconds) |
| `PW_NEG_SOLAR` | yes | Allow negative solar values |
| `PW_GRACEFUL_DEGRADATION` | yes | Serve stale data when offline |

### Pydantic Settings

Use `Field` with `validation_alias` for environment variable mapping:

```python
class Settings(BaseSettings):
    server_port: int = Field(
        default=8675,
        validation_alias=AliasChoices("PW_PORT", "PORT")
    )
```

## Testing

### Test Structure

```
tests/
├── conftest.py          # Fixtures and mocks
├── test_api_*.py        # API endpoint tests
├── test_gateway_manager.py
├── test_config.py
└── test_edge_cases.py
```

### Key Fixtures

```python
@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)

@pytest.fixture
def connected_gateway(mock_gateway_manager, mock_pypowerwall):
    """Add a connected gateway to the manager."""
    ...
```

### Async Tests

Use `@pytest.mark.asyncio` for async test functions:

```python
@pytest.mark.asyncio
async def test_polling_updates_gateway_data(mock_gateway_manager):
    await mock_gateway_manager._poll_gateway("test-gateway")
    assert status.online is True
```

## Documentation

### Docstrings

Use Google-style docstrings:

```python
def get_gateway(self, gateway_id: str) -> Optional[GatewayStatus]:
    """Get status for a specific gateway with graceful degradation support.
    
    Args:
        gateway_id: Gateway identifier
        
    Returns:
        GatewayStatus object or None if gateway not found
        
    Example:
        status = gateway_manager.get_gateway("home")
        if status and status.online:
            print(f"Battery: {status.data.soe}%")
    """
```

### Module Docstrings

Every module should have a docstring explaining its purpose:

```python
"""
Gateway Manager - Manages connections to multiple Powerwall gateways.

This is the central hub of the server that manages all pypowerwall connections,
performs background polling, caches data, and provides fast API responses.

Architecture:
    - Singleton pattern (single gateway_manager instance)
    - Background polling task runs every PW_CACHE_EXPIRE seconds
    ...
"""
```

## Common Pitfalls

### 1. Forgetting Late Import

```python
# This will fail with "local variable 'settings' referenced before assignment"
async def _poll_gateway(self, gateway_id: str):
    if settings.neg_solar:  # ❌ settings not imported!
        ...
```

### 2. Mutating Cached Data

```python
# ❌ This mutates the cache!
aggregates = status.data.aggregates
aggregates["solar"]["instant_power"] = 0  # Affects all future reads!

# ✅ Copy first if you need to modify
from copy import deepcopy
aggregates = deepcopy(status.data.aggregates)
```

### 3. Blocking the Event Loop

```python
# ❌ This blocks all other requests
result = pw.poll("/api/meters/aggregates")

# ✅ Use executor
result = await loop.run_in_executor(self._executor, pw.poll, path)
```

### 4. Missing Timeout Protection

```python
# ❌ Can hang forever
result = await loop.run_in_executor(self._executor, pw.poll, path)

# ✅ Always use timeout
result = await asyncio.wait_for(
    loop.run_in_executor(self._executor, pw.poll, path),
    timeout=10.0
)
```

## File Overview

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app, lifespan, middleware, CLI |
| `app/config.py` | Pydantic settings, environment variables |
| `app/core/gateway_manager.py` | Connection management, polling, caching |
| `app/api/legacy.py` | Backward-compatible proxy endpoints |
| `app/api/gateways.py` | Multi-gateway REST API |
| `app/api/aggregates.py` | Combined data from all gateways |
| `app/api/websockets.py` | Real-time streaming |
| `app/models/gateway.py` | Pydantic models |
| `app/utils/transform.py` | Static file serving |
| `app/utils/stats_tracker.py` | Request statistics |

## Version Management

Update version in **both** files when releasing:

1. `app/config.py`: `SERVER_VERSION = "x.y.z"`
2. `pyproject.toml`: `version = "x.y.z"`

Then update `RELEASE.md` with changes.
