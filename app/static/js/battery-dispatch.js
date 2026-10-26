/* Battery dispatch controls for the Console. Requests use the shared token
 * and control API helpers; polling and page load never send a command. */
(function () {
    'use strict';

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
                if (!duration.value.trim() || !Number.isInteger(seconds) || seconds < 1 || seconds > 86400) {
                    showMessage('Enter a whole duration from 1 to 86400 seconds.', 'error');
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
                if (!release && body.power_watts > 0) {
                    const confirmed = await confirmDialog({
                        title: 'Start battery discharge?',
                        body: `Discharge at ${body.power_watts.toLocaleString()} W for ${body.duration_seconds} seconds? This can export electricity to the grid even when Grid Export is set to Solar Only or Never, and drain the battery below your Backup Reserve. Only continue if your utility permits export and you accept using your backup energy.`,
                        confirmLabel: 'Start discharge',
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
            setAvailable(value) {
                available = value === true;
                update();
            },
        };
    }

    window.PWBatteryDispatch = { create };
})();
