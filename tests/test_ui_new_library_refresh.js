// Live Library refresh: cover badge, finished check, line and spine follow the above-0% rule.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('function refreshDashboard()');
assert.notEqual(start, -1, 'refreshDashboard missing');
const end = source.indexOf('.finally(', start);
const close = source.indexOf('\n            }', end);
const script = source.slice(start, close + 14).replace(/\{\{[\s\S]*?\}\}/g, '""');

function makeCard() {
  const node = () => ({ hidden: false, textContent: '', style: {} });
  const nodes = {
    '.progress-percent': node(), '.progress-done': node(), '.progress-track': node(),
    '.progress-bar-fill': node(), '.spine-progress': node(),
  };
  const attrs = new Set();
  return { nodes, attrs, dataset: {}, querySelector: sel => nodes[sel] || null, querySelectorAll: () => [], toggleAttribute: (name, on) => { if (on) attrs.add(name); else attrs.delete(name); } };
}

async function refresh(card, book) {
  const context = {
    document: { hidden: false, querySelectorAll: () => [card] },
    fetch: async () => ({ json: async () => ({ mappings: [{ abs_id: 'b', ...book }] }) }),
    setTimeout() {}, bbFormat: s => s,
    updateCtcAlignmentStatus() {}, updateReadalongStatus() {}, updateFormatStrip() {},
  };
  vm.createContext(context);
  vm.runInContext(`let refreshPaused = false;\n${script}\nrefreshDashboard();`, context);
  await new Promise(resolve => setImmediate(resolve));
}

const visible = card => Object.keys(card.nodes).filter(sel => sel !== '.progress-bar-fill' && !card.nodes[sel].hidden).sort();

(async () => {
  const card = makeCard();
  await refresh(card, { unified_progress: 47.6, states: { abs: { percentage: 47.6 }, kosync: { percentage: 0 } } });
  assert.deepEqual(visible(card), ['.progress-percent', '.progress-track', '.spine-progress']);
  assert.equal(card.nodes['.progress-percent'].textContent, '47%');
  assert.equal(card.nodes['.progress-bar-fill'].style.width, '47.6%');
  assert.equal(card.dataset.progress, 47.6);
  // A book in progress stands face-out in the Spines view; finished or cleared books go back to spines.
  assert.ok(card.attrs.has('data-reading'));

  await refresh(card, { unified_progress: 100, states: { abs: { percentage: 100 } } });
  assert.deepEqual(visible(card), ['.progress-done', '.spine-progress']);
  assert.ok(!card.attrs.has('data-reading'));

  // Clear progress leaves 0% reset snapshots: the refresh hides everything again.
  await refresh(card, { unified_progress: 47.6, states: { abs: { percentage: 47.6 } } });
  await refresh(card, { unified_progress: 0, states: { abs: { percentage: 0 }, kosync: { percentage: 0 } } });
  assert.deepEqual(visible(card), []);
  assert.ok(!card.attrs.has('data-reading'));

  const fresh = makeCard();
  await refresh(fresh, { unified_progress: 0, states: {} });
  assert.deepEqual(visible(fresh), []);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
