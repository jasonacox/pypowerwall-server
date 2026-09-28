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
    {prefix}/{gateway_id}/grid_connected  str    — "true" | "false" (true when grid_status=="UP")
    {prefix}/{gateway_id}/grid_charging   str    — "true" | "false" (grid charging allowed)
    {prefix}/{gateway_id}/grid_export     str    — "battery_ok" | "pv_only" | "never"
    {prefix}/{gateway_id}/time_remaining  float  — hours of backup remaining
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

    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}/voltage          float — V
    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}/current          float — A
    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}/power            float — W
    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}/energy_imported  int   — Wh, lifetime
    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}/energy_exported  int   — Wh, lifetime
    {prefix}/{gateway_id}/meters/remote/{din}/ct{n}                  JSON  — full per-CT data

    Remote-meter lifetime energy is converted from Tesla's watt-seconds to
    whole Wh; the per-CT JSON includes Location ("site" / "solar" / "load").

    Tesla Remote Meter: a wireless CT meter (config.json type "trm_mb").
    {din} is the meter's own device identifier; {n} is the CT index (a meter
    can report more than one CT, and a gateway can have more than one meter).
    Sourced from pw.vitals()'s TRM--<din> blocks (pypowerwall >= 0.18.2 in
    TEDAPI modes; Basic LAN skips vitals) - absent when no remote meter.
"""
import asyncio
import json
import logging
import ssl
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Control-channel safety limits. Commands act on physical hardware, so the
# inbound path is bounded: oversized payloads are rejected before parsing
# (a 1 MB payload must never reach a Tesla write), the broker-side queue is
# capped, and bursts collapse to latest-wins per control instead of N serial
# Tesla writes.
MAX_CONTROL_PAYLOAD_BYTES = 1024
MAX_QUEUED_CONTROL_MESSAGES = 100
CONTROL_COALESCE_WINDOW_S = 0.05
CONTROL_MAX_BATCH = 100


class MqttPublisher:
    """Async MQTT publisher with persistent connection and reconnect logic."""

    def __init__(self):
        self._client = None              # aiomqtt.Client instance (inside context)
        self._connected: bool = False    # True only while inside active async with
        self._connection_task: Optional[asyncio.Task] = None
        self._shutdown: bool = False
        # Per gateway: the optional entities (strings, remote-meter CTs)
        # already announced; a gateway key means base discovery was sent
        self._discovery_sent: Dict[str, frozenset] = {}
        # Per gateway: control config topics already announced (for stale
        # entity removal when MQTT_CONTROLS bits are turned off)
        self._discovery_controls_sent: Dict[str, set] = {}
        # Per gateway: last announced (mask, is_v1r, grid_capable) control
        # state. The signature union only grows, so a mask shrink to 0
        # (whose signature carries no controls key at all) would never
        # re-fire discovery — this state check catches it so stale
        # entities are actually removed.
        self._discovery_controls_state: Dict[str, tuple] = {}
        self._backoff: int = 2           # current reconnect backoff in seconds
        self._controls_warn_done: bool = False  # half-configured controls warning

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

    def _control_announce_state(self, status) -> tuple:
        """(mask, is_v1r, grid_capable) for control discovery on this snapshot.

        Single place computing what _publish_ha_discovery announces and what
        the discovery signature tracks, so both stay in sync.
        """
        from app.config import settings  # late import
        from app.mqtt.ha_discovery import is_v1r_gateway

        mask = settings.mqtt_controls_mask
        if mask == 0 or not settings.mqtt_controls_available:
            return (0, False, False)
        gw = status.gateway if status else None
        data = status.data if status else None
        return (
            mask,
            is_v1r_gateway(gw, data),
            _grid_controls_capable(gw, data),
        )

    async def _publish_ha_discovery(self, gateway_id: str, status) -> None:
        """Publish Home Assistant auto-discovery payloads for a gateway.

        Called once per gateway on first connection (tracked in _discovery_sent).
        Re-sent after every broker reconnect so HA re-discovers after restarts.
        Control entities whose MQTT_CONTROLS bit was turned off are removed
        by publishing an empty retained config (HA drops the entity).

        Args:
            gateway_id: Gateway identifier.
            status:     GatewayStatus used to extract name and version.
        """
        if not self._connected or self._client is None:
            return
        try:
            from app.config import settings  # late import
            from app.mqtt.ha_discovery import (
                build_discovery_payloads,
                extract_remote_meters,
            )

            gateway_name = (
                status.gateway.name
                if status.gateway and status.gateway.name
                else gateway_id
            )
            version = status.data.version if status.data else None

            string_ids = None
            if status.data and status.data.strings and isinstance(status.data.strings, dict):
                string_ids = list(status.data.strings.keys())

            remote_meters = (
                extract_remote_meters(status.data.vitals) if status.data else {}
            )

            # Controls follow the MQTT_CONTROLS bitmask; islanding buttons
            # only for v1r transport, grid controls only where executable.
            controls_mask, is_v1r, grid_capable = self._control_announce_state(status)

            payloads = build_discovery_payloads(
                gateway_id=gateway_id,
                gateway_name=gateway_name,
                topic_prefix=settings.mqtt_topic_prefix,
                ha_prefix=settings.mqtt_ha_prefix,
                version=version,
                string_ids=string_ids,
                remote_meters=remote_meters or None,
                controls=controls_mask,
                is_v1r=is_v1r,
                grid_capable=grid_capable,
            )
            for topic, payload in payloads:
                await self._safe_publish(topic, payload, retain=True, qos=settings.mqtt_qos)

            # Stale control entities (bit turned off, capability lost) are
            # removed by publishing an empty retained config — HA drops them.
            current_controls = _control_config_topics(payloads)
            previous_controls = self._discovery_controls_sent.get(gateway_id, set())
            for stale in sorted(previous_controls - current_controls):
                await self._safe_publish(
                    stale, "", retain=True, qos=settings.mqtt_qos
                )
                logger.info(
                    f"MQTT HA discovery removed control entity '{stale}' "
                    f"for gateway '{gateway_id}'"
                )
            self._discovery_controls_sent[gateway_id] = current_controls

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

        # Send HA discovery payloads the first time we see this gateway, and
        # again whenever a snapshot reports strings, remote-meter CTs or the
        # v1r capability not announced yet (re-sent after reconnect too:
        # _discovery_sent is cleared there). Storing the union means a later
        # snapshot without them (e.g. a vitals timeout) doesn't re-send.
        from app.mqtt.ha_discovery import discovery_signature

        data = status.data if status else None
        controls_mask, controls_v1r, controls_grid = self._control_announce_state(status)
        controls_state = (controls_mask, controls_v1r, controls_grid)
        signature = discovery_signature(
            data.strings if data else None,
            data.vitals if data else None,
            controls=controls_mask,
            is_v1r=controls_v1r,
            grid_capable=controls_grid,
        )
        announced = self._discovery_sent.get(gateway_id)
        last_controls = self._discovery_controls_state.get(gateway_id)
        if (
            announced is None
            or not signature <= announced
            or last_controls != controls_state
        ):
            from app.config import settings  # late import
            if settings.mqtt_ha_discovery:
                await self._publish_ha_discovery(gateway_id, status)
            self._discovery_sent[gateway_id] = (announced or frozenset()) | signature
            self._discovery_controls_state[gateway_id] = controls_state

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
                    # Derived binary: grid_connected = true only when UP, else false (incl. unknown/SYNCING)
                    await self._safe_publish(
                        f"{prefix}/grid_connected",
                        "true" if data.grid_status == "UP" else "false",
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

                if data.grid_charging is not None:
                    await self._safe_publish(
                        f"{prefix}/grid_charging",
                        "true" if data.grid_charging else "false",
                        retain, qos,
                    )

                if data.grid_export is not None:
                    await self._safe_publish(
                        f"{prefix}/grid_export",
                        str(data.grid_export),
                        retain, qos,
                    )

                time_remaining = _safe_float(data.time_remaining)
                if time_remaining is not None:
                    # Topic rounded to 2 decimals for HA; summary JSON keeps raw precision
                    await self._safe_publish(
                        f"{prefix}/time_remaining",
                        f"{time_remaining:.2f}",
                        retain, qos,
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

                # Remote meter topics (Tesla wireless CT meters - one or more
                # CTs per meter, one or more meters per gateway)
                if data.vitals:
                    from app.mqtt.ha_discovery import extract_remote_meters

                    remote_meters = extract_remote_meters(data.vitals)
                    for din, cts in remote_meters.items():
                        for ct_index, fields in cts.items():
                            ct_prefix = f"{prefix}/meters/remote/{din}/ct{ct_index}"
                            voltage = _safe_float(fields.get("InstVoltage"))
                            if voltage is not None:
                                await self._safe_publish(
                                    f"{ct_prefix}/voltage", f"{voltage:.2f}",
                                    retain, qos,
                                )
                            current = _safe_float(fields.get("InstCurrent"))
                            if current is not None:
                                await self._safe_publish(
                                    f"{ct_prefix}/current", f"{current:.2f}",
                                    retain, qos,
                                )
                            power = _safe_float(fields.get("InstRealPower"))
                            if power is not None:
                                await self._safe_publish(
                                    f"{ct_prefix}/power", f"{power:.1f}", retain, qos
                                )
                            # Lifetime accumulators arrive in watt-seconds; HA's
                            # energy dashboard (and the rest of this file's
                            # energy sensors) expects Wh.
                            energy_imported_ws = _safe_float(
                                fields.get("EnergyImportedWs")
                            )
                            if energy_imported_ws is not None:
                                await self._safe_publish(
                                    f"{ct_prefix}/energy_imported",
                                    f"{energy_imported_ws / 3600:.0f}", retain, qos,
                                )
                            energy_exported_ws = _safe_float(
                                fields.get("EnergyExportedWs")
                            )
                            if energy_exported_ws is not None:
                                await self._safe_publish(
                                    f"{ct_prefix}/energy_exported",
                                    f"{energy_exported_ws / 3600:.0f}", retain, qos,
                                )
                            # Full per-CT JSON for consumers that want everything
                            await self._safe_publish(
                                ct_prefix, json.dumps(fields), retain, qos
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
                    "grid_connected": (data.grid_status == "UP") if data.grid_status is not None else None,
                    "mode": data.mode,
                    "reserve": data.reserve,
                    "version": data.version,
                    "grid_charging": data.grid_charging,
                    "grid_export": data.grid_export,
                    "time_remaining": data.time_remaining,
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
                    # Bound the inbound queue: command bursts must not grow
                    # memory without limit (latest-wins collapses them anyway).
                    max_queued_incoming_messages=MAX_QUEUED_CONTROL_MESSAGES,
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
                    self._discovery_controls_sent.clear()
                    self._discovery_controls_state.clear()
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

                    # Subscribe to control command topics if controls are enabled (broker-trust, no token in payload).
                    # Topic pattern: {prefix}/{gateway_id}/control/{control}/set  e.g. pypowerwall/home/control/reserve/set
                    control_task = None
                    if settings.mqtt_controls_available:
                        try:
                            await client.subscribe(
                                f"{settings.mqtt_topic_prefix}/+/control/+/set", qos=1
                            )
                            enabled = settings.mqtt_control_names()
                            logger.info(
                                "MQTT controls subscribed to %s/+/control/+/set "
                                "(enabled: %s)",
                                settings.mqtt_topic_prefix,
                                ", ".join(enabled),
                            )
                            if settings.mqtt_control_allowed("islanding"):
                                logger.warning(
                                    "MQTT ISLANDING control is enabled: broker "
                                    "clients can open the grid contactor — "
                                    "restrict broker access and ACL "
                                    "pypowerwall/+/control/# accordingly"
                                )
                            control_task = asyncio.create_task(
                                self._control_message_loop(client),
                                name="mqtt-control-handler",
                            )
                        except Exception as e:
                            logger.warning(f"MQTT control subscribe failed: {e}")
                    elif (
                        not self._controls_warn_done
                        and settings.mqtt_controls_mask != 0
                        and settings.control_secret
                    ):
                        # Bits requested + secret but no broker user/password:
                        # controls stay off (fail closed). Warn once so the
                        # silent off-state after upgrade is discoverable.
                        self._controls_warn_done = True
                        logger.warning(
                            "MQTT controls requested (MQTT_CONTROLS + "
                            "PW_CONTROL_SECRET) but disabled: set MQTT_USERNAME "
                            "and MQTT_PASSWORD so the broker can enforce the "
                            "control-topic ACL"
                        )

                    # Inner heartbeat loop: stays alive until a publish failure
                    # sets _connected=False, or until shutdown is requested.
                    # The 5-second sleep matches the default poll interval so we
                    # detect disconnect promptly without busy-waiting. It also
                    # watches the control handler: if that task died silently,
                    # reconnect (which recreates it) instead of losing commands.
                    try:
                        while self._connected and not self._shutdown:
                            if control_task is not None and control_task.done():
                                logger.warning(
                                    "MQTT control handler ended unexpectedly — "
                                    "reconnecting..."
                                )
                                self._connected = False
                                break
                            await asyncio.sleep(5)
                    finally:
                        if control_task and not control_task.done():
                            control_task.cancel()
                            try:
                                await control_task
                            except asyncio.CancelledError:
                                pass

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

    async def _control_message_loop(self, client) -> None:
        """Handle incoming HA control commands via MQTT (broker-trust, no token).

        Subscribes to ``{prefix}/+/control/+/set`` after connect.  Each message
        is validated (MQTT_CONTROLS bit, type/range/allowlist, capability) and
        routed on a single path like the HTTP ``POST /control/*`` endpoints
        (write_lock, timeout, islanding cooldown) — never retried on another
        path, since a timeout leaves the first write running.
        ``PW_CONTROL_SECRET`` is never read from the payload — trust comes from
        broker authentication (``MQTT_USERNAME``/``PASSWORD`` + optional ``MQTT_TLS``)
        and ACL ``pypowerwall/+/control/#``.

        Inbound safety: payloads over 1 KB are rejected before parsing,
        retained replays are cleared with a warning (``retain=false`` is
        expected from HA), and bursts collapse to latest-wins per control
        instead of N serial Tesla writes.
        """
        try:
            message_iter = client.messages.__aiter__()
            while not self._shutdown:
                try:
                    first = await message_iter.__anext__()
                except StopAsyncIteration:
                    break
                batch = await self._collect_control_burst(message_iter, first)
                for topic, payload_bytes in await self._coalesce_controls(
                    client, batch
                ):
                    await self._handle_control_message(
                        client, topic, payload_bytes
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"MQTT control message loop error: {e}")
        if not self._shutdown:
            # Iterator exhausted (broker closed the stream) or unexpected
            # error: force a reconnect instead of silently losing commands.
            logger.warning("MQTT control message loop ended — forcing reconnect")
            self._connected = False

    async def _collect_control_burst(self, message_iter, first) -> list:
        """Collect a burst of immediately-available messages (latest-wins).

        Waits briefly for followers so 50 rapid commands collapse instead of
        becoming 50 serial Tesla writes; a lone command costs one short idle
        window. Bounded so a flood cannot grow memory without limit.
        """
        batch = [first]
        for _ in range(CONTROL_MAX_BATCH - 1):
            try:
                nxt = await asyncio.wait_for(
                    message_iter.__anext__(),
                    timeout=CONTROL_COALESCE_WINDOW_S,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                break  # idle (TimeoutError) or closed (StopAsyncIteration)
            batch.append(nxt)
        return batch

    async def _coalesce_controls(self, client, batch) -> list:
        """Filter a burst to executable commands, latest per topic wins.

        Retained replays are warned about and cleared; oversized payloads
        are rejected; undecodable topics are dropped. Returns
        [(topic, payload_bytes)] with only the last message per topic.
        """
        candidates: dict = {}
        for message in batch:
            try:
                retained = bool(getattr(message, "retain", False))
            except Exception:
                retained = False
            try:
                topic = (
                    message.topic.value
                    if hasattr(message.topic, "value")
                    else str(message.topic)
                )
                payload_bytes = (
                    bytes(message.payload)
                    if isinstance(message.payload, (bytes, bytearray))
                    else str(message.payload).encode("utf-8", "replace")
                )
            except Exception as e:
                logger.warning(f"MQTT control message decode failed: {e}")
                continue
            if retained:
                await self._warn_retained_command(client, topic)
                continue
            if len(payload_bytes) > MAX_CONTROL_PAYLOAD_BYTES:
                logger.warning(
                    f"MQTT control command on '{topic}' rejected: payload "
                    f"{len(payload_bytes)} bytes exceeds "
                    f"{MAX_CONTROL_PAYLOAD_BYTES} byte cap"
                )
                continue
            candidates[topic] = payload_bytes
        return list(candidates.items())

    async def _warn_retained_command(self, client, topic: str) -> None:
        """Warn about a retained command replay and clear it from the broker.

        A retained ``.../control/+/set`` would otherwise re-execute on every
        reconnect, so it is never executed — and removed so it cannot fire
        later. Clearing is awaited inline (deterministic, still best-effort:
        a topic we cannot publish to just logs).
        """
        logger.warning(
            f"MQTT control command on '{topic}' ignored: retained commands "
            "are not executed (publish with retain=false)"
        )
        publish = getattr(client, "publish", None)
        if publish is None:
            return
        try:
            await publish(topic, "", qos=1, retain=True)
        except Exception as e:
            logger.debug(f"MQTT retained clear failed for '{topic}': {e}")

    async def _handle_control_message(
        self, client, topic: str, payload_bytes: bytes
    ) -> None:
        """Validate and dispatch one control command (all work inside try)."""
        try:
            from app.config import MQTT_CONTROL_BITS
            from app.config import settings as _settings
            from app.core.gateway_manager import gateway_manager

            prefix = _settings.mqtt_topic_prefix
            parts = topic.split("/")
            # Expect 5 parts: prefix / gw / control / ctrl / set
            if (
                len(parts) != 5
                or parts[0] != prefix
                or parts[2] != "control"
                or parts[4] != "set"
            ):
                logger.warning(f"MQTT control: malformed topic '{topic}'")
                return
            gateway_id = parts[1]
            control = parts[3]

            # Validate gateway exists (inside the per-message try so one bad
            # lookup can never kill the loop)
            gw = gateway_manager.gateways.get(gateway_id)
            if gw is None:
                logger.warning(
                    f"MQTT control: unknown gateway '{gateway_id}' "
                    f"for '{control}'"
                )
                return
            if control not in MQTT_CONTROL_BITS:
                logger.warning(
                    f"MQTT control: unknown control '{control}' "
                    f"for '{gateway_id}'"
                )
                return
            if not _settings.mqtt_control_allowed(control):
                logger.warning(
                    f"MQTT control {control} for '{gateway_id}' rejected: "
                    "MQTT_CONTROLS bit not set"
                )
                return

            # Parse JSON payload (size already capped before coalescing)
            try:
                payload = (
                    json.loads(payload_bytes.decode("utf-8"))
                    if payload_bytes
                    else {}
                )
                if not isinstance(payload, dict):
                    raise ValueError("payload not a dict")
            except Exception as e:
                logger.warning(
                    f"MQTT control {control} for '{gateway_id}' rejected: "
                    f"bad JSON ({e})"
                )
                return

            # Route to gateway_manager (broker-trust, no token check)
            if control == "reserve":
                val = payload.get("value")
                # bool is an int subclass — True/False must not pass as 1/0
                if (
                    not isinstance(val, int)
                    or isinstance(val, bool)
                    or not 0 <= val <= 100
                ):
                    logger.warning(
                        f"MQTT control reserve for '{gateway_id}' rejected: "
                        f"invalid value '{_short(val)}'"
                    )
                    return
                result, path = await _route_gateway_control(
                    gateway_id, "set_reserve", val, timeout=10.0
                )
                audit = f"value={val}"

            elif control == "mode":
                val = payload.get("value")
                if val not in ("self_consumption", "backup", "autonomous"):
                    logger.warning(
                        f"MQTT control mode for '{gateway_id}' rejected: "
                        f"invalid value '{_short(val)}'"
                    )
                    return
                result, path = await _route_gateway_control(
                    gateway_id, "set_mode", val, timeout=10.0
                )
                audit = f"value={val}"

            elif control == "grid_charging":
                val = payload.get("value")
                if not isinstance(val, bool):
                    logger.warning(
                        f"MQTT control grid_charging for '{gateway_id}' "
                        f"rejected: invalid value '{_short(val)}'"
                    )
                    return
                if not _grid_controls_capable(gateway_manager, gw):
                    logger.warning(
                        f"MQTT control grid_charging for '{gateway_id}' "
                        "rejected: gateway cannot execute it"
                    )
                    return
                result, path = await _route_gateway_control(
                    gateway_id, "set_grid_charging", val, timeout=10.0
                )
                audit = f"value={val}"

            elif control == "grid_export":
                val = payload.get("value")
                if val not in ("battery_ok", "pv_only", "never"):
                    logger.warning(
                        f"MQTT control grid_export for '{gateway_id}' "
                        f"rejected: invalid value '{_short(val)}'"
                    )
                    return
                if not _grid_controls_capable(gateway_manager, gw):
                    logger.warning(
                        f"MQTT control grid_export for '{gateway_id}' "
                        "rejected: gateway cannot execute it"
                    )
                    return
                result, path = await _route_gateway_control(
                    gateway_id, "set_grid_export", val, timeout=10.0
                )
                audit = f"value={val}"

            elif control == "islanding":
                action = payload.get("action")
                confirm = payload.get("confirm")
                if action not in ("off_grid", "on_grid") or confirm is not True:
                    logger.warning(
                        f"MQTT control islanding for '{gateway_id}' rejected: "
                        "need action off_grid/on_grid with confirm:true"
                    )
                    return
                if not _gateway_is_v1r(gateway_manager, gateway_id):
                    logger.warning(
                        f"MQTT control islanding for '{gateway_id}' rejected: "
                        "no confirmed v1r transport"
                    )
                    return
                method = (
                    "go_off_grid" if action == "off_grid" else "reconnect_grid"
                )
                kwargs = {"confirm": True} if action == "off_grid" else {}
                try:
                    # Local v1r/TEDAPI only, same 10 s timeout as HTTP —
                    # never the shared cloud connection, never retried.
                    result = await gateway_manager.local_control(
                        gateway_id, method, timeout=10.0, **kwargs
                    )
                    path = "local"
                except Exception as e:
                    # Islanding cooldown / in-progress errors bubble as
                    # exceptions (same machinery as the WebUI path)
                    logger.warning(
                        f"MQTT control islanding {action} for '{gateway_id}' "
                        f"failed: {e}"
                    )
                    return
                if not _island_ack_ok(result):
                    logger.warning(
                        f"MQTT control islanding {action} for '{gateway_id}' "
                        "not acknowledged; check grid status"
                    )
                    return
                audit = f"action={action}"
            else:  # pragma: no cover — unreachable via MQTT_CONTROL_BITS gate
                return

            if _is_error_result(result):
                logger.warning(
                    f"MQTT control {control} for '{gateway_id}' failed: "
                    f"gateway reported {_short(result)}"
                )
            elif result is not None:
                logger.info(
                    f"MQTT control {control} for '{gateway_id}' applied "
                    f"({audit} via {path})"
                )
            else:
                logger.warning(
                    f"MQTT control {control} for '{gateway_id}' failed: "
                    "no result (offline/unsupported)"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"MQTT control handler error for '{topic}': {e}")


def _short(val: object, limit: int = 200) -> str:
    """Truncate a value for log lines (payloads are capped, values vary)."""
    text = str(val)
    return text if len(text) <= limit else text[:limit] + "…"


def _is_error_result(result: object) -> bool:
    """True when a library response reports an error instead of a value."""
    return isinstance(result, dict) and ("error" in result or "ERROR" in result)


def _island_ack_ok(result: object) -> bool:
    """Hardware acknowledgement check, same rule as HTTP POST /control/islanding.

    Only ``{"result": 1}`` counts (int, not bool); anything else — including
    error dicts — must never be presented as a successful contactor command.
    """
    if not isinstance(result, dict) or _is_error_result(result):
        return False
    ack = result.get("result")
    return isinstance(ack, int) and not isinstance(ack, bool) and ack == 1


def _grid_controls_capable(gateway_manager, gateway) -> bool:
    """True when this gateway can actually execute grid setter commands.

    The grid setters are logging stubs on plain local/hybrid connections
    and TEDAPI full — only cloud/FleetAPI, a hybrid cloud bound to this
    gateway, or a confirmed v1r transport execute them. Used both for
    discovery (don't announce what can't run) and dispatch (defense in
    depth, since anyone with broker access can publish).
    """
    if gateway is None:
        return False
    if getattr(gateway, "cloud_mode", False) or getattr(gateway, "fleetapi", False):
        return True
    try:
        bound = getattr(gateway_manager, "_cloud_control_gateway_id", None)
        if (
            gateway_manager._cloud_control is not None
            and bound is not None
            and bound == gateway.id
        ):
            return True
    except Exception:
        pass
    try:
        status = gateway_manager.get_gateway(gateway.id)
        data = status.data if status else None
        mode = getattr(data, "tedapi_mode", None)
        return bool(getattr(gateway, "rsa_key_configured", False) and mode == "v1r")
    except Exception:
        return False


def _control_config_topics(payloads: list) -> set:
    """Config topics of control entities in a discovery payload list."""
    topics = set()
    for topic, payload in payloads:
        try:
            doc = json.loads(payload)
        except Exception:
            continue
        if isinstance(doc, dict) and "/control/" in str(doc.get("command_topic", "")):
            topics.add(topic)
    return topics


def _gateway_is_v1r(gateway_manager, gateway_id: str) -> bool:
    """True when the addressed gateway uses the v1r transport (PW2 + PW3).

    Fail-closed like the Console gate: unknown mode (cold start, cloud
    failover) rejects. Discovery only announces the islanding buttons for
    such gateways; the control loop enforces the same rule since MQTT
    topics can be published by anyone with broker access.
    """
    try:
        from app.mqtt.ha_discovery import is_v1r_gateway

        gw = gateway_manager.gateways.get(gateway_id)
        status = gateway_manager.get_gateway(gateway_id)
        return is_v1r_gateway(gw, status.data if status else None)
    except Exception:
        return False


async def _route_gateway_control(
    gateway_id: str, method: str, *args, timeout: float = 10.0
):
    """Route a validated control write on exactly one path (no retry).

    Same single-path rule as the HTTP ``POST /control/*`` routes: a timeout
    leaves the first write running in its worker thread, so retrying on
    another path can land the command twice — potentially on another site.

    - cloud_mode/FleetAPI gateways: their own connection (targets its site).
    - Other gateways: the shared hybrid cloud connection, but ONLY for the
      gateway it was built from (recorded id — the connection carries that
      gateway's credentials and cannot target another site).
    - Otherwise the gateway's own local connection (v1r/local stubs answer
      with ``{'error': ...}``, which the caller treats as a failure).

    Returns (result, path) with path in {"cloud", "local", "none"}.
    """
    from app.core.gateway_manager import gateway_manager

    gw = gateway_manager.gateways.get(gateway_id)
    if gw is not None and (
        getattr(gw, "cloud_mode", False) or getattr(gw, "fleetapi", False)
    ):
        result = await gateway_manager.local_control(
            gateway_id, method, *args, timeout=timeout
        )
        return result, "local"
    if (
        gateway_manager._cloud_control is not None
        and getattr(gateway_manager, "_cloud_control_gateway_id", None)
        == gateway_id
    ):
        result = await gateway_manager.cloud_control(
            method, *args, timeout=timeout
        )
        return result, "cloud"
    if gw is None:
        return None, "none"
    result = await gateway_manager.local_control(
        gateway_id, method, *args, timeout=timeout
    )
    return result, "local"


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
