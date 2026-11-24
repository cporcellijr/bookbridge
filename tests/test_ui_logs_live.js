// Logs live mode and auto-refresh poll only while the tab is visible and catch up when it is shown again.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_logs_scripts.html', 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('    function debounce(');
const end = source.indexOf('    // Keyboard shortcuts', start);
assert.ok(start !== -1 && end !== -1, 'live mode script missing');
const script = source.slice(start, end).replace(/\{\{.*?\}\}/g, '""');

const calls = { history: 0, live: 0 };
const intervals = new Map();
let nextTimer = 1;
const element = () => ({ checked: false, style: {}, children: [], listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } });
const visibility = [];
const sandbox = {
  document: { hidden: false, addEventListener: (type, fn) => { if (type === 'visibilitychange') visibility.push(fn); } },
  liveMode: element(), autoRefresh: element(), refreshBtn: element(), logLevel: element(), linesCount: element(),
  searchInput: element(), loadMore: element(), displayedLinesStats: element(), logContent: element(),
  scrollToTop: element(), scrollToBottom: element(),
  fetchLogs: () => { calls.history += 1; return Promise.resolve({ displayed_lines: 0 }); },
  fetchLiveLogs: () => { calls.live += 1; return Promise.resolve({}); },
  setInterval: fn => { intervals.set(nextTimer, fn); return nextTimer++; },
  clearInterval: id => intervals.delete(id),
  setTimeout: () => 0, clearTimeout() {},
  bbPlural: () => '', LINES_SHOWN_LIVE: '', showFilterPending() {}, filterPending: () => false, showNoLogsFound() {},
};
vm.runInNewContext(`let autoRefreshInterval = null, liveRefreshInterval = null, currentOffset = 0;\n${script}\n` +
  'this.toggleLiveMode = toggleLiveMode;', sandbox);
const tick = () => [...intervals.values()].forEach(fn => fn());
const show = hidden => { sandbox.document.hidden = hidden; visibility.forEach(fn => fn()); };

sandbox.liveMode.checked = true;
sandbox.toggleLiveMode();
assert.equal(calls.history, 1);
tick();
assert.equal(calls.live, 1, 'a visible tab polls');

show(true);
tick(); tick(); tick();
assert.equal(calls.live, 1, 'a hidden tab makes no live requests');
assert.equal(calls.history, 1);

show(false);
assert.equal(calls.history, 2, 'showing the tab reloads the history at once');
tick();
assert.equal(calls.live, 2, 'polling resumes');

// With live mode off, returning to the tab fetches nothing extra.
sandbox.liveMode.checked = false;
sandbox.toggleLiveMode();
const history = calls.history;
show(true); show(false);
assert.equal(calls.history, history);
assert.equal(intervals.size, 0);

// Auto-refresh (30 s) also skips hidden tabs and refreshes at once when shown.
sandbox.autoRefresh.checked = true;
sandbox.autoRefresh.listeners.change({ target: sandbox.autoRefresh });
assert.equal(intervals.size, 1);
const before = calls.history;
tick();
assert.equal(calls.history, before + 1, 'a visible tab auto-refreshes');
show(true);
tick(); tick();
assert.equal(calls.history, before + 1, 'a hidden tab skips auto-refresh');
show(false);
assert.equal(calls.history, before + 2, 'showing the tab refreshes at once');
assert.equal(calls.live, 2);

console.log('logs live checks passed');
