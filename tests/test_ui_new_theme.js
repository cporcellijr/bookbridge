// P15c: pre-paint theme, live device changes, and saved rail quick switch.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('templates/next/base.html', 'utf8');
const head = template.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(head, 'the New shell needs a pre-paint theme script');
const shell = fs.readFileSync('static/js/next/shell.js', 'utf8');

function mediaQuery(light, legacy = false) {
    return {
        matches: light,
        listener: null,
        addEventListener: legacy ? undefined : function (name, listener) {
            assert.equal(name, 'change');
            this.listener = listener;
        },
        addListener: legacy ? function (listener) { this.listener = listener; } : undefined,
        change(lightMode) {
            this.matches = lightMode;
            if (this.listener) this.listener({matches: lightMode});
        }
    };
}

function beforePaint(preference, lightMode) {
    const root = {dataset: {theme: 'dark', themePref: preference}};
    const media = mediaQuery(lightMode);
    const window = {matchMedia: query => {
        assert.equal(query, '(prefers-color-scheme: light)');
        return media;
    }};
    vm.runInNewContext(head[1], {document: {documentElement: root}, window});
    return root.dataset.theme;
}

assert.equal(beforePaint('day', false), 'light');
assert.equal(beforePaint('night', true), 'dark');
assert.equal(beforePaint('auto', true), 'light');
assert.equal(beforePaint('auto', false), 'dark');

function mount(preference, lightMode, {legacy = false, fail = false} = {}) {
    const root = {dataset: {themePref: preference, theme: 'dark'}};
    const media = mediaQuery(lightMode, legacy);
    const attrs = {};
    const label = {textContent: ''};
    const toggle = {
        setAttribute(name, value) { attrs[name] = value; },
        querySelector(selector) { return selector === '.theme-label' ? label : null; },
        addEventListener(name, cb) { if (name === 'click') this.click = cb; }
    };
    const select = {value: preference};
    const requests = [];
    const touched = [];
    const document = {
        documentElement: root,
        querySelector(selector) {
            if (selector === '[data-theme-toggle]') return toggle;
            if (selector === 'select[name="UI_THEME"]') return select;
            return null;
        },
        querySelectorAll() { return []; },
        addEventListener() {}
    };
    const window = {
        matchMedia: query => {
            assert.equal(query, '(prefers-color-scheme: light)');
            return media;
        },
        BB_STRINGS: {},
        localStorage: {
            getItem(key) { touched.push(['read', key]); return null; },
            setItem(key, value) { touched.push(['write', key, value]); }
        },
        fetch(url, options) {
            requests.push({url, options});
            return fail ? Promise.reject(new Error('Offline')) : Promise.resolve({ok: true});
        }
    };
    vm.runInNewContext(shell, {document, window});
    return {root, media, toggle, label, attrs, select, requests, touched};
}

let page = mount('auto', true);
assert.equal(page.root.dataset.theme, 'light');
assert.equal(page.attrs['aria-pressed'], 'true');
assert.equal(page.attrs['aria-label'], 'Switch to night theme');
assert.equal(page.label.textContent, 'Night theme');

page.media.change(false);
assert.equal(page.root.dataset.theme, 'dark');
assert.equal(page.attrs['aria-pressed'], 'false');
assert.equal(page.attrs.title, 'Switch to day theme');
assert.equal(page.label.textContent, 'Day theme');
page.media.change(true);
assert.equal(page.root.dataset.theme, 'light');

page.toggle.click();
assert.equal(page.root.dataset.themePref, 'night');
assert.equal(page.root.dataset.theme, 'dark');
assert.equal(page.select.value, 'night');
assert.equal(page.requests.length, 1);
assert.equal(page.requests[0].url, '/account/display');
assert.equal(page.requests[0].options.method, 'POST');
assert.equal(page.requests[0].options.headers['Content-Type'], 'application/json');
assert.equal(page.requests[0].options.headers.Accept, 'application/json');
assert.equal(JSON.parse(page.requests[0].options.body).UI_THEME, 'night');
page.media.change(false);
page.media.change(true);
assert.equal(page.root.dataset.theme, 'dark', 'explicit theme must ignore device changes');

page.toggle.click();
assert.equal(page.root.dataset.themePref, 'day');
assert.equal(page.root.dataset.theme, 'light');
assert.equal(JSON.parse(page.requests[1].options.body).UI_THEME, 'day');
assert.equal(page.attrs['aria-pressed'], 'true');
assert.equal(page.attrs['aria-label'], 'Switch to night theme');
assert.equal(page.touched.length, 0, 'legacy browser theme must never be read or written');

page = mount('auto', false, {legacy: true});
assert.ok(page.media.listener, 'older Safari media-query change listener must work');
page.media.change(true);
assert.equal(page.root.dataset.theme, 'light');

page = mount('day', false, {fail: true});
page.toggle.click();
assert.equal(page.root.dataset.theme, 'dark');
assert.equal(page.root.dataset.themePref, 'night');
assert.equal(page.requests.length, 1);
assert.equal(page.touched.length, 0);

Promise.resolve().then(() => console.log('New theme behavior ok'));
