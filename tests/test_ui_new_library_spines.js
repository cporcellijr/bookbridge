const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('function spineColours(img)');
assert.notEqual(start, -1, 'spineColours missing');
const end = source.indexOf('\n    function paintSpines', start);
const script = source.slice(start, end);

function coverOf(pixels, { tainted = false } = {}) {
  const data = new Uint8ClampedArray(12 * 18 * 4);
  pixels.forEach(([r, g, b], i) => data.set([r, g, b, 255], i * 4));
  const context = {
    drawImage() {},
    getImageData() { if (tainted) throw new Error('tainted'); return { data }; },
  };
  const sandbox = { document: { createElement: () => ({ getContext: () => context }) } };
  vm.runInNewContext(script + '\nresult = spineColours({});', sandbox);
  return sandbox.result;
}
const fill = (count, rgb) => Array.from({ length: count }, () => rgb);

// A red title block on a mostly white cover: the colour wins, light ink on the dark red.
const red = coverOf([...fill(151, [250, 250, 250]), ...fill(65, [200, 30, 30])]);
assert.deepEqual([...red], ['#c81e1e', '#fafafa', '#f3eee4']);

// A pale yellow cover gets dark ink.
const pale = coverOf(fill(216, [240, 220, 120]));
assert.equal(pale[0], '#f0dc78');
assert.equal(pale[2], '#1c1a17');

// A mostly black cover with a dark red title takes the red, not the black.
assert.equal(coverOf([...fill(180, [8, 8, 10]), ...fill(36, [150, 20, 25])])[0], '#961419');

// A dark navy cover reads as navy rather than black.
assert.equal(coverOf([...fill(150, [12, 12, 12]), ...fill(66, [20, 30, 70])])[0], '#141e46');

// A plain black cover stays black rather than falling back to a generic colour.
assert.equal(coverOf(fill(216, [10, 10, 10]))[0], '#0a0a0a');

// A canvas the browser won't let us read gives no colours, so the spine keeps the default.
assert.equal(coverOf(fill(216, [200, 30, 30]), { tainted: true }), null);

console.log('spine colour checks passed');
