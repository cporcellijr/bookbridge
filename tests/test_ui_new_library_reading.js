const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const template = fs.readFileSync('templates/next/index.html', 'utf8');
assert.match(template, /id="library-grid"/);
assert.doesNotMatch(template, /dashboard-section|not-started-header|in-progress-grid|finished-grid/);

function extract(begin, end) {
    const start = source.indexOf(begin);
    const stop = source.indexOf(end, start);
    assert(start >= 0 && stop > start, 'Expected live function block: ' + begin);
    return source.slice(start, stop);
}
const sortFns = extract('function parseLastSync(', 'function sortCards(');
const vmContext = { Date: { now: () => 1800000000000 } };
function comparator(direction = 'asc') {
    return vm.runInNewContext(sortFns + '; makeSortComparator("reading", ' +
        JSON.stringify(direction) + ')', vmContext);
}
function card(name, progress, sync, children = null) {
    const group = children !== null;
    return {
        dataset: { title: name, seriesName: group ? name : '',
                   progress: String(progress), lastSyncUnix: String(sync) },
        classList: { contains: cls => cls === (group ? 'series-group' : 'book-card') },
        querySelectorAll: () => children || [],
        style: {}
    };
}

const ongoingOld = card('In progress old', 30, 1700000000);
const ongoingNew = card('In progress new', 50, 1700500000);
const notStarted = card('Not started', 0, 1700999999);
const finished = card('Finished', 100, 1700999999);
const books = [finished, ongoingOld, notStarted, ongoingNew];
assert.deepEqual([...books].sort(comparator()), [ongoingNew, ongoingOld, notStarted, finished],
    'Reading status sorts progress buckets before sync recency');
assert.deepEqual([...books].sort(comparator('desc')), [finished, notStarted, ongoingOld, ongoingNew],
    'Reverse button reverses the complete reading order');

const group = card('Mixed series', 0, 1600000000, [
    card('Finished volume', 100, 1700000000),
    card('Currently reading volume', 20, 1700800000),
    card('Unstarted volume', 0, 1700100000),
]);
assert.ok(comparator()(group, notStarted) < 0,
    'One in-progress child puts the whole series in progress');
assert.ok(comparator()(group, ongoingNew) < 0,
    'Series uses newest child sync, not its lead or group timestamp');
const completedSeries = card('Completed series', 100, 1700999999, [
    card('Done 1', 100, 1700000000), card('Done 2', 100, 1700500000)
]);
assert.ok(comparator()(notStarted, completedSeries) < 0,
    'Not started comes before a completely finished series');

// Evaluate the actual saved-sort preference block, not a reimplementation.
const preferences = extract('let sortState = {', '// Parse "Time Ago"');
function restored(store) {
    const context = { sortSelect: {value: ''}, localStorage: {
        getItem(key) { return Object.hasOwn(store, key) ? store[key] : null; }
    }, console };
    return vm.runInNewContext(preferences +
        '; ({savedSort, selected: sortSelect.value, direction: sortState.reading, oldDirection: sortState.status})', context);
}
let saved = restored({});
assert.equal(saved.savedSort, 'reading');
assert.equal(saved.selected, 'reading');
assert.equal(saved.direction, 'asc');
saved = restored({
    abs_kosync_sort: 'status',
    abs_kosync_sort_state: JSON.stringify({status:'desc'})
});
assert.equal(saved.savedSort, 'reading');
assert.equal(saved.selected, 'reading');
assert.equal(saved.direction, 'desc', 'Legacy saved direction is retained');
assert.equal(saved.oldDirection, undefined);

const visibleFn = extract('function hasVisibleGridItems()', 'function distinctBookCards()');
function hasVisible(children, groupingOff = false) {
    const ctx = {
        libraryGrid: { children },
        document: { body: { classList: { contains: cls => cls === 'series-grouping-off' && groupingOff } } },
    };
    return vm.runInNewContext(visibleFn + '; hasVisibleGridItems()', ctx);
}
const visible = card('Visible', 0, 0);
const hidden = card('Hidden', 0, 0);
hidden.classList.contains = cls => cls === 'hidden' || cls === 'book-card';
assert.equal(hasVisible([hidden]), false, 'Only hidden top-level entries means no results');
assert.equal(hasVisible([hidden, visible]), true, 'A visible book suppresses no-results');
assert.equal(hasVisible([group], true), false, 'A grouped series is not visible with grouping disabled');
assert.equal(hasVisible([group], false), true, 'A grouped series is visible with grouping enabled');
assert.match(source, /const noResults = !hasVisibleGridItems\(\)/);
