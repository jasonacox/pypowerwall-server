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

    Per-unit device signals (Powerwall temperatures and fan speeds, from
    vitals + get_fan_speeds(), keyed by unit serial):
    {prefix}/{gateway_id}/devices/{serial}/temperature/pack_max     float — °C (PW3 battery pack)
    {prefix}/{gateway_id}/devices/{serial}/temperature/pack_min     float — °C (PW3 battery pack)
    {prefix}/{gateway_id}/devices/{serial}/temperature/shunt        float — °C (PW3 shunt)
    {prefix}/{gateway_id}/devices/{serial}/temperature/ambient      float — °C (PW3 inverter ambient)
    {prefix}/{gateway_id}/devices/{serial}/temperature/controller   float — °C (PW2/2+ TETHC ambient)
    {prefix}/{gateway_id}/devices/{serial}/fan/a/rpm                int   — rpm (PW3 fan A)
    {prefix}/{gateway_id}/devices/{serial}/fan/a/duty               float — %   (PW3 fan A duty)
    {prefix}/{gateway_id}/devices/{serial}/fan/b/rpm                int   — rpm (PW3 fan B)
    {prefix}/{gateway_id}/devices/{serial}/fan/b/duty               float — %   (PW3 fan B duty)
    {prefix}/{gateway_id}/devices/{serial}/fan/rpm                  int   — rpm (PW2/2+ fan)
    {prefix}/{gateway_id}/devices/{serial}/fan/target_rpm           int   — rpm (PW2/2+ fan target)
    {prefix}/{gateway_id}/devices/{serial}                          JSON  — full per-unit signals
    Only the signals each unit reports are published - a PW2 unit gets fan
    rpm but no duty, and an expansion pack gets pack temps but no fans.

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
        # Gateways whose per-unit signal extraction last failed: warn once
        # (with traceback), then log at debug until an extraction succeeds.
        self._signal_extract_failed: set = set()
        # Per gateway: last announced control state (see
        # _control_announce_state); discovery re-fires when it changes
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

    def _control_announce_state(self, gateway_id: str) -> tuple:
        """(mask, writable, is_v1r): which control entities to announce.

        Uses the same checks as command dispatch, so Home Assistant only
        shows controls this gateway can actually execute.
        """
        from app.config import settings  # late import
        from app.core.gateway_manager import gateway_manager

        if not settings.mqtt_controls_available:
            return (0, False, False)
        return (
            settings.mqtt_controls,
            _write_path(gateway_manager, gateway_id) is not None,
            _gateway_is_v1r(gateway_manager, gateway_id),
        )

    async def _publish_ha_discovery(
        self, gateway_id: str, status, device_signals: dict
    ) -> None:
        """Publish Home Assistant auto-discovery payloads for a gateway.

        Called once per gateway on first connection (tracked in _discovery_sent).
        Re-sent after every broker reconnect so HA re-discovers after restarts.
        Control entities not announced this time (bit off, controls disabled,
        capability lost) get an empty retained config, so Home Assistant drops
        them, including ones announced before a restart.

        Args:
            gateway_id:     Gateway identifier.
            status:         GatewayStatus used to extract name and version.
            device_signals: Per-unit signals from extract_unit_signals(),
                            computed once per poll by the caller.
        """
        if not self._connected or self._client is None:
            return
        try:
            from app.config import settings  # late import
            from app.mqtt.ha_discovery import (
                build_discovery_payloads,
                control_config_topics,
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

            controls_mask, writable, is_v1r = self._control_announce_state(gateway_id)

            payloads = build_discovery_payloads(
                gateway_id=gateway_id,
                gateway_name=gateway_name,
                topic_prefix=settings.mqtt_topic_prefix,
                ha_prefix=settings.mqtt_ha_prefix,
                version=version,
                string_ids=string_ids,
                remote_meters=remote_meters or None,
                device_signals=device_signals or None,
                controls=controls_mask,
                writable=writable,
                is_v1r=is_v1r,
            )
            for topic, payload in payloads:
                await self._safe_publish(topic, payload, retain=True, qos=settings.mqtt_qos)

            # Stateless: clear every control entity not announced now. A
            # topic that holds nothing is a no-op on the broker.
            announced = {topic for topic, _ in payloads}
            for topic in control_config_topics(gateway_id, settings.mqtt_ha_prefix):
                if topic not in announced:
                    await self._safe_publish(
                        topic, "", retain=True, qos=settings.mqtt_qos
                    )

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
        # again whenever a snapshot reports strings, remote-meter CTs or
        # per-unit device signals not announced yet (re-sent after reconnect
        # too: _discovery_sent is cleared there). Storing the union means a
        # later snapshot without them (e.g. a vitals timeout) doesn't re-send.
        from app.core.signals import (
            SIGNAL_GROUPS,
            SIGNAL_METRICS,
            extract_unit_signals,
        )
        from app.mqtt.ha_discovery import DEVICE_METRIC_TOPICS, discovery_signature

        data = status.data if status else None
        # Extract per-unit signals once per poll; discovery signature, HA
        # discovery and the per-unit topics below all reuse this result.
        # Guarded: a malformed snapshot must degrade to "no device signals",
        # never to an exception that would stop the whole gateway's MQTT.
        device_signals: dict = {}
        if data:
            try:
                device_signals = extract_unit_signals(data.vitals, data.fan_speeds)
                self._signal_extract_failed.discard(gateway_id)
            except Exception:
                first = gateway_id not in self._signal_extract_failed
                self._signal_extract_failed.add(gateway_id)
                logger.log(
                    logging.WARNING if first else logging.DEBUG,
                    "Per-unit signal extraction failed for gateway '%s'",
                    gateway_id,
                    exc_info=True,
                )
        controls_state = self._control_announce_state(gateway_id)
        signature = discovery_signature(
            data.strings if data else None,
            data.vitals if data else None,
            device_signals,
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
                await self._publish_ha_discovery(gateway_id, status, device_signals)
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

                # Per-unit device signal topics (Powerwall temperatures
                # and fan speeds, keyed by unit serial — the same units as
                # the web console's Powerwall Status table). Uses the
                # signals extracted once per poll above.
                for serial, signals in device_signals.items():
                    device_prefix = f"{prefix}/devices/{serial}"
                    rounded: dict = {}
                    for metric_id, value in signals.items():
                        entry = DEVICE_METRIC_TOPICS.get(metric_id)
                        if entry is None:
                            continue
                        topic_suffix = entry[0]
                        # Precision comes from the registry (SIGNAL_GROUPS
                        # decimals), one rounding for both the topic text
                        # and the per-unit JSON below; "+ 0.0" keeps -0.04
                        # from publishing as "-0.0". Whole numbers (d == 0)
                        # go into the JSON as ints.
                        decimals = SIGNAL_GROUPS[SIGNAL_METRICS[metric_id]["group"]][
                            "decimals"
                        ]
                        rounded_value = round(value, decimals) + 0.0
                        rounded[metric_id] = (
                            int(rounded_value) if decimals == 0 else rounded_value
                        )
                        await self._safe_publish(
                            f"{device_prefix}/{topic_suffix}",
                            f"{rounded_value:.{decimals}f}",
                            retain,
                            qos,
                        )
                    # Full per-unit JSON for consumers that want everything
                    await self._safe_publish(
                        device_prefix, json.dumps(rounded), retain, qos
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
                        control_topic = f"{settings.mqtt_topic_prefix}/+/control/+/set"
                        try:
                            granted = await client.subscribe(control_topic, qos=1)
                        except Exception as e:
                            # Transient: reconnect after the backoff, which
                            # subscribes again, instead of running without controls
                            raise RuntimeError(f"control subscribe failed: {e}") from e
                        if any(getattr(c, "value", c) >= 0x80 for c in granted or ()):
                            # Refused by the broker (e.g. its ACL): a retry
                            # can't help, so say so and keep monitoring
                            logger.error(
                                "MQTT broker refused the subscription to %s, so "
                                "controls are off: allow this user to read it in "
                                "the broker ACL",
                                control_topic,
                            )
                        else:
                            logger.info(
                                "MQTT controls subscribed to %s (enabled: %s)",
                                control_topic,
                                ", ".join(settings.mqtt_control_names()),
                            )
                            if settings.mqtt_control_allowed("islanding"):
                                logger.warning(
                                    "MQTT ISLANDING control is enabled: broker "
                                    "clients can open the grid contactor. "
                                    "Restrict broker access and ACL %s/+/control/#",
                                    settings.mqtt_topic_prefix,
                                )
                            control_task = asyncio.create_task(
                                self._control_message_loop(client),
                                name="mqtt-control-handler",
                            )
                    elif settings.mqtt_controls and not self._controls_warn_done:
                        # Controls requested but a prerequisite is missing:
                        # they stay off (fail closed). Say which, once.
                        self._controls_warn_done = True
                        missing = [
                            name
                            for name, value in (
                                ("MQTT_USERNAME", settings.mqtt_username),
                                ("MQTT_PASSWORD", settings.mqtt_password),
                                ("PW_CONTROL_SECRET", settings.control_secret),
                            )
                            if not value
                        ]
                        logger.warning(
                            "MQTT controls requested (MQTT_CONTROLS=%s) but "
                            "disabled: set %s",
                            settings.mqtt_controls,
                            ", ".join(missing),
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
        """Run control commands from ``{prefix}/+/control/+/set``.

        Trust comes from broker authentication and its topic ACL;
        ``PW_CONTROL_SECRET`` is never read from a payload. A burst collapses
        to the latest command per topic, and each command runs on exactly one
        connection (no retry), like the HTTP ``POST /control/*`` routes. If
        the loop ends while we are running, force a reconnect, which restarts
        it.
        """
        try:
            messages = client.messages.__aiter__()
            while not self._shutdown:
                first = await messages.__anext__()
                batch = await self._collect_control_burst(messages, first)
                for topic, payload, retained in self._latest_commands(batch):
                    if retained:
                        logger.warning(
                            f"MQTT control command on {topic!r} ignored: "
                            "retained commands are not executed "
                            "(publish with retain=false)"
                        )
                    else:
                        await self._handle_control_message(topic, payload)
                    # Delete any retained copy, whatever the sender did, so a
                    # stored command can never run again on a reconnect.
                    await self._clear_command(client, topic)
        except asyncio.CancelledError:
            raise
        except StopAsyncIteration:
            pass
        except Exception as e:
            logger.warning(f"MQTT control message loop error: {e}")
        if not self._shutdown:
            logger.warning("MQTT control message loop ended, forcing reconnect")
            self._connected = False

    async def _collect_control_burst(self, messages, first) -> list:
        """The first message plus any that arrive within the coalesce window.

        Never cancel ``__anext__()``: a cancelled call can lose a message it
        already took from aiomqtt's queue. Wait out the window, then take only
        what is already queued.
        """
        batch = [first]
        await asyncio.sleep(CONTROL_COALESCE_WINDOW_S)
        while len(messages) and len(batch) < CONTROL_MAX_BATCH:
            batch.append(await messages.__anext__())
        return batch

    @staticmethod
    def _latest_commands(batch) -> list:
        """[(topic, payload, retained)] with the latest message per topic.

        Empty payloads are dropped: that is how a retained topic is cleared,
        including the echo of our own clear.
        """
        latest: dict = {}
        for message in batch:
            topic = str(message.topic)
            payload = message.payload
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            if not payload:
                logger.debug(f"MQTT control: empty payload on {topic!r} ignored")
                continue
            latest.pop(topic, None)  # keep the order of the latest messages
            latest[topic] = (bytes(payload), bool(message.retain))
        return [(topic, p, r) for topic, (p, r) in latest.items()]

    @staticmethod
    async def _clear_command(client, topic: str) -> None:
        """Delete a retained command (no-op when nothing is retained)."""
        try:
            await client.publish(topic, b"", qos=1, retain=True)
        except Exception as e:
            logger.debug(f"MQTT control: clearing {topic!r} failed: {e}")

    async def _handle_control_message(self, topic: str, payload_bytes: bytes) -> None:
        """Validate one control command and run it. Never raises."""
        try:
            from app.config import MQTT_CONTROL_BITS
            from app.config import settings as _settings
            from app.core.gateway_manager import gateway_manager

            # {prefix}/{gateway}/control/{name}/set; the prefix may have levels
            prefix = f"{_settings.mqtt_topic_prefix}/"
            parts = topic[len(prefix):].split("/") if topic.startswith(prefix) else []
            if len(parts) != 4 or parts[1] != "control" or parts[3] != "set":
                logger.warning(f"MQTT control: malformed topic {topic!r}")
                return
            gateway_id, control = parts[0], parts[2]
            label = f"MQTT control {control!r} for {gateway_id!r}"
            if gateway_id not in gateway_manager.gateways:
                logger.warning(f"{label} rejected: unknown gateway")
                return
            if control not in MQTT_CONTROL_BITS:
                logger.warning(f"{label} rejected: unknown control")
                return
            if not _settings.mqtt_control_allowed(control):
                logger.warning(f"{label} rejected: MQTT_CONTROLS bit not set")
                return
            if len(payload_bytes) > MAX_CONTROL_PAYLOAD_BYTES:
                logger.warning(
                    f"{label} rejected: payload of {len(payload_bytes)} bytes "
                    f"exceeds the {MAX_CONTROL_PAYLOAD_BYTES} byte cap"
                )
                return
            try:
                payload = json.loads(payload_bytes.decode("utf-8"))
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                logger.warning(f"{label} rejected: payload is not a JSON object")
                return

            if control == "islanding":
                action = payload.get("action")
                confirm = payload.get("confirm")
                if action not in ("off_grid", "on_grid") or confirm is not True:
                    logger.warning(
                        f"{label} rejected: need action off_grid/on_grid "
                        "with confirm:true"
                    )
                    return
                if not _gateway_is_v1r(gateway_manager, gateway_id):
                    logger.warning(f"{label} rejected: no confirmed v1r transport")
                    return
                method = "go_off_grid" if action == "off_grid" else "reconnect_grid"
                kwargs = {"confirm": True} if action == "off_grid" else {}
                try:
                    # Local v1r only, never the shared cloud connection, same
                    # 10 s timeout as HTTP, never retried
                    result = await gateway_manager.local_control(
                        gateway_id, method, timeout=10.0, **kwargs
                    )
                except Exception as e:  # cooldown or a command in progress
                    logger.warning(f"{label} action={action} failed: {e}")
                    return
                audit = f"action={action} via local v1r"
                ok = _island_ack_ok(result)
            elif control == "reserve_hold":
                # Battery hold: reserve = cached SoC. Takes an empty JSON
                # object; anything else is rejected below.
                if payload:
                    logger.warning(
                        f"{label} rejected: reserve_hold requires "
                        "an empty JSON object"
                    )
                    return
                hold_status = gateway_manager.get_gateway(gateway_id)
                hold_soe = (
                    hold_status.data.soe
                    if hold_status
                    and hold_status.data
                    and hold_status.data.soe is not None
                    else None
                )
                if hold_soe is None:
                    logger.warning(
                        f"{label} rejected: battery state of charge "
                        "unavailable"
                    )
                    return
                try:
                    hold_level = max(0, min(100, int(round(float(hold_soe)))))
                except (TypeError, ValueError, OverflowError):
                    logger.warning(
                        f"{label} rejected: bad SoC {_short(hold_soe)}"
                    )
                    return
                path = _write_path(gateway_manager, gateway_id)
                if path is None:
                    logger.warning(
                        f"{label} rejected: this gateway can't write it (needs "
                        "cloud, FleetAPI, hybrid cloud or v1r)"
                    )
                    return
                if path == "hybrid cloud":
                    result = await gateway_manager.cloud_control(
                        "set_reserve", hold_level, timeout=10.0
                    )
                else:
                    result = await gateway_manager.local_control(
                        gateway_id, "set_reserve", hold_level, timeout=10.0
                    )
                audit = (
                    f"reserve={hold_level} (SoC {float(hold_soe):.1f}) "
                    f"via {path}"
                )
                ok = result is not None and not _is_error_result(result)
            else:
                value = payload.get("value")
                if not _CONTROL_VALUE_OK[control](value):
                    hint = _CONTROL_VALUE_HINT[control]
                    if (
                        control == "reserve"
                        and isinstance(value, float)
                        and value.is_integer()
                        and 0 <= value <= 100
                    ):
                        # 40.0 reads as 40: name the decimal point as the problem
                        reason = (
                            f"{value!r} has a decimal point, send {int(value)} ({hint})"
                        )
                    else:
                        reason = f"invalid value {_short(value)} (must be {hint})"
                    logger.warning(f"{label} rejected: {reason}")
                    return
                path = _write_path(gateway_manager, gateway_id)
                if path is None:
                    logger.warning(
                        f"{label} rejected: this gateway can't write it (needs "
                        "cloud, FleetAPI, hybrid cloud or v1r)"
                    )
                    return
                method = _CONTROL_METHODS[control]
                if path == "hybrid cloud":
                    result = await gateway_manager.cloud_control(
                        method, value, timeout=10.0
                    )
                else:
                    result = await gateway_manager.local_control(
                        gateway_id, method, value, timeout=10.0
                    )
                audit = f"value={value!r} via {path}"
                ok = result is not None and not _is_error_result(result)

            if ok:
                logger.info(f"{label} applied ({audit})")
            else:
                logger.warning(
                    f"{label} failed ({audit}): {_short(result)}; check the "
                    "gateway state"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"MQTT control handler error for {topic!r}: {e}")


# Value checks and library setters for the value controls (same rules as the
# HTTP POST /control/* routes). bool is an int subclass, so reserve rejects it.
_CONTROL_VALUE_OK = {
    "reserve": lambda v: (
        isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100
    ),
    "mode": lambda v: v in ("self_consumption", "backup", "autonomous"),
    "grid_charging": lambda v: isinstance(v, bool),
    "grid_export": lambda v: v in ("battery_ok", "pv_only", "never"),
}
_CONTROL_VALUE_HINT = {
    "reserve": "a whole number from 0 to 100",
    "mode": "self_consumption, backup or autonomous",
    "grid_charging": "true or false",
    "grid_export": "battery_ok, pv_only or never",
}
_CONTROL_METHODS = {
    "reserve": "set_reserve",
    "mode": "set_mode",
    "grid_charging": "set_grid_charging",
    "grid_export": "set_grid_export",
}


def _short(val: object, limit: int = 200) -> str:
    """repr() for log lines, truncated: received values can hold newlines."""
    text = repr(val)
    return text if len(text) <= limit else text[:limit] + "…"


def _is_error_result(result: object) -> bool:
    """True when a library response reports an error instead of a value."""
    return isinstance(result, dict) and ("error" in result or "ERROR" in result)


def _island_ack_ok(result: object) -> bool:
    """Hardware acknowledgement, same rule as HTTP POST /control/islanding.

    Only ``{"result": 1}`` counts (an int, not True).
    """
    if not isinstance(result, dict) or _is_error_result(result):
        return False
    ack = result.get("result")
    return isinstance(ack, int) and not isinstance(ack, bool) and ack == 1


def _write_path(gateway_manager, gateway_id: str) -> Optional[str]:
    """The connection that writes reserve, mode and grid settings, or None.

    pypowerwall can only write these over cloud, FleetAPI or v1r (on TEDAPI
    full and plain local they log an ERROR and return None). The shared
    hybrid cloud connection only counts for the gateway it is bound to.
    Discovery and dispatch both use this, so Home Assistant only shows
    controls the gateway can run.
    """
    gw = gateway_manager.gateways.get(gateway_id)
    if gw is None:
        return None
    if gw.fleetapi:
        return "fleetapi"
    if gw.cloud_mode:
        return "cloud"
    if (
        gateway_manager._cloud_control is not None
        and gateway_manager._cloud_control_gateway_id == gateway_id
    ):
        return "hybrid cloud"
    if _gateway_is_v1r(gateway_manager, gateway_id):
        return "local v1r"
    return None


def _gateway_is_v1r(gateway_manager, gateway_id: str) -> bool:
    """True when the gateway is confirmed on the v1r transport (PW2 and PW3).

    Fails closed like the Console: an unknown mode (cold start, cloud
    failover) is not v1r.
    """
    try:
        from app.mqtt.ha_discovery import is_v1r_gateway

        status = gateway_manager.get_gateway(gateway_id)
        return is_v1r_gateway(
            gateway_manager.gateways.get(gateway_id),
            status.data if status else None,
        )
    except Exception:
        return False


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
