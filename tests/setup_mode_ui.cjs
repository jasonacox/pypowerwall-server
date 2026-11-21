// Exercise the setup page's real updateModeFields/collectGateway functions.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const setupSource = fs.readFileSync(
    path.join(__dirname, '../app/static/js/setup.js'),
    'utf8'
);

const exposed = setupSource.replace(
    /\}\)\(\);\s*$/,
    'globalThis.__PWSetupTest = { updateModeFields, collectGateway };\n})();\n'
);
assert.notEqual(exposed, setupSource, 'test hook inserted into setup.js');

const context = {
    window: {},
    document: {
        addEventListener() {},
        getElementById() { return null; },
    },
    localStorage: {
        getItem() { return null; },
        setItem() {},
        removeItem() {},
    },
    sessionStorage: {
        getItem() { return null; },
        setItem() {},
        removeItem() {},
    },
    fetch: async () => { throw new Error('unexpected fetch'); },
    console,
};
vm.createContext(context);
vm.runInContext(exposed, context, { filename: 'setup.js' });

const { updateModeFields, collectGateway } = context.__PWSetupTest;

function field(value = '') {
    const node = {
        value,
        hidden: false,
        dataset: {},
        textContent: '',
    };
    node.closest = () => node._field || (node._field = { hidden: false });
    return node;
}

function makeCard(mode) {
    const fields = {};
    for (const name of [
        'id', 'name', 'mode', 'type', 'host', 'port', 'timezone',
        'email', 'authpath', 'gw_pwd', 'password', 'rsa_key_path', 'wifi_host',
    ]) {
        fields[name] = field();
    }
    fields.id.value = 'home';
    fields.name.value = 'Home';
    fields.mode.value = mode;
    fields.type.value = 'powerwall';
    fields.timezone.value = 'UTC';
    fields.host.value = '192.168.91.1';
    fields.email.value = 'owner@example.com';
    fields.authpath.value = '/auth';

    const modeFields = ['gw_pwd', 'password', 'rsa_key_path', 'wifi_host', 'email', 'authpath']
        .map((name) => {
            const node = field(fields[name].value);
            node.dataset.modeField = name;
            return node;
        });

    const elements = {
        '.generate-key': field(),
        '.check-auth': field(),
        '[data-cloud-auth]': field(),
        '[data-v1r-registration]': field(),
        '[data-fleet-registration]': field(),
        '[data-mode-description]': field(),
        '[data-mode-help]': field(),
    };
    const card = {
        dataset: {},
        querySelector(selector) {
            const match = selector.match(/^\[data-field="([^"]+)"\]$/);
            if (match) return fields[match[1]] || null;
            return elements[selector] || null;
        },
        querySelectorAll(selector) {
            if (selector === '[data-mode-field]') return modeFields;
            return [];
        },
    };
    return { card, fields, modeFields, elements };
}

test('local hides email/authpath and does not send them', () => {
    const { card, modeFields } = makeCard('local');
    updateModeFields(card);
    const email = modeFields.find((node) => node.dataset.modeField === 'email');
    const authpath = modeFields.find((node) => node.dataset.modeField === 'authpath');
    assert.equal(email.hidden, true);
    assert.equal(authpath.hidden, true);

    const data = collectGateway(card);
    assert.equal(Object.prototype.hasOwnProperty.call(data, 'email'), false);
    assert.equal(Object.prototype.hasOwnProperty.call(data, 'authpath'), false);
    assert.equal(data.mode, 'local');
    assert.equal(data.host, '192.168.91.1');
});

for (const mode of ['basic_lan', 'tedapi', 'tedapi_v1r', 'hybrid_tedapi']) {
    test(`${mode} keeps email/authpath available for hybrid cloud control`, () => {
        const { card, modeFields } = makeCard(mode);
        updateModeFields(card);
        const email = modeFields.find((node) => node.dataset.modeField === 'email');
        const authpath = modeFields.find((node) => node.dataset.modeField === 'authpath');
        assert.equal(email.hidden, false);
        assert.equal(authpath.hidden, false);

        const data = collectGateway(card);
        assert.equal(data.email, 'owner@example.com');
        assert.equal(data.authpath, '/auth');
        assert.equal(data.mode, mode);
    });
}

const authControlVisibility = {
    local: { cloud: false, check: false },
    basic_lan: { cloud: false, check: false },
    tedapi: { cloud: false, check: false },
    tedapi_v1r: { cloud: false, check: false },
    hybrid_tedapi: { cloud: true, check: true },
    cloud: { cloud: true, check: true },
    hybrid: { cloud: true, check: true },
    fleetapi: { cloud: false, check: true },
    hybrid_fleetapi: { cloud: false, check: true },
};

for (const [mode, expected] of Object.entries(authControlVisibility)) {
    test(`${mode} cloud auth control visibility`, () => {
        const { card, elements } = makeCard(mode);
        updateModeFields(card);
        assert.equal(elements['[data-cloud-auth]'].hidden, !expected.cloud);
        assert.equal(elements['.check-auth'].hidden, !expected.check);
    });
}
