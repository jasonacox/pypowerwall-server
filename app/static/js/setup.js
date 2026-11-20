(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const SETUP_TOKEN_KEY = "pw_setup_token";
    const SETUP_REMEMBER_KEY = "pw_setup_remember";
    const state = {
        token: "",
        status: null,
        schema: null,
        gatewaySeq: 0,
        step: 1,
        initialSettings: {},
        validated: false,
        envRevision: null,
        envTarget: null,
    };

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function message(text, kind) {
        const box = $("global-message");
        if (!text) {
            box.hidden = true;
            box.textContent = "";
            box.className = "setup-alert";
            return;
        }
        box.hidden = false;
        box.textContent = text;
        box.className = "setup-alert " + (kind || "");
    }

    async function api(path, options) {
        const opts = Object.assign({}, options || {});
        opts.headers = Object.assign(
            {
                "Content-Type": "application/json",
                "X-PW-Setup-Token": state.token,
            },
            opts.headers || {}
        );
        const response = await fetch("/api/setup" + path, opts);
        const text = await response.text();
        let body = null;
        try {
            body = text ? JSON.parse(text) : null;
        } catch (err) {
            body = text;
        }
        if (!response.ok) {
            let detail = body && body.detail ? body.detail : text;
            if (typeof detail !== "string") {
                detail = JSON.stringify(detail);
            }
            const error = new Error(detail || ("HTTP " + response.status));
            error.status = response.status;
            throw error;
        }
        return body;
    }

    const MODE_DESCRIPTIONS = {
        tedapi:
            "Local data over the gateway WiFi (normally 192.168.91.1). Read-only unless hybrid.",
        tedapi_v1r:
            "Local data over the wired LAN with a registered RSA key. Can write settings and island locally.",
        local:
            "Powerwall 2 local API, like the proxy's local mode. Detected on connect: the gateway answers /api/status.",
        basic_lan:
            "Powerwall 3 wired LAN, detected on connect when /api/status isn't served (re-checked after 5 minutes, then doubling up to 6 hours). Core metrics only: aggregates, battery level, grid status.",
        hybrid_tedapi:
            "Local customer login plus embedded TEDAPI; normally at the gateway WiFi address.",
        cloud:
            "Tesla cloud. Use the Tesla login in the setup panel below to create the auth files.",
        fleetapi: "Tesla Fleet API.",
        hybrid:
            "Reads stay local; control writes use a Tesla cloud connection, which also fills values the local API lacks (Basic LAN mode and reserve, grid charging/export settings).",
        hybrid_fleetapi:
            "Reads stay local; control writes use Tesla Fleet API. Requires a local credential, Tesla account email, auth path and FleetAPI login.",
    };

    const SETTING_LABELS = {
        siteid: "Site ID",
        server_host: "Bind address",
        pw_authpath: "Auth path",
        auth_mode: "Auth mode",
        cache_expire: "Polling interval",
        cache_ttl: "Cache TTL",
        tedapi_recovery: "TEDAPI recovery",
        tedapi_probe_interval: "TEDAPI probe interval",
        mqtt_qos: "MQTT QoS",
        proxy_base_url: "Proxy base URL",
        cors_origins: "CORS origins",
        local_first: "Local-first",
        wifi_v1r_fallback: "WiFi v1r fallback",
        neg_solar: "Allow negative solar",
        https_mode: "HTTPS mode",
        https_port: "HTTPS port",
        https_cert: "HTTPS certificate",
        https_key: "HTTPS private key",
        timeseries_retention: "Time-series retention",
        timeseries_daily_retention: "Time-series daily retention",
        timeseries_signal_retention: "Time-series signal retention",
        timeseries_signal_interval: "Time-series signal interval",
        timeseries_path: "Time-series path",
        fleetapi_http: "FleetAPI HTTP mode",
        mqtt_ha_discovery: "MQTT HA discovery",
        mqtt_ha_prefix: "MQTT HA prefix",
        mqtt_topic_prefix: "MQTT topic prefix",
        mqtt_tls_ca_cert: "MQTT TLS CA certificate",
    };

    const SETTING_DESCRIPTIONS = {
        server_host: "Address to listen on. Also server.host in the config file.",
        server_port: "Port to listen on. Also server.port in the config file.",
        debug: "Debug logging, including one access log line per HTTP request.",
        https_mode:
            "Also serves HTTPS on the HTTPS port; HTTP keeps working for existing clients and health checks.",
        https_port:
            "HTTPS port when HTTPS mode is enabled. Not 8676, which Powerwall-Dashboard's weather service uses.",
        https_cert:
            "Path to a PEM certificate file for HTTPS, with the private key (both or neither). Without them the server generates a self-signed certificate once in /data/tls (else data/tls) and reuses it.",
        https_key: "Path to the PEM private key file for the HTTPS certificate (unencrypted).",
        proxy_base_url:
            "Sub-path when served behind a reverse proxy, e.g. /pypowerwall. The prefix is stripped from incoming paths and added to URLs in the UI pages.",
        cors_origins:
            "JSON array of allowed CORS origins. * allows any origin without credentials; an explicit list allows credentials.",
        style:
            "Power Flow theme at /: clear, black, white, grafana, grafana-dark, solar or dakboard. /?style= overrides it per request.",
        cache_expire: "Seconds between background polls. Data routes read from this cache.",
        browser_cache: "Accepted for proxy compatibility; the server does not use it.",
        timeout: "HTTP timeout in seconds for local gateway requests.",
        pool_maxsize: "pypowerwall HTTP connection pool size.",
        suppress_network_errors: "Accepted for proxy compatibility; the server does not use it.",
        network_error_rate_limit: "Accepted for proxy compatibility; the server does not use it.",
        fail_fast: "Accepted for proxy compatibility; the server does not use it.",
        graceful_degradation:
            "While a gateway is offline, keep serving its last good data for up to Cache TTL seconds.",
        health_check: "Accepted for proxy compatibility; the server does not use it.",
        cache_ttl:
            "Maximum age in seconds of the last good data served while offline; after that, data routes return their empty defaults.",
        tedapi_recovery:
            "Probe local gateways and reconnect when the connection drops to SolarOnly fallback. State shows in /health and /stats.",
        tedapi_probe_interval: "Seconds between those probes.",
        neg_solar:
            "Allow negative solar power, as in the proxy. With no, negative solar is set to 0 and added to home load.",
        rate_limit_enabled: "Enable per-client-IP fixed-window rate limiting on every HTTP route.",
        rate_limit_max_requests: "Requests per window per client IP.",
        rate_limit_window_seconds: "Window length in seconds.",
        rate_limit_max_buckets:
            "Client IPs tracked before stale or oldest entries are pruned.",
        siteid:
            "Tesla energy site ID on accounts with several sites; picks the site for Cloud and FleetAPI gateways and the site the hybrid cloud connection controls.",
        control_secret:
            "Enables all write routes; clients send it as the control token. Unset means read-only. The setup API also accepts it as the admin token.",
        islanding_cooldown:
            "Minimum seconds between TEDAPI v1r islanding commands per gateway; 0 disables the cooldown.",
        battery_dispatch:
            "Yes enables timed battery dispatch (POST /control/dispatch and the Console panel). Needs PW_CONTROL_SECRET and a local TEDAPI v1r connection. POST /control/resume works without it.",
        cloud_hybrid:
            "Starts the hybrid cloud connection without PW_CONTROL_SECRET, for its read-only values (Basic LAN mode and reserve, grid charging/export settings). With PW_CONTROL_SECRET set it starts anyway. PW_EMAIL alone does not start it.",
        cloud_read_fallback:
            "Reads the Cloud-Mode data set from the bound Cloud/FleetAPI connection after three consecutive failed local polls. Needs an unambiguous site (PW_SITEID, .pypowerwall.site, a single-site account or a live FleetAPI connection); PW_FAILOVER=no disables it.",
        local_first:
            "Sends reserve, mode and grid writes over a confirmed local v1r connection first, even when a hybrid cloud connection exists. Needs a v1r or hybrid gateway.",
        wifi_v1r_fallback:
            "Makes signed WiFi-v1r the only leader route after the library's LAN failover trips, for reads and for reserve/mode/grid, max backup and islanding writes; there is no legacy WiFi TEDAPI fallback. Needs PW_WIFI_HOST and an RSA key.",
        failover:
            "pypowerwall transport failover. No pins the configured transports: the v1r leader stays on the wired LAN, failed requests are not rerouted, and a local connection never falls back to Cloud or FleetAPI.",
        fleetapi_http:
            "1.1 forces HTTP/1.1 for all Tesla FleetAPI requests (default: HTTP/2), including a hybrid cloud connection that uses FleetAPI. Escape hatch for intermittent HTTP/2 connection-reuse errors (COMPRESSION_ERROR, h2 state errors) on some sites; reported in /stats under config.",
        pw_authpath:
            "Directory with the Tesla auth files. Also the default auth directory for the hybrid cloud connection. FleetAPI uses .pypowerwall.fleetapi.",
        auth_mode: "Accepted for proxy compatibility; the server does not use it.",
        cache_file:
            "pypowerwall auth cache file. Defaults to PW_AUTH_PATH/.powerwall, else /tmp/.powerwall.",
        pw_rsa_key_path:
            "RSA-4096 private key PEM; selects TEDAPI v1r in the single-gateway form.",
        pw_wifi_host:
            "Gateway WiFi address used for follower Powerwalls and as the fallback when the wired LAN fails.",
        timeseries_retention:
            "Raw power sample retention. 0 = unlimited; -1 disables the whole subsystem (no SQLite file, no /history data).",
        timeseries_daily_retention:
            "Retention of daily kWh totals and daily temperature/fan rollups. 0 = unlimited.",
        timeseries_signal_retention:
            "Raw Powerwall temperature/fan sample retention. 0 = unlimited; -1 stops recording them.",
        timeseries_signal_interval:
            "Seconds between temperature/fan samples per gateway; values below 30s are raised to 30s.",
        timeseries_path:
            "SQLite database path. Defaults to /data/timeseries.db if /data exists, else data/timeseries.db.",
        mqtt_host: "Broker host or IP. Required to enable MQTT.",
        mqtt_port: "Broker port.",
        mqtt_username: "Broker username.",
        mqtt_password: "Broker password.",
        mqtt_tls: "Connect with TLS.",
        mqtt_tls_ca_cert: "CA certificate path for TLS.",
        mqtt_tls_insecure: "Skip broker certificate verification. Testing only.",
        mqtt_topic_prefix: "Root topic prefix.",
        mqtt_retain: "Publish retained messages.",
        mqtt_qos: "QoS level (0, 1 or 2).",
        mqtt_ha_discovery: "Publish Home Assistant discovery payloads.",
        mqtt_ha_prefix: "Home Assistant discovery prefix.",
        mqtt_client_id: "MQTT client identifier.",
        mqtt_keepalive: "Keepalive in seconds.",
        mqtt_controls:
            "Opt-in bitmask for Home Assistant controls (needs PW_CONTROL_SECRET and MQTT_USERNAME/MQTT_PASSWORD): 1 reserve (incl. reserve_hold), 2 mode, 4 grid_charging, 8 grid_export, 16 islanding, 32 battery dispatch/resume. 15 = value controls, 31 = value controls + islanding, 63 = all, 0 = monitoring only. Existing 15/31 masks do not enable dispatch. Islanding and dispatch need v1r; dispatch also needs PW_BATTERY_DISPATCH=yes. Anything other than an integer 0-63 is logged as an error and treated as 0.",
    };

    const MQTT_CONTROL_CHOICES = [
        { value: "0", label: "0 — Monitoring only" },
        { value: "1", label: "1 — Backup reserve" },
        { value: "2", label: "2 — Operating mode" },
        { value: "4", label: "4 — Grid charging" },
        { value: "8", label: "8 — Grid export" },
        { value: "15", label: "15 — Backup reserve, mode, grid charging and grid export" },
        { value: "16", label: "16 — Islanding" },
        { value: "31", label: "31 — Backup reserve, mode, grid charging, grid export and islanding" },
        { value: "32", label: "32 — Battery dispatch/resume" },
        { value: "47", label: "47 — Backup reserve, mode, grid charging, grid export and battery dispatch/resume" },
        { value: "63", label: "63 — All controls" },
    ];

    const SETTING_CHOICES = {
        style: [
            { value: "clear", label: "Clear" },
            { value: "black", label: "Black" },
            { value: "white", label: "White" },
            { value: "grafana", label: "Grafana" },
            { value: "grafana-dark", label: "Grafana Dark" },
            { value: "solar", label: "Solar" },
            { value: "dakboard", label: "Dakboard" },
        ],
        auth_mode: [{ value: "cookie", label: "cookie" }],
        fleetapi_http: [
            { value: "", label: "Default (HTTP/2)" },
            { value: "1.1", label: "1.1 (HTTP/1.1)" },
        ],
        mqtt_qos: [
            { value: "0", label: "0" },
            { value: "1", label: "1" },
            { value: "2", label: "2" },
        ],
        mqtt_controls: MQTT_CONTROL_CHOICES,
    };

    const MQTT_CONTROLS_WARNING = [
        "<strong>⚠️ WARNING: USE AT YOUR OWN RISK</strong>",
        "<p>MQTT controls let anything allowed to publish to the control topics on your MQTT broker (<code>{MQTT_TOPIC_PREFIX}/+/control/+/set</code>; on a broker without an ACL, that's every client) change how your Powerwall runs: the backup reserve, the operating mode, grid charging and grid export, and (with <code>16</code>) disconnecting your home from the grid. A misconfigured or compromised broker, a hacked smart-home device, a buggy automation or a simple mistake could:</p>",
        "<ul><li>cause a <strong>power outage</strong> in your home,</li><li>leave you <strong>without backup power</strong> when the grid goes down (for example, a reserve set to 0),</li><li><strong>damage</strong> equipment or appliances, or</li><li>raise your energy costs or conflict with your utility agreement.</li></ul>",
        '<p>This software is provided "as is", without warranty of any kind (see the MIT license), and is not made or supported by Tesla. <strong>By setting <code>MQTT_CONTROLS</code> to anything other than <code>0</code>, you acknowledge these risks and accept full responsibility for the results.</strong> Think twice before turning this on: enable only the controls you need, leave going off grid (<code>16</code>) off unless you truly need it, and secure your broker first.</p>',
    ].join("");

    const BATTERY_DISPATCH_WARNING = [
        "<strong>⚠️ WARNING: USE AT YOUR OWN RISK. IT COULD VOID YOUR WARRANTY.</strong>",
        "<p>Battery dispatch takes direct control of your Powerwall's battery through an undocumented Tesla command. It is meant only for people who know what they are doing.</p>",
        "<p><strong>Battery dispatch could void or limit your Tesla Powerwall warranty.</strong> The Powerwall warranty quoted in pypowerwall#450 gives unlimited cycles only for solar self-consumption, time-based control and backup; any other use is limited to 37.8 MWh of total energy throughput. Check your warranty first.</p>",
        "<ul><li>A <strong>discharge</strong> overrides the backup reserve and the grid export setting. It can drain the battery below your reserve, leaving you <strong>without backup power</strong> if the grid goes down, and it can <strong>export to the grid</strong> even with export set to <code>pv_only</code> or <code>never</code>, which may conflict with your utility agreement.</li><li>A <strong>charge</strong> can draw power from the grid at your current rate, and may ignore the grid charging setting (not yet verified).</li><li>Behavior during a grid outage, and on firmware we haven't tested, is unknown, and any Tesla firmware update could change it.</li></ul>",
        "<p><strong>We have no idea whether incorrect use could damage your home, your Powerwall system or the grid.</strong> This software is provided \"as is\", without warranty of any kind (see the MIT license), and is not made or supported by Tesla. Battery dispatch is off unless you set <code>PW_BATTERY_DISPATCH=yes</code>. <strong>By setting it, you accept these risks and full responsibility for the results.</strong></p>",
    ].join("");

    const ISLANDING_WARNING = [
        "<strong>⚠️ WARNING — Grid island control physically operates your home's grid contactor. Use with extreme care.</strong>",
        "<p>Going off-grid can interrupt power and temporarily interrupt solar production. While islanded, Powerwall may raise the home's electrical frequency to limit or stop solar production, particularly when the battery is near full or available charging capacity is limited. This can cause lights to flicker and affect frequency-sensitive equipment.</p>",
        "<p>Your home depends on available battery and solar power while disconnected from the grid; if those cannot support the load, your home can lose power. Do not assume that a nearly full battery guarantees a seamless transition.</p>",
        "<p>Do not automate these commands without understanding the failure modes; test only when someone is present. An acknowledgement is not confirmation that the home changed grid state: check <code>GET /api/system_status/grid_status</code> after the transition, allowing for the configured polling interval. After an error or timeout, the outcome may be unknown and the command may still complete; do not automatically retry or send the opposite command.</p>",
    ].join("");

    const SETTING_WARNINGS = {
        battery_dispatch: BATTERY_DISPATCH_WARNING,
        mqtt_controls: MQTT_CONTROLS_WARNING,
        islanding_cooldown: ISLANDING_WARNING,
    };

    const SETTING_GROUP_ORDER = [
        "Server, HTTPS and UI",
        "Performance and robustness",
        "Rate limiting",
        "Local and WiFi fallback",
        "Cloud and failover",
        "Control and safety",
        "MQTT and Home Assistant",
        "Time series",
        "Files and legacy",
        "Advanced",
    ];

    const SETTING_ORDER = [
        "server_host",
        "server_port",
        "proxy_base_url",
        "cors_origins",
        "debug",
        "style",
        "https_mode",
        "https_port",
        "https_cert",
        "https_key",
        "cache_expire",
        "timeout",
        "pool_maxsize",
        "cache_ttl",
        "graceful_degradation",
        "tedapi_recovery",
        "tedapi_probe_interval",
        "neg_solar",
        "browser_cache",
        "suppress_network_errors",
        "network_error_rate_limit",
        "fail_fast",
        "health_check",
        "rate_limit_enabled",
        "rate_limit_max_requests",
        "rate_limit_window_seconds",
        "rate_limit_max_buckets",
        "local_first",
        "wifi_v1r_fallback",
        "siteid",
        "failover",
        "fleetapi_http",
        "cloud_hybrid",
        "cloud_read_fallback",
        "control_secret",
        "islanding_cooldown",
        "battery_dispatch",
        "mqtt_host",
        "mqtt_port",
        "mqtt_username",
        "mqtt_password",
        "mqtt_tls",
        "mqtt_tls_ca_cert",
        "mqtt_tls_insecure",
        "mqtt_topic_prefix",
        "mqtt_retain",
        "mqtt_qos",
        "mqtt_ha_discovery",
        "mqtt_ha_prefix",
        "mqtt_client_id",
        "mqtt_keepalive",
        "mqtt_controls",
        "timeseries_retention",
        "timeseries_daily_retention",
        "timeseries_signal_retention",
        "timeseries_signal_interval",
        "timeseries_path",
        "pw_authpath",
        "auth_mode",
        "cache_file",
    ];

    const SECRET_EYE_OPEN =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    const SECRET_EYE_CLOSED =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
    const INFO_ICON =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';

    function settingLabel(name, field) {
        let text = SETTING_LABELS[name];
        if (!text) {
            text = name.replace(/_/g, " ");
            text = text.replace(/\b(https|mqtt|ha|cors|url|id|tls|ca|fleetapi|api)\b/gi, (word) =>
                word.toLowerCase() === "fleetapi"
                    ? "FleetAPI"
                    : word.toUpperCase()
            );
            text = text.charAt(0).toUpperCase() + text.slice(1);
        }
        return text;
    }

    function settingIsOptional(field) {
        return Boolean(field && String(field.type || "").includes("Optional"));
    }

    function enhanceSecretInputs(root) {
        const scope = root || document;
        scope.querySelectorAll('input[type="password"]').forEach((input) => {
            if (input.dataset.secretEnhanced === "true" || input.closest(".control-token-row")) {
                return;
            }
            input.dataset.secretEnhanced = "true";
            const row = document.createElement("div");
            row.className = "control-token-row";
            if (input.id === "setting-control_secret") {
                row.classList.add("has-secret-actions");
            }
            input.parentNode.insertBefore(row, input);
            row.appendChild(input);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "control-btn control-icon-btn";
            button.innerHTML = SECRET_EYE_OPEN;
            const noun = input.dataset.secretLabel || "password";
            button.title = "Show " + noun;
            button.setAttribute("aria-label", "Show " + noun);
            if (input.id) button.setAttribute("aria-controls", input.id);
            button.addEventListener("click", () => {
                const show = input.type === "password";
                input.type = show ? "text" : "password";
                button.innerHTML = show ? SECRET_EYE_CLOSED : SECRET_EYE_OPEN;
                button.title = (show ? "Hide " : "Show ") + noun;
                button.setAttribute("aria-label", button.title);
            });
            row.appendChild(button);
            if (input.id && input.id.startsWith("setting-")) {
                const clear = document.createElement("button");
                clear.type = "button";
                clear.className = "control-btn";
                clear.textContent = "Clear";
                clear.title = "Clear the stored secret";
                clear.setAttribute("aria-label", "Clear the stored secret");
                clear.addEventListener("click", () => {
                    input.value = "";
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.dataset.secretCleared = "true";
                });
                row.appendChild(clear);
                input.addEventListener("input", () => {
                    delete input.dataset.secretCleared;
                });
            }
            if (input.id === "setting-control_secret") {
                const generate = document.createElement("button");
                generate.type = "button";
                generate.className = "control-btn";
                generate.textContent = "Generate";
                generate.title = "Generate a random control token";
                generate.setAttribute("aria-label", "Generate a random control token");
                generate.addEventListener("click", () => {
                    const bytes = new Uint8Array(32);
                    crypto.getRandomValues(bytes);
                    input.type = "text";
                    input.value = Array.from(bytes, (byte) =>
                        byte.toString(16).padStart(2, "0")
                    ).join("");
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    button.innerHTML = SECRET_EYE_CLOSED;
                    button.title = "Hide token";
                    button.setAttribute("aria-label", "Hide token");
                });
                row.appendChild(generate);
            }
        });
    }

    function isRememberedSetupToken() {
        return $("setup-remember") && $("setup-remember").checked === true;
    }

    function clearStoredSetupToken() {
        try {
            localStorage.removeItem(SETUP_TOKEN_KEY);
            sessionStorage.removeItem(SETUP_TOKEN_KEY);
        } catch (err) {}
    }

    function storeSetupToken(value) {
        const token = String(value || "").trim();
        try {
            if (!token) {
                clearStoredSetupToken();
                return;
            }
            if (isRememberedSetupToken()) {
                localStorage.setItem(SETUP_TOKEN_KEY, token);
                sessionStorage.removeItem(SETUP_TOKEN_KEY);
            } else {
                sessionStorage.setItem(SETUP_TOKEN_KEY, token);
                localStorage.removeItem(SETUP_TOKEN_KEY);
            }
        } catch (err) {}
    }

    function initSetupTokenStorage() {
        const input = $("setup-token");
        const rememberBox = $("setup-remember");
        let saved = "";
        try {
            saved = localStorage.getItem(SETUP_TOKEN_KEY) || "";
            const remember = !!saved || localStorage.getItem(SETUP_REMEMBER_KEY) === "1";
            if (rememberBox) rememberBox.checked = remember;
            if (!saved) saved = sessionStorage.getItem(SETUP_TOKEN_KEY) || "";
        } catch (err) {
            saved = "";
        }
        if (saved && input) input.value = saved;
        if (input) {
            input.addEventListener("input", () => storeSetupToken(input.value));
        }
        if (rememberBox) {
            rememberBox.addEventListener("change", () => {
                try {
                    localStorage.setItem(SETUP_REMEMBER_KEY, rememberBox.checked ? "1" : "0");
                } catch (err) {}
                storeSetupToken(input ? input.value : "");
            });
        }
    }

    function fieldLabel(field) {
        const isFieldLabel = (child) =>
            child.tagName === "LABEL" &&
            !child.classList.contains("control-remember") &&
            !child.classList.contains("control-toggle");
        const direct = Array.from(field.children).find(isFieldLabel);
        if (direct) return direct;
        const row = Array.from(field.children).find(
            (child) => child.classList && child.classList.contains("control-label-row")
        );
        return row
            ? row.querySelector("label:not(.control-remember):not(.control-toggle)")
            : null;
    }

    function fieldHints(field) {
        return Array.from(field.children).filter(
            (child) => child.classList && child.classList.contains("hint")
        );
    }

    function fieldLabelText(label) {
        const clone = label.cloneNode(true);
        clone.querySelectorAll(".optional").forEach((node) => node.remove());
        return clone.textContent.replace(/\s+/g, " ").trim();
    }

    function openFieldInfo(button) {
        const field = button.closest("[data-info-field], .field, .settings-field");
        if (!field) return;
        const label = fieldLabel(field);
        const title = button.dataset.infoTitle || (label ? fieldLabelText(label) : "Field information");
        const body = fieldHints(field)
            .map((hint) => hint.textContent.replace(/\s+/g, " ").trim())
            .filter(Boolean)
            .join("\n\n");
        const dialog = $("setup-info-dialog");
        if (!dialog || typeof dialog.showModal !== "function") {
            window.alert(title + "\n\n" + body);
            return;
        }
        $("setup-info-icon").innerHTML = INFO_ICON;
        $("setup-info-title").textContent = title;
        $("setup-info-body").textContent = body;
        dialog.className = "confirm-dialog info";
        if (dialog.open) dialog.close();
        dialog.showModal();
        $("setup-info-ok").focus();
    }

    function enhanceFieldHints(root) {
        const scope = root || document;
        scope.querySelectorAll("[data-info-field], .field, .settings-field").forEach((field) => {
            if (field.dataset.infoEnhanced === "true") return;
            const label = fieldLabel(field);
            if (!label) return;
            const hints = fieldHints(field).filter((hint) => hint.textContent.trim());
            if (!hints.length) return;
            field.dataset.infoEnhanced = "true";
            const row = document.createElement("div");
            row.className = "control-label-row";
            label.parentNode.insertBefore(row, label);
            row.appendChild(label);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "control-btn control-icon-btn control-info-btn";
            button.innerHTML = INFO_ICON;
            const labelText = fieldLabelText(label);
            button.dataset.infoTitle = labelText;
            button.title = "About " + labelText;
            button.setAttribute("aria-label", "About " + labelText);
            button.setAttribute("aria-haspopup", "dialog");
            button.setAttribute("aria-controls", "setup-info-dialog");
            button.addEventListener("click", () => openFieldInfo(button));
            row.appendChild(button);
            hints.forEach((hint) => {
                hint.hidden = true;
            });
        });
    }

    function setStep(step) {
        state.step = step;
        $("setup-intro").hidden = step > 1;
        if (step > 1 && !$("global-message").classList.contains("error")) {
            message("");
        }
        document.querySelectorAll(".wizard-step").forEach((panel) => {
            panel.classList.toggle("active", Number(panel.dataset.stepPanel) === step);
        });
        document.querySelectorAll("#stepper li").forEach((item) => {
            const value = Number(item.dataset.step);
            item.classList.toggle("active", value === step);
            item.classList.toggle("done", value < step);
        });
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function statusLabel(value) {
        const labels = {
            unconfigured: "Not configured",
            configured: "Configured",
            partial: "Partially configured",
            invalid: "Invalid configuration",
            unreachable: "Gateway unreachable",
            auth_failed: "Authentication failed",
            env_managed: "Managed by environment",
            restart_required: "Restart required",
            none: "None",
            pw_config: "PW_CONFIG",
            pw_gateways: "PW_GATEWAYS",
            legacy_env: "Legacy environment",
            default_file: "Default file",
        };
        return labels[String(value || "").toLowerCase()] || String(value || "unknown");
    }

    function renderStatus() {
        const status = state.status;
        $("status-card").hidden = false;
        $("status-pill").textContent = statusLabel(status.state);
        $("status-source").textContent = statusLabel(status.config_source);
        $("status-path").textContent = status.config_path || "—";
        $("status-gateways").textContent = String(status.gateway_count);
        $("status-writable").textContent = status.can_write ? "Yes" : "No";
        const warnings = $("status-warnings");
        const items = (status.warnings || []).slice();
        if (status.config_error) {
            items.unshift(status.config_error);
        }
        if (!status.can_write) {
            items.unshift(
                "Configuration is managed by environment variables or PW_GATEWAYS and cannot be overwritten by the wizard."
            );
        }
        if (items.length) {
            warnings.hidden = false;
            warnings.className = "notice warning";
            warnings.innerHTML = items.map((item) => "<div>" + escapeHtml(item) + "</div>").join("");
        } else {
            warnings.hidden = true;
        }
        $("continue-to-gateways").disabled = false;
        $("delete-config").hidden = !(
            status.can_write &&
            status.config_source === "default_file" &&
            status.config_file_revision
        );
        state.initialSettings = Object.assign({}, status.settings || {});
    }

    function loadGateways() {
        const list = $("gateway-list");
        list.innerHTML = "";
        state.gatewaySeq = 0;
        const configs = (state.status && state.status.gateway_configs) || [];
        if (!configs.length) {
            addGateway({});
        } else {
            configs.forEach((config) => addGateway(config));
        }
        const sourceWarning = $("gateway-source-warning");
        if (state.status && !state.status.can_write) {
            sourceWarning.hidden = false;
            sourceWarning.className = "notice warning";
            sourceWarning.textContent =
                "The running configuration comes from environment variables or PW_GATEWAYS. You can inspect and test it, but saving is disabled until that source is removed.";
        } else {
            sourceWarning.hidden = true;
        }
    }

    function addGateway(config) {
        const template = $("gateway-template");
        const fragment = template.content.cloneNode(true);
        const card = fragment.querySelector(".gateway-card");
        const index = state.gatewaySeq++;
        card.dataset.index = String(index);
        card.dataset.originalId = config.id || "";
        card.querySelector(".gateway-index").textContent = "Gateway " + (index + 1);
        card.querySelector(".gateway-title").textContent = config.name || config.id || "New gateway";
        const set = (field, value) => {
            const input = card.querySelector('[data-field="' + field + '"]');
            if (input && value !== undefined && value !== null) {
                input.value = value;
                input.dataset.initial = String(value);
            }
        };
        [
            "id",
            "name",
            "mode",
            "type",
            "host",
            "port",
            "timezone",
            "rsa_key_path",
            "wifi_host",
            "email",
            "authpath",
        ].forEach((field) => set(field, config[field]));
        if (!config.mode) {
            card.querySelector('[data-field="mode"]').value = "tedapi";
        }
        if (!config.timezone) {
            const defaults =
                (state.schema && state.schema.gateway_defaults) || {};
            card.querySelector('[data-field="timezone"]').value =
                defaults.timezone || "America/Los_Angeles";
        }
        const mode = card.querySelector('[data-field="mode"]');
        mode.addEventListener("change", () => updateModeFields(card));
        card.querySelector('[data-field="id"]').addEventListener("input", () => {
            const value = card.querySelector('[data-field="id"]').value.trim();
            card.querySelector(".gateway-title").textContent = value || "New gateway";
        });
        card.querySelector(".remove-gateway").addEventListener("click", () => {
            card.remove();
            renumberGateways();
        });
        card.querySelector(".test-gateway").addEventListener("click", () => testGateway(card));
        card.querySelector(".generate-key").addEventListener("click", () => generateKey(card));
        card.querySelector(".check-auth").addEventListener("click", () => checkAuth(card));
        card.querySelector(".cloud-start").addEventListener("click", () => startCloudAuth(card));
        card.querySelector(".cloud-use-existing").addEventListener("click", () => useCloudAuth(card));
        card.querySelector(".cloud-complete").addEventListener("click", () => completeCloudAuth(card));
        card.querySelector(".v1r-login").addEventListener("click", () => startV1rLogin(card));
        card.querySelector(".v1r-complete").addEventListener("click", () => completeV1rLogin(card));
        card.querySelector(".v1r-auth-upload").addEventListener("click", () => uploadV1rAuthFile(card));
        card.querySelector(".v1r-auth-file-button").addEventListener("click", () => {
            card.querySelector(".v1r-auth-file").click();
        });
        card.querySelector(".v1r-auth-file").addEventListener("change", () => {
            updateV1rFileSelection(card);
        });
        card.querySelector(".v1r-register").addEventListener("click", () => registerV1rKey(card, "owner"));
        card.querySelector(".v1r-register-fleet").addEventListener("click", () => registerV1rKey(card, "fleetapi"));
        card.querySelector(".v1r-check").addEventListener("click", () => checkV1rKey(card));
        card.querySelector(".cloud-auth-file-button").addEventListener("click", () => {
            card.querySelector(".cloud-auth-file").click();
        });
        card.querySelector(".cloud-auth-file").addEventListener("change", () => {
            updateAuthFileSelection(card, ".cloud-auth-file", "[data-cloud-file-name]", ".cloud-auth-upload");
        });
        card.querySelector(".cloud-auth-upload").addEventListener("click", () => uploadAuthFile(card, "cloud"));
        card.querySelector(".cloud-token-import").addEventListener("click", () => importCloudTokens(card));
        card.querySelector(".fleet-auth-file-button").addEventListener("click", () => {
            card.querySelector(".fleet-auth-file").click();
        });
        card.querySelector(".fleet-auth-file").addEventListener("change", () => {
            updateAuthFileSelection(card, ".fleet-auth-file", "[data-fleet-file-name]", ".fleet-auth-upload");
        });
        card.querySelector(".fleet-auth-upload").addEventListener("click", () => uploadAuthFile(card, "fleetapi"));
        card.querySelector(".fleet-key").addEventListener("click", () => generateFleetKey(card));
        card.querySelector(".fleet-copy-key").addEventListener("click", () => copyFleetKey(card));
        card.querySelector(".fleet-start").addEventListener("click", () => startFleetSetup(card));
        card.querySelector(".fleet-complete").addEventListener("click", () => completeFleetSetup(card));
        updateModeFields(card);
        enhanceFieldHints(card);
        enhanceSecretInputs(card);
        $("gateway-list").appendChild(fragment);
    }

    function renumberGateways() {
        document.querySelectorAll(".gateway-card").forEach((card, index) => {
            card.querySelector(".gateway-index").textContent = "Gateway " + (index + 1);
        });
    }

    function updateModeFields(card) {
        const mode = card.querySelector('[data-field="mode"]').value;
        const description = card.querySelector("[data-mode-description]");
        if (description) {
            description.textContent = MODE_DESCRIPTIONS[mode] || "";
        }
        const show = {
            gw_pwd: ["tedapi", "tedapi_v1r", "hybrid_tedapi", "hybrid", "hybrid_fleetapi"],
            password: ["tedapi_v1r", "local", "basic_lan", "hybrid_tedapi", "hybrid", "hybrid_fleetapi"],
            rsa_key_path: ["tedapi_v1r", "hybrid", "hybrid_fleetapi"],
            wifi_host: ["tedapi_v1r", "hybrid", "hybrid_fleetapi"],
            email: [
                "tedapi",
                "tedapi_v1r",
                "local",
                "basic_lan",
                "hybrid_tedapi",
                "cloud",
                "fleetapi",
                "hybrid",
                "hybrid_fleetapi",
            ],
            authpath: [
                "tedapi",
                "tedapi_v1r",
                "local",
                "basic_lan",
                "hybrid_tedapi",
                "cloud",
                "fleetapi",
                "hybrid",
                "hybrid_fleetapi",
            ],
        };
        card.querySelectorAll("[data-mode-field]").forEach((field) => {
            const key = field.dataset.modeField;
            field.hidden = !(show[key] || []).includes(mode);
        });
        const host = card.querySelector('[data-field="host"]');
        const port = card.querySelector('[data-field="port"]');
        const cloudOnly = mode === "cloud" || mode === "fleetapi";
        host.closest(".field").hidden = cloudOnly;
        port.closest(".field").hidden = cloudOnly;
        const v1rModes = ["tedapi_v1r", "hybrid", "hybrid_fleetapi"];
        const cloudControlModes = ["cloud", "hybrid"];
        const fleetControlModes = ["fleetapi", "hybrid_fleetapi"];
        const authModes = ["cloud", "fleetapi", "hybrid", "hybrid_fleetapi"];
        const keyButton = card.querySelector(".generate-key");
        keyButton.hidden = !v1rModes.includes(mode);
        const authButton = card.querySelector(".check-auth");
        authButton.hidden = !authModes.includes(mode);
        const cloudAuth = card.querySelector("[data-cloud-auth]");
        cloudAuth.hidden = !cloudControlModes.includes(mode);
        const v1rRegistration = card.querySelector("[data-v1r-registration]");
        if (v1rRegistration) v1rRegistration.hidden = !v1rModes.includes(mode);
        const fleetRegistration = card.querySelector("[data-fleet-registration]");
        if (fleetRegistration) fleetRegistration.hidden = !fleetControlModes.includes(mode);
        const help = card.querySelector("[data-mode-help]");
        if (cloudControlModes.includes(mode)) {
            help.hidden = false;
            help.textContent =
                "Use Start Tesla login in the panel below to create the auth files, then test the connection.";
        } else if (fleetControlModes.includes(mode)) {
            help.hidden = false;
            help.textContent =
                "Use the Fleet API setup panel below; it verifies the app/public key and creates .pypowerwall.fleetapi.";
        } else {
            help.hidden = true;
            help.textContent = "";
        }
    }

    async function startCloudAuth(card) {
        const container = card.querySelector(".diagnostics");
        const email = card.querySelector('[data-field="email"]').value.trim();
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const region = card.querySelector(".cloud-region").value;
        if (!email || !authpath) {
            container.innerHTML = '<div class="notice error">Tesla account email and auth path are required.</div>';
            return;
        }
        try {
            const existing = await api("/auth/status", {
                method: "POST",
                body: JSON.stringify({ mode: "cloud", authpath: authpath || null }),
            });
            if (existing.files && existing.files.auth) {
                const overwrite = window.confirm(
                    "An existing Cloud auth file was found. Overwrite it with a new Tesla login?"
                );
                if (!overwrite) {
                    container.innerHTML = '<div class="notice warning">Existing auth kept. Use "Use existing Cloud auth" to select its site.</div>';
                    return;
                }
            }
        } catch (err) {
            // The start endpoint still performs its own auth-file handling.
        }
        container.innerHTML = '<div class="notice">Building the Tesla login URL&hellip;</div>';
        try {
            const result = await api("/auth/cloud/start", {
                method: "POST",
                body: JSON.stringify({ email: email, authpath: authpath, region: region }),
            });
            card.querySelector(".cloud-link").innerHTML =
                'Open this link, sign in, then copy the resulting callback URL:<br><a href="' +
                escapeHtml(result.auth_url) +
                '" target="_blank" rel="noopener noreferrer">Tesla login</a>';
            container.innerHTML = "";
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    async function useCloudAuth(card) {
        const container = card.querySelector(".diagnostics");
        const email = card.querySelector('[data-field="email"]').value.trim();
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        if (!authpath) {
            container.innerHTML = '<div class="notice error">Auth path is required.</div>';
            return;
        }
        container.innerHTML = '<div class="notice">Checking the existing Tesla auth file&hellip;</div>';
        try {
            const result = await api("/auth/cloud/use", {
                method: "POST",
                body: JSON.stringify({ email: email || null, authpath: authpath }),
            });
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            if (result.email) {
                card.querySelector('[data-field="email"]').value = result.email;
            }
            const sites = (result.sites || [])
                .map((site) => {
                    const selected = String(result.siteid) === String(site.siteid);
                    return (
                        '<button type="button" class="btn btn-small btn-secondary select-site" data-siteid="' +
                        escapeHtml(site.siteid) +
                        '">' +
                        escapeHtml((site.name || site.siteid) + (selected ? " (selected)" : "")) +
                        "</button>"
                    );
                })
                .join("");
            container.innerHTML =
                '<div class="notice ' +
                (result.needs_site ? "warning" : "success") +
                '">' +
                escapeHtml(result.notes && result.notes[0] ? result.notes[0] : "Existing Cloud auth loaded.") +
                (result.needs_site ? " Select an energy site:" : " Site: " + escapeHtml(result.siteid || "")) +
                '</div><div class="cloud-sites">' +
                sites +
                "</div>";
            container.querySelectorAll(".select-site").forEach((button) => {
                button.addEventListener("click", () => selectCloudSite(card, button.dataset.siteid));
            });
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    async function completeCloudAuth(card) {
        const container = card.querySelector(".diagnostics");
        const callback = card.querySelector(".cloud-callback").value.trim();
        if (!callback) {
            container.innerHTML = '<div class="notice error">Paste the callback URL first.</div>';
            return;
        }
        container.innerHTML = '<div class="notice">Exchanging the Tesla authorization code&hellip;</div>';
        try {
            const result = await api("/auth/cloud/complete", {
                method: "POST",
                body: JSON.stringify({ callback_url: callback }),
            });
            card.querySelector(".cloud-callback").value = "";
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            if (result.email) {
                card.querySelector('[data-field="email"]').value = result.email;
            }
            const sites = (result.sites || [])
                .map((site) => {
                    const selected = String(result.siteid) === String(site.siteid);
                    return (
                        '<button type="button" class="btn btn-small btn-secondary select-site" data-siteid="' +
                        escapeHtml(site.siteid) +
                        '">' +
                        escapeHtml((site.name || site.siteid) + (selected ? " (selected)" : "")) +
                        "</button>"
                    );
                })
                .join("");
            container.innerHTML =
                '<div class="notice ' +
                (result.needs_site ? "warning" : "success") +
                '">Tesla authentication saved.' +
                (result.needs_site ? " Select an energy site:" : " Site: " + escapeHtml(result.siteid || "")) +
                '</div><div class="cloud-sites">' +
                sites +
                "</div>";
            card.querySelectorAll(".select-site").forEach((button) => {
                button.addEventListener("click", () => selectCloudSite(card, button.dataset.siteid));
            });
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    async function selectCloudSite(card, siteid) {
        const container = card.querySelector(".diagnostics");
        try {
            await api("/auth/cloud/site", {
                method: "POST",
                body: JSON.stringify({
                    email: card.querySelector('[data-field="email"]').value.trim(),
                    authpath: card.querySelector('[data-field="authpath"]').value.trim(),
                    siteid: siteid,
                }),
            });
            card.querySelector('[data-field="siteid"]').value = siteid;
            container.innerHTML = '<div class="notice success">Site ' + escapeHtml(siteid) + " selected.</div>";
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    function setFleetAuthStatus(card, text, kind) {
        const box = card.querySelector(".fleet-auth-status");
        box.className = "v1r-auth-status fleet-auth-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setFleetCompleteStatus(card, text, kind) {
        const box = card.querySelector(".fleet-complete-status");
        box.className = "v1r-auth-status fleet-complete-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setFleetStatus(card, text, kind) {
        const box = card.querySelector(".fleet-status");
        box.className = "v1r-register-status fleet-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setFleetPhase(card, text, kind) {
        const pill = card.querySelector("[data-fleet-phase]");
        pill.className = "status-pill" + (kind ? " " + kind : "");
        pill.textContent = text;
    }

    async function generateFleetKey(card) {
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const button = card.querySelector(".fleet-key");
        setV1rButtonBusy(button, true, "Generating…");
        setFleetAuthStatus(card, "Generating the FleetAPI EC key pair…", "");
        try {
            const result = await api("/fleetapi/key", {
                method: "POST",
                body: JSON.stringify({ authpath: authpath || null }),
            });
            const box = card.querySelector(".fleet-key-result");
            box.hidden = false;
            card.querySelector("[data-fleet-public-key]").textContent = result.public_key_pem;
            setFleetAuthStatus(
                card,
                result.message +
                "\nPrivate key: " + result.private_key_path +
                "\nPublic key file: " + result.public_key_path,
                result.created ? "success" : "warning"
            );
            setFleetPhase(card, "Key ready", result.created ? "ok" : "warning");
        } catch (err) {
            setFleetAuthStatus(card, err.message, "error");
            setFleetPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function copyFleetKey(card) {
        const text = card.querySelector("[data-fleet-public-key]").textContent;
        if (!text) {
            setFleetAuthStatus(card, "Generate the PEM key first.", "error");
            return;
        }
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
            } else {
                const range = document.createRange();
                range.selectNodeContents(card.querySelector("[data-fleet-public-key]"));
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
                document.execCommand("copy");
            }
            setFleetAuthStatus(card, "Public key copied to the clipboard.", "success");
        } catch (err) {
            setFleetAuthStatus(card, "Copy failed; select the public key text and copy it manually.", "warning");
        }
    }

    async function startFleetSetup(card) {
        const clientId = card.querySelector(".fleet-client-id").value.trim();
        const clientSecret = card.querySelector(".fleet-client-secret").value.trim();
        const domain = card.querySelector(".fleet-domain").value.trim();
        const redirect = card.querySelector(".fleet-redirect").value.trim();
        const region = card.querySelector(".fleet-region").value;
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        if (!clientId || !clientSecret || !domain) {
            setFleetAuthStatus(card, "Client ID, Client Secret and Domain are required.", "error");
            setFleetPhase(card, "Action required", "error");
            return;
        }
        const button = card.querySelector(".fleet-start");
        setV1rButtonBusy(button, true, "Verifying…");
        setFleetAuthStatus(card, "Verifying the public key and registering the partner account…", "");
        setFleetPhase(card, "Partner setup", "");
        try {
            const result = await api("/fleetapi/start", {
                method: "POST",
                body: JSON.stringify({
                    authpath: authpath || null,
                    client_id: clientId,
                    client_secret: clientSecret,
                    domain: domain,
                    redirect_uri: redirect || null,
                    region: region,
                }),
            });
            const link = card.querySelector(".fleet-login-link");
            link.href = result.auth_url;
            link.hidden = false;
            card.querySelector(".fleet-login-result").hidden = false;
            setFleetAuthStatus(
                card,
                result.message + " Open the Tesla login, then paste the full callback URL below.",
                "warning"
            );
            setFleetPhase(card, "Tesla sign-in", "warning");
        } catch (err) {
            setFleetAuthStatus(card, err.message, "error");
            setFleetPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function completeFleetSetup(card) {
        const callback = card.querySelector(".fleet-callback").value.trim();
        if (!callback) {
            setFleetCompleteStatus(card, "Paste the full Tesla callback URL first.", "error");
            return;
        }
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const button = card.querySelector(".fleet-complete");
        setV1rButtonBusy(button, true, "Completing…");
        setFleetCompleteStatus(card, "Exchanging the FleetAPI authorization code…", "");
        try {
            const result = await api("/fleetapi/complete", {
                method: "POST",
                body: JSON.stringify({
                    authpath: authpath || null,
                    callback_url: callback,
                }),
            });
            card.querySelector(".fleet-callback").value = "";
            card.querySelector(".fleet-login-result").hidden = true;
            card.querySelector(".fleet-login-link").hidden = true;
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            renderFleetSites(card, result.sites || []);
            setFleetCompleteStatus(card, result.message, result.needs_site ? "warning" : "success");
            setFleetStatus(card, result.message, result.needs_site ? "warning" : "success");
            setFleetPhase(card, result.needs_site ? "Select site" : "Ready", result.needs_site ? "warning" : "ok");
        } catch (err) {
            setFleetCompleteStatus(card, err.message, "error");
            setFleetPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    function renderFleetSites(card, sites) {
        const container = card.querySelector(".fleet-sites");
        container.innerHTML = "";
        if (!sites || !sites.length) return;
        const siteidInput = card.querySelector('[data-field="siteid"]');
        sites.forEach((site) => {
            const selected = String(siteidInput.value) === String(site.siteid);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-small " + (selected ? "btn-primary" : "btn-secondary");
            button.textContent = (site.name || site.siteid) + (selected ? " (selected)" : "");
            button.addEventListener("click", () => selectFleetSite(card, site.siteid, sites));
            container.appendChild(button);
        });
    }

    async function selectFleetSite(card, siteid, sites) {
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        setFleetStatus(card, "Saving the FleetAPI site selection…", "");
        try {
            const result = await api("/fleetapi/site", {
                method: "POST",
                body: JSON.stringify({ authpath: authpath || null, siteid: siteid }),
            });
            card.querySelector('[data-field="siteid"]').value = siteid;
            renderFleetSites(card, sites);
            setFleetStatus(card, result.message, "success");
            setFleetPhase(card, "Site selected", "ok");
        } catch (err) {
            setFleetStatus(card, err.message, "error");
            setFleetPhase(card, "Action required", "error");
        }
    }

    function setV1rStatus(card, text, kind) {
        const box = card.querySelector(".v1r-status");
        box.className = "v1r-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setV1rAuthStatus(card, text, kind) {
        const box = card.querySelector(".v1r-auth-status");
        box.className = "v1r-auth-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setV1rRegisterStatus(card, text, kind) {
        const box = card.querySelector(".v1r-register-status");
        box.className = "v1r-register-status" + (kind ? " " + kind : "");
        box.textContent = text || "";
    }

    function setV1rPhase(card, text, kind) {
        const pill = card.querySelector("[data-v1r-phase]");
        pill.className = "status-pill" + (kind ? " " + kind : "");
        pill.textContent = text;
    }

    function setV1rButtonBusy(button, busy, label) {
        if (!button) return;
        if (busy) {
            if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
            button.textContent = label;
            button.disabled = true;
        } else {
            button.disabled = false;
            if (button.dataset.idleLabel) {
                button.textContent = button.dataset.idleLabel;
                delete button.dataset.idleLabel;
            }
        }
    }

    function renderV1rSites(card, sites) {
        const container = card.querySelector(".v1r-sites");
        container.innerHTML = "";
        if (!sites || !sites.length) return;
        const siteidInput = card.querySelector('[data-field="siteid"]');
        sites.forEach((site) => {
            const selected = String(siteidInput.value) === String(site.siteid);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-small " + (selected ? "btn-primary" : "btn-secondary");
            button.dataset.siteid = site.siteid;
            button.textContent = (site.name || site.siteid) + (selected ? " (selected)" : "");
            button.addEventListener("click", () => {
                siteidInput.value = site.siteid;
                renderV1rSites(card, sites);
                setV1rRegisterStatus(
                    card,
                    "Site " + site.siteid + " selected. Register the v1r key.",
                    "warning"
                );
                setV1rPhase(card, "Ready to register", "");
            });
            container.appendChild(button);
        });
    }

    function applyV1rResult(card, result) {
        if (result.private_key_path) {
            card.querySelector('[data-field="rsa_key_path"]').value = result.private_key_path;
        }
        if (result.siteid) {
            card.querySelector('[data-field="siteid"]').value = result.siteid;
        }
        renderV1rSites(card, result.sites || []);
        const proof = card.querySelector(".v1r-proof");
        proof.hidden = !(result.pending && result.proof_hint);
        proof.textContent = result.proof_hint || "";
        card.querySelector(".v1r-check").hidden = !result.pending;
        if (result.needs_site) {
            setV1rStatus(card, "", "");
            setV1rRegisterStatus(card, result.message || "", "warning");
            setV1rPhase(card, "Select site", "warning");
            return;
        }
        setV1rRegisterStatus(card, "", "");
        const kind = result.verified ? "success" : (result.timed_out || result.removed ? "error" : "warning");
        let message = result.message || "";
        if (result.fingerprint) message += "\nFingerprint: " + result.fingerprint;
        setV1rStatus(card, message, kind);
        if (result.verified) {
            setV1rPhase(card, "Verified", "ok");
        } else if (result.timed_out) {
            setV1rPhase(card, "Verification expired", "error");
        } else if (result.removed) {
            setV1rPhase(card, "Key removed", "error");
        } else if (result.pending) {
            setV1rPhase(card, "Pending verification", "warning");
        } else {
            setV1rPhase(card, "Check status", "warning");
        }
    }

    function updateV1rFileSelection(card) {
        const fileInput = card.querySelector(".v1r-auth-file");
        const file = fileInput.files && fileInput.files[0];
        card.querySelector("[data-v1r-file-name]").textContent = file
            ? file.name
            : "No file selected";
        card.querySelector(".v1r-auth-upload").disabled = !file;
    }

    async function uploadV1rAuthFile(card) {
        const fileInput = card.querySelector(".v1r-auth-file");
        const file = fileInput.files && fileInput.files[0];
        if (!file) {
            setV1rAuthStatus(card, "Choose a .pypowerwall.auth file first.", "error");
            return;
        }
        if (file.size > 256 * 1024) {
            setV1rAuthStatus(card, "The selected auth file is too large.", "error");
            return;
        }
        const button = card.querySelector(".v1r-auth-upload");
        setV1rButtonBusy(button, true, "Uploading…");
        setV1rAuthStatus(card, "Reading and saving the Tesla login…", "");
        try {
            const authpath = card.querySelector('[data-field="authpath"]').value.trim();
            const content = await file.text();
            const result = await api("/v1r/auth-file", {
                method: "POST",
                body: JSON.stringify({
                    authpath: authpath || null,
                    filename: file.name,
                    content: content,
                }),
            });
            fileInput.value = "";
            updateV1rFileSelection(card);
            card.querySelector(".v1r-login-result").hidden = true;
            card.querySelector(".v1r-login-link").hidden = true;
            setV1rAuthStatus(card, result.message, "success");
            setV1rRegisterStatus(card, "Tesla login is ready. Register the v1r key.", "success");
            setV1rPhase(card, "Ready to register", "ok");
        } catch (err) {
            setV1rAuthStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function startV1rLogin(card) {
        const email = card.querySelector(".v1r-email").value.trim();
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        if (!email || !authpath) {
            setV1rAuthStatus(card, "Tesla account email and auth path are required.", "error");
            setV1rPhase(card, "Action required", "error");
            return;
        }
        const button = card.querySelector(".v1r-login");
        setV1rButtonBusy(button, true, "Starting…");
        setV1rAuthStatus(card, "Building the Tesla login URL…", "");
        setV1rPhase(card, "Tesla sign-in", "");
        try {
            const result = await api("/auth/cloud/start", {
                method: "POST",
                body: JSON.stringify({ email: email, authpath: authpath }),
            });
            const link = card.querySelector(".v1r-login-link");
            link.href = result.auth_url;
            link.hidden = false;
            card.querySelector(".v1r-login-result").hidden = false;
            setV1rAuthStatus(
                card,
                "Tesla login page ready. Open it, sign in, then paste the callback URL below.",
                "warning"
            );
        } catch (err) {
            setV1rAuthStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function completeV1rLogin(card) {
        const callback = card.querySelector(".v1r-callback").value.trim();
        if (!callback) {
            setV1rAuthStatus(card, "Paste the Tesla callback URL first.", "error");
            return;
        }
        const button = card.querySelector(".v1r-complete");
        setV1rButtonBusy(button, true, "Completing…");
        setV1rAuthStatus(card, "Exchanging the Tesla authorization code…", "");
        try {
            const result = await api("/auth/cloud/complete", {
                method: "POST",
                body: JSON.stringify({ callback_url: callback }),
            });
            card.querySelector(".v1r-callback").value = "";
            card.querySelector(".v1r-login-result").hidden = true;
            card.querySelector(".v1r-login-link").hidden = true;
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            if (result.email) {
                card.querySelector(".v1r-email").value = result.email;
            }
            renderV1rSites(card, result.sites || []);
            setV1rAuthStatus(
                card,
                "Tesla login saved" + (result.email ? " for " + result.email : "") + ".",
                "success"
            );
            if (result.needs_site) {
                setV1rRegisterStatus(card, "Select the energy site for this gateway, then register the v1r key.", "warning");
                setV1rPhase(card, "Select site", "warning");
            } else {
                setV1rRegisterStatus(card, "Tesla login is ready. Register the v1r key.", "success");
                setV1rPhase(card, "Ready to register", "ok");
            }
        } catch (err) {
            setV1rAuthStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function registerV1rKey(card, method = "owner") {
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const email = card.querySelector(".v1r-email").value.trim();
        const siteid = card.querySelector('[data-field="siteid"]').value.trim();
        const fleet = method === "fleetapi";
        const button = card.querySelector(fleet ? ".v1r-register-fleet" : ".v1r-register");
        clearV1rPolling(card);
        clearV1rCheckPolling(card);
        setV1rButtonBusy(card.querySelector(".v1r-check"), false);
        card.dataset.v1rMethod = method;
        card.querySelector(".v1r-proof").hidden = true;
        card.querySelector(".v1r-check").hidden = true;
        setV1rButtonBusy(button, true, "Registering…");
        setV1rRegisterStatus(card, fleet ? "Registering the v1r key with FleetAPI…" : "Registering the v1r key…", "");
        setV1rPhase(card, "Registering", "");
        try {
            const result = await api("/v1r/register", {
                method: "POST",
                body: JSON.stringify({
                    authpath: authpath || null,
                    email: email || null,
                    siteid: siteid || null,
                    method,
                }),
            });
            applyV1rResult(card, result);
            if (result.pending) startV1rPolling(card);
        } catch (err) {
            setV1rRegisterStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    function clearV1rPolling(card) {
        if (card._v1rPollTimer) {
            clearTimeout(card._v1rPollTimer);
            delete card._v1rPollTimer;
        }
    }

    function clearV1rCheckPolling(card) {
        if (card._v1rCheckTimer) {
            clearTimeout(card._v1rCheckTimer);
            delete card._v1rCheckTimer;
        }
    }

    function startV1rPolling(card) {
        clearV1rPolling(card);
        clearV1rCheckPolling(card);
        let remaining = 3;
        const tick = async () => {
            if (!document.body.contains(card) || remaining <= 0) return;
            remaining -= 1;
            const authpath = card.querySelector('[data-field="authpath"]').value.trim();
            const email = card.querySelector(".v1r-email").value.trim();
            const siteid = card.querySelector('[data-field="siteid"]').value.trim();
            if (!siteid) return;
            try {
                const result = await api("/v1r/register/status", {
                    method: "POST",
                    body: JSON.stringify({
                        authpath: authpath || null,
                        email: email || null,
                        siteid: siteid,
                        method: card.dataset.v1rMethod || "owner",
                    }),
                });
                applyV1rResult(card, result);
                if (result.verified || result.timed_out || result.removed || result.needs_site) {
                    return;
                }
            } catch (err) {
                setV1rRegisterStatus(
                    card,
                    "Automatic status check failed; use Check verification.",
                    "warning"
                );
                return;
            }
            if (remaining > 0) {
                card._v1rPollTimer = setTimeout(tick, 5000);
            }
        };
        tick();
    }

    async function checkV1rKey(card) {
        const siteid = card.querySelector('[data-field="siteid"]').value.trim();
        if (!siteid) {
            setV1rRegisterStatus(card, "Select the Tesla energy site first.", "error");
            setV1rPhase(card, "Select site", "warning");
            return;
        }
        clearV1rPolling(card);
        clearV1rCheckPolling(card);
        await pollV1rVerification(card, Date.now() + 120000, true);
    }

    async function pollV1rVerification(card, deadline, first) {
        if (!document.body.contains(card)) return;
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const email = card.querySelector(".v1r-email").value.trim();
        const siteid = card.querySelector('[data-field="siteid"]').value.trim();
        const button = card.querySelector(".v1r-check");
        const method = card.dataset.v1rMethod || "owner";
        if (first) {
            setV1rButtonBusy(button, true, "Checking…");
            setV1rStatus(card, "Checking key verification…", "");
        }
        try {
            const result = await api("/v1r/register/status", {
                method: "POST",
                body: JSON.stringify({
                    authpath: authpath || null,
                    email: email || null,
                    siteid: siteid,
                    method,
                }),
            });
            applyV1rResult(card, result);
            if (result.verified || result.timed_out || result.removed || result.needs_site) {
                setV1rButtonBusy(button, false);
                return;
            }
        } catch (err) {
            setV1rStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
            setV1rButtonBusy(button, false);
            return;
        }
        if (Date.now() >= deadline) {
            setV1rStatus(
                card,
                "Verification is still pending after 120 seconds. Check again.",
                "warning"
            );
            setV1rButtonBusy(button, false);
            return;
        }
        setV1rStatus(card, "Waiting for verification (up to 120 seconds)…", "warning");
        card._v1rCheckTimer = setTimeout(
            () => pollV1rVerification(card, deadline, false),
            10000
        );
    }

    async function checkAuth(card) {
        const container = card.querySelector(".diagnostics");
        container.innerHTML = '<div class="notice">Checking auth files&hellip;</div>';
        try {
            const gatewayMode = card.querySelector('[data-field="mode"]').value;
            const authMode =
                gatewayMode === "fleetapi" || gatewayMode === "hybrid_fleetapi"
                    ? "fleetapi"
                    : "cloud";
            const result = await api("/auth/status", {
                method: "POST",
                body: JSON.stringify({
                    mode: authMode,
                    authpath: card.querySelector('[data-field="authpath"]').value.trim() || null,
                }),
            });
            const files = Object.keys(result.files || {})
                .map((key) => key + ": " + (result.files[key] ? "present" : "missing"))
                .join("<br>");
            let nextStep = "All required auth files are present. Test the connection.";
            if (!result.ready && authMode === "fleetapi") {
                nextStep = "Use the Fleet API setup panel below, then test the connection.";
            } else if (!result.ready) {
                nextStep = "Use Start Tesla login in the panel below, then test the connection.";
            }
            container.innerHTML =
                '<div class="notice ' +
                (result.ready ? "success" : "warning") +
                '">' +
                files +
                "<br>" +
                nextStep +
                "</div>";
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    async function generateKey(card) {
        const container = card.querySelector(".diagnostics");
        const button = card.querySelector(".generate-key");
        container.innerHTML = "";
        setV1rButtonBusy(button, true, "Generating…");
        setV1rRegisterStatus(card, "Generating an RSA-4096 key…", "");
        try {
            const authpath = card.querySelector('[data-field="authpath"]').value.trim();
            const result = await api("/v1r/key", {
                method: "POST",
                body: JSON.stringify({ authpath: authpath || null }),
            });
            card.querySelector('[data-field="rsa_key_path"]').value = result.private_key_path;
            setV1rRegisterStatus(
                card,
                "v1r key ready.\nKey: " +
                result.private_key_path +
                "\nFingerprint: " +
                (result.fingerprint || "not reported by the server"),
                "success"
            );
            setV1rPhase(card, "Key ready", "ok");
        } catch (err) {
            setV1rRegisterStatus(card, err.message, "error");
            setV1rPhase(card, "Action required", "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    function collectGateway(card) {
        const value = (field) => {
            const input = card.querySelector('[data-field="' + field + '"]');
            return input ? input.value.trim() : "";
        };
        const mode = value("mode");
        const data = {
            id: value("id"),
            name: value("name") || undefined,
            mode: mode,
            type: value("type") || "powerwall",
            timezone:
                value("timezone") ||
                (state.schema &&
                    state.schema.gateway_defaults &&
                    state.schema.gateway_defaults.timezone) ||
                "America/Los_Angeles",
        };
        if (card.dataset.originalId) {
            data.original_id = card.dataset.originalId;
        }
        const hostModes = ["tedapi", "tedapi_v1r", "local", "basic_lan", "hybrid_tedapi", "hybrid", "hybrid_fleetapi"];
        if (hostModes.includes(mode)) {
            data.host = value("host");
        }
        const port = value("port");
        if (port) {
            data.port = Number(port);
        }
        const secretModes = {
            gw_pwd: ["tedapi", "tedapi_v1r", "hybrid_tedapi", "hybrid", "hybrid_fleetapi"],
            password: ["tedapi_v1r", "local", "basic_lan", "hybrid_tedapi", "hybrid", "hybrid_fleetapi"],
            email: [
                "tedapi",
                "tedapi_v1r",
                "local",
                "basic_lan",
                "hybrid_tedapi",
                "cloud",
                "fleetapi",
                "hybrid",
                "hybrid_fleetapi",
            ],
            authpath: [
                "tedapi",
                "tedapi_v1r",
                "local",
                "basic_lan",
                "hybrid_tedapi",
                "cloud",
                "fleetapi",
                "hybrid",
                "hybrid_fleetapi",
            ],
            rsa_key_path: ["tedapi_v1r", "hybrid", "hybrid_fleetapi"],
            wifi_host: ["tedapi_v1r", "hybrid", "hybrid_fleetapi"],
        };
        Object.keys(secretModes).forEach((field) => {
            if (!secretModes[field].includes(mode)) {
                return;
            }
            const input = card.querySelector('[data-field="' + field + '"]');
            if (input && input.dataset.secretCleared === "true") {
                data[field] = null;
                return;
            }
            const current = value(field);
            if (current) {
                data[field] = current;
            } else if (input && input.dataset.initial) {
                data[field] = null;
            }
        });
        return data;
    }

    function collectGateways() {
        return Array.from(document.querySelectorAll(".gateway-card")).map(collectGateway);
    }

    function renderDiagnostics(container, result) {
        const steps = (result && result.steps) || [];
        container.innerHTML = steps
            .map(
                (step) =>
                    '<div class="diagnostic-step ' +
                    escapeHtml(step.status) +
                    '"><strong>' +
                    escapeHtml(step.status) +
                    "</strong><span><b>" +
                    escapeHtml(step.label) +
                    "</b>: " +
                    escapeHtml(step.detail || "") +
                    "</span></div>"
            )
            .join("");
        if (result && result.warnings && result.warnings.length) {
            container.innerHTML +=
                '<div class="notice warning">' +
                result.warnings.map(escapeHtml).join("<br>") +
                "</div>";
        }
    }

    async function testGateway(card) {
        const container = card.querySelector(".diagnostics");
        container.innerHTML = '<div class="notice">Testing…</div>';
        try {
            const result = await api("/test-connection", {
                method: "POST",
                body: JSON.stringify(collectGateway(card)),
            });
            renderDiagnostics(container, result);
        } catch (err) {
            container.innerHTML = '<div class="notice error">' + escapeHtml(err.message) + "</div>";
        }
    }

    function settingsGroup(name) {
        if (name.startsWith("mqtt_")) return "MQTT and Home Assistant";
        if (name.startsWith("timeseries_")) return "Time series";
        if (
            ["server_host", "server_port", "cors_origins", "https_mode", "https_port", "https_cert", "https_key", "proxy_base_url", "style", "debug"].includes(name)
        ) return "Server, HTTPS and UI";
        if (name.startsWith("rate_limit_")) return "Rate limiting";
        if (
            ["siteid", "failover", "fleetapi_http", "cloud_hybrid", "cloud_read_fallback"].includes(name)
        ) return "Cloud and failover";
        if (["local_first", "wifi_v1r_fallback"].includes(name)) return "Local and WiFi fallback";
        if (["control_secret", "islanding_cooldown", "battery_dispatch"].includes(name)) {
            return "Control and safety";
        }
        if (
            [
                "cache_expire",
                "browser_cache",
                "timeout",
                "pool_maxsize",
                "cache_ttl",
                "suppress_network_errors",
                "network_error_rate_limit",
                "fail_fast",
                "graceful_degradation",
                "health_check",
                "tedapi_recovery",
                "tedapi_probe_interval",
                "neg_solar",
            ].includes(name)
        ) return "Performance and robustness";
        if (["pw_authpath", "auth_mode", "cache_file", "timeseries_path"].includes(name)) {
            return "Files and legacy";
        }
        return "Advanced";
    }

    function inputType(field) {
        if (SETTING_CHOICES[field.name]) return "select";
        if (field.secret) return "password";
        if (field.type.includes("bool")) return "checkbox";
        if (field.type.includes("int")) return "number";
        return "text";
    }

    function initialSettingValue(field) {
        const current = state.initialSettings[field.name];
        if (current === "***") return "";
        if (current !== undefined) return formatSettingValue(current);
        return formatSettingValue(field.default);
    }

    function formatSettingValue(value) {
        if (value === null || value === undefined) return "";
        if (Array.isArray(value)) return JSON.stringify(value);
        if (typeof value === "boolean") return value ? "Enabled" : "Disabled";
        return String(value);
    }

    function settingDefaultHint(field) {
        const current = state.initialSettings[field.name];
        if (current === "***" || current === undefined) return "";
        const defaultValue = formatSettingValue(field.default);
        if (!defaultValue || formatSettingValue(current) === defaultValue) return "";
        return "Server default: " + defaultValue;
    }

    function renderSettings() {
        const groups = new Map();
        const fields = (state.schema && state.schema.settings) || [];
        fields.forEach((field) => {
            const group = settingsGroup(field.name);
            if (!groups.has(group)) groups.set(group, []);
            groups.get(group).push(field);
        });
        const root = $("settings-groups");
        root.innerHTML = "";
        const orderedGroups = [...groups.entries()].sort((a, b) => {
            const left = SETTING_GROUP_ORDER.indexOf(a[0]);
            const right = SETTING_GROUP_ORDER.indexOf(b[0]);
            return (left < 0 ? SETTING_GROUP_ORDER.length : left) -
                (right < 0 ? SETTING_GROUP_ORDER.length : right);
        });
        orderedGroups.forEach(([groupName, groupFields]) => {
            groupFields.sort((a, b) => {
                const left = SETTING_ORDER.indexOf(a.name);
                const right = SETTING_ORDER.indexOf(b.name);
                if (left !== right) {
                    return (left < 0 ? SETTING_ORDER.length : left) -
                        (right < 0 ? SETTING_ORDER.length : right);
                }
                return settingLabel(a.name, a).localeCompare(settingLabel(b.name, b));
            });
            const details = document.createElement("details");
            details.className = "settings-group";
            details.open = false;
            details.innerHTML = "<summary>" + escapeHtml(groupName) + "</summary>";
            const body = document.createElement("div");
            body.className = "settings-group-body";
            groupFields.forEach((field) => {
                const wrapper = document.createElement("div");
                wrapper.className = "settings-field";
                const label = document.createElement("label");
                label.textContent = settingLabel(field.name);
                if (settingIsOptional(field)) {
                    const optional = document.createElement("span");
                    optional.className = "optional";
                    optional.textContent = " (optional)";
                    label.appendChild(optional);
                }
                label.htmlFor = "setting-" + field.name;
                const type = inputType(field);
                let input;
                if (type === "checkbox") {
                    input = document.createElement("input");
                    input.type = "checkbox";
                    const current = state.initialSettings[field.name];
                    input.checked = typeof current === "boolean" ? current : Boolean(field.default);
                } else if (type === "select") {
                    input = document.createElement("select");
                    const initial = initialSettingValue(field);
                    (SETTING_CHOICES[field.name] || []).forEach((choice) => {
                        const option = document.createElement("option");
                        option.value = choice.value;
                        option.textContent = choice.label;
                        input.appendChild(option);
                    });
                    input.value = initial;
                    if (input.value !== initial) {
                        const custom = document.createElement("option");
                        custom.value = initial;
                        custom.textContent = initial || "Default";
                        input.appendChild(custom);
                        input.value = initial;
                    }
                } else {
                    input = document.createElement("input");
                    input.type = type;
                    if (type === "number") input.step = "1";
                    const initial = initialSettingValue(field);
                    if (field.secret && state.initialSettings[field.name] === "***") {
                        input.placeholder = "Leave blank";
                    } else {
                        input.value = initial;
                    }
                }
                input.id = "setting-" + field.name;
                input.dataset.setting = field.name;
                input.dataset.settingType = field.type;
                input.dataset.initial = type === "checkbox" ? String(input.checked) : String(input.value);
                if (field.type.includes("Optional")) input.dataset.settingNullable = "true";
                if (field.secret) {
                    input.autocomplete = "new-password";
                    input.dataset.secretLabel = field.name === "control_secret" ? "token" : "password";
                }
                wrapper.appendChild(label);
                if (type === "checkbox") {
                    const toggle = document.createElement("label");
                    toggle.className = "control-toggle";
                    const track = document.createElement("span");
                    track.className = "control-toggle-track";
                    const thumb = document.createElement("span");
                    thumb.className = "control-toggle-thumb";
                    track.appendChild(thumb);
                    const toggleText = document.createElement("span");
                    toggleText.className = "control-toggle-text";
                    toggleText.textContent = input.checked ? "Enabled" : "Disabled";
                    input.addEventListener("change", () => {
                        toggleText.textContent = input.checked ? "Enabled" : "Disabled";
                    });
                    toggle.appendChild(input);
                    toggle.appendChild(track);
                    toggle.appendChild(toggleText);
                    wrapper.appendChild(toggle);
                } else {
                    wrapper.appendChild(input);
                }
                if (SETTING_DESCRIPTIONS[field.name]) {
                    const description = document.createElement("span");
                    description.className = "hint";
                    description.textContent = SETTING_DESCRIPTIONS[field.name];
                    wrapper.appendChild(description);
                }
                const defaultHint = settingDefaultHint(field);
                if (defaultHint) {
                    const hint = document.createElement("span");
                    hint.className = "hint setting-default";
                    hint.textContent = defaultHint;
                    wrapper.appendChild(hint);
                }
                if (field.secret) {
                    const hint = document.createElement("span");
                    hint.className = "hint";
                    hint.textContent = "Stored secret; blank keeps the current value.";
                    wrapper.appendChild(hint);
                }
                if (SETTING_WARNINGS[field.name]) {
                    const warning = document.createElement("div");
                    warning.className = "notice warning setup-warning";
                    warning.innerHTML = SETTING_WARNINGS[field.name];
                    wrapper.appendChild(warning);
                }
                body.appendChild(wrapper);
            });
            details.appendChild(body);
            root.appendChild(details);
        });
        enhanceFieldHints(root);
        enhanceSecretInputs(root);
    }

    function collectOptions() {
        const options = {};
        document.querySelectorAll("[data-setting]").forEach((input) => {
            const name = input.dataset.setting;
            const initial = input.dataset.initial;
            if (input.dataset.secretCleared === "true") {
                options[name] = null;
                return;
            }
            const current = input.type === "checkbox" ? String(input.checked) : String(input.value);
            if (current === initial) return;
            if (input.value === "" && initial === "***") return;
            if (input.type === "checkbox") {
                options[name] = input.checked;
            } else if (input.tagName === "SELECT") {
                if (input.value === "") {
                    options[name] = null;
                } else if ((input.dataset.settingType || "").toLowerCase().includes("int")) {
                    options[name] = Number(input.value);
                } else {
                    options[name] = input.value;
                }
            } else if (input.type === "number") {
                if (input.value !== "") options[name] = Number(input.value);
            } else if (input.dataset.settingType && input.dataset.settingType.toLowerCase().includes("list")) {
                try {
                    options[name] = JSON.parse(input.value);
                } catch (err) {
                    options[name] = input.value;
                }
            } else if (input.value !== "") {
                options[name] = input.value;
            } else if (input.dataset.settingNullable === "true") {
                options[name] = null;
            } else if (initial === "***") {
                options[name] = input.value;
            }
        });
        return options;
    }

    function buildRequest() {
        return {
            gateways: collectGateways(),
            options: collectOptions(),
            expected_revision: state.status
            ? state.status.config_file_revision || state.status.config_revision
            : null,
        };
    }

    function maskedRequest(request) {
        const copy = JSON.parse(JSON.stringify(request));
        (copy.gateways || []).forEach((gateway) => {
            ["gw_pwd", "password"].forEach((key) => {
                if (gateway[key]) gateway[key] = "***";
            });
        });
        if (copy.options) {
            ["control_secret", "mqtt_password"].forEach((key) => {
                if (copy.options[key]) copy.options[key] = "***";
            });
        }
        return copy;
    }

    function renderReview() {
        const request = buildRequest();
        $("review-json").textContent = JSON.stringify(maskedRequest(request), null, 2);
        $("validation-summary").className = "notice";
        $("validation-summary").textContent = "Not validated yet.";
        $("save-config").disabled = true;
        state.validated = false;
        setStep(4);
    }

    async function validateConfig() {
        const summary = $("validation-summary");
        summary.className = "notice";
        summary.textContent = "Validating…";
        try {
            const result = await api("/validate", {
                method: "POST",
                body: JSON.stringify(buildRequest()),
            });
            if (result.valid) {
                summary.className = "notice success";
                summary.textContent =
                    "Configuration is valid." +
                    (result.warnings && result.warnings.length
                        ? " " + result.warnings.map((item) => item.message).join(" ")
                        : "");
                $("save-config").disabled = !(state.status && state.status.can_write);
                state.validated = true;
            } else {
                summary.className = "notice error";
                summary.innerHTML = result.errors
                    .map((item) => escapeHtml(item.field + ": " + item.message))
                    .join("<br>");
                $("save-config").disabled = true;
                state.validated = false;
            }
        } catch (err) {
            summary.className = "notice error";
            summary.textContent = err.message;
            $("save-config").disabled = true;
            state.validated = false;
        }
    }

    async function saveConfig() {
        if (!state.validated) return;
        if (
            state.status &&
            state.status.restart_required &&
            !window.confirm(
                "The configuration file changed since the server started. Overwrite it with this form?"
            )
        ) {
            return;
        }
        if (
            state.status &&
            state.status.config_error &&
            !window.confirm(
                "The existing configuration file could not be parsed at startup. " +
                "Saving will replace it and write a backup. Continue?"
            )
        ) {
            return;
        }
        const resultBox = $("save-result");
        resultBox.className = "notice";
        resultBox.textContent = "Saving...";
        try {
            const result = await api("/configure", {
                method: "POST",
                body: JSON.stringify(buildRequest()),
            });
            if (state.status) {
                state.status.config_path = result.config_path;
                state.status.config_revision = result.revision;
                state.status.config_file_revision = result.revision;
                state.status.restart_required = true;
            }
            $("save-status").textContent = result.saved ? "Saved" : "Not saved";
            $("save-status").className = "status-pill ok";
            const warnings = (result.warnings || []).join(" ");
            resultBox.className = warnings ? "notice warning" : "notice success";
            resultBox.textContent =
                "Configuration saved to " +
                result.config_path +
                (result.backup_path ? " (backup: " + result.backup_path + ")" : "") +
                ". Restart the container/process to activate it." +
                (warnings ? " " + warnings : "");
            setStep(5);
        } catch (err) {
            resultBox.className = "notice error";
            resultBox.textContent = err.message;
        }
    }

    async function connectWithToken(event) {
        event.preventDefault();
        const input = $("setup-token");
        const token = input.value.trim();
        if (!token) return;
        state.token = token;
        storeSetupToken(token);
        message("");
        try {
            const [status, schema] = await Promise.all([api("/status"), api("/schema")]);
            state.status = status;
            state.schema = schema;
            populateTimezones();
            renderStatus();
            renderGateways();
            renderSettings();
            message("Setup API connected.", "success");
        } catch (err) {
            state.token = "";
            if (err.status === 401) {
                clearStoredSetupToken();
                input.value = "";
                input.focus();
            }
            message(err.message, "error");
        }
    }

    async function previewEnv() {
        const box = $("env-preview");
        const status = $("env-import-status");
        const help = $("env-import-help");
        try {
            const result = await api("/import-env");
            state.envRevision = result.revision || null;
            state.envTarget = result.target_path || null;
            box.hidden = false;
            box.textContent = result.yaml;
            $("activate-env").hidden = false;
            help.hidden = false;
            help.textContent =
                `Nothing has been written yet. The button above writes this ` +
                `environment configuration to ${result.target_path}. ` +
                `The server must be restarted afterwards.` +
                (result.requires_removal
                    ? " Environment or CLI values still take precedence until you remove them."
                    : " A backup is created when the file already exists.");
            status.textContent = "";
            status.className = "hint";
        } catch (err) {
            box.hidden = false;
            box.textContent = err.message;
            $("activate-env").hidden = true;
            help.hidden = true;
            help.textContent = "";
            status.textContent = "";
        }
    }

    async function activateEnv() {
        const status = $("env-import-status");
        const help = $("env-import-help");
        if (!state.envTarget) return;
        const confirmed = window.confirm(
            `Write the current environment configuration to ${state.envTarget}?\n\n` +
            "This writes gateway passwords and control secrets to the YAML file. " +
            "Remove environment variables that should come from the file and restart the server afterward."
        );
        if (!confirmed) {
            status.textContent = "Migration cancelled.";
            status.className = "hint";
            return;
        }
        status.textContent = "Writing configuration...";
        status.className = "hint";
        try {
            const result = await api("/import-env", {
                method: "POST",
                body: JSON.stringify({ expected_revision: state.envRevision || null }),
            });
            $("activate-env").hidden = true;
            help.hidden = true;
            help.textContent = "";
            status.textContent = result.notes.join(" ");
            status.className = result.requires_removal ? "hint error" : "hint success";
        } catch (err) {
            status.textContent = err.message;
            status.className = "hint error";
        }
    }

    async function deleteConfig() {
        if (!state.status || !state.status.config_path) return;
        const target = state.status.config_path;
        const confirmed = window.confirm(
            `Delete ${target}? A backup will be written to ${target}.bak. ` +
            "Restart the server/container afterward."
        );
        if (!confirmed) return;
        try {
            const result = await api("/delete-config", {
                method: "POST",
                body: JSON.stringify({
                    expected_revision: state.status.config_file_revision || null,
                }),
            });
            state.status.restart_required = true;
            state.status.state = "restart_required";
            state.status.config_file_revision = null;
            state.status.config_error = null;
            renderStatus();
            message(
                result.message +
                    (result.backup_path ? " Backup: " + result.backup_path : ""),
                "success"
            );
        } catch (err) {
            message(err.message, "error");
        }
    }

    async function refreshStatus() {
        try {
            state.status = await api("/status");
            renderStatus();
            renderGateways();
            message("Status refreshed.", "success");
        } catch (err) {
            message(err.message, "error");
        }
    }

    function populateTimezones() {
        const list = $("timezone-options");
        const zones = (state.schema && state.schema.timezones) || [];
        if (!list) return;
        list.innerHTML = "";
        const fragment = document.createDocumentFragment();
        zones.forEach((zone) => {
            const option = document.createElement("option");
            option.value = zone;
            fragment.appendChild(option);
        });
        list.appendChild(fragment);
    }

    function renderScanResults(result) {
        const container = $("scan-results");
        const status = $("scan-status");
        container.innerHTML = "";
        status.textContent = result.message || "";
        (result.gateways || []).forEach((gateway) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-small btn-secondary";
            button.textContent = gateway.ip +
                (gateway.din ? " — " + gateway.din : "") +
                (gateway.firmware ? " — " + gateway.firmware : "");
            button.addEventListener("click", () => {
                const cards = Array.from(document.querySelectorAll(".gateway-card"));
                let card = cards.find(
                    (item) => !item.querySelector('[data-field="host"]').value.trim()
                );
                if (!card) {
                    addGateway({ mode: "tedapi", host: gateway.ip });
                    const updated = document.querySelectorAll(".gateway-card");
                    card = updated[updated.length - 1];
                } else {
                    card.querySelector('[data-field="host"]').value = gateway.ip;
                }
                if (card) {
                    card.scrollIntoView({ block: "center", behavior: "smooth" });
                }
                status.textContent = "Host set to " + gateway.ip + ". Select a mode and test the connection.";
            });
            container.appendChild(button);
        });
    }

    async function scanNetwork() {
        const button = $("scan-gateways");
        const status = $("scan-status");
        const cidr = $("scan-cidr").value.trim();
        setV1rButtonBusy(button, true, "Scanning…");
        status.textContent = "Scanning the private network…";
        $("scan-results").innerHTML = "";
        try {
            const result = await api("/scan", {
                method: "POST",
                body: JSON.stringify({ cidr: cidr || null }),
            });
            renderScanResults(result);
        } catch (err) {
            status.textContent = err.message;
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    function renderGateways() {
        if (state.schema && !$("timezone-options").childElementCount) {
            populateTimezones();
        }
        loadGateways();
        $("continue-to-settings").disabled = false;
    }

    function bind() {
        $("token-form").addEventListener("submit", connectWithToken);
        $("preview-env").addEventListener("click", previewEnv);
        $("activate-env").addEventListener("click", activateEnv);
        $("refresh-status").addEventListener("click", refreshStatus);
        $("delete-config").addEventListener("click", deleteConfig);
        $("add-gateway").addEventListener("click", () => addGateway({}));
        $("scan-output-toggle").addEventListener("click", () => {
            const section = $("scan-output");
            const toggle = $("scan-output-toggle");
            const collapsed = section.classList.toggle("collapsed");
            toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        });
        $("scan-gateways").addEventListener("click", scanNetwork);
        $("continue-to-gateways").addEventListener("click", () => {
            if (!state.status) return;
            renderGateways();
            setStep(2);
        });
        $("continue-to-settings").addEventListener("click", () => {
            if (!state.schema) return;
            renderSettings();
            setStep(3);
        });
        $("continue-to-review").addEventListener("click", renderReview);
        $("validate-config").addEventListener("click", validateConfig);
        $("save-config").addEventListener("click", saveConfig);
        document.querySelectorAll("[data-back]").forEach((button) => {
            button.addEventListener("click", () => setStep(Number(button.dataset.back)));
        });
        const infoDialog = $("setup-info-dialog");
        if (infoDialog) {
            const closeInfo = () => {
                if (infoDialog.open) infoDialog.close();
            };
            $("setup-info-cancel").addEventListener("click", closeInfo);
            $("setup-info-ok").addEventListener("click", closeInfo);
            infoDialog.addEventListener("click", (event) => {
                if (event.target === infoDialog) closeInfo();
            });
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        bind();
        enhanceSecretInputs(document);
        enhanceFieldHints(document);
        initSetupTokenStorage();
        setStep(1);
    });
    function updateAuthFileSelection(card, fileSelector, nameSelector, uploadSelector) {
        const fileInput = card.querySelector(fileSelector);
        const file = fileInput.files && fileInput.files[0];
        card.querySelector(nameSelector).textContent = file ? file.name : "No file selected";
        card.querySelector(uploadSelector).disabled = !file;
    }

    async function importCloudTokens(card) {
        const email = card.querySelector('[data-field="email"]').value.trim();
        const authpath = card.querySelector('[data-field="authpath"]').value.trim();
        const siteid = card.querySelector('[data-field="siteid"]').value.trim();
        const refresh = card.querySelector(".cloud-refresh-token");
        const access = card.querySelector(".cloud-access-token");
        const status = card.querySelector(".cloud-token-status");
        const button = card.querySelector(".cloud-token-import");
        const showStatus = (text, kind) => {
            status.className = "v1r-auth-status cloud-token-status" + (kind ? " " + kind : "");
            status.textContent = text || "";
        };
        if (!email) {
            showStatus("Set the Tesla email in the gateway form first.", "error");
            return;
        }
        if (!refresh.value.trim()) {
            showStatus("Paste the Refresh Token first.", "error");
            return;
        }
        setV1rButtonBusy(button, true, "Saving.");
        showStatus("Saving the pasted tokens.", "");
        try {
            const result = await api("/auth/tokens", {
                method: "POST",
                body: JSON.stringify({
                    email: email,
                    authpath: authpath || null,
                    siteid: siteid || null,
                    refresh_token: refresh.value.trim(),
                    access_token: access.value.trim() || null,
                }),
            });
            refresh.value = "";
            access.value = "";
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            if (result.email) {
                card.querySelector('[data-field="email"]').value = result.email;
            }
            showStatus(result.message, "success");
        } catch (err) {
            showStatus(err.message, "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }

    async function uploadAuthFile(card, mode) {
        const isFleet = mode === "fleetapi";
        const fileInput = card.querySelector(isFleet ? ".fleet-auth-file" : ".cloud-auth-file");
        const file = fileInput.files && fileInput.files[0];
        const button = card.querySelector(isFleet ? ".fleet-auth-upload" : ".cloud-auth-upload");
        const status = isFleet
            ? card.querySelector(".fleet-auth-status")
            : card.querySelector(".cloud-import-status");
        const showStatus = (text, kind) => {
            status.className = "v1r-auth-status " + (isFleet ? "fleet-auth-status" : "cloud-import-status") + (kind ? " " + kind : "");
            status.textContent = text || "";
        };
        if (!file) {
            showStatus("Choose an auth file first.", "error");
            return;
        }
        if (file.size > 256 * 1024) {
            showStatus("The selected auth file is too large.", "error");
            return;
        }
        setV1rButtonBusy(button, true, "Importing…");
        showStatus("Importing the auth file…", "");
        try {
            const authpath = card.querySelector('[data-field="authpath"]').value.trim();
            const siteid = card.querySelector('[data-field="siteid"]').value.trim();
            const content = await file.text();
            const result = await api("/auth/file", {
                method: "POST",
                body: JSON.stringify({
                    mode: mode,
                    authpath: authpath || null,
                    siteid: siteid || null,
                    content: content,
                }),
            });
            fileInput.value = "";
            updateAuthFileSelection(
                card,
                isFleet ? ".fleet-auth-file" : ".cloud-auth-file",
                isFleet ? "[data-fleet-file-name]" : "[data-cloud-file-name]",
                isFleet ? ".fleet-auth-upload" : ".cloud-auth-upload"
            );
            if (result.siteid) {
                card.querySelector('[data-field="siteid"]').value = result.siteid;
            }
            if (result.email) {
                card.querySelector('[data-field="email"]').value = result.email;
            }
            showStatus(result.message, "success");
            if (isFleet) {
                setFleetPhase(card, "Config imported", "ok");
            }
        } catch (err) {
            showStatus(err.message, "error");
        } finally {
            setV1rButtonBusy(button, false);
        }
    }
})();
