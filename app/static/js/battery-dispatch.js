/* Battery dispatch controls for the Console. Requests use the shared token
 * and control API helpers; polling and page load never send a command.
 * Every dispatch asks for confirmation: use at your own risk. */
(function () {
    'use strict';

    // The Console caps a dispatch at 4 hours; the API accepts up to 86400 s.
    const MAX_CONSOLE_SECONDS = 14400;
    const RISK = 'Use at your own risk: we don\'t know what incorrect use could do to your home, your Powerwall system or the grid.';

    function clock(ms) {
        return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function confirmation(watts, seconds) {
        // The gateway starts the timer when it accepts the command, so the end
        // time is an estimate that assumes you confirm right away.
        const until = `for ${seconds} seconds, ending about ${clock(Date.now() + seconds * 1000)} if you confirm now (the time starts when the gateway accepts the command)`;
        const power = Math.abs(watts).toLocaleString();
        if (watts > 0) {
            return {
                title: 'Start battery discharge?',
                body: `Discharge at ${power} W ${until}? This can export electricity to the grid even when Grid Export is set to Solar Only or Never, and drain the battery below your Backup Reserve. ${RISK}`,
                confirmLabel: 'Start discharge',
            };
        }
        if (watts < 0) {
            return {
                title: 'Start battery charge?',
                body: `Charge at ${power} W ${until}? This can draw power from the grid at your current rate, and may ignore your Grid Charging setting. ${RISK}`,
                confirmLabel: 'Start charge',
            };
        }
        return {
            title: 'Hold the battery at 0 W?',
            body: `Hold the battery at 0 W ${until}? It won't power your home or store solar during that time, so your home may draw from the grid. ${RISK}`,
            confirmLabel: 'Hold at 0 W',
        };
    }

    function create({ getToken, rememberToken, onUnauthorized, post, errorText, confirmDialog }) {
        const panel = document.getElementById('control-dispatch-section');
        const form = document.getElementById('control-dispatch-form');
        const power = document.getElementById('control-dispatch-power');
        const duration = document.getElementById('control-dispatch-duration');
        const start = document.getElementById('control-dispatch-start');
        const resume = document.getElementById('control-dispatch-resume');
        const message = document.getElementById('control-dispatch-message');
        const banner = document.getElementById('control-dispatch-active');
        let available = false;
        let busy = false;
        // The running dispatch this server sent ({power_watts, deadline} with
        // deadline on this browser's clock) and whether a dispatch command is
        // still being processed. The banner is outside the panel: it stays
        // visible when dispatch is unavailable, e.g. after losing v1r.
        let active = null;
        let pending = false;
        let expiryTimer = null;
        // Bumped on each successful command, so a status response requested
        // before it can't overwrite the newer banner state.
        let revision = 0;

        function update() {
            panel.style.display = available ? '' : 'none';
            form.querySelectorAll('input, button').forEach((input) => {
                input.disabled = !available || busy;
            });
            form.setAttribute('aria-busy', String(busy));
        }

        function renderBanner() {
            if (!banner) return;
            const parts = [];
            if (active) {
                const watts = active.power_watts;
                const action = watts > 0 ? `discharging at ${watts.toLocaleString()} W`
                    : watts < 0 ? `charging at ${Math.abs(watts).toLocaleString()} W` : 'holding the battery at 0 W';
                parts.push(`Battery dispatch running: ${action} until about ${clock(active.deadline)}.`);
                parts.push(available
                    ? 'Select Resume normal operation to stop it early.'
                    : 'Local resume isn\'t available right now; the gateway ends the dispatch on its own.');
            }
            if (pending) {
                parts.push('A battery dispatch command is still being processed. Check the power flow for actual battery power.');
            }
            banner.textContent = parts.join(' ');
            banner.style.display = parts.length ? 'block' : 'none';
        }

        // remaining: seconds left, counted from now on this browser's clock,
        // so a skewed clock still ends the banner at the right moment.
        function setRunning(watts, remaining) {
            if (expiryTimer) window.clearTimeout(expiryTimer);
            expiryTimer = null;
            active = null;
            if (Number.isFinite(watts) && Number.isFinite(remaining) && remaining > 0) {
                active = { power_watts: watts, deadline: Date.now() + remaining * 1000 };
                expiryTimer = window.setTimeout(() => {
                    expiryTimer = null;
                    active = null;
                    renderBanner();
                }, remaining * 1000);
            }
            renderBanner();
        }

        function showMessage(text, type) {
            message.textContent = text;
            message.className = 'control-message' + (type ? ' ' + type : '');
            message.style.display = text ? '' : 'none';
        }

        async function send(release) {
            if (!available || busy) return;
            const token = getToken();
            if (!token) {
                showMessage('Enter your control token first.', 'error');
                document.getElementById('control-token')?.focus();
                return;
            }

            let body = {};
            let direction;
            if (!release) {
                direction = form.querySelector('input[name="control-dispatch-direction"]:checked')?.value;
                const watts = Number(power.value);
                const seconds = Number(duration.value);
                if (!power.value.trim() || !Number.isFinite(watts) || watts < 0) {
                    showMessage('Enter a power of 0 W or greater.', 'error');
                    power.focus();
                    return;
                }
                if (!duration.value.trim() || !Number.isInteger(seconds) || seconds < 1 || seconds > MAX_CONSOLE_SECONDS) {
                    showMessage(`Enter a whole duration from 1 to ${MAX_CONSOLE_SECONDS} seconds (4 hours).`, 'error');
                    duration.focus();
                    return;
                }
                if (direction !== 'charge' && direction !== 'discharge') {
                    showMessage('Choose charge or discharge.', 'error');
                    return;
                }
                body = { power_watts: direction === 'charge' ? -watts : watts, duration_seconds: seconds };
            }

            busy = true;
            update();
            const button = release ? resume : start;
            const label = button.textContent;
            try {
                if (!release) {
                    const confirmed = await confirmDialog({
                        ...confirmation(body.power_watts, body.duration_seconds),
                        danger: true,
                    });
                    if (!confirmed || !available) {
                        showMessage('Dispatch cancelled.', '');
                        return;
                    }
                }
                rememberToken(token);
                button.textContent = 'Sending…';
                showMessage(release ? 'Requesting normal operation…' : 'Sending battery dispatch…', '');
                const { resp, payload } = await post(release ? 'resume' : 'dispatch', body, token);
                if (!resp.ok || payload?.error || payload?.ERROR) {
                    if (resp.status === 401) onUnauthorized();
                    throw new Error(errorText(resp.status, payload, 'Battery dispatch request failed.'));
                }
                const acknowledged = release
                    ? payload?.resume_battery_operation === true
                    : payload?.power_watts === body.power_watts
                        && payload?.duration_seconds === body.duration_seconds;
                if (!acknowledged) throw new Error('The gateway did not acknowledge this request.');
                revision++;
                pending = false;
                if (release) {
                    setRunning(null, 0);
                    showMessage('Resume accepted. The battery will follow its configured operating mode.', 'success');
                } else {
                    setRunning(body.power_watts, body.duration_seconds);
                    const action = body.power_watts === 0 ? 'Zero-power'
                        : direction === 'charge' ? 'Charging' : 'Discharging';
                    showMessage(`${action} request accepted: ${Math.abs(body.power_watts).toLocaleString()} W for ${body.duration_seconds} s. Check the power flow for actual battery power.`, 'success');
                }
            } catch (error) {
                showMessage((error.message || 'Request failed.') + ' Check battery power; a dispatch ends when its duration expires.', 'error');
            } finally {
                busy = false;
                button.textContent = label;
                update();
            }
        }

        form.addEventListener('submit', (event) => {
            event.preventDefault();
            send(false);
        });
        resume.addEventListener('click', () => send(true));
        update();
        return {
            // Read before requesting /control/status; pass it back with the result.
            revision() { return revision; },
            // value: dispatch is opted in and available (shows the controls).
            // dispatch: {active: the server's dispatch_active
            // ({power_watts, remaining_seconds}) or null, pending: bool}, or
            // undefined when unknown (the banner is left as it is).
            // requestedAt: the revision() read before that status request; a
            // response requested before a newer successful command doesn't
            // change the banner.
            setAvailable(value, dispatch, requestedAt) {
                available = value === true;
                update();
                if (dispatch !== undefined && (requestedAt === undefined || requestedAt === revision)) {
                    pending = dispatch?.pending === true;
                    setRunning(dispatch?.active?.power_watts, dispatch?.active?.remaining_seconds);
                } else {
                    renderBanner();
                }
            },
        };
    }

    window.PWBatteryDispatch = { create };
})();
