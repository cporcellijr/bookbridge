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
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
