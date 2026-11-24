const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8').replaceAll(String.fromCharCode(13, 10), String.fromCharCode(10));
const mutations = {
  status: ["return rank === ({in_progress: 0, not_started: 1, finished: 2})[statusValue];", "return false;"],
  group: ["groupedStatus.set(card, matches)", "groupedStatus.set(card, false)"],
  toggle: ["picked.includes(value) ? picked.filter(item => item !== value) : [...picked, value]", "[value]"],
  label: ["else if (picked.length > 1) label += ' · ' + picked.length;", "else if (picked.length > 1) label += '';"],
  search: ["if (query && option.value !== 'all' && !label.toLocaleLowerCase().includes(query)) return;", "if (false) return;"],
  restore: ["if (candidate.series === 'standalone') candidate.series = 'none';", "if (candidate.series === 'standalone') candidate.series = 'all';"],
  storage: ["status: statusValues", "status: []"],
  anyValue: ["return !values.length || values.some(value => matches(entry, value));", "return !values.length || matches(entry, values[0]);"],
  keepOpen: ["Array.from(holder?.children || []).find(button => button.dataset.value === value)?.focus();", "closeFilterPanel();"],
  attention: ["attention: attentionOnlyFilter ? attentionOnlyFilter.checked : savedAttentionPreference,", "attention: !!attentionOnlyFilter?.checked,"],
  focus: ["if (restoreFocus) filterButton?.focus();", "void filterButton;"],
  outsideFocus: ["closeFilterPanel(false);", "closeFilterPanel();"],
  clearFacet: ["setGroupValues(select, []);", "void select;"],
  clearSearch: ["if (dashboardSearch) dashboardSearch.value = '';", "if (dashboardSearch) dashboardSearch.value = dashboardSearch.value;"],
  clearAttention: ["if (attentionOnlyFilter) attentionOnlyFilter.checked = false;", "if (attentionOnlyFilter) attentionOnlyFilter.checked = true;"],
  clearSavedAttention: ["savedAttentionPreference = false;" + String.fromCharCode(10) + "                for (const group", "savedAttentionPreference = true;" + String.fromCharCode(10) + "                for (const group"],
  clearAuthorQuery: ["for (const group of ['author', 'series']) {", "for (const group of ['series']) {"],
  clearSeriesQuery: ["for (const group of ['author', 'series']) {", "for (const group of ['author']) {"],
  clearRerun: ["filterBooks();" + String.fromCharCode(10) + "                positionLibraryFilterPopover();", "void filterBooks;" + String.fromCharCode(10) + "                positionLibraryFilterPopover();"],
  clearFocus: ["if (focusTrigger) filterButton?.focus();", "void focusTrigger;"],
  popoverReposition: ["positionLibraryFilterPopover();" + String.fromCharCode(10) + "                if (focusTrigger)", "void positionLibraryFilterPopover;" + String.fromCharCode(10) + "                if (focusTrigger)"],
  activeFacet: ["Object.values(filterGroups).some(select => groupValues(select).length > 0)", "false"],
  activeSearch: ["|| !!dashboardSearch?.value.trim()" + String.fromCharCode(10) + "                    || (attentionOnlyFilter", "|| false" + String.fromCharCode(10) + "                    || (attentionOnlyFilter"],
  activeAttention: ["(attentionOnlyFilter?.checked === true);", "false;"],
  disableClear: ["if (filterClearButton) filterClearButton.disabled = !active;", "if (filterClearButton) filterClearButton.disabled = true;"],
  hideReset: ["if (filterResetButton) filterResetButton.hidden = !active;", "if (filterResetButton) filterResetButton.hidden = true;"],
  preserveSort: ["if (dashboardSearch) dashboardSearch.value = '';", "if (dashboardSearch) dashboardSearch.value = ''; sortSelect.value = 'title';"],
  preserveGrouping: ["savedAttentionPreference = false;" + String.fromCharCode(10) + "                for (const group", "savedAttentionPreference = false; seriesGroupingToggle.checked = false;" + String.fromCharCode(10) + "                for (const group"],
};
const mutationKey = process.env.P16E_MUTATE || process.env.P16B_MUTATE;
if (mutationKey) {
  const mutation = mutations[mutationKey];
  assert(mutation, 'Unknown mutation');
  assert.equal(source.split(mutation[0]).length - 1, 1, 'Mutation target must exist exactly once: ' + mutationKey);
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
function select(values, picked = []) {
  return {options: values.map(([value, text]) => ({value, textContent: text, hidden: false, disabled: false, selected: picked.includes(value)}))};
}
// Test-side view of a hidden multi-select: read its picks, or set them.
function picks(sel) {return sel.options.filter(o => o.selected && o.value !== 'all').map(o => o.value);}
function pick(sel, values) {sel.options.forEach(o => {o.selected = values.includes(o.value);});}
const choices = {
  status: select([['all','All statuses'],['in_progress','In progress'],['not_started','Not started'],['finished','Finished']]),
  format: select([['all','All formats'],['paired','Listening and reading'],['has_audio','Has Audio'],['audiobook_only','Audio only'],['ebook_only','Ebook only']]),
  author: select([['all','All authors'],['none','No author'],['author:Ada','Ada'],['author:Ben','Ben']]),
  series: select([['all','All series'],['none','No series'],['series:One','One'],['series:Two','Two']]),
};
const predicates = extract('function needsAttention(card)', 'function filterBooks()') + extract('function groupValues(', 'function passesLibraryFilters(');
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
const facetSearchFields = {author: {value: ""}, series: {value: ""}};
const savedWrites = [];
let groupingOff = false;
const document = {
  body: {classList: {contains: k => k === 'series-grouping-off' && groupingOff}},
  getElementById(id) {
    return facetSearchFields[id.replace(/^filter-/, '').replace(/-search$/, '')] || null;
  },
  querySelectorAll(query) {
    if (query === '.book-card') return cards;
    if (query === '.series-group') return [group];
    throw new Error('Unexpected query ' + query);
  },
};
const filtering = scope(predicates + statusSort +
  extract('function filterBooks()', 'window.toggleSeries ='), {
  document, getLastSyncAge: () => 0,
  filterSelect: select(choices.format.options.map(o => [o.value, o.textContent])),
  statusFilter: select(choices.status.options.map(o => [o.value, o.textContent])),
  authorFilter: select(choices.author.options.map(o => [o.value, o.textContent])),
  seriesFilter: select(choices.series.options.map(o => [o.value, o.textContent])),
  attentionOnlyFilter: {checked: false}, dashboardSearch: {value: ''},
  updateFacetOptions() {}, renderFilterOptions() {}, updateFilterButtonLabel() {},
  updateFilteredCount() {}, updateSearchHandoff() {}, placeShelfProps() {},
  filterInitialized: true,
  localStorage: {setItem: (key, value) => savedWrites.push([key, JSON.parse(value)])},
  console,
});
function hidden(card) {return card.classList.contains('hidden');}
pick(filtering.statusFilter, ['in_progress']); filtering.filterBooks();
assert.equal(hidden(group), false, 'An in-progress member shows the series group');
assert(members.every(member => !hidden(member)), 'Status filter cannot hide any member of a matching group');
assert.deepEqual(flat.map(hidden), [false, true, true], 'Flat cards use their own status');
pick(filtering.statusFilter, ['not_started']); filtering.filterBooks();
assert.equal(hidden(group), true, 'A mixed series must not appear under Not started');
assert(members.every(hidden), 'A non-matching group is hidden in its entirety');
assert.deepEqual(flat.map(hidden), [true, false, true]);
pick(filtering.statusFilter, ['finished']); filtering.filterBooks();
assert.equal(hidden(group), true);
assert.deepEqual(flat.map(hidden), [true, true, false]);
pick(filtering.statusFilter, []); filtering.filterBooks();
assert.equal(hidden(group), false);
assert(cards.every(member => !hidden(member)));
groupingOff = true;
pick(filtering.statusFilter, ['in_progress']); filtering.filterBooks();
assert.deepEqual(members.map(hidden), [false, true, true], 'Flat series members each use their own progress');
groupingOff = false;
pick(filtering.statusFilter, []);
pick(filtering.filterSelect, ['ebook_only']); filtering.filterBooks();
assert(cards.every(hidden), 'Format filter still operates on all the individual cards');
pick(filtering.filterSelect, []);
pick(filtering.authorFilter, ['none']); filtering.filterBooks();
assert(cards.every(hidden), 'No author still uses its existing matching rule');
pick(filtering.authorFilter, []);
pick(filtering.seriesFilter, ['none']); filtering.filterBooks();
assert(members.every(hidden) && flat.every(member => !hidden(member)), 'No series keeps standalone books');
pick(filtering.seriesFilter, []); filtering.filterBooks();
// Several picks in one group match any of them; groups combine with AND.
pick(filtering.statusFilter, ['in_progress', 'finished']); groupingOff = true; filtering.filterBooks();
assert.deepEqual(flat.map(hidden), [false, true, false], 'In progress OR Finished');
assert.deepEqual(members.map(hidden), [false, true, true]);
pick(filtering.seriesFilter, ['none']); filtering.filterBooks();
assert(members.every(hidden), 'Status AND No series hides series members');
assert.deepEqual(flat.map(hidden), [false, true, false], 'Status picks still apply alongside series');
pick(filtering.seriesFilter, ['none', 'series:One']); filtering.filterBooks();
assert.deepEqual(members.map(hidden), [false, true, true], 'No series OR One brings the in-progress member back');
pick(filtering.statusFilter, []); pick(filtering.seriesFilter, []); groupingOff = false; filtering.filterBooks();
assert.deepEqual(savedWrites.at(-1)[1].status, [], 'Lists are saved');
pick(filtering.statusFilter, ['not_started', 'finished']); filtering.filterBooks();
assert.deepEqual(savedWrites.at(-1)[1].status, ['not_started', 'finished'], 'Every pick is saved');
pick(filtering.statusFilter, []); filtering.filterBooks();
assert.equal(savedWrites.at(-1)[0], 'abs_kosync_library_filters', 'Lists use their own key; Classic keeps abs_kosync_filters');
assert.deepEqual(Object.keys(savedWrites.at(-1)[1]).sort(), ['attention','author','format','series','status']);
pick(filtering.statusFilter, ['not_started']);
filtering.filterBooks();
assert.deepEqual(savedWrites.at(-1)[1].status, ['not_started'], 'status persists');
filtering.attentionOnlyFilter = null;
filtering.savedAttentionPreference = true;
filtering.filterBooks();
assert.equal(savedWrites.at(-1)[1].attention, true, 'Missing attention checkbox cannot erase a migrated preference');
filtering.attentionOnlyFilter = {checked: false};
const closures = [];
const focused = [];
const rebuilt = {children: [{dataset: {value: 'all'}, focus() {focused.push('all');}},
                            {dataset: {value: 'author:Ben'}, focus() {focused.push('author:Ben');}}]};
const reset = scope(extract('function groupValues(', 'function passesLibraryFilters(') + extract('function chooseLibraryFilter(', 'function renderFilterOptions()'), {
  filterGroups: choices, filterBooks() {}, closeFilterPanel() {closures.push(true);},
  document: {getElementById: id => id === 'filter-author-options' ? rebuilt : null},
});
const state = () => Object.entries(choices).map(([key, sel]) => [key, picks(sel)]);
reset.chooseLibraryFilter('author', 'author:Ada');
reset.chooseLibraryFilter('author', 'author:Ben');
assert.deepEqual(state(), [['status',[]],['format',[]],['author',['author:Ada','author:Ben']],['series',[]]],
  'A second author adds to the first');
assert.deepEqual(focused, ['author:Ben'], 'Focus returns to the rebuilt option just toggled');
reset.chooseLibraryFilter('status', 'in_progress');
assert.deepEqual(state(), [['status',['in_progress']],['format',[]],['author',['author:Ada','author:Ben']],['series',[]]],
  'A pick in another group keeps the authors');
reset.chooseLibraryFilter('author', 'author:Ada');
assert.deepEqual(picks(choices.author), ['author:Ben'], 'Picking again removes it');
reset.chooseLibraryFilter('author', 'all');
assert.deepEqual(state(), [['status',['in_progress']],['format',[]],['author',[]],['series',[]]],
  'All clears only its own group');
reset.chooseLibraryFilter('status', 'in_progress');
assert(Object.values(choices).every(sel => picks(sel).length === 0));
assert.equal(closures.length, 0, 'Picking keeps the panel open');


const label = {textContent: ''};
const labelCode = extract('function hasActiveLibraryFilters()', '// A click elsewhere')
  .replace(/\{\{\s*_\('Filter'\)\|tojson\s*\}\}/, JSON.stringify('Filter'));
const labelClasses = new Set();
const labelReset = {hidden: true};
const labelClear = {disabled: true};
const labelSearch = {value: ''};
const labelAttention = {checked: false};
const labels = scope(extract('function groupValues(', 'function passesLibraryFilters(') + labelCode, {
  filterButtonLabel: label, filterGroups: choices,
  filterButton: {classList: {toggle(key, on) {if (on) labelClasses.add(key); else labelClasses.delete(key);}}},
  filterResetButton: labelReset, filterClearButton: labelClear,
  dashboardSearch: labelSearch, attentionOnlyFilter: labelAttention, savedAttentionPreference: false,
  facetOptionBaseLabel: option => option.textContent,
});
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter');
pick(choices.format, ['audiobook_only']);
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · Audio only', 'One pick shows its name');
pick(choices.author, ['author:Ada']);
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · 2', 'Several picks show a count');
pick(choices.format, []);
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · Ada');
pick(choices.author, []);
pick(choices.status, ['in_progress', 'finished']);
labels.updateFilterButtonLabel();
assert.equal(label.textContent, 'Filter · 2');
pick(choices.status, []);
labels.updateFilterButtonLabel();
assert(labelReset.hidden && labelClear.disabled && !labelClasses.has('is-active'), 'Nothing active hides × and disables Clear');
for (const [kind, turnOn, turnOff] of [
  ['facet', () => {pick(choices.series, ['series:Two']);}, () => {pick(choices.series, []);}],
  ['search', () => {labelSearch.value = 's';}, () => {labelSearch.value = '';}],
  ['attention', () => {labelAttention.checked = true;}, () => {labelAttention.checked = false;}],
]) {
  turnOn(); labels.updateFilterButtonLabel();
  assert(!labelReset.hidden && !labelClear.disabled && labelClasses.has('is-active'), kind + ' shows × and enables Clear');
  turnOff(); labels.updateFilterButtonLabel();
  assert(labelReset.hidden && labelClear.disabled && !labelClasses.has('is-active'), kind + ' no longer active hides ×');
}
labels.attentionOnlyFilter = null;
labels.savedAttentionPreference = true;
labels.updateFilterButtonLabel();
assert.equal(labelReset.hidden, true, 'A saved preference with no attention box filters nothing, so no ×');
labels.savedAttentionPreference = false;
labels.updateFilterButtonLabel();
assert.equal(labelReset.hidden, true);
assert.match(source, /filterResetButton\?\.addEventListener\('click', \(\) => clearLibraryFilters\(true\)\)/);
assert.match(source, /filterClearButton\?\.addEventListener\('click', \(\) => \{\s*clearLibraryFilters\(\)/);

const holders = {};
const searches = {author: {value: ''}, series: {value: ''}};
for (const group of Object.keys(choices)) holders[group] = {
  children: [], replaceChildren() {this.children = [];},
  appendChild(button) {this.children.push(button);},
};
const rendering = scope(extract('function groupValues(', 'function passesLibraryFilters(') + extract('function renderFilterOptions()', 'function isValidLibraryFilter('), {
  filterPanel: {},
  filterGroups: choices,
  document: {
    getElementById(id) {
      if (id.endsWith('-options')) return holders[id.slice(7, -8)];
      if (id.endsWith('-search')) return searches[id.slice(7, -7)] || null;
      return null;
    },
    createElement() {return {dataset: {}, setAttribute(k,v) {this[k]=v;}, addEventListener() {}};},
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
choices.author.options[3].hidden = false;
pick(choices.author, ['author:Ada', 'author:Ben']);
rendering.renderFilterOptions();
assert.deepEqual(holders.author.children.map(b => b['aria-pressed']), ['false', 'false', 'true', 'true'],
  'Every pick is pressed; All is not');
pick(choices.author, []);
rendering.renderFilterOptions();
assert.equal(holders.author.children[0]['aria-pressed'], 'true', 'All is pressed when nothing is picked');

const migrations = scope(extract('function isValidLibraryFilter(', 'if (filterButton) {'), {
  filterGroups: choices,
});
function normalized(saved = null, legacy = null) {
  const actual = migrations.normalizeSavedLibraryFilters(saved, legacy);
  return JSON.parse(JSON.stringify(actual));
}
assert.deepEqual(normalized(null), {status:[],format:[],author:[],series:[],attention:false});
assert.deepEqual(normalized(null, 'audiobook').format, ['paired', 'audiobook_only'], 'Legacy scalar audiobook means any audio');
assert.deepEqual(normalized({format:'has_audio'}).format, ['paired', 'audiobook_only'], 'Retired Has Audio becomes its two formats');
assert.equal(normalized({format:'needs_attention'}).attention, true, 'Old attention format turns on the independent checkbox');
assert.deepEqual(normalized({format:'needs_attention'}).format, []);
assert.deepEqual(normalized({series:'standalone'}).series, ['none'], 'Old standalone maps to No series');
assert.deepEqual(normalized({series:'in_series'}).series, [], 'Old in_series widens to All');
assert.deepEqual(normalized({author:'author:Missing'}).author, [], 'Missing remembered author defaults to All');
assert.deepEqual(normalized({series:'series:Missing'}).series, [], 'Missing remembered series defaults to All');
assert.deepEqual(normalized({format:'invalid'}).format, [], 'Missing format resets');
assert.deepEqual(normalized({status:'invalid'}).status, [], 'Invalid status resets');
assert.deepEqual(normalized({status:'in_progress',format:'paired',author:'author:Ada',series:'series:One',attention:true}),
 {status:['in_progress'],format:['paired'],author:['author:Ada'],series:['series:One'],attention:true},
 'A Classic single-value save restores every group together');
assert.deepEqual(normalized({status:['in_progress','finished'],author:['author:Ada','author:Missing','author:Ada']}),
 {status:['in_progress','finished'],format:[],author:['author:Ada'],series:[],attention:false},
 'Saved lists restore, minus missing and duplicate values');
assert.deepEqual(normalized({author:'none'}).author, ['none'], 'No author restores');
assert.deepEqual(normalized({series:'none'}).series, ['none'], 'No series restores');

const panel = {hidden: true, querySelector: () => ({focus() {}})};
const backdrop = {hidden: true};
const trigger = {attrs: {}, focusCount: 0, setAttribute(k,v) {this.attrs[k]=v;}, focus() {this.focusCount++;}};
const panelContext = scope(extract('function closeFilterPanel(', 'function chooseLibraryFilter('), {
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
panelContext.openFilterPanel();
panelContext.closeFilterPanel(false);
assert.equal(trigger.focusCount, 1, 'A click outside leaves focus where the reader clicked');
// On a phone the open sheet makes the page around it inert; the backdrop stays tappable.
function node(name, children = [], inert = false) {
  const element = {name, children, inert, parentElement: null};
  children.forEach(child => { child.parentElement = element; });
  return element;
}
const sheetPanel = Object.assign(node('panel'), {hidden: true, querySelector: () => null, style: {removeProperty() {}, setProperty() {}}, getBoundingClientRect: () => ({left: 0, width: 300})});
const sheetBackdrop = node('backdrop');
const toolbar = node('toolbar'), shelf = node('shelf'), header = node('header'), drawer = node('drawer', [], true);
node('main', [node('host', [toolbar, sheetBackdrop, sheetPanel]), shelf]);
const body = node('body', [header, sheetPanel.parentElement.parentElement, drawer]);
const sheetWindow = {innerWidth: 390, addEventListener() {}};
const sheetContext = scope(extract('function closeFilterPanel(', 'function chooseLibraryFilter('), {
  filterPanel: sheetPanel, filterBackdrop: sheetBackdrop, filterButton: trigger, window: sheetWindow, document: {body},
});
sheetContext.openFilterPanel();
assert.deepEqual([toolbar, shelf, header].map(e => e.inert), [true, true, true], 'Phone sheet makes the page behind it inert');
assert.equal(sheetBackdrop.inert, false, 'The backdrop stays tappable so it can close the sheet');
sheetContext.closeFilterPanel();
assert.deepEqual([toolbar, shelf, header, drawer].map(e => e.inert), [false, false, false, true], 'Closing restores only what the sheet made inert');
sheetWindow.innerWidth = 1280;
sheetContext.openFilterPanel();
assert.equal(toolbar.inert || shelf.inert || header.inert, false, 'The desktop popover leaves the page usable');
sheetContext.closeFilterPanel();
assert.match(source, /event.key === 'Escape'[\s\S]*?closeFilterPanel\(\)/);
assert.match(source, /!event.composedPath\(\)\.includes\(filterHost\)\) \{\s*closeFilterPanel\(false\);/, 'Outside clicks close without stealing focus, even after an option is rebuilt');
// Exercise Clear with the real filterBooks implementation and storage writes.
const resetLabelCode = (extract('function hasActiveLibraryFilters()', '// A click elsewhere')
  .replace(/\{\{\s*_\('Filter'\)\|tojson\s*\}\}/, JSON.stringify('Filter')))
  + extract('function clearLibraryFilters(', '// Offers a route into');
const resetClasses = new Set();
const resetTrigger = {
  focusCount: 0, focus() {this.focusCount++;},
  classList: {toggle(key, on) {if (on) resetClasses.add(key); else resetClasses.delete(key);}},
};
const resetX = {hidden: true};
const clearInPanel = {disabled: true};
const sort = {value: 'reading'};
const direction = {textContent: '↑'};
const groupingToggle = {checked: true};
let repositionCount = 0;
Object.assign(filtering, {
  filterGroups: {status: filtering.statusFilter, format: filtering.filterSelect,
                 author: filtering.authorFilter, series: filtering.seriesFilter},
  filterButton: resetTrigger, filterButtonLabel: {textContent: ''},
  filterResetButton: resetX, filterClearButton: clearInPanel,
  sortSelect: sort, directionBtn: direction, seriesGroupingToggle: groupingToggle,
  facetOptionBaseLabel: option => option.textContent,
  positionLibraryFilterPopover() {repositionCount++;},
});
scope(resetLabelCode, filtering);
pick(filtering.filterGroups.status, ['in_progress', 'finished']);
filtering.dashboardSearch.value = 'sea';
filtering.attentionOnlyFilter.checked = true;
facetSearchFields.author.value = 'Ada';
facetSearchFields.series.value = 'One';
filtering.savedAttentionPreference = true;
filtering.filterBooks();
assert(!resetX.hidden && !clearInPanel.disabled && resetClasses.has('is-active'));
const beforeClearWrites = savedWrites.length;
filtering.clearLibraryFilters(true);
assert(Object.values(filtering.filterGroups).every(select => picks(select).length === 0), 'Every facet clears');
assert.equal(filtering.dashboardSearch.value, '', 'Search clears');
assert.equal(filtering.attentionOnlyFilter.checked, false, 'Attention checkbox unticks');
assert.equal(filtering.savedAttentionPreference, false, 'Saved attention preference resets');
assert.equal(facetSearchFields.author.value, '', 'Author typeahead clears');
assert.equal(facetSearchFields.series.value, '', 'Series typeahead clears');
assert(cards.every(card => !hidden(card)), 'The full grid is visible after reset');
assert.equal(savedWrites.length, beforeClearWrites + 1, 'Clear reruns the grid and saves once');
assert.deepEqual(savedWrites.at(-1)[1],
  {format:[],series:[],author:[],attention:false,status:[]},
  'The saved state survives reload cleared, including Classic-compatible shape');
assert.equal(sort.value, 'reading', 'Sort selection is untouched');
assert.equal(direction.textContent, '↑', 'Sort direction is untouched');
assert.equal(groupingToggle.checked, true, 'Group series is untouched');
assert(resetX.hidden && clearInPanel.disabled && !resetClasses.has('is-active'), 'Clear hides × and disables itself');
assert.equal(filtering.filterButtonLabel.textContent, 'Filter');
assert.equal(resetTrigger.focusCount, 1, '× moves focus to Filter after hiding itself');
assert.equal(repositionCount, 1, 'Clear repositions the open desktop popover after toolbar relayout');

// The box is conditional; its former state must not persist if it is absent.
pick(filtering.filterGroups.format, ['paired']);
filtering.savedAttentionPreference = true;
filtering.attentionOnlyFilter = null;
filtering.clearLibraryFilters();
assert.equal(filtering.savedAttentionPreference, false, 'Clear resets preference without checkbox');
assert.deepEqual(savedWrites.at(-1)[1],
  {format:[],series:[],author:[],attention:false,status:[]});
assert.equal(sort.value, 'reading');
assert.equal(groupingToggle.checked, true);
for (const [key, value] of [
  ['status', 'in_progress'], ['format', 'paired'],
  ['author', 'author:Ada'], ['series', 'series:One'],
]) {
  pick(filtering.filterGroups[key], [value]);
  filtering.clearLibraryFilters();
  assert(Object.values(filtering.filterGroups).every(select => picks(select).length === 0),
    'Clear resets the ' + key + ' facet');
  assert.deepEqual(savedWrites.at(-1)[1],
    {format:[],series:[],author:[],attention:false,status:[]},
    'Each facet reset persists: ' + key);
}
console.log('Library P16b/P16e filter, clear, persistence, focus and popover regressions ok');
