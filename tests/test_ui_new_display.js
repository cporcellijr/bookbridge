const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/next/display.js', 'utf8');
assert.match(fs.readFileSync('static/js/next/cover-loader.js','utf8'), /getElementById\('library-page'\) \|\| document\.getElementById\('display-preview-stage'\)/, 'preview thumbnails must use the shared cover loader');

function mount({fails = false, key = 'UI_LIBRARY_STYLE', choices = ['cards', 'spines', 'faceout', 'list'], applyTheme = null, sizeRow = null, finishRow = null} = {}) {
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
    const stage = {dataset: {view: 'covers', libraryStyle: 'cards', shelfFinish: 'theme'}, querySelectorAll() { return []; }};
    const page = {classList: {add(name) { values.className = name; }}};
    const document = {
        querySelector(selector) {
            if (selector === '.display-page') return page;
            if (selector === '.display-size-row') return sizeRow;
            if (selector === '.display-finish-row') return finishRow;
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

function mountCoverSetting(key, {initial = 'true', fails = false} = {}) {
    const requests = [], listeners = {};
    const stage = {dataset: {libraryStyle: 'cards', view: 'covers'}, querySelectorAll() { return []; }};
    const status = {textContent: '', toggleAttribute() {}, removeAttribute() {}};
    const group = {querySelector() { return status; }};
    const isSize = key === 'UI_COVER_SIZE';
    const checkbox = {checked: initial === 'true', disabled: false, attrs: {},
        addEventListener(name, fn) {listeners[name] = fn;},
        setAttribute(name, value) {this.attrs[name] = value;}};
    const input = {value: isSize ? initial : ''};
    const down = {dataset: {displayStep: '-1'}, value: String(Math.max(1, Number(initial)-1)), disabled: initial === '1'};
    const up = {dataset: {displayStep: '1'}, value: String(Math.min(5, Number(initial)+1)), disabled: initial === '5'};
    const help = {textContent: ''};
    const dots = Array.from({length: 5}, (_, i) => ({dataset: {sizeDot: String(i+1)}, active: false,
        classList: {toggle(name, value) { dots[i].active = value; }}}));
    const form = {
        dataset: {displaySetting: key},
        addEventListener(name, fn) {listeners[name] = fn;},
        querySelector(selector) {
            if (selector === '[role="switch"]') return isSize ? null : checkbox;
            if (selector === 'input[name="UI_COVER_SIZE"]') return isSize ? input : null;
            if (selector === '.display-size-help') return help;
            return null;
        },
        querySelectorAll(selector) {
            if (selector === '[data-display-value]') return [];
            if (selector === '[data-display-step]') return isSize ? [down, up] : [];
            if (selector === '[data-size-dot]') return isSize ? dots : [];
            if (selector === 'button, input[type="checkbox"]') return isSize ? [down, up] : [checkbox];
            return [];
        },
        closest() { return group; },
        contains(control) { return control === down || control === up; }
    };
    const document = {
        querySelector(selector) {
            if (selector === '.display-page') return {classList: {add() {}}};
            if (selector === '[data-display-setting="' + key + '"]') return form;
            return null;
        },
        querySelectorAll(selector) { return selector === '.display-page form[data-display-setting]' ? [form] : []; },
        getElementById(id) { return id === 'display-preview-stage' ? stage : null; },
        addEventListener() {}
    };
    const window = {bbT: value => value, setTimeout: () => 1, clearTimeout() {},
        fetch(url, options) { requests.push({url, options}); return Promise.resolve({ok: !fails}); }};
    vm.runInNewContext(source, {document, window, WeakMap, console});
    function changeToggle(checked) {
        checkbox.checked = checked;
        listeners.change();
    }
    function clickStep(direction) {
        const control = direction < 0 ? down : up;
        let prevented = false;
        listeners.click({target: {closest(selector) {
            return selector === '[data-display-step]' ? control : null;
        }}, preventDefault() { prevented = true; }});
        assert.equal(prevented, true);
    }
    return {changeToggle, clickStep, requests, checkbox, input, down, up, dots, stage, help, status};
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
    const sizeRow = {hidden: false};
    const finishRow = {hidden: true};
    const styles = mount({sizeRow, finishRow});
    assert.equal(finishRow.hidden, true, 'Cards initially hide Shelf finish');
    await styles.click('spines');
    assert.equal(sizeRow.hidden, true, 'spines hide the cover-size row immediately');
    assert.equal(finishRow.hidden, false, 'spines show the finish row immediately');
    await styles.click('faceout');
    assert.equal(sizeRow.hidden, false);
    assert.equal(finishRow.hidden, false, 'faceout shows Shelf finish');
    await styles.click('list');
    assert.equal(sizeRow.hidden, true);
    assert.equal(finishRow.hidden, true, 'list hides Shelf finish');
    await styles.click('cards');
    assert.equal(sizeRow.hidden, false);
    assert.equal(finishRow.hidden, true, 'cards hide Shelf finish');
    const failedStyle = mount({fails: true, sizeRow: {hidden: false}});
    await failedStyle.click('list');
    assert.equal(failedStyle.stage.dataset.libraryStyle, 'cards');
    assert.equal(failedStyle.stage.dataset.view, 'covers');
    const failedFinishRow = {hidden: true};
    const failedStyleFinish = mount({fails: true, finishRow: failedFinishRow});
    await failedStyleFinish.click('spines');
    assert.equal(failedFinishRow.hidden, true, 'failed style save restores finish visibility');
    for (const finish of ['wood', 'dark', 'plain']) {
        const control = mount({key: 'UI_SHELF_FINISH', choices: ['theme', 'wood', 'dark', 'plain']});
        await control.click(finish);
        assert.equal(control.requests.length, 1, 'finish change makes exactly one POST');
        assert.equal(control.requests[0].url, '/account/display');
        assert.equal(JSON.parse(control.requests[0].options.body).UI_SHELF_FINISH, finish);
        assert.equal(control.stage.dataset.shelfFinish, finish, 'preview updates immediately');
        assert.equal(control.buttons.find(button => button.dataset.displayValue === finish).attrs['aria-pressed'], 'true');
        const failed = mount({key: 'UI_SHELF_FINISH', choices: ['theme', 'wood', 'dark', 'plain'], fails: true});
        await failed.click(finish);
        assert.equal(failed.stage.dataset.shelfFinish, 'theme', 'failed finish save rolls back preview');
        assert.equal(failed.buttons[0].attrs['aria-pressed'], 'true');
        assert.equal(failed.attrs['data-error'], true);
    }

    for (const [key, dataset] of [
        ['UI_SHOW_PROGRESS', 'showProgress'], ['UI_SHOW_FINISHED', 'showFinished'],
        ['UI_SHOW_FORMAT_STRIP', 'showFormatStrip'], ['UI_SHOW_RATINGS', 'showRatings'],
        ['UI_SHOW_TITLES', 'showTitles']
    ]) {
        const control = mountCoverSetting(key);
        control.changeToggle(false);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(control.requests.length, 1, key + ' must make one request');
        assert.equal(JSON.parse(control.requests[0].options.body)[key], 'false');
        assert.equal(control.stage.dataset[dataset], 'false', key + ' must update live preview');
        assert.equal(control.checkbox.attrs['aria-checked'], 'false');
        const failing = mountCoverSetting(key, {fails: true});
        failing.changeToggle(false);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(failing.stage.dataset[dataset], 'true', key + ' rollback restores preview');
        assert.equal(failing.checkbox.checked, true);
    }
    const stepper = mountCoverSetting('UI_COVER_SIZE', {initial: '2'});
    stepper.clickStep(-1);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(JSON.parse(stepper.requests[0].options.body).UI_COVER_SIZE, '1');
    assert.equal(stepper.stage.dataset.coverSize, '1');
    assert.equal(stepper.down.disabled, true, 'minus disabled at step 1');
    assert.equal(stepper.up.disabled, false);
    assert.equal(stepper.help.textContent, '4 per row on a phone');
    stepper.clickStep(1);
    await new Promise(resolve => setImmediate(resolve));
    for (let size = 3; size <= 5; size++) {
        stepper.clickStep(1);
        await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(stepper.stage.dataset.coverSize, '5');
    assert.equal(stepper.up.disabled, true, 'plus disabled at step 5');
    assert.equal(stepper.dots.filter(dot => dot.active).length, 5);
    const failedStep = mountCoverSetting('UI_COVER_SIZE', {initial: '2', fails: true});
    failedStep.clickStep(1);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(failedStep.input.value, '2');
    assert.equal(failedStep.stage.dataset.coverSize, '2');
    assert.equal(failedStep.dots.filter(dot => dot.active).length, 2);
    assert.equal(failedStep.down.disabled, false);
    assert.equal(failedStep.up.disabled, false);
    console.log('Display save, feedback, rollback, and preview behavior ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
