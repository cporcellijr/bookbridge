const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const template = fs.readFileSync('templates/next/index.html', 'utf8');
assert.match(template, /'faceout':\s*'spines'/, 'faceout must render on the Spines shelf path');
const start = source.indexOf('    if (libraryPage) {');
const end = source.indexOf('    function selectedBookIds()', start);
assert(start > 0 && end > start, 'server-rendered view initialization is required');
const script = source.slice(start, end);
assert(!source.includes('abs_kosync_view'), 'legacy browser view storage must not be used');
for (const [style, view] of [['cards', 'covers'], ['spines', 'spines'], ['list', 'list'], ['faceout', 'spines']]) {
    const events = [];
    const selectModeButton = {hidden: false};
    const seriesGroupingToggle = {checked: true, disabled: false};
    const libraryPage = {dataset: {view, libraryStyle: style}};
    const context = {
        libraryPage, selectModeButton, seriesGroupingToggle,
        setBookSelectMode(value) { events.push(['select', value]); },
        paintSpines(value) { events.push(['paint', value]); },
        applySeriesGrouping(value) { events.push(['group', value]); }
    };
    vm.runInNewContext(script, context);
    assert.equal(libraryPage.dataset.view, view);
    assert.equal(libraryPage.dataset.libraryStyle, style);
    assert.equal(selectModeButton.hidden, view === 'spines');
    assert.equal(seriesGroupingToggle.disabled, view !== 'covers');
    assert.deepEqual(events.map(event => [...event]), [
        ...(view === 'spines' ? [['select', false]] : []),
        ['group', view === 'covers'], ['paint', view === 'spines']
    ]);
}
