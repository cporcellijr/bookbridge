const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('/* P3 additions:');
const end = source.indexOf('</script>', start);
assert(start >= 0 && end > start);
const script = source.slice(start, end).replace(/\{\{\s*_\('([^']*)'\)\|tojson\s*\}\}/g, (_full, text) => JSON.stringify(text));

const callbacks = {click: [], keydown: [], change: []};
const listeners = {
  on(type, handler, capture = false) { (callbacks[type] ||= []).push({handler, capture}); },
  emit(type, event) {
    for (const {handler} of callbacks[type] || []) handler(event);
  },
};
const count = {textContent: ''};
const bar = {
  hidden: true, attrs: {},
  setAttribute(key, value) { this.attrs[key] = value; },
  removeAttribute(key) { delete this.attrs[key]; },
};
function button(view) {
  return {
    dataset: {view}, hidden: false, attrs: {},
    addEventListener(type, handler) { this.handler = handler; },
    setAttribute(key, value) { this.attrs[key] = value; },
    click() { this.handler?.(); },
  };
}
const select = button('select');
select.focus = () => { select.focused = true; };
const page = {dataset: {view: 'covers', selectMode: 'off'}};
const actions = Array.from({length: 5}, () => ({disabled: false}));
const links = [];
const cards = [];
const inputs = [];
function makeBook(id) {
  const input = {
    value: id, checked: false, disabled: false,
    matches(selector) { return selector === '[data-select-book]'; },
    dispatchEvent(e) { listeners.emit(e.type, {target: this}); },
  };
  const card = {
    dataset: {absId: id},
    classes: new Set(),
    classList: {toggle(name, enabled) { if(enabled) card.classes.add(name); else card.classes.delete(name); }},
    querySelector(selector) {
      if (selector === '[data-select-book]:checked') return input.checked ? input : null;
      if (selector === '[data-select-book]') return input;
      return null;
    },
  };
  const link = {
    dataset: {}, tabIndex: 0, attrs: {},
    getAttribute(key) { return this.attrs[key] ?? null; },
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; if (key === 'tabindex') this.tabIndex = 0; },
    matches() { return false; },
    closest(selector) { return selector === '.book-card' ? card : null; },
  };
  inputs.push(input); cards.push(card); links.push(link);
  return {input, card, link};
}
const first = makeBook('one');
const second = makeBook('two');
const searchOverlay = {hidden: true};
const document = {
  addEventListener(type, callback, capture) { listeners.on(type, callback, capture); },
  getElementById(id) {
    return ({'library-page': page, 'select-mode-button': select, 'selection-bar': bar, 'selection-count': count, 'bb-search-overlay': searchOverlay})[id] ?? null;
  },
  querySelectorAll(selector) {
    if (selector === '[data-select-book]') return inputs;
    if (selector === '[data-select-book]:checked') return inputs.filter(input => input.checked);
    if (selector === '.book-card') return cards;
    if (selector === '.book-card:not(.hidden)') return cards;
    if (selector === '.book-open') return links;
    if (selector === '[data-bulk-action], [data-selection-clear]') return actions;
    if (selector === '#selection-bar button, [data-select-book], .library-toolbar button') return [...actions, ...inputs, select];
    return [];
  },
  querySelector() { return null; },
};
let requests = 0, reloads = 0;
const cache = new Map();
const context = {
  document, window: {},
  seriesGroupingToggle: {checked: true, disabled: false},
  applySeriesGrouping() {}, filterSelect: {}, filterBooks() {},
  localStorage: {getItem() {return null;}, setItem() {}},
  sessionStorage: {
    getItem(key) {return cache.get(key) ?? null;},
    setItem(key, value) {cache.set(key, value);},
    removeItem(key) {cache.delete(key);},
  },
  bbFormat: (format, data) => format.replace(/\{(\w+)\}/g, (_, key) => String(data[key])),
  Event: class { constructor(type, opts) { this.type = type; this.bubbles = opts?.bubbles; } },
  fetch: async () => { requests++; return {ok: true, json: async () => ({success: true})}; },
  location: {reload() {reloads++;}},
  confirm: () => true, setTimeout() {},
  encodeURIComponent,
  console: {error() {}, debug() {}},
};
vm.runInNewContext(script, context);
function cardClick(link) {
  const event = {
    target: link, prevented: false, stopped: false,
    preventDefault() {this.prevented = true;},
    stopImmediatePropagation() {this.stopped = true;},
  };
  listeners.emit('click', event);
  return event;
}
function key(key, extra = {}) {
  let prevented = false;
  listeners.emit('keydown', {key, ...extra, preventDefault() {prevented = true;}});
  return prevented;
}
function selectedCount() {return inputs.filter(input => input.checked).length;}

(async () => {
  assert.equal(page.dataset.selectMode, 'off');
  assert.equal(bar.hidden, true);
  assert(actions.every(button => button.disabled));
  assert.equal(cardClick(first.link).prevented, false, 'normal card opens the drawer');
  assert.equal(selectedCount(), 0);

  select.click();
  assert.equal(page.dataset.selectMode, 'on');
  assert.equal(select.attrs['aria-pressed'], 'true');
  assert.equal(bar.hidden, false);
  assert.equal(count.textContent, '0 selected');
  assert(links.every(link => link.tabIndex === -1), 'links are removed from the tab order');

  const click = cardClick(first.link);
  assert(click.prevented && click.stopped, 'a selected card cannot open the drawer');
  assert.equal(count.textContent, '1 selected');
  assert(first.card.classes.has('is-selected'));
  assert(actions.every(button => !button.disabled));

  second.input.checked = true;
  second.input.dispatchEvent(new context.Event('change', {bubbles: true}));
  assert.equal(count.textContent, '2 selected', 'native checkbox changes remain supported');

  // P14 #4: Escape that closes Ctrl K search, or that another handler already took, keeps the selection.
  searchOverlay.hidden = false;
  assert.equal(key('Escape'), false);
  searchOverlay.hidden = true;
  assert.equal(key('Escape', {defaultPrevented: true}), false);
  assert.equal(page.dataset.selectMode, 'on');
  assert.equal(count.textContent, '2 selected');

  assert(key('Escape'));
  assert.equal(page.dataset.selectMode, 'off');
  assert.equal(bar.hidden, true);
  assert.equal(selectedCount(), 0);
  assert.equal(select.attrs['aria-pressed'], 'false');
  assert(links.every(link => link.tabIndex === 0));
  assert(select.focused);

  select.click();
  assert.equal(page.dataset.view, 'covers');
  assert.equal(select.hidden, false);
  cardClick(first.link);
  cardClick(second.link);
  const running = context.window.bulkSyncNow();
  assert.equal(key('Escape'), false, 'Escape waits while a batch is running');
  assert.equal(page.dataset.selectMode, 'on');
  await running;
  assert.equal(requests, 2);
  assert.equal(page.dataset.selectMode, 'on', 'a bulk action must not exit selection mode');
  assert.equal(bar.hidden, false);
  assert.equal(count.textContent, '2 completed');
  assert.equal(selectedCount(), 0);
  assert(actions.every(button => button.disabled));

  // A successful remove/finish reloads to refresh cards, restoring mode once and the message.
  cardClick(first.link);
  await context.window.bulkMarkFinished();
  assert.equal(reloads, 1);
  assert.equal(cache.get('bb-library-after-bulk'), '1 completed');

  context.window.exitBookSelection();
  assert.equal(page.dataset.selectMode, 'off');
  assert.equal(selectedCount(), 0);
  console.log('select mode interaction checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
