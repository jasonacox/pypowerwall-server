// Console renderers must show server and config strings as text, not markup.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const consoleSource = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');

function consoleFunction(name) {
    const start = consoleSource.search(new RegExp(`        (?:async )?function ${name}\\(`));
    assert.ok(start >= 0, `Console function ${name} exists`);
    const end = consoleSource.indexOf('\n        }', start);
    assert.ok(end >= 0);
    return consoleSource.slice(start, end + '\n        }'.length);
}

function load(names, context = {}) {
    vm.createContext(context);
    vm.runInContext(names.map(consoleFunction).join('\n'), context);
    return context;
}

const PAYLOAD = '<b>bold</b><img src=x onerror="alert(1)">';
const ESCAPED = '&lt;b&gt;bold&lt;/b&gt;&lt;img src=x onerror=&quot;alert(1)&quot;&gt;';

test('escapeHtml escapes markup characters and blanks null/undefined', () => {
    const { escapeHtml } = load(['escapeHtml']);
    assert.equal(escapeHtml(`&<>"'`), '&amp;&lt;&gt;&quot;&#39;');
    assert.equal(escapeHtml(null), '');
    assert.equal(escapeHtml(undefined), '');
    assert.equal(escapeHtml(0), '0');
});

test('gateway list escapes name, host, mode, firmware and error', async () => {
    const container = { innerHTML: '' };
    const context = load(['escapeHtml', 'loadGateways'], {
        secondaryLoadComplete: false,
        console,
        document: { getElementById: () => container },
        fetch: async url => ({
            ok: true,
            json: async () => (url === '/version'
                ? { version: PAYLOAD }
                : {
                    gw1: {
                        online: true,
                        gateway: { name: PAYLOAD, host: PAYLOAD },
                        data: { connection_mode: PAYLOAD },
                        error: PAYLOAD,
                    },
                }),
        }),
    });
    await context.loadGateways();
    const html = container.innerHTML;
    assert.ok(!html.includes('<b>') && !html.includes('<img'), html);
    for (const cls of ['gateway-name', 'gateway-host', 'gateway-mode', 'gateway-error']) {
        assert.ok(html.includes(`<div class="${cls}">${ESCAPED}</div>`) ||
            html.includes(`<span class="${cls}">${ESCAPED}</span>`), cls);
    }
    assert.ok(html.includes(`Firmware: ${ESCAPED}</span>`));
});

test('alerts, strings and section headers escape server text', () => {
    const { formatAlerts, formatStrings } = load(['escapeHtml', 'formatAlerts', 'formatStrings']);
    const alerts = formatAlerts([PAYLOAD]);
    assert.ok(alerts.includes(`<span>${ESCAPED}</span>`) && !alerts.includes('<b>'), alerts);
    const strings = formatStrings({ [PAYLOAD]: { Connected: true, Voltage: PAYLOAD, Current: 1, Power: 2 } }).html;
    assert.ok(strings.includes(`<span class="string-name">${ESCAPED}</span>`), strings);
    assert.ok(strings.includes(`${ESCAPED}V</span>`) && !strings.includes('<b>'), strings);
});
