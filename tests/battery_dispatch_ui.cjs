// Exercise the Console controller without a browser or a Powerwall.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/static/js/battery-dispatch.js'), 'utf8');
const consoleSource = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');

function consoleFunction(name) {
    // Run the actual Console functions so their availability wiring is covered
    // in addition to the standalone form controller below.
    const pattern = new RegExp(`        (?:async )?function ${name}\\(`);
    const start = consoleSource.search(pattern);
    assert.ok(start >= 0, `Console function ${name} exists`);
    const end = consoleSource.indexOf('\n        }', start);
    assert.ok(end >= 0);
    return consoleSource.slice(start, end + '\n        }'.length);
}

function availabilityFixture({ capability = true, enabled = true, statsMode = 'full' } = {}) {
    let status = { enabled, battery_dispatch: capability };
    let statusError = false;
    let statsError = false;
    const elements = new Map();
    const context = {
        controlEnabled: false,
        controlIslandingAvailable: false,
        available: false,
        gatewayMeta: {}, window: {}, console: { log() {} },
        document: { getElementById(id) {
            if (!elements.has(id)) elements.set(id, { style: {}, textContent: '' });
            return elements.get(id);
        } },
        updateIslandingControls() {},
        loadControlValues: async () => {},
        controlBatteryDispatch: { setAvailable(value) { context.available = value; } },
        fetch: async url => ({
            ok: url === '/control/status' ? !statusError : url === '/stats' ? !statsError : true,
            status: 503,
            json: async () => url === '/control/status' ? status : url === '/stats'
                ? { tedapi: true, tedapi_mode: statsMode } : {},
        }),
    };
    for (const name of ['loadAlerts', 'loadStrings', 'loadGridStatus', 'loadPowerwalls', 'loadGateways', 'loadMqtt', 'loadDailyEnergy']) {
        context[name] = () => {};
    }
    vm.createContext(context);
    vm.runInContext(['loadStats', 'loadControlStatus', 'refreshSecondaryData'].map(consoleFunction).join('\n'), context);
    return {
        context,
        setStatus(value) { status = value; },
        failStatus() { statusError = true; },
        failStats() { statsError = true; },
    };
}

for (const [capability, statsMode] of [[true, 'full'], [false, 'v1r']]) {
    for (const statsFirst of [true, false]) {
        test(`Console uses default capability ${capability} with aggregate ${statsMode}; stats first: ${statsFirst}`, async () => {
            const f = availabilityFixture({ capability, statsMode });
            const first = statsFirst ? 'loadStats' : 'loadControlStatus';
            const second = statsFirst ? 'loadControlStatus' : 'loadStats';
            await f.context[first]();
            await f.context[second]();
            assert.equal(f.context.available, capability);
        });
    }
}

test('secondary polling refreshes capabilities when the live connection changes', async () => {
    const f = availabilityFixture({ statsMode: 'v1r' });
    await f.context.loadControlStatus();
    assert.equal(f.context.available, true);
    f.setStatus({ enabled: true, battery_dispatch: false });
    f.context.refreshSecondaryData();
    await new Promise(setImmediate);
    assert.equal(f.context.available, false);
    f.setStatus({ enabled: true, battery_dispatch: true });
    f.context.refreshSecondaryData();
    await new Promise(setImmediate);
    assert.equal(f.context.available, true);
});

test('unknown capability and disabled control hide dispatch', async () => {
    const f = availabilityFixture();
    f.setStatus({ enabled: true });
    await f.context.loadControlStatus();
    assert.equal(f.context.available, false);
    f.setStatus({ enabled: false, battery_dispatch: true });
    await f.context.loadControlStatus();
    assert.equal(f.context.available, false);
});

test('status errors fail closed while aggregate stats errors do not hide valid controls', async () => {
    const f = availabilityFixture();
    await f.context.loadControlStatus();
    f.failStats();
    await f.context.loadStats();
    assert.equal(f.context.available, true);
    f.failStatus();
    await f.context.loadControlStatus();
    assert.equal(f.context.available, false);
});

function fixture(options = {}) {
    const elements = {};
    for (const name of ['section', 'form', 'power', 'duration', 'start', 'resume', 'message']) {
        elements['control-dispatch-' + name] = {
            style: {}, attrs: {}, listeners: {}, value: '', disabled: false,
            textContent: name, focused: false,
            setAttribute(name, value) { this.attrs[name] = value; },
            addEventListener(name, handler) { this.listeners[name] = handler; },
            focus() { this.focused = true; },
        };
    }
    const tokenInput = { focus() { this.focused = true; } };
    const form = elements['control-dispatch-form'];
    let direction = 'discharge';
    form.querySelector = () => direction === null ? null : { value: direction };
    form.querySelectorAll = () => ['power', 'duration', 'start', 'resume'].map(name => elements['control-dispatch-' + name]);
    elements['control-dispatch-power'].value = '5000';
    elements['control-dispatch-duration'].value = '30';
    const context = { window: {}, document: { getElementById: id => id === 'control-token' ? tokenInput : elements[id] } };
    vm.runInNewContext(source, context);
    const requests = [];
    const confirmations = [];
    let token = options.token === undefined ? 'test-token' : options.token;
    let forgotten = 0;
    let remembered = '';
    const controller = context.window.PWBatteryDispatch.create({
        getToken: () => token,
        rememberToken: value => { remembered = value; },
        onUnauthorized: () => { token = ''; forgotten++; },
        post: async (route, body, auth) => {
            requests.push(JSON.parse(JSON.stringify({ route, body, auth })));
            if (options.post) return options.post(route, body, auth);
            return { resp: { ok: true, status: 200 }, payload: route === 'resume' ? { resume_battery_operation: true } : body };
        },
        errorText: (status, payload) => payload?.detail || `Request failed (${status})`,
        confirmDialog: async details => {
            confirmations.push(details);
            assert.equal(requests.length, 0, 'confirmation precedes the request');
            return options.confirm ? options.confirm(details) : true;
        },
    });
    async function fire(name, event) {
        elements['control-dispatch-' + name].listeners[event]({ preventDefault() {} });
        await new Promise(setImmediate);
    }
    return {
        controller, elements, requests, confirmations, tokenInput, fire,
        setDirection(value) { direction = value; },
        get forgotten() { return forgotten; },
        get remembered() { return remembered; },
        get message() { return elements['control-dispatch-message'].textContent; },
    };
}

test('loading and availability changes never send a command', async () => {
    const f = fixture();
    assert.equal(f.elements['control-dispatch-section'].style.display, 'none');
    await f.fire('form', 'submit');
    f.controller.setAvailable(true);
    assert.equal(f.elements['control-dispatch-start'].disabled, false);
    f.controller.setAvailable(false);
    await f.fire('resume', 'click');
    assert.equal(f.requests.length, 0);
});

for (const [direction, watts] of [['discharge', 5000], ['charge', -5000]]) {
    test(`${direction} sends watts, duration and the shared token`, async () => {
        const f = fixture();
        f.controller.setAvailable(true);
        f.setDirection(direction);
        await f.fire('form', 'submit');
        assert.deepEqual(f.requests, [{ route: 'dispatch', body: { power_watts: watts, duration_seconds: 30 }, auth: 'test-token' }]);
        assert.equal(f.remembered, 'test-token');
        assert.match(f.message, /request accepted/);
        assert.match(f.message, /actual battery power/);
        assert.equal(f.confirmations.length, direction === 'discharge' ? 1 : 0);
    });
}

test('zero power remains a dispatch', async () => {
    const f = fixture();
    f.controller.setAvailable(true);
    f.elements['control-dispatch-power'].value = '0';
    await f.fire('form', 'submit');
    assert.equal(f.requests[0].route, 'dispatch');
    assert.equal(f.requests[0].body.power_watts, 0);
    assert.match(f.message, /Zero-power/);
    assert.equal(f.confirmations.length, 0);
});

test('discharge confirmation warns about export and backup reserve; cancelling sends nothing', async () => {
    const f = fixture({ confirm: () => false });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    assert.equal(f.confirmations.length, 1);
    const dialog = f.confirmations[0];
    assert.match(dialog.body, /5,000 W for 30 seconds/);
    assert.match(dialog.body, /export electricity to the grid/);
    assert.match(dialog.body, /Solar Only or Never/);
    assert.match(dialog.body, /below your Backup Reserve/);
    assert.equal(dialog.danger, true);
    assert.equal(f.requests.length, 0);
    assert.equal(f.remembered, '');
    assert.equal(f.elements['control-dispatch-form'].attrs['aria-busy'], 'false');
    assert.equal(f.elements['control-dispatch-start'].disabled, false);
    assert.match(f.message, /cancelled/);
});

test('pending confirmation blocks duplicate commands and sends the confirmed values', async () => {
    let finish;
    const f = fixture({ confirm: () => new Promise(resolve => { finish = resolve; }) });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    assert.equal(f.elements['control-dispatch-start'].disabled, true);
    await f.fire('form', 'submit');
    await f.fire('resume', 'click');
    assert.equal(f.confirmations.length, 1);
    assert.equal(f.requests.length, 0);
    f.elements['control-dispatch-power'].value = '1000';
    finish(true);
    await new Promise(setImmediate);
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].body.power_watts, 5000);
    assert.equal(f.elements['control-dispatch-start'].disabled, false);
});

test('losing availability while confirming prevents discharge', async () => {
    let finish;
    const f = fixture({ confirm: () => new Promise(resolve => { finish = resolve; }) });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    f.controller.setAvailable(false);
    finish(true);
    await new Promise(setImmediate);
    assert.equal(f.requests.length, 0);
    assert.equal(f.elements['control-dispatch-form'].attrs['aria-busy'], 'false');
});

for (const [field, values] of [['power', ['', '-1', 'NaN', 'Infinity']], ['duration', ['', '0', '-1', '1.5', '86401']]]) {
    for (const value of values) {
        test(`invalid ${field} ${JSON.stringify(value)} never sends`, async () => {
            const f = fixture();
            f.controller.setAvailable(true);
            f.elements['control-dispatch-' + field].value = value;
            await f.fire('form', 'submit');
            assert.equal(f.requests.length, 0);
            assert.equal(f.confirmations.length, 0);
            assert.equal(f.elements['control-dispatch-' + field].focused, true);
        });
    }
}

test('resume works with invalid dispatch fields', async () => {
    const f = fixture();
    f.controller.setAvailable(true);
    f.elements['control-dispatch-power'].value = '';
    f.elements['control-dispatch-duration'].value = '';
    await f.fire('resume', 'click');
    assert.deepEqual(f.requests, [{ route: 'resume', body: {}, auth: 'test-token' }]);
    assert.match(f.message, /Resume accepted/);
    assert.equal(f.confirmations.length, 0);
});

test('a missing token never sends', async () => {
    const f = fixture({ token: '' });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    await f.fire('resume', 'click');
    assert.equal(f.requests.length, 0);
    assert.equal(f.tokenInput.focused, true);
    assert.equal(f.confirmations.length, 0);
});

test('a rejected token is forgotten', async () => {
    const f = fixture({ post: async () => ({ resp: { ok: false, status: 401 }, payload: { detail: 'Invalid control token' } }) });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    assert.equal(f.forgotten, 1);
    assert.match(f.message, /Invalid control token/);
    await f.fire('resume', 'click');
    assert.equal(f.requests.length, 1);
});

test('pending commands block duplicate dispatch and resume requests', async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const f = fixture({ post: () => pending });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    assert.equal(f.elements['control-dispatch-form'].attrs['aria-busy'], 'true');
    assert.equal(f.elements['control-dispatch-resume'].disabled, true);
    await f.fire('form', 'submit');
    await f.fire('resume', 'click');
    assert.equal(f.requests.length, 1);
    finish({ resp: { ok: true, status: 200 }, payload: { power_watts: 5000, duration_seconds: 30 } });
    await new Promise(setImmediate);
    assert.equal(f.elements['control-dispatch-start'].disabled, false);
});

for (const response of [
    { resp: { ok: false, status: 503 }, payload: { detail: 'Battery dispatch command failed' } },
    { resp: { ok: true, status: 200 }, payload: null },
    { resp: { ok: true, status: 200 }, payload: { power_watts: -5000, duration_seconds: 30 } },
]) {
    test(`unacknowledged response never shows success: ${JSON.stringify(response)}`, async () => {
        const f = fixture({ post: async () => response });
        f.controller.setAvailable(true);
        await f.fire('form', 'submit');
        assert.match(f.elements['control-dispatch-message'].className, /error/);
        assert.equal(f.elements['control-dispatch-start'].disabled, false);
    });
}

test('network failure permits resume without resending the dispatch', async () => {
    const f = fixture({ post: async () => { throw new Error('Network unavailable'); } });
    f.controller.setAvailable(true);
    await f.fire('form', 'submit');
    assert.match(f.message, /Network unavailable/);
    assert.equal(f.elements['control-dispatch-resume'].disabled, false);
    assert.equal(f.requests.length, 1);
});
