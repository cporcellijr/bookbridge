// Group series on Spines, Face-out and List: each series sits together, in reading order,
// where its hidden stack sorted. Runs the real functions from the Library script.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('            function flatLibraryView() {');
const end = source.indexOf('            function sortBooks(sortBy, direction) {', start);
assert(start > 0 && end > start, 'flat-view series helpers are required');

const grid = {children: []};
const byId = {};
function node(name, data = {}, classes = ['book-card']) {
    const element = {
        name, dataset: data, classes, parentNode: grid,
        matches(selector) { return selector === '.series-group' && classes.includes('series-group'); },
        closest(selector) { return this.matches(selector) ? this : (this.group || null); },
        after(...nodes) {
            nodes.forEach(item => grid.children.splice(grid.children.indexOf(item), 1));
            grid.children.splice(grid.children.indexOf(this) + 1, 0, ...nodes);
        },
    };
    return element;
}
// Stack A (sorted second) has a lead card (#449) plus children; stack B has children only.
const groupA = node('A', {}, ['series-group']); byId['group-a'] = groupA;
const groupB = node('B', {}, ['series-group']); byId['group-b'] = groupB;
const childrenA = {group: groupA, closest() { return groupA; }}; byId['children-a'] = childrenA;
const childrenB = {group: groupB, closest() { return groupB; }}; byId['children-b'] = childrenB;
const a3 = node('a3', {seriesContainerId: 'group-a', seriesSequence: '3', seriesLead: '1'});
const a1 = node('a1', {seriesContainerId: 'children-a', seriesSequence: '1', seriesOrder: '0'});
const a2 = node('a2', {seriesContainerId: 'children-a', seriesSequence: '2', seriesOrder: '1'});
const aX = node('aX', {seriesContainerId: 'children-a', seriesSequence: '', seriesOrder: '2'});
const b2 = node('b2', {seriesContainerId: 'children-b', seriesSequence: '2', seriesOrder: '1'});
const b1 = node('b1', {seriesContainerId: 'children-b', seriesSequence: '1', seriesOrder: '0'});
const solo = node('solo');
// What a sort leaves behind: series books scattered by their own keys, stacks sorted as units.
grid.children.push(a2, groupB, solo, b2, a3, groupA, aX, b1, a1);
grid.querySelectorAll = selector => {
    assert.equal(selector, ':scope > .book-card[data-series-container-id]');
    return grid.children.filter(item => item.classes.includes('book-card') && item.dataset.seriesContainerId);
};
grid.closest = selector => selector === '.library-page:not([data-view="covers"])' ? {} : null;

const context = {libraryGrid: grid, document: {getElementById: id => byId[id] || null}, Number, parseFloat, Map, Infinity};
vm.runInNewContext(source.slice(start, end) + '\nthis.flat = flatLibraryView(); gatherSeries();', context);
assert.equal(context.flat, true);
assert.deepEqual(grid.children.map(item => item.name),
    ['B', 'b1', 'b2', 'solo', 'A', 'a1', 'a2', 'a3', 'aX'],
    'each series follows its stack in reading order; unnumbered books go last');
console.log('Flat-view series grouping ok');
