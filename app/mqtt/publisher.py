"""
MQTT Publisher — pushes Powerwall telemetry to an MQTT broker.

Enabled by setting MQTT_HOST in environment (see app/config.py for full variable list).
When MQTT_HOST is not set this module is completely inert — no imports of aiomqtt
happen, no background task is started, and no code paths in the poll loop are changed.

Architecture
------------
A single long-running asyncio task (_connection_loop) maintains a persistent
connection to the broker with automatic reconnect and exponential backoff.
After each successful gateway poll, gateway_manager calls:

    asyncio.create_task(mqtt_publisher.publish_gateway(gateway_id, status))

The task is fire-and-forget: MQTT failures are logged at DEBUG level and never
propagate back to the poll loop, so HTTP API reliability is unaffected.

Thread safety
-------------
The publisher runs entirely within the asyncio event loop.  No threading locks
are required because all state mutations happen from coroutines (single thread).
The _connected flag and _client reference are read/written only from coroutines.

Reconnect strategy
------------------
The connection loop uses exponential backoff: 2 s, 4 s, 8 s … capped at 60 s.
On each successful publish failure, _connected is set to False; the connection
loop detects this on its next 5-second heartbeat and tears down the context
manager, triggering the outer reconnect logic.

Topic layout
------------
    {prefix}/{gateway_id}/battery         float  — Tesla-scaled SOE %
    {prefix}/{gateway_id}/battery_raw     float  — raw SOE %
    {prefix}/{gateway_id}/solar           float  — W (positive = producing)
    {prefix}/{gateway_id}/grid            float  — W (positive = importing)
    {prefix}/{gateway_id}/home            float  — W
    {prefix}/{gateway_id}/powerwall       float  — W (positive = discharging)
    {prefix}/{gateway_id}/grid_status     str    — "UP" | "DOWN" | "unknown"
    {prefix}/{gateway_id}/mode            str    — operation mode
    {prefix}/{gateway_id}/reserve         float  — backup reserve %
    {prefix}/{gateway_id}/total_capacity  int    — total battery capacity (Wh)
    {prefix}/{gateway_id}/current_charge  int    — current battery charge (Wh)
    {prefix}/{gateway_id}/online          str    — "true" | "false"
    {prefix}/{gateway_id}/aggregates      JSON   — full aggregates dict
    {prefix}/{gateway_id}/status          JSON   — summary dict
    {prefix}/{gateway_id}/availability    str    — "online" | "offline" (LWT)

    {prefix}/{gateway_id}/grid_energy_imported     int — Wh, lifetime grid import (whole Wh, no decimals)
    {prefix}/{gateway_id}/grid_energy_exported     int — Wh, lifetime grid export (whole Wh, no decimals)
    {prefix}/{gateway_id}/home_energy_imported     int — Wh, lifetime home consumption (whole Wh, no decimals)
    {prefix}/{gateway_id}/solar_energy_exported    int — Wh, lifetime solar production (whole Wh, no decimals)
    {prefix}/{gateway_id}/battery_energy_imported  int — Wh, lifetime battery charged (whole Wh, no decimals)
    {prefix}/{gateway_id}/battery_energy_exported  int — Wh, lifetime battery discharged (whole Wh, no decimals)

    {prefix}/{gateway_id}/strings/{A-F}/voltage   float — V
    {prefix}/{gateway_id}/strings/{A-F}/current   float — A
    {prefix}/{gateway_id}/strings/{A-F}/power     float — W
    {prefix}/{gateway_id}/strings/{A-F}           JSON  — full string data

    {prefix}/{gateway_id}/strings/{AB,CD,EF}/voltage  float — V (from first string in pair)
    {prefix}/{gateway_id}/strings/{AB,CD,EF}/current  float — A (sum of pair)
    {prefix}/{gateway_id}/strings/{AB,CD,EF}/power    float — W (sum of pair)

    Multi-PW3 single-gateway: also AB1/CD1/EF1, AB2/CD2/EF2 etc.
"""
import asyncio
import json
import logging
import ssl
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


class MqttPublisher:
    """Async MQTT publisher with persistent connection and reconnect logic."""

    def __init__(self):
        self._client = None              # aiomqtt.Client instance (inside context)
        self._connected: bool = False    # True only while inside active async with
        self._connection_task: Optional[asyncio.Task] = None
        self._shutdown: bool = False
        self._discovery_sent: Set[str] = set()   # gateway IDs with discovery published
        self._backoff: int = 2           # current reconnect backoff in seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when MQTT_HOST is configured."""
        from app.config import settings  # late import — avoids circular deps
        return settings.mqtt_enabled

    @property
    def connected(self) -> bool:
        """True when the broker connection is currently active."""
        return self._connected

    async def start(self) -> None:
        """Start the background connection task.  Called from main.py lifespan."""
        if not self.enabled:
            return
        self._shutdown = False
        self._connection_task = asyncio.create_task(
            self._connection_loop(), name="mqtt-connection"
        )
        logger.info("MQTT publisher starting...")

    async def publish_offline(self, gateway_ids: List[str]) -> None:
        """Mark per-gateway availability topics offline (used at shutdown).

        The broker LWT only covers the global availability topic; without
        this, per-gateway topics stay 'online' after a clean shutdown and HA
        keeps showing stale retained sensor values as live.
        """
        if not self.enabled or not self._connected or self._client is None:
            return
        from app.config import settings  # late import

        for gateway_id in gateway_ids:
            await self._safe_publish(
                f"{settings.mqtt_topic_prefix}/{gateway_id}/availability",
                "offline",
                settings.mqtt_retain,
                settings.mqtt_qos,
            )

    async def stop(self) -> None:
        """Gracefully stop the publisher.  Called from main.py lifespan shutdown."""
        if not self.enabled:
            return
        self._shutdown = True
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        logger.info("MQTT publisher stopped.")

    async def _publish_ha_discovery(self, gateway_id: str, status) -> None:
        """Publish Home Assistant auto-discovery payloads for a gateway.

        Called once per gateway on first connection (tracked in _discovery_sent).
        Re-sent after every broker reconnect so HA re-discovers after restarts.

        Args:
            gateway_id: Gateway identifier.
            status:     GatewayStatus used to extract name and version.
        """
        if not self._connected or self._client is None:
            return
        try:
            from app.config import settings  # late import
            from app.mqtt.ha_discovery import build_discovery_payloads

            gateway_name = (
                status.gateway.name
                if status.gateway and status.gateway.name
                else gateway_id
            )
            version = status.data.version if status.data else None

            string_ids = None
            if status.data and status.data.strings and isinstance(status.data.strings, dict):
                string_ids = list(status.data.strings.keys())

            payloads = build_discovery_payloads(
                gateway_id=gateway_id,
                gateway_name=gateway_name,
                topic_prefix=settings.mqtt_topic_prefix,
                ha_prefix=settings.mqtt_ha_prefix,
                version=version,
                string_ids=string_ids,
            )
            for topic, payload in payloads:
                await self._safe_publish(topic, payload, retain=True, qos=settings.mqtt_qos)

            logger.info(
                f"MQTT HA discovery published for gateway '{gateway_id}' "
                f"({len(payloads)} entities)"
            )
        except Exception as e:
            logger.debug(f"MQTT HA discovery error for {gateway_id}: {e}")

    async def publish_gateway(self, gateway_id: str, status) -> None:
        """Publish all sensor topics for a single gateway after a successful poll.

        This is called from gateway_manager._poll_gateway() via create_task(),
        so it runs as a fire-and-forget coroutine.  All exceptions are swallowed.

        Args:
            gateway_id: Gateway identifier (used as sub-topic component).
            status:     GatewayStatus object with current data.
        """
        if not self._connected or self._client is None:
            return

        # Send HA discovery payloads the first time we see this gateway
        # (also re-sent after reconnect since _discovery_sent is cleared there)
        if gateway_id not in self._discovery_sent:
            from app.config import settings  # late import
            if settings.mqtt_ha_discovery:
                await self._publish_ha_discovery(gateway_id, status)
            self._discovery_sent.add(gateway_id)

        try:
            from app.config import settings  # late import
            prefix = f"{settings.mqtt_topic_prefix}/{gateway_id}"
            qos = settings.mqtt_qos
            retain = settings.mqtt_retain

            data = status.data

            # --- scalar sensor topics ---
            await self._safe_publish(
                f"{prefix}/online",
                "true" if status.online else "false",
                retain, qos,
            )

            # Gateway friendly name (from gateways.yaml)
            if status.gateway and status.gateway.name:
                await self._safe_publish(
                    f"{prefix}/name", status.gateway.name, retain, qos
                )

            if data is not None:
                # Battery state-of-energy
                if data.soe is not None:
                    await self._safe_publish(
                        f"{prefix}/battery", f"{data.soe:.1f}", retain, qos
                    )
                if data.soe_raw is not None:
                    await self._safe_publish(
                        f"{prefix}/battery_raw", f"{data.soe_raw:.1f}", retain, qos
                    )

                # Battery energy state from the cached system status.  These
                # values are in Wh and represent the whole battery system for
                # this gateway (not an individual battery block).
                total_capacity = _extract_battery_energy(
                    data.system_status, "nominal_full_pack_energy"
                )
                current_charge = _extract_battery_energy(
                    data.system_status, "nominal_energy_remaining"
                )
                if total_capacity is not None:
                    await self._safe_publish(
                        f"{prefix}/total_capacity",
                        f"{total_capacity:.0f}",
                        retain,
                        qos,
                    )
                if current_charge is not None:
                    await self._safe_publish(
                        f"{prefix}/current_charge",
                        f"{current_charge:.0f}",
                        retain,
                        qos,
                    )

                # Power flow from aggregates
                if data.aggregates:
                    agg = data.aggregates
                    solar = _extract_power(agg, "solar")
                    grid = _extract_power(agg, "site")
                    home = _extract_power(agg, "load")
                    pw_power = _extract_power(agg, "battery")

                    if solar is not None:
                        await self._safe_publish(
                            f"{prefix}/solar", f"{solar:.1f}", retain, qos
                        )
                    if grid is not None:
                        await self._safe_publish(
                            f"{prefix}/grid", f"{grid:.1f}", retain, qos
                        )
                    if home is not None:
                        await self._safe_publish(
                            f"{prefix}/home", f"{home:.1f}", retain, qos
                        )
                    if pw_power is not None:
                        await self._safe_publish(
                            f"{prefix}/powerwall", f"{pw_power:.1f}", retain, qos
                        )

                    # Full aggregates JSON (useful for Node-RED, InfluxDB, etc.)
                    await self._safe_publish(
                        f"{prefix}/aggregates",
                        json.dumps(agg),
                        retain, qos,
                    )

                    # Lifetime energy accumulators (Wh).  PW3/TEDAPI gateways
                    # get these overlaid onto aggregates by pypowerwall>=0.16.5
                    # (native gateway endpoint); PW2/local mode has always
                    # carried them.  Topic names follow the server's scalar
                    # power convention (site -> grid, load -> home) and mirror
                    # exactly what /api/meters/aggregates reports — including
                    # 0 on gateways whose firmware lacks the endpoint.
                    for topic_suffix, section, field in (
                        ("grid_energy_imported", "site", "energy_imported"),
                        ("grid_energy_exported", "site", "energy_exported"),
                        ("home_energy_imported", "load", "energy_imported"),
                        ("solar_energy_exported", "solar", "energy_exported"),
                        ("battery_energy_imported", "battery", "energy_imported"),
                        ("battery_energy_exported", "battery", "energy_exported"),
                    ):
                        energy_val = _extract_energy(agg, section, field)
                        if energy_val is not None:
                            await self._safe_publish(
                                f"{prefix}/{topic_suffix}",
                                f"{energy_val:.0f}",
                                retain, qos,
                            )

                if data.grid_status is not None:
                    await self._safe_publish(
                        f"{prefix}/grid_status",
                        str(data.grid_status),
                        retain, qos,
                    )

                if data.mode is not None:
                    await self._safe_publish(
                        f"{prefix}/mode", str(data.mode), retain, qos
                    )

                if data.reserve is not None:
                    await self._safe_publish(
                        f"{prefix}/reserve", f"{data.reserve:.1f}", retain, qos
                    )

                if data.version is not None:
                    await self._safe_publish(
                        f"{prefix}/version", str(data.version), retain, qos
                    )

                # Solar string topics (voltage, current, power per string)
                if data.strings and isinstance(data.strings, dict):
                    strings_prefix = f"{prefix}/strings"
                    for string_id, string_data in data.strings.items():
                        if not isinstance(string_data, dict):
                            continue
                        s_prefix = f"{strings_prefix}/{string_id}"
                        for metric in ("Voltage", "Current", "Power"):
                            val = string_data.get(metric)
                            if val is not None:
                                try:
                                    await self._safe_publish(
                                        f"{s_prefix}/{metric.lower()}",
                                        f"{float(val):.2f}",
                                        retain, qos,
                                    )
                                except (ValueError, TypeError):
                                    pass
                        # Full string JSON for consumers that want everything
                        await self._safe_publish(
                            s_prefix,
                            json.dumps(string_data),
                            retain, qos,
                        )

                    # Derived paired-string rollups for PW3
                    # PW3 physically pairs inputs A+B, C+D, E+F.
                    # Multi-PW3 single-gateway setups may also have A1-F1,
                    # A2-F2, etc. — we detect suffixes and pair them too.
                    pair_bases = [("A", "B"), ("C", "D"), ("E", "F")]
                    # Collect unique suffixes ("" for A-F, "1" for A1-F1, ...)
                    suffixes = set()
                    for key in data.strings:
                        if isinstance(key, str):
                            base = key.rstrip("0123456789")
                            suffix = key[len(base):]
                            if base in ("A", "B", "C", "D", "E", "F"):
                                suffixes.add(suffix)
                    for suffix in sorted(suffixes):
                        for (first, second), pair_name_base in zip(
                            pair_bases, ("AB", "CD", "EF")
                        ):
                            a_key = first + suffix
                            b_key = second + suffix
                            sa = data.strings.get(a_key, {})
                            sb = data.strings.get(b_key, {})
                            if not isinstance(sa, dict) or not isinstance(sb, dict):
                                continue
                            if not sa or not sb:
                                continue
                            pair_name = pair_name_base + suffix.upper()
                            p_prefix = f"{strings_prefix}/{pair_name}"
                            v_a = _safe_float(sa.get("Voltage"))
                            if v_a is not None:
                                await self._safe_publish(
                                    f"{p_prefix}/voltage",
                                    f"{v_a:.2f}", retain, qos,
                                )
                            c_a = _safe_float(sa.get("Current"))
                            c_b = _safe_float(sb.get("Current"))
                            if c_a is not None or c_b is not None:
                                total_c = (c_a or 0.0) + (c_b or 0.0)
                                await self._safe_publish(
                                    f"{p_prefix}/current",
                                    f"{total_c:.2f}", retain, qos,
                                )
                            p_a = _safe_float(sa.get("Power"))
                            p_b = _safe_float(sb.get("Power"))
                            if p_a is not None or p_b is not None:
                                total_p = (p_a or 0.0) + (p_b or 0.0)
                                await self._safe_publish(
                                    f"{p_prefix}/power",
                                    f"{total_p:.2f}", retain, qos,
                                )

                # Summary JSON topic
                summary = {
                    "online": status.online,
                    "soe": data.soe,
                    "soe_raw": data.soe_raw,
                    "total_capacity": total_capacity,
                    "current_charge": current_charge,
                    "solar": solar if data.aggregates else None,
                    "grid": grid if data.aggregates else None,
                    "home": home if data.aggregates else None,
                    "powerwall": pw_power if data.aggregates else None,
                    "grid_status": data.grid_status,
                    "mode": data.mode,
                    "reserve": data.reserve,
                    "version": data.version,
                }
                await self._safe_publish(
                    f"{prefix}/status", json.dumps(summary), retain, qos
                )

            # Per-gateway availability must track the actual gateway state.
            # Discovery uses availability_mode "all", so if this topic never
            # goes "offline" HA keeps showing stale retained sensor values
            # after the gateway drops (the LWT only covers the global topic).
            await self._safe_publish(
                f"{prefix}/availability",
                "online" if status.online else "offline",
                retain, qos,
            )

        except Exception as e:
            # Catch-all: MQTT must never raise into the poll loop
            logger.debug(f"MQTT publish_gateway error for {gateway_id}: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _safe_publish(
        self, topic: str, payload: str, retain: bool, qos: int
    ) -> None:
        """Publish a single message, marking disconnected on failure."""
        if not self._connected or self._client is None:
            return
        try:
            await self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            logger.debug(f"MQTT publish failed on {topic}: {e}")
            # Signal the connection loop to reconnect
            self._connected = False

    async def _connection_loop(self) -> None:
        """Maintain a persistent MQTT connection with exponential-backoff reconnect.

        Design
        ------
        * Outer while loop: reconnect on any error.
        * Inner while loop: heartbeat that keeps the async-with context alive
          and detects when _connected has been set to False by a failed publish.
        * On CancelledError (shutdown): exits cleanly.
        * LWT (Last Will and Testament) ensures the broker publishes "offline"
          to the availability topics if the connection drops unexpectedly.
        """
        try:
            import aiomqtt  # deferred — only loaded when MQTT is enabled
        except ImportError:
            logger.error(
                "aiomqtt is not installed. "
                "Install it with: pip install 'aiomqtt>=2.3.0'"
            )
            return

        from app.config import settings  # late import

        self._backoff = 2

        while not self._shutdown:
            try:
                # Build Last-Will-and-Testament payloads for all known gateway IDs.
                # We use the first gateway's availability topic for the LWT; individual
                # gateway availability is updated inside publish_gateway().
                # This is a best-effort LWT — the broker publishes it if we disconnect
                # without a clean DISCONNECT packet (e.g. crash, network loss).
                will_topic = f"{settings.mqtt_topic_prefix}/availability"
                will = aiomqtt.Will(
                    topic=will_topic,
                    payload="offline",
                    qos=1,
                    retain=True,
                )

                # Build TLS context if requested
                tls_context: Optional[ssl.SSLContext] = None
                if settings.mqtt_tls:
                    tls_context = ssl.create_default_context(
                        cafile=settings.mqtt_tls_ca_cert or None
                    )
                    if settings.mqtt_tls_insecure:
                        tls_context.check_hostname = False
                        tls_context.verify_mode = ssl.CERT_NONE

                client_kwargs = dict(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_username,
                    password=settings.mqtt_password,
                    keepalive=settings.mqtt_keepalive,
                    identifier=settings.mqtt_client_id,
                    will=will,
                    tls_context=tls_context,
                )

                logger.info(
                    f"MQTT connecting to {settings.mqtt_host}:{settings.mqtt_port}"
                )

                async with aiomqtt.Client(**client_kwargs) as client:
                    self._client = client
                    self._connected = True
                    self._backoff = 2  # reset on successful connect
                    # Clear discovery set so HA payloads are re-sent after reconnect
                    self._discovery_sent.clear()
                    logger.info(
                        f"MQTT connected to {settings.mqtt_host}:{settings.mqtt_port}"
                    )

                    # Publish the global "online" availability heartbeat.
                    # This is the retained counterpart to the LWT "offline" payload.
                    # HA discovery payloads reference this topic with
                    # availability_mode="all", so without this message every entity
                    # stays stuck at "unavailable" even when state data is flowing.
                    global_avail_topic = f"{settings.mqtt_topic_prefix}/availability"
                    await self._safe_publish(
                        global_avail_topic, "online",
                        retain=True, qos=settings.mqtt_qos,
                    )

                    # Inner heartbeat loop: stays alive until a publish failure
                    # sets _connected=False, or until shutdown is requested.
                    # The 5-second sleep matches the default poll interval so we
                    # detect disconnect promptly without busy-waiting.
                    while self._connected and not self._shutdown:
                        await asyncio.sleep(5)

                    # If we exited the inner loop due to a publish failure
                    # (not shutdown), let the context manager close cleanly then
                    # fall through to the reconnect logic below.
                    if not self._shutdown:
                        logger.debug("MQTT inner loop exited — reconnecting...")

            except asyncio.CancelledError:
                # Shutdown requested — exit cleanly
                self._connected = False
                self._client = None
                break

            except Exception as e:
                self._connected = False
                self._client = None
                if not self._shutdown:
                    logger.warning(
                        f"MQTT connection error: {e}. Retrying in {self._backoff}s"
                    )
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 60)

        self._connected = False
        self._client = None


def _safe_float(val) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _extract_power(aggregates: dict, key: str) -> Optional[float]:
    """Safely extract instant_power (W) from an aggregates dict."""
    try:
        return float(aggregates[key]["instant_power"])
    except (KeyError, TypeError, ValueError):
        return None


def _extract_energy(aggregates: Optional[dict], section: str, field: str) -> Optional[float]:
    """Safely extract a lifetime energy accumulator (Wh) from aggregates."""
    try:
        val = aggregates[section][field]
    except (KeyError, TypeError):
        return None
    return _safe_float(val)


def _extract_battery_energy(
    system_status: Optional[dict], field: str
) -> Optional[float]:
    """Extract a total battery energy value (Wh) from cached system status.

    TEDAPI normally provides the total at the top level.  Some gateway
    responses only include per-battery values, so sum those as a fallback.
    """
    if not isinstance(system_status, dict):
        return None

    value = _safe_float(system_status.get(field))
    if value is not None:
        return value

    blocks = system_status.get("battery_blocks")
    if not isinstance(blocks, list):
        return None

    block_values = [
        block_value
        for block in blocks
        if isinstance(block, dict)
        for block_value in [_safe_float(block.get(field))]
        if block_value is not None
    ]
    return sum(block_values) if block_values else None


# Module-level singleton — imported by gateway_manager and main.py
mqtt_publisher = MqttPublisher()
