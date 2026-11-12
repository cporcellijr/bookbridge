const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const mutations = {
  status: ["return rank === ({in_progress: 0, not_started: 1, finished: 2})[statusValue];", "return false;"],
  group: ["groupedStatus.set(card, matches)", "groupedStatus.set(card, false)"],
  exclusive: ["control.value = key === group ? value : 'all';", "control.value = key === group ? value : control.value;"],
  label: ["if (choice) label += ' · ' + facetOptionBaseLabel(choice);", "if (choice) label += '';"],
  search: ["if (query && option.value !== 'all' && !label.toLocaleLowerCase().includes(query)) return;", "if (false) return;"],
  restore: ["if (candidate.series === 'standalone') candidate.series = 'none';", "if (candidate.series === 'standalone') candidate.series = 'all';"],
  storage: ["status: statusValue", "status: 'all'"],
  attention: ["attention: attentionOnlyFilter ? attentionOnlyFilter.checked : savedAttentionPreference,", "attention: !!attentionOnlyFilter?.checked,"],
  focus: ["filterButton?.focus();", "void filterButton;"],
};
if (process.env.P16B_MUTATE) {
  const mutation = mutations[process.env.P16B_MUTATE];
  assert(mutation, 'Unknown mutation');
  assert(source.includes(mutation[0]), 'Mutation target missing: ' + process.env.P16B_MUTATE);
  source = source.replace(mutation[0], mutation[1]);
}
function extract(first, last) {
  const a = source.indexOf(first), b = source.indexOf(last, a);
  assert(a >= 0 && b > a, 'Missing live code: ' + first);
  return source.slice(a, b);
}
function scope(script, environment) {
  vm.runInNewContext(script, environment);
  return environment;
}
function select(values, value = 'all') {
  return {value, options: values.map(([value, text]) => ({value, textContent: text, hidden: false, disabled: false}))};
}
const choices = {
  status: select([['all','All statuses'],['in_progress','In progress'],['not_started','Not started'],['finished','Finished']]),
  format: select([['all','All formats'],['paired','Listening and reading'],['has_audio','Has Audio'],['audiobook_only','Audio only'],['ebook_only','Ebook only']]),
  author: select([['all','All authors'],['none','No author'],['author:Ada','Ada'],['author:Ben','Ben']]),
  series: select([['all','All series'],['none','No series'],['series:One','One'],['series:Two','Two']]),
};
const predicates = extract('function needsAttention(card)', 'function filterBooks()');
const statusSort = extract('function readingSortDetails(el)', '// Returns the comparator');
function book(title, progress, seriesName = '', author = 'Ada', syncMode = 'audiobook') {
  const classes = new Set();
  return {
    dataset: {title, progress: String(progress), seriesName, author, syncMode, filename: '', status: 'active'},
    style: {},
    classList: {contains: key => classes.has(key), add: key => classes.add(key), remove: key => classes.delete(key)},
    querySelectorAll: () => [],
  };
}
const members = [book('Reading', 35, 'One'), book('Unstarted A', 0, 'One'), book('Unstarted B', 0, 'One')];
const flat = [book('Flat reading', 60), book('Flat unstarted', 0), book('Flat finished', 100)];
const group = {
  dataset: {seriesName: 'One', author: 'Ada'},
  style: {},
  classList: (() => {const a = new Set(['series-group']); return {contains: k => a.has(k), add: k => a.add(k), remove: k => a.delete(k)};})(),
  querySelectorAll: () => members,
};
const cards = [...members, ...flat];
const savedWrites = [];
let groupingOff = false;
const document = {
  body: {classList: {contains: k => k === 'series-grouping-off' && groupingOff}},
  querySelectorAll(query) {
    if (query === '.book-card') return cards;
    if (query === '.series-group') return [group];
    throw new Error('Unexpected query ' + query);
  },
};
const filtering = scope(predicates + statusSort +
  extract('function filterBooks()', 'window.toggleSeries ='), {
  document, getLastSyncAge: () => 0,
  filterSelect: {value: 'all'}, statusFilter: {value: 'all'},
  authorFilter: {value: 'all'}, seriesFilter: {value: 'all'},
  attentionOnlyFilter: {checked: false}, dashboardSearch: {value: ''},
  updateFacetOptions() {}, renderFilterOptions() {}, updateFilterButtonLabel() {},
  updateFilteredCount() {}, updateSearchHandoff() {},
  filterInitialized: true,
  localStorage: {setItem: (key, value) => savedWrites.push([key, JSON.parse(value)])},
  console,
});
function hidden(card) {return card.classList.contains('hidden');}
filtering.statusFilter.value = 'in_progress'; filtering.filterBooks();
assert.equal(hidden(group), false, 'An in-progress member shows the series group');
assert(members.every(member => !hidden(member)), 'Status filter cannot hide any member of a matching group');
assert.deepEqual(flat.map(hidden), [false, true, true], 'Flat cards use their own status');
filtering.statusFilter.value = 'not_started'; filtering.filterBooks();
assert.equal(hidden(group), true, 'A mixed series must not appear under Not started');
assert(members.every(hidden), 'A non-matching group is hidden in its entirety');
assert.deepEqual(flat.map(hidden), [true, false, true]);
filtering.statusFilter.value = 'finished'; filtering.filterBooks();
assert.equal(hidden(group), true);
assert.deepEqual(flat.map(hidden), [true, true, false]);
filtering.statusFilter.value = 'all'; filtering.filterBooks();
assert.equal(hidden(group), false);
assert(cards.every(member => !hidden(member)));
groupingOff = true;
filtering.statusFilter.value = 'in_progress'; filtering.filterBooks();
assert.deepEqual(members.map(hidden), [false, true, true], 'Flat series members each use their own progress');
groupingOff = false;
filtering.statusFilter.value = 'all';
filtering.filterSelect.value = 'ebook_only'; filtering.filterBooks();
assert(cards.every(hidden), 'Format filter still operates on all the individual cards');
filtering.filterSelect.value = 'all';
filtering.authorFilter.value = 'none'; filtering.filterBooks();
assert(cards.every(hidden), 'No author still uses its existing matching rule');
filtering.authorFilter.value = 'all';
filtering.seriesFilter.value = 'none'; filtering.filterBooks();
assert(members.every(hidden) && flat.every(member => !hidden(member)), 'No series keeps standalone books');
filtering.seriesFilter.value = 'all'; filtering.filterBooks();
assert.equal(savedWrites.at(-1)[0], 'abs_kosync_filters');
assert.deepEqual(Object.keys(savedWrites.at(-1)[1]).sort(), ['attention','author','format','series','status']);
const savedShape = savedWrites.at(-1)[1];
assert.equal(savedShape.status, 'in_progress' === filtering.statusFilter.value ? 'in_progress' : 'all');
filtering.statusFilter.value = 'not_started';
filtering.filterBooks();
assert.equal(savedWrites.at(-1)[1].status, 'not_started', 'status persists in the Classic-compatible key');
filtering.attentionOnlyFilter = null;
filtering.savedAttentionPreference = true;
filtering.filterBooks();
assert.equal(savedWrites.at(-1)[1].attention, true, 'Missing attention checkbox cannot erase a migrated preference');
filtering.attentionOnlyFilter = {checked: false};
const closures = [];
const reset = scope(extract('function chooseLibraryFilter(', 'function renderFilterOptions()'), {
  filterGroups: choices, filterBooks() {}, closeFilterPanel() {closures.push(true);},
});
reset.chooseLibraryFilter('author', 'author:Ada');
assert.deepEqual(Object.entries(choices).map(([key,s]) => [key,s.value]),
  [['status','all'],['format','all'],['author','author:Ada'],['series','all']],
  'An author choice resets all other facets');
reset.chooseLibraryFilter('status', 'in_progress');
assert.deepEqual(Object.entries(choices).map(([key,s]) => s.value),
  ['in_progress','all','all','all'], 'Status resets author');
reset.chooseLibraryFilter('format', 'all');
assert(Object.values(choices).every(s => s.value === 'all'), 'Choosing All clears every group');
assert.equal(closures.length, 3, 'An option closes the panel');


const label = {textContent: ''};
const labelCode = extract('function updateFilterButtonLabel()', 'function closeFilterPanel()')
  .replace(/\{\{\s*_\('Filter'\)\|tojson\s*\}\}/, JSON.stringify('Filter'));
const labels = scope(labelCode, {
  filterButtonLabel: label, filterGroups: choices,
  facetOptionBaseLabel: option => option.textContent,
});
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter');
choices.format.value = 'audiobook_only';
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · Audio only');
choices.format.value = 'all';
choices.author.value = 'author:Ada';
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · Ada');
choices.author.value = 'all';
choices.status.value = 'in_progress';
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · In progress');
choices.status.value = 'all';

const holders = {};
const searches = {author: {value: ''}, series: {value: ''}};
for (const group of Object.keys(choices)) holders[group] = {
  children: [], replaceChildren() {this.children = [];},
  appendChild(button) {this.children.push(button);},
};
const rendering = scope(extract('function renderFilterOptions()', 'function isValidLibraryFilter('), {
  filterPanel: {},
  filterGroups: choices,
  document: {
    getElementById(id) {
      if (id.endsWith('-options')) return holders[id.slice(7, -8)];
      if (id.endsWith('-search')) return searches[id.slice(7, -7)] || null;
      return null;
    },
    createElement() {return {setAttribute(k,v) {this[k]=v;}, addEventListener() {}};},
  },
  facetOptionBaseLabel: option => option.textContent,
  chooseLibraryFilter() {},
});
rendering.renderFilterOptions();
assert.deepEqual(holders.author.children.map(b => b.textContent),
  ['All authors', 'No author', 'Ada', 'Ben']);
searches.author.value = 'aD';
rendering.renderFilterOptions();
assert.deepEqual(holders.author.children.map(b => b.textContent), ['All authors', 'Ada'],
  'Typing narrows the author list without closing the panel');
searches.author.value = '';
searches.series.value = 'two';
rendering.renderFilterOptions();
assert.deepEqual(holders.series.children.map(b => b.textContent), ['All series', 'Two'],
  'Series search uses its own text box');
searches.series.value = '';
rendering.renderFilterOptions();
assert.equal(holders.author.children.length, 4, 'Clearing author search restores the options');
assert.equal(holders.series.children.length, 4, 'Clearing series search restores the options');
choices.author.options[3].hidden = true;
rendering.renderFilterOptions();
assert(!holders.author.children.some(button => button.textContent === 'Ben'),
  'A zero-book option is hidden');

const migrations = scope(extract('function isValidLibraryFilter(', 'if (filterButton) {'), {
  filterGroups: choices,
});
function normalized(saved = null, legacy = null) {
  const actual = migrations.normalizeSavedLibraryFilters(saved, legacy);
  return JSON.parse(JSON.stringify(actual));
}
assert.deepEqual(normalized(null), {status:'all',format:'all',author:'all',series:'all',attention:false});
assert.equal(normalized(null, 'audiobook').format, 'has_audio', 'Legacy scalar audiobook means has_audio');
assert.equal(normalized({format:'needs_attention'}).attention, true, 'Old attention format turns on the independent checkbox');
assert.equal(normalized({format:'needs_attention'}).format, 'all');
assert.equal(normalized({series:'standalone'}).series, 'none', 'Old standalone maps to No series');
assert.equal(normalized({series:'in_series'}).series, 'all', 'Old in_series widens to All');
assert.equal(normalized({author:'author:Missing'}).author, 'all', 'Missing remembered author defaults to All');
assert.equal(normalized({series:'series:Missing'}).series, 'all', 'Missing remembered series defaults to All');
assert.equal(normalized({format:'invalid'}).format, 'all', 'Missing format resets');
assert.equal(normalized({status:'invalid'}).status, 'all', 'Invalid status resets');
assert.deepEqual(normalized({status:'in_progress',format:'paired',author:'author:Ada',series:'series:One',attention:true}),
 {status:'in_progress',format:'all',author:'all',series:'all',attention:true},
 'Several saved facets resolve in Status, Format, Author, Series priority');
assert.equal(normalized({format:'paired',author:'author:Ada'}).format, 'paired', 'Format wins over Author');
assert.equal(normalized({author:'author:Ada',series:'series:One'}).author, 'author:Ada', 'Author wins over Series');
assert.equal(normalized({author:'none'}).author, 'none', 'No author restores');
assert.equal(normalized({series:'none'}).series, 'none', 'No series restores');

const panel = {hidden: true, querySelector: () => ({focus() {}})};
const backdrop = {hidden: true};
const trigger = {attrs: {}, focusCount: 0, setAttribute(k,v) {this.attrs[k]=v;}, focus() {this.focusCount++;}};
const panelContext = scope(extract('function closeFilterPanel()', 'function chooseLibraryFilter('), {
  filterPanel: panel, filterBackdrop: backdrop, filterButton: trigger,
});
panelContext.openFilterPanel();
assert.equal(panel.hidden, false);
assert.equal(backdrop.hidden, false);
assert.equal(trigger.attrs['aria-expanded'], 'true');
panelContext.closeFilterPanel();
assert.equal(panel.hidden, true);
assert.equal(backdrop.hidden, true);
assert.equal(trigger.attrs['aria-expanded'], 'false');
assert.equal(trigger.focusCount, 1, 'Closing returns focus to the trigger');
assert.match(source, /event.key === 'Escape'[\s\S]*?closeFilterPanel\(\)/);
assert.match(source, /!filterHost.contains\(event.target\)[\s\S]*?closeFilterPanel\(\)/);
console.log('Library P16b status, grouping, selection, search, restore, label and popover regressions ok');
