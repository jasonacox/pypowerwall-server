/* Console battery dispatch controls. Only a user action sends a command
 * (each dispatch is confirmed first); page load and polling never do. */
(function () {
    'use strict';

    // The Console caps a dispatch at 4 hours; the API accepts up to 86400 s.
    const MAX_CONSOLE_SECONDS = 14400;
    const RISK = 'Use at your own risk: it could void or limit your Tesla warranty, and we don\'t know what incorrect use could do to your home, your Powerwall system or the grid, and behavior during a grid outage is untested.';

    function clock(ms) {
        return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    // End times are estimates: the gateway's timer starts when it accepts.
    function until(seconds) {
        return `until about ${clock(Date.now() + seconds * 1000)}`;
    }

    function confirmation(watts, seconds) {
        const timing = `for ${seconds} seconds`;
        const power = Math.abs(watts).toLocaleString();
        if (watts > 0) {
            return {
                title: 'Start battery discharge?',
                body: `Discharge at ${power} W ${timing}? This can export electricity to the grid even when Grid Export is set to Solar or Never, and drain the battery below your Backup Reserve. ${RISK}`,
                confirmLabel: 'Start discharge',
            };
        }
        if (watts < 0) {
            return {
                title: 'Start battery charge?',
                body: `Charge at ${power} W ${timing}? This can draw power from the grid at your current rate, and may ignore your Grid Charging setting. ${RISK}`,
                confirmLabel: 'Start charge',
            };
        }
        return {
            title: 'Hold the battery at 0 W?',
            body: `Hold the battery at 0 W ${timing}? It won't power your home or store solar during that time, so your home may draw from the grid. ${RISK}`,
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
        let available = false;
        let busy = false;

        function update() {
            panel.style.display = available ? '' : 'none';
            form.querySelectorAll('input, button').forEach((input) => {
                input.disabled = !available || busy;
            });
            form.setAttribute('aria-busy', String(busy));
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
                if (release) {
                    showMessage('Resume accepted. The battery will follow its configured operating mode.', 'success');
                } else {
                    const action = body.power_watts === 0 ? 'Hold at 0 W'
                        : direction === 'charge' ? `Charge at ${Math.abs(body.power_watts).toLocaleString()} W`
                            : `Discharge at ${body.power_watts.toLocaleString()} W`;
                    showMessage(`${action} accepted, ${until(body.duration_seconds)}. Check the power flow for actual battery power.`, 'success');
                }
            } catch (error) {
                showMessage((error.message || 'Request failed.') + ' Check the power flow before retrying: any dispatch the gateway accepted ends on its own when its duration expires.', 'error');
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
            setAvailable(value) {
                available = value === true;
                update();
            },
        };
    }

    window.PWBatteryDispatch = { create };
})();
