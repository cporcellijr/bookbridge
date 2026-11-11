// Library header contracts: one global search input and independent attention filtering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const library = fs.readFileSync('templates/next/index.html', 'utf8');
const base = fs.readFileSync('templates/next/base.html', 'utf8');
const scripts = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const globalSearch = fs.readFileSync('static/js/next/search.js', 'utf8');

assert.ok(!library.includes('block topbar_search'), 'Library must inherit global search');
assert.ok(!library.includes('id="dashboard-search"'), 'No duplicate text search in Library');
assert.ok(!scripts.includes('[data-library-search-slot]'), 'No moved Library-only search field');
assert.ok(base.includes('id="bb-search-open"'), 'Shared top bar has the launcher');
assert.ok(base.includes('id="bb-search-input"'), 'Shared search dialog owns the input');
assert.ok(globalSearch.includes("fetch('/api/me/books'"), 'Global search indexes books');
assert.ok(globalSearch.includes("fetch('/settings'"), 'Global search can index settings');
assert.ok(globalSearch.includes('catalog.pages'), 'Global search indexes pages');
assert.ok(globalSearch.includes('event.ctrlKey || event.metaKey'), 'Ctrl/Command K remains supported');

const format = library.match(/<select id="filter-select"[^>]*>([\s\S]*?)<\/select>/);
assert.ok(format, 'Format is a visible select');
assert.ok(!format[0].includes('class="hidden"'), 'Format cannot be hidden');
assert.ok(!format[1].includes('needs_attention'), 'Attention is not a format');
assert.ok(library.includes('id="attention-only-filter"'), 'Attention filter stays available');
assert.ok(scripts.includes("savedFormat === 'needs_attention'"), 'Old saved attention filter migrates');

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
for (const status of ['error', 'failed', 'failed_retry_later', 'failed_permanent', 'processing', 'forging', 'pending']) {
    assert.equal(sandbox.needsAttention(card(status)), true, status);
}
assert.equal(sandbox.needsAttention(card('active', true)), true);
assert.equal(sandbox.needsAttention(card('active')), false);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'all'), true);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'has_audio'), true);
assert.equal(sandbox.matchesFormatFilter(card('active'), 'ebook_only'), false);
console.log('global search and independent Library attention filter ok');
