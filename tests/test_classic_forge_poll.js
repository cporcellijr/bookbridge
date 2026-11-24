// Classic Library's Forge banner polls only while the tab is visible and checks at once when shown.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/index.html', 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('            // Poll every 5s');
const end = source.indexOf('</script>', start);
assert.ok(start !== -1 && end !== -1, 'Forge poll script missing');
const script = source.slice(start, end);

let checks = 0;
const intervals = [];
const visibility = [];
const sandbox = {
  checkForgeStatus: () => { checks += 1; },
  setInterval: fn => { intervals.push(fn); return intervals.length; },
  document: {
    hidden: false,
    addEventListener: (type, fn) => { if (type === 'visibilitychange') visibility.push(fn); },
  },
};
vm.runInNewContext(script, sandbox);
const tick = () => intervals.forEach(fn => fn());
const show = hidden => { sandbox.document.hidden = hidden; visibility.forEach(fn => fn()); };

tick();
assert.equal(checks, 1, 'a visible tab polls');
show(true);
tick(); tick(); tick();
assert.equal(checks, 1, 'a hidden tab makes no Forge requests');
show(false);
assert.equal(checks, 2, 'showing the tab checks at once');

console.log('forge poll checks passed');
