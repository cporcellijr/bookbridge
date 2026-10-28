const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../templates/settings.html'), 'utf8');
const slice = (from, to) => {
    const start = html.indexOf(from);
    const end = html.indexOf(to, start);
    assert.ok(start >= 0 && end > start, from);
    return html.slice(start, end);
};
const tabs = slice('const SETTINGS_TABS', '// Settings captured at startup');
const showTab = slice('function showSettingsTab(', '// --- Per-field markers');
const filter = slice('// Search and the changed-only filter', "document.addEventListener('DOMContentLoaded'");

const classList = () => {
    const names = new Set();
    return {
        names,
        toggle: (name, on) => (on ? names.add(name) : names.delete(name)),
        contains: name => names.has(name),
        remove: name => names.delete(name),
    };
};
const bar = { style: {} };
const form = { style: {} };
const search = { value: '' };
const changedOnly = { checked: false };
const main = { classList: classList() };
const body = { classList: classList(), textContent: 'Sync every (minutes)' };
body.classList.names.add('collapsed');
const card = {
    dataset: {},
    textContent: 'When BookBridge syncs Sync every (minutes)',
    style: {},
    classList: classList(),
    querySelectorAll: () => [],
    querySelector: sel => (sel.includes('.collapsed') && body.classList.contains('collapsed') ? body : null),
};
const byId = { settingsForm: form, return_tab: { value: '' }, settings_search: search, changed_only: changedOnly };
const context = vm.createContext({
    document: {
        getElementById: id => byId[id] || null,
        querySelector: sel => (sel === '.submit-bar' ? bar : sel === '.settings-main' ? main : null),
        querySelectorAll: sel => (sel === '.settings-main .settings-section' ? [card] : []),
    },
    history: { replaceState() {} },
    location: { hash: '#maintenance' },
});
vm.runInContext(tabs + showTab + filter, context);

context.showSettingsTab('maintenance');
assert.equal(bar.style.display, 'none', 'Maintenance hides Save');

search.value = 'Sync every';
context.applySettingsFilter();
assert.equal(card.style.display, '', 'the matching setting is shown');
assert.equal(bar.style.display, '', 'Save comes back for a search started from Maintenance');
assert.ok(!body.classList.contains('collapsed'), 'a collapsed card opens to show the match');

search.value = '';
context.applySettingsFilter();
assert.equal(bar.style.display, 'none', 'clearing the search returns to Maintenance');
