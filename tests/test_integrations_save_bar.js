// Behaviour of the shared integrations Save bar (#632), run against a minimal fake DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/integrations-save.js'), 'utf8');
const adminScripts = fs.readFileSync(path.join(__dirname, '../templates/next/_admin_user_integrations_scripts.html'), 'utf8');
const accountScripts = fs.readFileSync(path.join(__dirname, '../templates/next/_account_integrations_scripts.html'), 'utf8');

function runBookFusionSuccess(script, marker, context) {
    const start = script.indexOf(marker);
    assert.notEqual(start, -1, `callback marker exists: ${marker}`);
    const open = script.indexOf('{', start);
    let depth = 0;
    let end = open;
    for (; end < script.length; end++) {
        if (script[end] === '{') depth++;
        if (script[end] === '}' && --depth === 0) break;
    }
    const block = script.slice(start, end + 1).replace(/\{\{[\s\S]*?\|tojson\s*\}\}/g, "'BookFusion linked'");
    vm.runInContext(`(function () { ${block} })()`, context);
}

function option(value, defaultSelected) {
    return { value, defaultSelected: !!defaultSelected };
}

function checkbox(name, checked) {
    return { name, type: 'checkbox', tagName: 'INPUT', defaultChecked: checked, checked };
}

function text(name, value, type) {
    return { name, type: type || 'text', tagName: 'INPUT', defaultValue: value, value };
}

function select(name, options) {
    const chosen = options.find(o => o.defaultSelected) || options[0];
    return { name, type: 'select-one', tagName: 'SELECT', options, value: chosen.value };
}

function page() {
    const fields = {
        grimmory: checkbox('BOOKLORE_ENABLED', false),
        bookfusion: checkbox('BOOKFUSION_ENABLED', false),
        server: text('BOOKLORE_SERVER', 'http://grimmory:6060'),
        password: text('BOOKLORE_PASSWORD', '', 'password'),
        libraryId: text('BOOKLORE_LIBRARY_ID', '3'),
        pref: select('AUTO_SHELF', [option(''), option('true', true), option('false')]),
        source: select('DEVICE_SYNC_COLLECTION_SOURCE', [option('off'), option('grimmory'), option('hardcover')]),
        scan: select('SHELFMARK_AUDIOBOOK_LIBRARY_ID', [option(''), option('7', true)]),
        hidden: text('BOOKORBIT_ENABLED', 'on', 'hidden'),
    };
    // Unnamed controls: the collection gate proxy, a library picker, Test and Save buttons.
    const gate = { name: '', type: 'checkbox', tagName: 'INPUT', defaultChecked: false, checked: false };
    const picker = { name: '', type: 'select-one', tagName: 'SELECT', options: [option('')], value: '' };
    const testButton = { name: '', type: 'button', tagName: 'BUTTON' };
    const button = { type: 'submit', disabled: false };
    const note = { hidden: true };
    const handlers = {};
    const form = {
        elements: [...Object.values(fields), gate, picker, testButton, button],
        addEventListener(name, handler) { (handlers[name] = handlers[name] || []).push(handler); },
    };
    const bar = {
        closest: selector => (selector === 'form' ? form : null),
        querySelector: selector => ({ 'button[type="submit"]': button, '.integrations-dirty': note })[selector],
    };
    const context = vm.createContext({
        document: {
            readyState: 'complete',
            querySelectorAll: selector => (selector === '.integrations-save-bar' ? [bar] : []),
            addEventListener() { throw new Error('init should run immediately once the DOM is ready'); },
        },
    });
    vm.runInContext(source, context);
    const fire = (name, target) => (handlers[name] || []).forEach(handler => handler({ target }));
    const state = () => ({ disabled: button.disabled, noteHidden: note.hidden });
    return { fields, gate, picker, fire, state, context };
}

const CLEAN = { disabled: true, noteHidden: true };
const DIRTY = { disabled: false, noteHidden: false };

// Reported scenario: flipping the Grimmory switch must surface Save; flipping it back clears it.
{
    const p = page();
    assert.deepEqual(p.state(), CLEAN, 'loads clean');
    p.fields.grimmory.checked = true;
    p.fire('change', p.fields.grimmory);
    assert.deepEqual(p.state(), DIRTY, 'Grimmory switch on is unsaved');
    p.fields.grimmory.checked = false;
    p.fire('change', p.fields.grimmory);
    assert.deepEqual(p.state(), CLEAN, 'switching Grimmory back is clean again');
}

// Text, password and select edits; only a full revert is clean.
{
    const p = page();
    p.fields.server.value = 'http://grimmory:6061';
    p.fire('input', p.fields.server);
    p.fields.password.value = 'secret';
    p.fire('input', p.fields.password);
    p.fields.pref.value = 'false';
    p.fire('change', p.fields.pref);
    assert.deepEqual(p.state(), DIRTY);
    p.fields.server.value = 'http://grimmory:6060';
    p.fire('input', p.fields.server);
    p.fields.password.value = '';
    p.fire('input', p.fields.password);
    assert.deepEqual(p.state(), DIRTY, 'one edit left keeps it dirty');
    p.fields.pref.value = 'true';
    p.fire('change', p.fields.pref);
    assert.deepEqual(p.state(), CLEAN, 'every edit reverted');
}

// A library picker writes the ID field from its own change handler; the event then bubbles.
{
    const p = page();
    p.picker.value = '9';
    p.fields.libraryId.value = '9';
    p.fire('change', p.picker);
    assert.deepEqual(p.state(), DIRTY, 'picked library differs from saved');
    p.picker.value = '3';
    p.fields.libraryId.value = '3';
    p.fire('change', p.picker);
    assert.deepEqual(p.state(), CLEAN, 'picking the saved library is clean');
}

// The collection gate has no name; it drives the saved source select.
{
    const p = page();
    p.gate.checked = true;
    p.fields.source.value = 'grimmory';
    p.fire('change', p.gate);
    assert.deepEqual(p.state(), DIRTY);
    p.gate.checked = false;
    p.fields.source.value = 'off';
    p.fire('change', p.gate);
    assert.deepEqual(p.state(), CLEAN);
}

// Lookups and Test do not change saved fields: replaced options keep the saved choice clean.
{
    const p = page();
    p.fields.scan.options = [option(''), option('5'), option('7')];
    p.fields.scan.value = '7';
    p.picker.options = [option(''), option('3'), option('9')];
    p.fire('input', p.picker);
    assert.deepEqual(p.state(), CLEAN, 'lookups alone stay clean');
}

// BookFusion linking saves its own switch, without clearing other unsaved edits.
{
    const p = page();
    p.fields.server.value = 'http://other';
    p.fire('input', p.fields.server);
    p.context.document.querySelector = () => p.fields.bookfusion;
    p.fields.bookfusion.dispatchEvent = event => p.fire(event.type, p.fields.bookfusion);
    p.context.toggleIntegrationGroup = () => {};
    p.context.setBookFusionLinked = () => {};
    p.context.setBookFusionStatus = () => {};
    p.context.Event = function (type) { return { type, bubbles: true }; };
    p.context.data = { ok: true };
    p.context.result = { ok: true, data: { ok: true } };
    runBookFusionSuccess(accountScripts, 'if (result.ok && data.ok) {', p.context);
    assert.equal(p.fields.bookfusion.checked, true, 'account callback checks the saved switch');
    assert.deepEqual(p.state(), DIRTY, 'the server edit is still unsaved');
    p.fields.server.value = 'http://grimmory:6060';
    p.fire('input', p.fields.server);
    assert.deepEqual(p.state(), CLEAN, 'linked BookFusion switch is the saved value now');
    p.fields.bookfusion.checked = false;
    p.fire('change', p.fields.bookfusion);
    assert.deepEqual(p.state(), DIRTY, 'unticking the linked switch is a real change');

    const admin = page();
    admin.fields.server.value = 'http://other';
    admin.fire('input', admin.fields.server);
    admin.context.document.querySelector = () => admin.fields.bookfusion;
    admin.fields.bookfusion.dispatchEvent = event => admin.fire(event.type, admin.fields.bookfusion);
    admin.context.toggleIntegrationGroup = () => {};
    admin.context.Event = function (type) { return { type, bubbles: true }; };
    admin.context.status = { textContent: '' };
    admin.context.data = { ok: true };
    runBookFusionSuccess(adminScripts, 'if (data.ok) {', admin.context);
    assert.equal(admin.fields.bookfusion.checked, true, 'admin callback checks the saved switch');
    assert.deepEqual(admin.state(), DIRTY, 'admin link preserves unrelated unsaved edits');
    admin.fields.server.value = 'http://grimmory:6060';
    admin.fire('input', admin.fields.server);
    assert.deepEqual(admin.state(), CLEAN, 'admin linked switch is saved while unrelated edits can revert');
    admin.fields.bookfusion.checked = false;
    admin.fire('change', admin.fields.bookfusion);
    assert.deepEqual(admin.state(), DIRTY, 'turning off the admin linked switch is a real change');
}

console.log('integrations save bar: ok');
