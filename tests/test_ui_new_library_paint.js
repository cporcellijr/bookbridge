// Cover painting: every view samples lazy covers as they load and never forces one to load.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('    function paintSpines(');
assert.notEqual(start, -1, 'paintSpines missing');
const end = source.indexOf('\n    }\n', start) + 6;
const script = source.slice(start, end);
// The page's own call, so a view-dependent argument would be exercised too.
const initCall = source.match(/\n\s*(paintSpines\([^)]*\);)/);
assert.ok(initCall, 'paintSpines call missing');
const init = `const initialLibraryView = 'spines';\n${initCall[1]}`;

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

function run(cards, cache = {}) {
  const sandbox = {
    document: { querySelectorAll: () => cards.filter(card => !('spinePainted' in card.dataset)) },
    localStorage: { getItem: key => cache[key] || null, setItem: (key, value) => { cache[key] = value; } },
    spineColours: () => ['#112233', '#445566', '#f3eee4'],
    console,
  };
  vm.runInNewContext(`${script}\n${init}`, sandbox);
  return cache;
}

// Covers on load: an unloaded cover is not forced to load; it is sampled when its lazy load arrives.
const lazy = makeCard('lazy');
const cache = run([lazy]);
assert.equal(lazy.img.loading, 'lazy');
assert.equal(lazy.listeners.length, 1);
assert.ok(!('spinePainted' in lazy.dataset));
lazy.img.naturalWidth = 100;
lazy.listeners[0]();
assert.equal(lazy.painted['--spine'], '#112233');
assert.equal(cache['bb-spine2:lazy'], '#112233,#445566,#f3eee4');

// A cached colour applies straight away, without waiting for the image.
const cached = makeCard('cached');
run([cached], { 'bb-spine2:cached': '#abcdef,#123456,#1c1a17' });
assert.equal(cached.painted['--spine'], '#abcdef');
assert.equal(cached.listeners.length, 0);
assert.equal(cached.img.loading, 'lazy');

// Spines (a 116-cover shelf downloaded 9.9 MB at once) leaves an unloaded cover lazy; painting again adds no second listener.
const waiting = makeCard('waiting');
run([waiting]);
run([waiting]);
assert.equal(waiting.img.loading, 'lazy');
assert.equal(waiting.listeners.length, 1);

console.log('paint checks passed');
