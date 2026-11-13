const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/acquisition_jobs.js', 'utf8');
const helperStart = source.indexOf('    function panel()');
const helperEnd = source.indexOf('    function showFeedback(', helperStart);
const refreshStart = source.indexOf('    async function validateBookOrbitEditors');
const refreshEnd = source.indexOf('    async function performProviderAction', refreshStart);
assert.ok(helperStart >= 0 && helperEnd > helperStart, 'panel focus helpers are present');
assert.ok(refreshStart >= 0 && refreshEnd > refreshStart, 'owned refresh functions are present');
const testedSource = source.slice(helperStart, helperEnd) + '\n'
    + source.slice(refreshStart, refreshEnd) + '\nthis.runRefresh = refreshPanel;';

function makeHarness(decisionAction) {
    let editorPresent = true;
    let currentPanel;
    let fragmentRequests = 0;
    const staleFocusedButton = {};
    const receipt = { dataset: { receiptId: 'receipt-1' } };
    const editor = {
        closest(selector) {
            assert.equal(selector, '.acquisition-receipt');
            return receipt;
        },
        remove() { editorPresent = false; },
    };
    const panelNode = {
        dataset: { fragmentUrl: '/queue/acquisition-fragment' },
        querySelector(selector) {
            return (selector.startsWith('.acquisition-bookorbit-editor')
                || selector.startsWith('[data-provider-editor]')) && editorPresent ? editor : null;
        },
        contains(node) { return node === staleFocusedButton; },
        replaceWith(updated) { currentPanel = updated; },
    };
    currentPanel = panelNode;
    const updatedPanel = { id: 'acquisition-jobs', refreshed: true };
    const context = {
        actionActive: false,
        document: {
            hidden: false,
            activeElement: staleFocusedButton,
            getElementById(id) { return id === 'acquisition-jobs' ? currentPanel : null; },
        },
        window: {},
        console,
        jsonFetch: async (url) => {
            assert.equal(url, '/api/acquisition/mine');
            return {
                response: { ok: true },
                data: { requests: [{ id: 'receipt-1', status: 'in_progress', decision_action: decisionAction }] },
            };
        },
        fetch: async (url) => {
            fragmentRequests += 1;
            assert.equal(url, '/queue/acquisition-fragment');
            return { ok: true, text: async () => '<section>fresh status</section>' };
        },
        DOMParser: class {
            parseFromString() { return { getElementById: () => updatedPanel }; }
        },
        refreshProviderOptions: async () => {},
    };
    vm.runInNewContext(testedSource, context);
    return { run: () => context.runRefresh(false), result: () => ({
        editorPresent, fragmentRequests, currentPanel,
    }) };
}


function providerControls({ hidden = false, disabled = false } = {}) {
    const help = { textContent: '', hidden: true };
    const settings = { hidden: true };
    const bookorbit = { hidden: true };
    const shelfFind = { hidden: true };
    const shelfBook = { hidden: true };
    return {
        hidden, disabled, dataset: { mediaKind: 'ebook' },
        help, settings, bookorbit,
        querySelector(selector) {
            return {
                '[data-provider-help]': help,
                '[data-provider-settings]': settings,
                '[data-provider-action="bookorbit-submit"]': bookorbit,
                '[data-provider-action="shelfmark-open"]': shelfFind,
                '[data-provider-action="shelfmark-request-book"]': shelfBook,
            }[selector] || null;
        },
    };
}

// Execute the full unchanged production IIFE, including initial options load,
// the actual 20s timer and fragment replacement. No network/browser is used.
async function makeProviderHarness(initialControls = [], replacementControls = []) {
    const hits = [];
    let panelNode;
    let nextControls = replacementControls;
    let timer;
    let click;
    let optionsFail = false;
    let optionsReject = false;
    let blockPost = false;
    let releasePost;
    let focus = false;
    let editorPresent = false;
    let hidden = false;
    let mineRequests = [];
    const focusNode = {};
    const receipt = { dataset: { receiptId: 'receipt-1' } };
    const editor = {
        closest() { return receipt; },
        remove() { editorPresent = false; },
    };
    function makePanel(controls) {
        return {
            controls,
            dataset: { fragmentUrl: '/job-queue?acquisition_fragment=1' },
            querySelector(selector) {
                if (selector === '[data-provider-controls]') return controls[0] || null;
                if (selector === '.acquisition-bookorbit-editor') return editorPresent ? editor : null;
                if (selector.startsWith('[data-provider-editor]')) return editorPresent ? editor : null;
                return null;
            },
            querySelectorAll() { return []; },
            contains(node) { return focus && node === focusNode; },
            replaceWith(replacement) { panelNode = replacement; },
        };
    }
    panelNode = makePanel(initialControls);
    const document = {
        get hidden() { return hidden; },
        activeElement: focusNode,
        getElementById(id) {
            if (id === 'acquisition-jobs') return panelNode;
            if (id === 'acquisition-job-i18n') return {
                textContent: JSON.stringify({
                    providerOptionsUnavailable: 'Availability unavailable',
                    providerUnavailable: 'No provider',
                    providerAccountDisabled: 'Account disabled',
                }),
            };
            return null;
        },
        querySelector() { return null; },
        querySelectorAll(selector) {
            assert.equal(selector, '[data-provider-controls]');
            return panelNode.controls;
        },
        addEventListener(name, listener) {
            if (name === 'click') click = listener;
        },
    };
    const context = {
        window: {
            setInterval(fn, ms) {
                assert.equal(ms, 20000, 'the 20-second reconciliation timer is unchanged');
                timer = fn;
            },
        },
        document,
        console: { warn() {} },
        DOMParser: class {
            parseFromString() {
                return { getElementById: () => makePanel(nextControls) };
            }
        },
        fetch: async (url, options = {}) => {
            hits.push({ url, method: options.method || 'GET' });
            if (url === '/api/acquisition/mine') return {
                ok: true, json: async () => ({ requests: mineRequests }),
            };
            if (url === '/job-queue?acquisition_fragment=1') return {
                ok: true, text: async () => '<section id="acquisition-jobs"></section>',
            };
            if (url === '/api/acquisition/options') {
                if (optionsReject) throw new Error('offline provider failure');
                return {
                ok: !optionsFail,
                json: async () => optionsFail ? { error: 'unavailable' } : {
                    services: {
                        bookorbit: { usable: true, formats: ['ebook'] },
                        shelfmark: { usable: false, formats: {}, find_formats: [] },
                    },
                },
            };
            }
            if (url === '/api/acquisition/receipt-1/cancel') {
                if (blockPost) return new Promise((resolve) => {
                    releasePost = () => resolve({ ok: true, json: async () => ({}) });
                });
                return { ok: true, json: async () => ({}) };
            }
            throw new Error('Unexpected request ' + url);
        },
    };
    const exportMarker = '    window.setInterval(() => refreshPanel(false), 20000);';
    assert.ok(source.includes(exportMarker));
    vm.runInNewContext(source.replace(exportMarker,
        '    window.__testRefreshPanel = refreshPanel;\n' + exportMarker), context);
    await new Promise((resolve) => setImmediate(resolve));
    return {
        hits,
        tick: () => timer(),
        force: () => context.window.__testRefreshPanel(true),
        setNextControls(controls) { nextControls = controls; },
        setOptionsFailure(value) { optionsFail = value; },
        setOptionsReject(value) { optionsReject = value; },
        blockProviderPost() { blockPost = true; },
        releaseProviderPost() { releasePost(); },
        setFocused(value) { focus = value; },
        setEditor(value) {
            editorPresent = value;
            mineRequests = value ? [{ id: 'receipt-1', status: 'in_progress', decision_action: 'pick_release' }] : [];
        },
        setHidden(value) { hidden = value; },
        get controls() { return panelNode.controls; },
        clickProvider(action) {
            const button = {
                dataset: { providerAction: action, receiptId: 'receipt-1' },
                closest() { return null; },
            };
            return click({ target: { closest(selector) {
                return selector === '[data-provider-action]' ? button : null;
            } } });
        },
        paths() { return hits.filter((row) => row.method === 'GET').map((row) => row.url); },
    };
}

async function verifyProviderRequestGuards() {
    const OPTIONS = '/api/acquisition/options';
    const MINE = '/api/acquisition/mine';
    const FRAGMENT = '/job-queue?acquisition_fragment=1';

    const empty = await makeProviderHarness();
    assert.deepEqual(empty.paths(), [], 'no initial options request without provider controls');
    await empty.tick();
    assert.deepEqual(empty.paths(), [MINE, FRAGMENT], 'reconciliation and fragment continue when empty');
    await empty.tick();
    assert.deepEqual(empty.paths(), [MINE, FRAGMENT, MINE, FRAGMENT],
        'no options request follows a control-free replacement');

    const control = providerControls();
    const present = await makeProviderHarness([control], [providerControls()]);
    assert.deepEqual(present.paths(), [OPTIONS], 'options remain fresh at startup with controls');
    assert.equal(control.bookorbit.hidden, false, 'provider visibility still gets applied');
    await present.tick();
    assert.deepEqual(present.paths(), [OPTIONS, MINE, FRAGMENT, OPTIONS],
        'options refresh after replacement with controls');
    assert.equal(present.controls[0].bookorbit.hidden, false);

    const appearing = await makeProviderHarness();
    appearing.setNextControls([providerControls()]);
    await appearing.tick();
    assert.deepEqual(appearing.paths(), [MINE, FRAGMENT, OPTIONS],
        'controls appearing later trigger a fresh options lookup');
    appearing.setNextControls([]);
    await appearing.tick();
    assert.deepEqual(appearing.paths(), [MINE, FRAGMENT, OPTIONS, MINE, FRAGMENT],
        'controls disappearing stop options lookups');
    appearing.setNextControls([providerControls()]);
    await appearing.tick();
    assert.equal(appearing.paths().filter((path) => path === OPTIONS).length, 2,
        'reappearing controls fetch again, without a stale cache');

    const disabledControl = providerControls({ hidden: true, disabled: true });
    const hiddenControls = await makeProviderHarness([disabledControl], [providerControls({ hidden: true, disabled: true })]);
    assert.deepEqual(hiddenControls.paths(), [OPTIONS], 'hidden/disabled nodes still count');
    await hiddenControls.tick();
    assert.deepEqual(hiddenControls.paths(), [OPTIONS, MINE, FRAGMENT, OPTIONS]);

    const failingControl = providerControls();
    const failures = await makeProviderHarness([failingControl], [providerControls()]);
    failures.setOptionsFailure(true);
    await failures.tick();
    assert.equal(failures.controls[0].help.textContent, 'Availability unavailable',
        'existing options error help remains visible');
    assert.equal(failures.controls[0].settings.hidden, false);
    const throwing = await makeProviderHarness([providerControls()], [providerControls()]);
    throwing.setOptionsReject(true);
    await throwing.tick();
    assert.equal(throwing.controls[0].help.textContent, 'Availability unavailable',
        'network errors retain the existing unavailable message');

    const protectedView = await makeProviderHarness([providerControls()], [providerControls()]);
    protectedView.hits.length = 0;
    protectedView.setHidden(true);
    await protectedView.tick();
    assert.deepEqual(protectedView.paths(), [], 'hidden documents do not poll');
    protectedView.setHidden(false);
    protectedView.setFocused(true);
    await protectedView.tick();
    assert.deepEqual(protectedView.paths(), [MINE], 'focused controls protect fragments');
    protectedView.hits.length = 0;
    protectedView.setFocused(false);
    protectedView.setEditor(true);
    await protectedView.tick();
    assert.deepEqual(protectedView.paths(), [MINE], 'active editors protect fragments');
    protectedView.hits.length = 0;
    await protectedView.force();
    assert.deepEqual(protectedView.paths(), [MINE, FRAGMENT, OPTIONS],
        'forced refresh still replaces and checks options when controls exist');

    const activeAction = await makeProviderHarness();
    activeAction.blockProviderPost();
    const pendingAction = activeAction.clickProvider('cancel-receipt');
    await activeAction.tick();
    assert.deepEqual(activeAction.hits.map((row) => row.url),
        ['/api/acquisition/receipt-1/cancel'], 'active provider action suppresses polling');
    activeAction.releaseProviderPost();
    await pendingAction;
    assert.deepEqual(activeAction.paths(), [MINE, FRAGMENT],
        'completed action still forces reconciliation and a fragment');

    const forcedEmpty = await makeProviderHarness();
    await forcedEmpty.clickProvider('cancel-receipt');
    assert.deepEqual(forcedEmpty.hits.map((row) => row.url), [
        '/api/acquisition/receipt-1/cancel', MINE, FRAGMENT,
    ], 'provider actions still force reconciliation and replacement without controls');

    console.log('provider options conditional requests and polling safety guards ok');
}

(async () => {
    const stale = makeHarness('wait');
    await stale.run();
    assert.equal(stale.result().editorPresent, false, 'the stale release editor is closed');
    assert.equal(stale.result().fragmentRequests, 1,
        'fresh job markup replaces the stale focused release action');
    assert.equal(stale.result().currentPanel.refreshed, true);

    const stillPickable = makeHarness('pick_release');
    await stillPickable.run();
    assert.equal(stillPickable.result().editorPresent, true, 'a current release editor stays open');
    assert.equal(stillPickable.result().fragmentRequests, 0,
        'normal focused editor state still protects the user choice');
    console.log('stale BookOrbit picker refresh and focused editor preservation ok');
    await verifyProviderRequestGuards();
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
