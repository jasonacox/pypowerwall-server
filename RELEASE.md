# Release Notes

This document tracks all notable changes to PyPowerwall Server.

## Version Format

This project follows [Semantic Versioning](https://semver.org/):
- **MAJOR** version for incompatible API changes
- **MINOR** version for new functionality in a backward compatible manner
- **PATCH** version for backward compatible bug fixes

## [Unreleased]

### Added
- None yet

### Changed
- None yet

### Fixed
- None yet

---

## Version History

### [0.1.0] - Initial Release

Initial release of PyPowerwall Server as next-generation evolution of pypowerwall proxy.

**Core Features:**
- Multi-gateway support for monitoring multiple Powerwall installations
- Background polling with intelligent caching (5-second default interval)
- Graceful degradation when gateways are temporarily offline
- WebSocket streaming for real-time updates (1-second intervals)
- Full backward compatibility with pypowerwall proxy endpoints
- TEDAPI, Cloud Mode, and FleetAPI connection support
- Tesla Power Flow animation UI with real-time updates
- Management console for gateway status
- Auto-generated API documentation (Swagger UI and ReDoc)
- Health monitoring endpoint: `/health`
- Comprehensive test suite with pytest
- Docker and docker-compose support
- Configuration via environment variables or YAML file

**API Endpoints:**
- Legacy proxy endpoints (backward compatible): `/vitals`, `/aggregates`, `/soe`, `/csv`, etc.
- Multi-gateway endpoints: `/api/gateways/*`
- Aggregate data endpoints: `/api/aggregate/*`
- WebSocket endpoints: `/ws/gateway/{id}` and `/ws/aggregate`

**Architecture:**
- FastAPI-based async server with sync pypowerwall integration
- ThreadPoolExecutor for non-blocking pypowerwall calls
- Exponential backoff for failed gateway connections
- Lazy initialization of pypowerwall connections
- Stateless server design (historical data in browser localStorage)
- Cached responses for instant API access
- Concurrent gateway polling using asyncio
- Dynamic thread pool sizing: max(10, num_gateways * 3)
- Automatic cleanup of dead WebSocket connections

**Connection Modes:**
- TEDAPI (local gateway access)
- Cloud Mode (remote access)
- FleetAPI support

**Deployment:**
- Docker and docker-compose
- Environment variable configuration
- YAML configuration file support

---

## Planned Features

### Future Releases

**MQTT Integration**
- Publish metrics to MQTT brokers
- Home Assistant MQTT discovery
- Configurable topic patterns and message formats

**Enhanced UI**
- Historical data visualization
- Multi-gateway dashboard
- Gateway comparison views
- Dark/light theme switching

**Performance**
- Configurable polling intervals per gateway
- Advanced caching strategies
- Metrics and monitoring

**Control Features**
- Enhanced control operations
- Batch control across multiple gateways
- Scheduling and automation

---

## Migration Notes

### From pypowerwall proxy

PyPowerwall Server is a drop-in replacement:
- All proxy API endpoints work unchanged
- Same environment variables supported
- No changes needed to Telegraf/Grafana integrations
- Simply change Docker image name

### Breaking Changes

None - Full backward compatibility maintained.

---

## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for development guidelines and how to submit changes.

## Support

- **Issues:** https://github.com/jasonacox/pypowerwall-server/issues
- **Discussions:** https://github.com/jasonacox/pypowerwall-server/discussions
- **Wiki:** https://github.com/jasonacox/pypowerwall-server/wiki
