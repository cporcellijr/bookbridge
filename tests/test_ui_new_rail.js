// The New rail collapses to icons from its toggle, remembers the choice, and labels icon links.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/next/shell.js', 'utf8');

function element(attrs = {}, dataset = {}) {
  return {
    attrs: {...attrs}, dataset: {...dataset},
    setAttribute(key, value) { this.attrs[key] = String(value); },
    removeAttribute(key) { delete this.attrs[key]; },
    getAttribute(key) { return this.attrs[key] ?? null; },
    addEventListener(type, handler) { this[`on${type}`] = handler; },
  };
}
function link(label, count) {
  const span = {textContent: label};
  const node = element();
  node.textContent = label + (count ?? '');
  node.querySelector = selector => (selector === 'span:not(.count)' ? span : null);
  return node;
}

function run(saved) {
  const store = new Map(saved ? [['bookbridge-rail', saved]] : []);
  const root = {dataset: saved === 'collapsed' ? {rail: 'collapsed'} : {}};
  const toggle = element({'aria-expanded': 'true'}, {collapseLabel: 'Collapse sidebar', expandLabel: 'Expand sidebar'});
  const links = [link('Library'), link('Discover', '3')];
  const document = {
    documentElement: root,
    querySelector: selector => (selector === '[data-rail-toggle]' ? toggle : null),
    querySelectorAll: selector => (selector === '.rail-nav > a' ? links : []),
    addEventListener() {},
  };
  const localStorage = {getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value)};
  vm.runInNewContext(source, {document, window: {localStorage}});
  return {root, toggle, links, store};
}

// Fresh browser: expanded, no tooltips.
let page = run(null);
assert.equal(page.root.dataset.rail, undefined);
assert.equal(page.toggle.getAttribute('aria-expanded'), 'true');
assert.equal(page.links[0].getAttribute('title'), null);

// Collapse: icons only, remembered, toggle relabelled, links titled without their counts.
page.toggle.onclick();
assert.equal(page.root.dataset.rail, 'collapsed');
assert.equal(page.store.get('bookbridge-rail'), 'collapsed');
assert.equal(page.toggle.getAttribute('aria-expanded'), 'false');
assert.equal(page.toggle.getAttribute('aria-label'), 'Expand sidebar');
assert.deepEqual(page.links.map(item => item.getAttribute('title')), ['Library', 'Discover']);

// Expand again: tooltips removed, choice remembered.
page.toggle.onclick();
assert.equal(page.root.dataset.rail, undefined);
assert.equal(page.store.get('bookbridge-rail'), 'expanded');
assert.equal(page.toggle.getAttribute('aria-label'), 'Collapse sidebar');
assert.equal(page.links[1].getAttribute('title'), null);

// A page opened with the saved collapsed state (set early by base.html) gets titles and labels.
page = run('collapsed');
assert.equal(page.toggle.getAttribute('aria-expanded'), 'false');
assert.equal(page.links[0].getAttribute('title'), 'Library');
console.log('rail collapse ok');
