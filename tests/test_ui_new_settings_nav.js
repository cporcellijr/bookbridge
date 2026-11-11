// Runs static/js/next/settings-p6.js against a tiny fake DOM.
// P14 #3: a search hid sections on every page; choosing a page afterwards left it empty.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/next/settings-p6.js'), 'utf8');
const classList = initial => {
    const set = new Set(initial || []);
    return {
        contains: c => set.has(c), add: c => set.add(c), remove: c => set.delete(c),
        toggle: (c, on) => (on === undefined ? !set.has(c) : on) ? set.add(c) : set.delete(c),
    };
};
const syncSection = { style: { display: 'none' }, dataset: {} };
const serviceSection = { style: { display: 'none' }, dataset: { servicePage: 'audiobookshelf' } };
const panels = ['integrations', 'sync'].map(panel => ({ dataset: { panel }, style: { display: 'none' }, classList: classList() }));
const main = { classList: classList(['filtering']) };
const search = { value: 'supabase' };
const changed = { checked: false };
const elements = { settings_search: search, changed_only: changed, settingsForm: { style: {} }, return_tab: { value: '' } };
const lists = {
    '.settings-tab': [],
    '.tab-panel': panels,
    '.settings-main .settings-section': [syncSection, serviceSection],
    '.settings-section[data-service-page]': [serviceSection],
};
const context = vm.createContext({
    window: {},
    location: { hash: '' },
    history: { replaceState() {} },
    document: {
        querySelector: selector => ({ '.settings-main': main, '.submit-bar': { style: {} } })[selector] || null,
        getElementById: id => elements[id] || null,
        querySelectorAll: selector => lists[selector] || [],
        addEventListener() {},
    },
});
vm.runInContext(source, context);

context.window.showSettingsTab('sync');
assert.equal(main.classList.contains('filtering'), false);
assert.equal(search.value, '');
assert.equal(syncSection.style.display, '', 'a section hidden by the search must show on its page again');
assert.equal(panels[1].classList.contains('active'), true);
assert.equal(serviceSection.style.display, 'none', 'service sections still show only on their own page');
