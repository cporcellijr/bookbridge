const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/next/display.js', 'utf8');
assert.match(fs.readFileSync('static/js/next/cover-loader.js','utf8'), /getElementById\('library-page'\) \|\| document\.getElementById\('display-preview-stage'\)/, 'preview thumbnails must use the shared cover loader');

function mount({fails = false, key = 'UI_LIBRARY_STYLE', choices = ['cards', 'spines', 'faceout', 'list'], applyTheme = null} = {}) {
    const values = {};
    const requests = [];
    const listeners = {};
    const attrs = {};
    const status = {
        textContent: '',
        toggleAttribute(name, enabled) { attrs[name] = enabled; },
        removeAttribute(name) { delete attrs[name]; }
    };
    const buttons = choices.map(value => ({
        dataset: {displayValue: value},
        attrs: {'aria-pressed': value === choices[0] ? 'true' : 'false'},
        disabled: false,
        setAttribute(name, value) { this.attrs[name] = value; }
    }));
    const group = {querySelector(selector) { return selector === '.display-save-status' ? status : null; }};
    const form = {
        dataset: {displaySetting: key},
        addEventListener(name, fn) { listeners[name] = fn; },
        querySelectorAll(selector) { return selector === '[data-display-value]' || selector === 'button, input[type="checkbox"]' ? buttons : []; },
        querySelector(selector) {
            if (selector === '[data-display-value][aria-pressed="true"]') return buttons.find(button => button.attrs['aria-pressed'] === 'true');
            return null;
        },
        contains(button) { return buttons.includes(button); },
        closest(selector) { return selector === '[data-display-group]' ? group : null; }
    };
    const stage = {dataset: {view: 'covers', libraryStyle: 'cards'}, querySelectorAll() { return []; }};
    const page = {classList: {add(name) { values.className = name; }}};
    const document = {
        querySelector(selector) {
            if (selector === '.display-page') return page;
            return selector === '[data-display-setting="' + key + '"]' ? form : null;
        },
        querySelectorAll(selector) { return selector === '.display-page form[data-display-setting]' ? [form] : []; },
        getElementById(id) { return id === 'display-preview-stage' ? stage : null; },
        addEventListener(name, fn) { values[name] = fn; }
    };
    const window = {
        bbT: value => value,
        fetch(url, options) {
            requests.push({url, options});
            return Promise.resolve({ok: !fails});
        },
        setTimeout() { return 1; }, clearTimeout() {}
    };
    if (applyTheme) window.bbApplyThemePreference = value => applyTheme(value, values);
    vm.runInNewContext(source, {document, window, WeakMap, console});
    async function click(value) {
        const button = buttons.find(button => button.dataset.displayValue === value);
        let prevented = false;
        listeners.click({target: {closest(selector) { return selector === '[data-display-value]' ? button : null; }}, preventDefault() { prevented = true; }});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(prevented, true);
    }
    return {click, buttons, stage, attrs, values, requests, status};
}

function verifyShellThemeBridge() {
    const shell = fs.readFileSync('static/js/next/shell.js', 'utf8');
    const root = {dataset: {themePref: 'day', theme: 'light'}};
    const messages = [];
    const toggle = {
        setAttribute(name, value) { this[name] = value; },
        querySelector() { return {textContent: ''}; },
        addEventListener(name, callback) { if (name === 'click') this.click = callback; }
    };
    const document = {
        documentElement: root,
        querySelector(selector) {return selector === '[data-theme-toggle]' ? toggle : null;},
        querySelectorAll() {return [];},
        addEventListener() {},
        dispatchEvent(event) {messages.push(['event', event.type, event.detail.value]);}
    };
    const window = {
        BB_STRINGS: {}, matchMedia() {return {matches: false, addEventListener() {}};},
        fetch() {throw new Error('Theme bridge should own the save');}
    };
    class CustomEvent {constructor(type, properties) {this.type=type; this.detail=properties.detail;}}
    vm.runInNewContext(shell, {window, document, CustomEvent});
    window.bbDisplaySaveTheme = (value, previous) => messages.push(['save', value, previous]);
    toggle.click();
    assert.equal(root.dataset.themePref, 'night');
    assert.equal(root.dataset.theme, 'dark');
    assert.deepEqual(messages, [['event', 'bb:theme-preference', 'night'], ['save', 'night', 'day']]);
}

(async () => {
    verifyShellThemeBridge();
    const good = mount();
    assert.equal(good.values.className, 'js-display');
    await good.click('faceout');
    assert.equal(good.requests.length, 1);
    assert.equal(good.requests[0].url, '/account/display');
    assert.equal(good.requests[0].options.method, 'POST');
    assert.equal(JSON.parse(good.requests[0].options.body).UI_LIBRARY_STYLE, 'faceout');
    assert.equal(good.stage.dataset.view, 'spines');
    assert.equal(good.stage.dataset.libraryStyle, 'faceout');
    assert.equal(good.status.textContent, 'Saved');
    assert.equal(good.attrs['data-visible'], true);
    assert.equal(good.buttons[2].attrs['aria-pressed'], 'true');
    assert.equal(good.buttons[2].disabled, false);
    await good.click('faceout');
    assert.equal(good.requests.length, 1, 'already-selected style must not post again');
    const bad = mount({fails: true});
    await bad.click('list');
    assert.equal(bad.requests.length, 1);
    assert.equal(bad.stage.dataset.view, 'covers', 'failed save restores preview');
    assert.equal(bad.stage.dataset.libraryStyle, 'cards');
    assert.equal(bad.buttons[0].attrs['aria-pressed'], 'true', 'failed save restores pressed control');
    assert.equal(bad.attrs['data-error'], true);
    assert.equal(bad.status.textContent, "Couldn't save");
    let applied = 0;
    const theme = mount({key: 'UI_THEME', choices: ['auto', 'day', 'night'], applyTheme(value, values) {
        applied += 1;
        if (applied > 5) throw new Error('theme change re-applied itself');
        values['bb:theme-preference']({detail: {value}});
    }});
    await theme.click('night');
    assert.equal(applied, 1, 'a theme click applies the theme once');
    assert.equal(theme.buttons[2].attrs['aria-pressed'], 'true');
    assert.equal(JSON.parse(theme.requests[0].options.body).UI_THEME, 'night');
    console.log('Display save, feedback, rollback, and preview behavior ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
