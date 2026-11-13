const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const changes = {
  swap: ["Number(row.last_slot_unix || 0) > currentMarker", "false"],
  quiet: ["Number(row.last_slot_unix || 0) > currentMarker", "true"],
  series: ["const heroHidden = card.dataset.heroExcluded === 'true' && !card.closest?.('.series-group');",
           "const heroHidden = card.dataset.heroExcluded === 'true';"],
  eligible: ["rows.filter(row => heroIsInProgress(row) &&", "rows.filter(row => true &&"],
  progress: ["heroRoot.querySelector('.hero-percent').textContent = percentage.toFixed(1) + '%';",
             "heroRoot.querySelector('.hero-percent').textContent = '0%';"],
  restore: ["else delete card.dataset.heroExcluded;", "else card.dataset.heroExcluded = 'true';"],
  count: ["if (absId && !heroHidden && !seen.has(absId))", "if (absId && !seen.has(absId))"],
  selection: ["if (typeof updateSelectionBar === 'function') updateSelectionBar();", "void 0;"],
};
const key = process.env.P16C_MUTATE;
if (key) {
  assert(changes[key], 'Unknown mutation');
  const [before, after] = changes[key];
  assert(source.includes(before), 'Missing mutation target: ' + key);
  source = source.replace(before, after);
}
const start = source.indexOf("const heroRoot = document.getElementById('library-hero');");
const stop = source.indexOf("let refreshPaused = false;", start);
assert(start >= 0 && stop > start, 'Missing actual hero lifecycle');
const code = source.slice(start, stop)
  .replace(/\{\{\s*_\('Open \{title\} by \{author\}'\)\|tojson\s*\}\}/g, JSON.stringify('Open {title} by {author}'))
  .replace(/\{\{\s*_\('Unknown author'\)\|tojson\s*\}\}/g, JSON.stringify('Unknown author'))
  .replace(/\{\{\s*_\('([^']*)'\)\|tojson\s*\}\}/g, (_, value) => JSON.stringify(value));
const node = () => ({textContent: '', hidden: false});
const ui = {
  '.hero-title': node(), '.hero-author': node(), '.hero-service': node(), '.hero-percent': node(),
  '.hero-cover': {children: [], replaceChildren(...items) {this.children = items;}},
  '.library-hero-link': {href: '/book/a', setAttribute(key,value) {this[key] = value;}},
};
const hero = {dataset: {absId: 'a', movementAt: '100', markerAt: '100'}, hidden: false,
  querySelector(selector) {return ui[selector];}};
function card(id, title) {
  const input = {checked: false, disabled: false};
  const image = {cloneNode() {return {type: 'image', id};}};
  const text = {cloneNode() {return {type: 'fallback', id};}};
  const cover = {querySelector(q) {return q === '.cover-image' ? image :
    q === '.typographic-cover' ? text : null;}};
  return {dataset: {absId: id, title, author: 'Author ' + id},
    querySelector(q) {if (q === '[data-select-book]') return input;
      if (q === '.book-open') return {href: '/book/' + id};
      if (q === '.cover-wrap') return cover; return null;},
    input};
}
const books = [card('a', 'A title'), card('b', 'B title'), card('c', 'C title'), card('d', 'D title')];
let sorting = 0, filtering = 0, selectionUpdates = 0;
const context = {
  document: {getElementById: () => hero, querySelectorAll: () => books},
  sortSelect: {value: 'reading'}, sortState: {reading: 'asc'},
  sortBooks() {sorting++;}, filterBooks() {filtering++;},
  updateSelectionBar() {selectionUpdates++;},
  bbFormat: (template, attrs) => template.replace(/\{(\w+)\}/g, (_, k) => attrs[k]),
  window: {requestAnimationFrame() {}, addEventListener() {}, setTimeout() {}},
};
vm.runInNewContext(code, context);
assert.equal(books[0].dataset.heroExcluded, 'true', 'Initial hero excluded before poll');
assert.equal(books[0].input.disabled, true);
const countSource = source.slice(source.indexOf('function distinctBookCards()'),
    source.indexOf('function updateFilteredCount()'));
assert(countSource.includes('card.dataset.heroExcluded'));
const countScope = {
    document: {querySelectorAll() { return books; }},
};
vm.runInNewContext(countSource, countScope);
assert.equal(countScope.distinctBookCards().length, 3, 'Grid count excludes hero before filtering');
books[0].closest = selector => selector === '.series-group' ? {} : null;
assert.equal(countScope.distinctBookCards().length, 4, 'Inside its series stack the hero book still counts');
delete books[0].closest;
function row(id, progress, marker, client='abs') {
  return {abs_id: id, unified_progress: progress, last_slot_unix: marker,
    last_sync_unix: marker + 1, last_slot_client: client,
    last_slot_service: client === 'abs' ? 'Audiobookshelf' : 'KOReader',
    states: {[client]: {percentage: progress, timestamp: 120}}};
}
const map = new Map(books.map(book => [book.dataset.absId, [book]]));
context.refreshHeroFromPoll([
  row('a', 36, 100), row('b', 25, 100),
  row('c', 100, 300), row('d', 0, 400),
], map);
assert.equal(hero.dataset.absId, 'a', 'Quiet poll and finished/unstarted rows do not swap');
assert.equal(ui['.hero-percent'].textContent, '36.0%', 'Quiet poll updates percent in place');
assert.equal(ui['.hero-service'].textContent, 'Audiobookshelf', 'Hero has the exact leader');
assert.equal(sorting, 0, 'Quiet poll does not resort the grid');
const unpropagated = row('b', 25, 0);
unpropagated.last_sync_unix = 9999;
context.refreshHeroFromPoll([row('a', 36, 100), unpropagated], map);
assert.equal(hero.dataset.absId, 'a', 'Updated state without propagated marker cannot swap');

books[1].input.checked = true;
context.refreshHeroFromPoll([
  row('a', 36, 100), row('b', 27, 111),
  row('c', 100, 300), row('d', 0, 400),
], map);
assert.equal(hero.dataset.absId, 'b', 'Newer movement swaps hero');
assert.equal(books[0].dataset.heroExcluded, undefined, 'Old hero returns to grid');
assert.equal(books[0].input.disabled, false, 'Old hero becomes selectable');
assert.equal(books[1].dataset.heroExcluded, 'true', 'New hero excluded from grid');
assert.equal(books[1].input.disabled, true, 'Hero cannot be selected');
assert.equal(books[1].input.checked, false, 'Selected new hero is deselected');
assert.equal(selectionUpdates, 1, 'Selection bar recomputed after a swap');
assert.equal(countScope.distinctBookCards().length, 3, 'Grid count follows new hero on swap');
assert.equal(ui['.hero-title'].textContent, 'B title', 'New hero uses grid title');
assert.equal(ui['.hero-cover'].children[0].id, 'b', 'New hero copies on-page cover');
assert.equal(ui['.library-hero-link'].href, '/book/b', 'New hero opens the book window');
assert.equal(ui['.hero-percent'].textContent, '27.0%', 'New hero progress updated in place');
assert.equal(sorting, 1);
assert(filtering >= 2);

context.refreshHeroFromPoll([
  row('a', 37, 100), row('b', 29, 111), row('c', 100, 888), row('d', 0, 999),
], map);
assert.equal(hero.dataset.absId, 'b', 'No later eligible movement retains hero');
assert.equal(ui['.hero-percent'].textContent, '29.0%', 'Continued polling refreshes numbers');
assert.equal(sorting, 1, 'No redundant sort on no-swap');
context.refreshHeroFromPoll([row('a', 37, 111), row('b', 100, 111)], map);
assert.equal(hero.dataset.absId, 'b', 'A finished hero stays until another book moves');
context.refreshHeroFromPoll([row('a', 37, 113), row('b', 100, 111)], map);
assert.equal(hero.dataset.absId, 'a', 'Finished hero only swaps on newer movement elsewhere');
hero.dataset.absId = '';
hero.dataset.markerAt = '0';
hero.hidden = true;
context.refreshHeroFromPoll([row('d', 32, 200)], map);
assert.equal(hero.dataset.absId, 'd', 'A new in-progress book creates a hero after an empty state');
assert.equal(hero.hidden, false);
console.log('P16c hero swap, quiet/invalid poll, progress and grid restoration passed');
