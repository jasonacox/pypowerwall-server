"""
pyPowerwall MQTT Monitor
========================
A lightweight tkinter GUI that subscribes to a pypowerwall-server MQTT broker
and shows live Powerwall telemetry updates.

Requirements:
    pip install paho-mqtt

Usage:
    python monitor.py [--host broker_host] [--port 1883] [--prefix pypowerwall]

The window shows live values for every gateway detected on the broker.
Values refresh automatically whenever the server publishes an update
(default: every 5 seconds).

Tested with Python 3.11+ on macOS, Linux, and Windows.
"""
import argparse
import json
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Attempt to import paho-mqtt; show a friendly error if missing
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print(
        "ERROR: paho-mqtt is not installed.\n"
        "Install it with:  pip install paho-mqtt\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Theme — dark palette
# ---------------------------------------------------------------------------

T = {
    "bg":           "#1E2228",   # window / canvas background
    "card_bg":      "#282C34",   # gateway card background
    "card_border":  "#3A3F4B",   # card border / divider
    "header_bg":    "#21252B",   # card header strip
    "bar_bg":       "#17191E",   # status bar
    "label_fg":     "#636D83",   # dim label text
    "value_fg":     "#ABB2BF",   # default value text
    "title_fg":     "#E5C07B",   # app title
    "header_fg":    "#61AFEF",   # gateway name in card header
    "time_fg":      "#4B5263",   # last-update timestamp
    "unit_fg":      "#4B5263",   # unit suffix
    "sep":          "#2C313A",   # separator line
    # value colours
    "green":        "#98C379",
    "yellow":       "#E5C07B",
    "blue":         "#61AFEF",
    "purple":       "#C678DD",
    "orange":       "#D19A66",
    "red":          "#E06C75",
    "cyan":         "#56B6C2",
    "grey":         "#5C6370",
}

# (label, unit, color-key, formatter)
#   formatter: "pct" | "watt" | "text"
SENSORS: list[tuple[str, str, str, str, str]] = [
    # key            label          unit   color      fmt
    ("battery",      "Battery",     "%",   "green",   "pct"),
    ("solar",        "Solar",       "W",   "yellow",  "watt"),
    ("grid",         "Grid",        "W",   "blue",    "watt"),
    ("home",         "Home Load",   "W",   "purple",  "watt"),
    ("powerwall",    "Powerwall",   "W",   "orange",  "watt"),
    ("reserve",      "Reserve",     "%",   "cyan",    "pct"),
    ("grid_status",  "Grid Status", "",    "value_fg","text"),
    ("mode",         "Mode",        "",    "value_fg","text"),
    ("version",      "Firmware",    "",    "grey",    "text"),
    ("online",       "Online",      "",    "value_fg","text"),
]

STATUS_COLORS: dict[str, str] = {
    "online":  "green",
    "offline": "red",
    "true":    "green",
    "false":   "red",
    "UP":      "green",
    "DOWN":    "red",
}

# Keys that use a smaller font or no fixed width
SPECIAL_FONT: dict[str, int] = {
    "version": 9,    # firmware string can be long
    "online":  14,   # circle indicator looks better larger
}
# Keys where we don't impose a character-width cap (let the column expand)
FREE_WIDTH_KEYS: frozenset[str] = frozenset({"mode", "version", "online"})


def fmt_value(key: str, fmt: str, raw: str) -> str:
    try:
        f = float(raw)
        if fmt == "pct":
            return f"{f:.1f}"
        if fmt == "watt":
            return f"{f:,.0f}"
    except ValueError:
        pass
    return raw


# ---------------------------------------------------------------------------
# MQTT worker thread
# ---------------------------------------------------------------------------

class MqttWorker(threading.Thread):
    def __init__(self, host: str, port: int, prefix: str,
                 username: Optional[str], password: Optional[str],
                 update_queue: queue.Queue):
        super().__init__(daemon=True, name="mqtt-worker")
        self.host = host
        self.port = port
        self.prefix = prefix
        self.username = username
        self.password = password
        self.queue = update_queue
        self._stop_event = threading.Event()
        self._client: Optional[mqtt.Client] = None

    def stop(self):
        self._stop_event.set()
        if self._client:
            self._client.disconnect()

    def run(self):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="pypowerwall-monitor",
            clean_session=True,
        )
        self._client = client
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self.queue.put(("status", "connecting", f"Connecting to {self.host}:{self.port}…"))
        try:
            client.connect(self.host, self.port, keepalive=60)
        except Exception as exc:
            self.queue.put(("status", "error", str(exc)))
            return
        while not self._stop_event.is_set():
            client.loop(timeout=1.0)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.queue.put(("status", "error", f"Connect failed: {reason_code}"))
        else:
            self.queue.put(("status", "connected", f"Connected to {self.host}:{self.port} — subscribed to {self.prefix}/#"))
            client.subscribe(f"{self.prefix}/#", qos=1)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if not self._stop_event.is_set():
            self.queue.put(("status", "reconnecting", f"Disconnected — reconnecting…"))

    def _on_message(self, client, userdata, msg):
        try:
            self.queue.put(("message", msg.topic,
                            msg.payload.decode("utf-8", errors="replace")))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class MonitorApp(tk.Tk):
    POLL_MS = 200

    def __init__(self, host: str, port: int, prefix: str,
                 username: Optional[str], password: Optional[str]):
        super().__init__()
        self.title("pyPowerwall MQTT Monitor")
        self.configure(bg=T["bg"])
        self.minsize(680, 420)
        self.resizable(True, True)

        self.prefix = prefix
        self._queue: queue.Queue = queue.Queue()
        self._gateways: dict[str, dict[str, str]] = {}
        self._gateway_frames: dict[str, "GatewayCard"] = {}
        self._last_update: dict[str, datetime] = {}

        self._build_ui()

        self._worker = MqttWorker(host, port, prefix, username, password, self._queue)
        self._worker.start()
        self.after(self.POLL_MS, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # ── Status bar (bottom) ────────────────────────────────────────
        bar = tk.Frame(self, bg=T["bar_bg"], pady=7, padx=14)
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        self._status_dot = tk.Label(bar, text="●", fg=T["grey"],
                                    bg=T["bar_bg"], font=("", 11))
        self._status_dot.pack(side=tk.LEFT)

        self._status_var = tk.StringVar(value="Initialising…")
        tk.Label(bar, textvariable=self._status_var, fg="#636D83",
                 bg=T["bar_bg"], font=("", 10)).pack(side=tk.LEFT, padx=(6, 0))

        # ── Scrollable content area ────────────────────────────────────
        self._canvas = tk.Canvas(self, bg=T["bg"], highlightthickness=0,
                                  bd=0)
        vsb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._inner = tk.Frame(self._canvas, bg=T["bg"])
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._win_id, width=e.width))

        # ── App title ─────────────────────────────────────────────────
        tk.Label(self._inner, text="⚡  pyPowerwall MQTT Monitor",
                 font=("", 17, "bold"), fg=T["title_fg"], bg=T["bg"],
                 pady=18).pack()

        self._no_data = tk.Label(self._inner,
                                  text="Waiting for data from broker…",
                                  font=("", 12), fg=T["grey"], bg=T["bg"])
        self._no_data.pack(pady=30)

    def _drain_queue(self):
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "message":
                    self._handle_msg(item[1], item[2])
        except queue.Empty:
            pass
        finally:
            self.after(self.POLL_MS, self._drain_queue)

    def _set_status(self, state: str, text: str):
        self._status_var.set(text)
        color = {"connected": T["green"], "error": T["red"],
                 "reconnecting": T["orange"], "connecting": T["blue"]}.get(state, T["grey"])
        self._status_dot.config(fg=color)

    def _handle_msg(self, topic: str, payload: str):
        parts = topic.split("/")
        if len(parts) < 3 or not topic.startswith(self.prefix + "/"):
            return
        gw_id = parts[1]
        suffix = "/".join(parts[2:])
        if suffix.startswith("config") or "/config" in suffix:
            return

        if gw_id not in self._gateways:
            self._gateways[gw_id] = {}
            card = GatewayCard(self._inner, gw_id)
            card.pack(fill=tk.X, padx=20, pady=10)
            self._gateway_frames[gw_id] = card
            if self._no_data.winfo_exists():
                self._no_data.pack_forget()

        if suffix == "name":
            card = self._gateway_frames.get(gw_id)
            if card:
                card.set_display_name(payload)
            return

        self._gateways[gw_id][suffix] = payload
        self._last_update[gw_id] = datetime.now()
        card = self._gateway_frames.get(gw_id)
        if card:
            card.refresh(self._gateways[gw_id], self._last_update[gw_id])

    def _on_close(self):
        self._worker.stop()
        self.destroy()


# ---------------------------------------------------------------------------
# Gateway card
# ---------------------------------------------------------------------------

class GatewayCard(tk.Frame):
    """Dark card showing all sensor rows for one gateway."""

    def __init__(self, parent, gateway_id: str):
        super().__init__(parent, bg=T["card_border"], padx=1, pady=1)
        self.gateway_id = gateway_id
        self._vars: dict[str, tk.StringVar] = {}
        self._labels: dict[str, tk.Label] = {}
        self._last_var = tk.StringVar(value="—")
        self._title_var = tk.StringVar(value=gateway_id.upper())
        self._build()

    def set_display_name(self, name: str) -> None:
        """Update the card title with the friendly gateway name."""
        self._title_var.set(name)

    def _build(self):
        inner = tk.Frame(self, bg=T["card_bg"])
        inner.pack(fill=tk.BOTH, expand=True)

        # ── Card header ───────────────────────────────────────────────
        hdr = tk.Frame(inner, bg=T["header_bg"], padx=16, pady=10)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, textvariable=self._title_var, font=("", 12, "bold"),
                 fg=T["header_fg"], bg=T["header_bg"]).pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=T["header_bg"])
        right.pack(side=tk.RIGHT)
        tk.Label(right, text="Last update  ", font=("", 9),
                 fg=T["time_fg"], bg=T["header_bg"]).pack(side=tk.LEFT)
        tk.Label(right, textvariable=self._last_var, font=("", 9, "bold"),
                 fg=T["label_fg"], bg=T["header_bg"]).pack(side=tk.LEFT)

        # thin separator
        tk.Frame(inner, bg=T["sep"], height=1).pack(fill=tk.X)

        # ── Sensor grid ───────────────────────────────────────────────
        body = tk.Frame(inner, bg=T["card_bg"], padx=20, pady=14)
        body.pack(fill=tk.X)

        # 2 columns of sensors, left and right halves separated by a spacer
        # Layout per sensor: [label] [value] [unit]
        COLS = 2
        for idx, (key, label, unit, color_key, fmt) in enumerate(SENSORS):
            col_base = (idx % COLS) * 4        # 0 or 4
            row = idx // COLS

            # Vertical rule between the two halves
            if col_base == 4:
                tk.Frame(body, bg=T["card_border"], width=1).grid(
                    row=row, column=3, sticky="ns", padx=(16, 0), pady=2
                )

            # Label
            lbl = tk.Label(body, text=label, font=("", 10), width=11,
                           anchor="e", fg=T["label_fg"], bg=T["card_bg"])
            lbl.grid(row=row, column=col_base, sticky="e",
                     padx=(16 if col_base == 4 else 0, 8), pady=5)

            # Value
            var = tk.StringVar(value="—")
            self._vars[key] = var
            fsize = SPECIAL_FONT.get(key, 11)
            val_kwargs: dict = dict(
                textvariable=var,
                font=("", fsize, "bold"),
                anchor="w",
                fg=T["value_fg"],
                bg=T["card_bg"],
            )
            if key not in FREE_WIDTH_KEYS:
                val_kwargs["width"] = 10
            val_lbl = tk.Label(body, **val_kwargs)
            val_lbl.grid(row=row, column=col_base + 1, sticky="ew", pady=5)
            self._labels[key] = val_lbl

            # Unit
            tk.Label(body, text=unit, font=("", 9), width=3, anchor="w",
                     fg=T["unit_fg"], bg=T["card_bg"]).grid(
                row=row, column=col_base + 2, sticky="w"
            )

        # Make value columns expand equally
        body.columnconfigure(1, weight=1)
        body.columnconfigure(5, weight=1)

    def refresh(self, data: dict[str, str], ts: datetime):
        self._last_var.set(ts.strftime("%H:%M:%S"))

        for key, label, unit, default_color_key, fmt in SENSORS:
            var = self._vars.get(key)
            lbl = self._labels.get(key)
            if var is None or lbl is None:
                continue

            raw = data.get(key)
            if raw is None and "status" in data:
                try:
                    summary = json.loads(data["status"])
                    v = summary.get(key)
                    raw = str(v) if v is not None else None
                except (json.JSONDecodeError, KeyError):
                    pass

            if key == "online":
                if raw is None:
                    var.set("●")
                    lbl.config(fg=T["grey"])
                else:
                    is_online = raw.lower() in ("true", "1", "online", "yes")
                    var.set("●")
                    lbl.config(fg=T["green"] if is_online else T["red"])
            elif raw is None:
                var.set("—")
                lbl.config(fg=T["grey"])
            else:
                var.set(fmt_value(key, fmt, raw))
                color_key = STATUS_COLORS.get(raw, default_color_key)
                lbl.config(fg=T.get(color_key, T["value_fg"]))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="pyPowerwall MQTT Monitor GUI")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--prefix", default="pypowerwall")
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    app = MonitorApp(
        host=args.host,
        port=args.port,
        prefix=args.prefix,
        username=args.username,
        password=args.password,
    )
    app.mainloop()


if __name__ == "__main__":
    main()
