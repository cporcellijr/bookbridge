'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const mutations = {
  clamp: ['if (text.scrollHeight > text.clientHeight + 1) {', 'if (true) {'],
  expand: ["text.classList.toggle('is-clamped', !open);", "text.classList.toggle('is-clamped', open);"],
  label: ["toggle.textContent = open ? toggle.dataset.less : toggle.dataset.more;", "toggle.textContent = open ? 'Less' : 'More';"],
  cached: ['if (text.textContent.trim()) show();', 'void 0;'],
  empty: ['if (!data || !data.text) return;', 'if (!data) return;'],
  safe: ['text.textContent = data.text;', 'text.innerHTML = data.text;'],
};
let source = fs.readFileSync('static/js/next/book.js', 'utf8');
const mutation = process.env.P16F_MUTATE;
if (mutation) {
  const [before, after] = mutations[mutation];
  assert(source.includes(before), 'Missing mutation target: ' + mutation);
  source = source.replace(before, after);
}

function setup({cached = '', url, height = 200, client = 100, reply}) {
  const classes = new Set();
  const attrs = {'aria-expanded': 'false'};
  let click;
  const text = {
    _text: cached, scrollHeight: height, clientHeight: client,
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; },
    set innerHTML(v) { this._text = v; this.unsafe = true; },
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, on) => on ? classes.add(c) : classes.delete(c),
    },
  };
  const toggle = {
    hidden: true, textContent: 'Plus', dataset: {more: 'Plus', less: 'Moins'},
    addEventListener: (_, fn) => { click = fn; },
    getAttribute: n => attrs[n], setAttribute: (n, v) => { attrs[n] = v; },
  };
  const section = {
    hidden: !!url, dataset: url ? {synopsisUrl: url} : {},
    querySelector: s => s === '[data-about-text]' ? text : toggle,
  };
  const requests = [];
  const context = {
    document: {
      querySelectorAll: () => [],
      querySelector: s => s === '[data-book-about]' ? section : null,
    },
    fetch(u) {
      requests.push(u);
      return reply ? reply() : Promise.reject(new Error('offline'));
    },
    bbT: s => s, window: {}, location: {}, confirm() {}, alert() {},
  };
  vm.runInNewContext(source, context);
  return {section, text, toggle, classes, attrs, requests, click: () => click()};
}
const settled = () => new Promise(resolve => setImmediate(resolve));
const ok = body => () => Promise.resolve({ok: true, json: () => Promise.resolve(body)});

(async function () {
  // Cached long text: shown at once, clamped, no request, More/Less cycle.
  let f = setup({cached: 'Long cached text'});
  assert.equal(f.section.hidden, false);
  assert.equal(f.requests.length, 0, 'Cached text never fetches');
  assert(f.classes.has('is-clamped'));
  assert.equal(f.toggle.hidden, false, 'Long text gets a toggle');
  f.click();
  assert(!f.classes.has('is-clamped'), 'More expands');
  assert.equal(f.attrs['aria-expanded'], 'true');
  assert.equal(f.toggle.textContent, 'Moins', 'Less label comes translated from the template');
  f.click();
  assert(f.classes.has('is-clamped'));
  assert.equal(f.toggle.textContent, 'Plus');

  // Cached short text: no toggle, no clamp.
  f = setup({cached: 'Short', height: 50, client: 100});
  assert.equal(f.section.hidden, false);
  assert.equal(f.toggle.hidden, true, 'Short text has no toggle');
  assert(!f.classes.has('is-clamped'));

  // Uncached: hidden, fetch, fill as plain text.
  f = setup({url: '/api/book/a/synopsis', reply: ok({text: '<b>Hi</b>'})});
  assert.equal(f.section.hidden, true);
  await settled();
  assert.deepEqual(f.requests, ['/api/book/a/synopsis']);
  assert.equal(f.section.hidden, false);
  assert.equal(f.text.textContent, '<b>Hi</b>');
  assert(!f.text.unsafe, 'Text is never parsed as HTML');
  assert.equal(f.toggle.hidden, false);

  // Null, 404 and network failure stay hidden.
  for (const reply of [ok({text: null}), () => Promise.resolve({ok: false}), undefined]) {
    f = setup({url: '/api/book/a/synopsis', reply});
    await settled();
    assert.equal(f.section.hidden, true, 'No text keeps the section hidden');
  }
})().catch(error => { console.error(error); process.exit(1); });
