// Cover painting: Covers samples lazy covers as they load; Spines forces hidden covers to load.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('    function paintSpines(eager)');
assert.notEqual(start, -1, 'paintSpines missing');
const end = source.indexOf('\n    }\n', start) + 6;
const script = source.slice(start, end);

function makeCard(absId, { loaded = false } = {}) {
  const listeners = [];
  const img = {
    complete: loaded, naturalWidth: loaded ? 100 : 0, loading: 'lazy',
    addEventListener(type, fn) { listeners.push(fn); },
  };
  const style = {};
  return {
    img, listeners,
    dataset: { absId },
    querySelector: sel => (sel === '.cover-image' ? img : null),
    style: { setProperty: (name, value) => { style[name] = value; } },
    painted: style,
  };
}

function run(cards, eager, cache = {}) {
  const sandbox = {
    document: { querySelectorAll: () => cards.filter(card => !('spinePainted' in card.dataset)) },
    localStorage: { getItem: key => cache[key] || null, setItem: (key, value) => { cache[key] = value; } },
    spineColours: () => ['#112233', '#445566', '#f3eee4'],
    console,
  };
  vm.runInNewContext(`${script}\npaintSpines(${eager});`, sandbox);
  return cache;
}

// Covers on load: an unloaded cover is not forced to load; it is sampled when its lazy load arrives.
const lazy = makeCard('lazy');
const cache = run([lazy], false);
assert.equal(lazy.img.loading, 'lazy');
assert.equal(lazy.listeners.length, 1);
assert.ok(!('spinePainted' in lazy.dataset));
lazy.img.naturalWidth = 100;
lazy.listeners[0]();
assert.equal(lazy.painted['--spine'], '#112233');
assert.equal(cache['bb-spine2:lazy'], '#112233,#445566,#f3eee4');

// A cached colour applies straight away, without waiting for the image.
const cached = makeCard('cached');
run([cached], false, { 'bb-spine2:cached': '#abcdef,#123456,#1c1a17' });
assert.equal(cached.painted['--spine'], '#abcdef');
assert.equal(cached.listeners.length, 0);
assert.equal(cached.img.loading, 'lazy');

// Entering Spines afterwards forces the still-unloaded cover to load, without a second listener.
const waiting = makeCard('waiting');
run([waiting], false);
run([waiting], true);
assert.equal(waiting.img.loading, 'eager');
assert.equal(waiting.listeners.length, 1);

console.log('paint checks passed');
