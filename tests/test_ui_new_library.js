const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('/* P3 additions:');
assert.notEqual(start, -1, 'P3 action block missing');
const end = source.indexOf('</script>', start);
assert.notEqual(end, -1, 'P3 action block is not a script');
const script = source.slice(start, end).replace(/\{\{\s*_\('([^']*)'\)\|tojson\s*\}\}/g, (_match, value) => JSON.stringify(value));

const ids = ['book-1', 'book-2', 'book-3'];
const inputs = ids.map(value => ({ value, checked: true }));
const progress = [];
let inFlight = 0;
let maxInFlight = 0;
let failBook2 = true;
let requests = [];
let prompts = [];
let alerts = [];
let reloads = 0;
const count = {
  _text: '',
  set textContent(value) { this._text = value; progress.push(value); },
  get textContent() { return this._text; },
};
const bar = { hidden: false, setAttribute() {}, removeAttribute() {} };
const document = {
  querySelectorAll(selector) {
    if (selector === '[data-select-book]:checked') return inputs.filter(input => input.checked);
    if (selector === '[data-select-book]') return inputs;
    return [];
  },
  getElementById(id) {
    if (id === 'selection-bar') return bar;
    if (id === 'selection-count') return count;
    return null;
  },
  addEventListener() {},
  querySelector() { return null; },
  querySelector() { return null; },
};
const context = {
  window: {},
  document,
  localStorage: { getItem: () => null, setItem() {} },
  setInterval() {},
  setTimeout() {},
  setTimeout() {},
  fetch: async (url, options) => {
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    requests.push({url, options});
    await new Promise(resolve => setTimeout(resolve, 2));
    inFlight--;
    if (failBook2 && url.includes('book-2')) return {ok: false, json: async () => ({success:false,error:'expected failure'})};
    return {ok: true, json: async () => ({success:true})};
  },
  confirm: message => { prompts.push(message); return true; },
  alert: message => alerts.push(message),
  bbFormat: (format, values) => format.replace(/\{(\w+)\}/g, (_match, key) => String(values[key])),
  location: {reload: () => { reloads++; }},
  encodeURIComponent,
  console: { error() {} },
};
vm.runInNewContext(script, context);

// Format strip: the glow follows the slot that led the last sync; faint slots never glow.
function fakeSlot(key, faint) {
  const classes = new Set(faint ? ['is-faint'] : []);
  const when = { textContent: '' };
  return {
    dataset: { slot: key }, when,
    classList: { contains: name => classes.has(name), toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) },
    has: name => classes.has(name),
    querySelector: () => when,
  };
}
const slots = [fakeSlot('ebook'), fakeSlot('audio'), fakeSlot('readalong', true), fakeSlot('koreader')];
const stripCard = { querySelector: () => ({ querySelectorAll: () => slots }) };
context.updateFormatStrip(stripCard, 'koreader', '2m ago');
assert(slots[3].has('is-last'));
assert.equal(slots[3].when.textContent, ' · synced 2m ago');
assert(!slots[0].has('is-last') && slots[0].when.textContent === '');
context.updateFormatStrip(stripCard, 'audio', '1m ago');
assert(slots[1].has('is-last') && !slots[3].has('is-last') && slots[3].when.textContent === '', 'the glow moves to the new leader');
context.updateFormatStrip(stripCard, 'readalong', 'now');
assert(!slots[2].has('is-last'), 'a faint slot never glows');

(async () => {
  await context.window.bulkSyncNow();
  assert.deepEqual(requests.map(request => request.url), ids.map(id => '/api/sync-now/' + id));
  assert.equal(maxInFlight, 1, 'selected actions must run sequentially');
  assert(progress.includes('Syncing 1 of 3'));
  assert(progress.includes('Syncing 2 of 3'));
  assert(progress.includes('Syncing 3 of 3'));
  assert.equal(count.textContent, '2 completed; 1 failed. Failed books remain selected.');
  assert.deepEqual(inputs.map(input => input.checked), [false, true, false], 'failed books stay selected');
  inputs.forEach(input => { input.checked = true; });
  failBook2 = false;

  requests = [];
  progress.length = 0;
  prompts = [];
  await context.window.bulkMarkFinished();
  assert.equal(prompts.length, 1, 'finishing the selection asks once');
  assert.deepEqual(requests.map(request => request.url), ids.map(id => '/api/mark-complete/' + id));
  assert.equal(maxInFlight, 1);
  assert(requests.every(request => JSON.parse(request.options.body).delete === false));
  assert.equal(reloads, 1);
  assert.equal(alerts.length, 0);

  inputs.forEach(input => { input.checked = true; });
  inputs.forEach(input => { input.checked = true; });
  requests = [];
  progress.length = 0;
  await context.window.bulkGenerateReadalong();
  assert.deepEqual(requests.map(request => request.url), ids.map(id => '/api/readalong-epub/' + id));
  assert.equal(maxInFlight, 1);
  assert.equal(alerts.length, 0);

  inputs.forEach(input => { input.checked = true; });
  inputs.forEach(input => { input.checked = true; });
  requests = [];
  progress.length = 0;
  await context.window.bulkDeleteBooks();
  assert.deepEqual(requests.map(request => request.url), ids.map(id => '/delete/' + id));
  assert.equal(maxInFlight, 1);
  assert.equal(prompts.length, 2, 'removal asks once for the whole selection');
  assert.equal(reloads, 2);
  assert.equal(alerts.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
