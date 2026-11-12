// Library header contracts: restored local filter, global search and independent attention filtering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const library = fs.readFileSync('templates/next/index.html', 'utf8');
const base = fs.readFileSync('templates/next/base.html', 'utf8');
const scripts = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const globalSearch = fs.readFileSync('static/js/next/search.js', 'utf8');

assert.ok(!library.includes('block topbar_search'), 'Library must inherit global search');
assert.ok(library.includes('id="dashboard-search"'), 'Library must restore the in-grid text filter');
assert.ok(library.includes('placeholder="{{ _(\'Filter books…\') }}"'), 'Search hint must be compact');
assert.ok(library.includes('aria-label="{{ _(\'Filter books\') }}"'), 'Local filter is named for assistive technology');
for (const id of ['author-filter', 'series-filter', 'sort-select']) {
    assert.ok(new RegExp('id="' + id + '"[^>]+aria-label=').test(library), id + ' must have an accessible name on mobile');
}
assert.ok(library.includes('id="search-handoff"') && library.includes('id="search-handoff-link"'), 'Discover handoff restored');
assert.ok(scripts.includes("const dashboardSearch = document.getElementById('dashboard-search');"), 'Filter is wired');
assert.ok(scripts.includes("dashboardSearch.addEventListener('input', filterBooks)"), 'Typing filters the grid');
assert.ok(scripts.includes('updateSearchHandoff(dashboardSearch ? dashboardSearch.value.trim() :'), 'Typing updates Discover handoff');
assert.ok(scripts.includes('encodeURIComponent(rawQuery)'), 'Discover URL escapes the query');
assert.ok(scripts.includes("const seriesName = (card.dataset.seriesName || '').toLowerCase();"),
    'Series-name filtering works after changing between Covers, Spines and List');
assert.ok(!scripts.includes('[data-library-search-slot]'), 'Local filter stays in the toolbar');
assert.ok(base.includes('id="bb-search-open"'), 'Shared top bar has the launcher');
assert.ok(base.includes('id="bb-search-input"'), 'Shared search dialog owns the input');
assert.ok(globalSearch.includes("fetch('/api/me/books'"), 'Global search indexes books');
assert.ok(globalSearch.includes("fetch('/settings'"), 'Global search can index settings');
assert.ok(globalSearch.includes('catalog.pages'), 'Global search indexes pages');
assert.ok(globalSearch.includes('event.ctrlKey || event.metaKey'), 'Ctrl/Command K remains supported');

const format = library.match(/<select id="filter-select"[^>]*>([\s\S]*?)<\/select>/);
assert.ok(format, 'Format select remains as hidden state for Classic-compatible storage');
assert.match(library, /class="library-filter-state" hidden/, 'Original selects are no longer in the toolbar');
assert.match(library, /id="library-filter-button"/, 'One visible Filter button replaces the select');
assert.ok(!format[1].includes('needs_attention'), 'Attention is not a format');
assert.ok(library.includes('id="attention-only-filter"'), 'Attention filter stays available');
assert.ok(scripts.includes("candidate.format === 'needs_attention'"), 'Old saved attention filter migrates');

// Execute the production attention predicate against independent card states.
const start = scripts.indexOf('function needsAttention(card)');
const end = scripts.indexOf('// \'all\', \'none\' (no series at all)', start);
assert.ok(start > 0 && end > start, 'Attention predicate must be present');
const sandbox = {};
vm.runInNewContext(scripts.slice(start, end), sandbox);
const card = (status, outOfSync = false, syncMode = 'audiobook') => ({
    dataset: {status, syncMode},
    classList: {contains(className) { return className === 'out-of-sync' && outOfSync; }},
});
for (const status of ['error', 'failed', 'failed_retry_later', 'failed_permanent']) {
    assert.equal(sandbox.needsAttention(card(status)), true, status);
}
for (const status of ['processing', 'forging', 'pending']) {
    assert.equal(sandbox.needsAttention(card(status)), false, status + ' is not an error');
}
assert.equal(sandbox.needsAttention(card('processing', true)), true, 'out-of-sync takes priority');
assert.equal(sandbox.needsAttention(card('active', true)), true);
assert.equal(sandbox.needsAttention(card('active')), false);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'all'), true);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'has_audio'), true);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'ebook_only'), false);
assert.ok(library.includes("url_for('job_queue_page')"), 'Processing links to the existing job queue');
assert.ok(scripts.includes("attentionPopover.addEventListener('toggle', positionAttentionPopover)"),
    'attention popover is repositioned when opened');
assert.ok(scripts.includes("attentionTrigger?.focus()"), 'Escape restores focus');
assert.ok(scripts.includes("filterCount.textContent = active"), 'unfiltered count is only in heading');

// Run the real count function, with the Jinja-translated message supplied as a string.
const countStart = scripts.indexOf('function updateFilteredCount()');
const countEnd = scripts.indexOf('// Narrow the Series', countStart);
assert.ok(countStart > 0 && countEnd > countStart);
const countFunction = scripts.slice(countStart, countEnd).replace(
    /bbFormat\(\{\{.*?\}\}, \{shown, total\}\)/,
    "bbFormat('{shown} of {total} books', {shown, total})",
);
const countCards = [{classList: {contains: () => false}}, {classList: {contains: () => false}}];
const countScope = {
    filterCount: {textContent: 'stale'},
    distinctBookCards: () => countCards,
    filterSelect: {value: 'all'},
    statusFilter: {value: 'all'},
    seriesFilter: {value: 'all'},
    authorFilter: {value: 'all'},
    attentionOnlyFilter: {checked: false},
    dashboardSearch: null,
    bbFormat: (message, values) => message.replace('{shown}', values.shown).replace('{total}', values.total),
};
vm.runInNewContext(countFunction, countScope);
countScope.updateFilteredCount();
assert.equal(countScope.filterCount.textContent, '', 'no repeated unfiltered count');
countScope.filterSelect.value = 'ebook_only';
countScope.updateFilteredCount();
assert.equal(countScope.filterCount.textContent, '2 of 2 books', 'active filter shows count even if every book matches');
countCards[0].classList.contains = name => name === 'hidden';
countScope.updateFilteredCount();
assert.equal(countScope.filterCount.textContent, '1 of 2 books', 'filtered count reflects visible cards');
console.log('global search, warning/processing split, count and popover contracts ok');
