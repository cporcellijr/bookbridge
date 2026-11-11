"""Execute the real New Library poller (static/js/next/poller.js) under fake timers."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function env() {
    let now = 0, nextId = 1;
    const timers = new Map(), listeners = [];
    const document = { hidden: false,
        addEventListener(n, f) { if (n === 'visibilitychange') listeners.push(f); },
        removeEventListener(n, f) { const i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1); } };
    const scope = { document, window: {}, Promise, Date: { now: () => now },
        setTimeout(fn, ms) { const id = nextId++; timers.set(id, { fn, at: now + ms }); return id; },
        clearTimeout(id) { timers.delete(id); } };
    scope.window = scope;
    vm.runInNewContext(fs.readFileSync('static/js/next/poller.js', 'utf8'), scope);
    return {
        scope, document, timers, listeners,
        async advance(ms) {
            const end = now + ms;
            for (;;) {
                const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                now = due[1].at; timers.delete(due[0]); due[1].fn();
                for (let i = 0; i < 20; i++) await Promise.resolve();
            }
            now = end;
        },
        async flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); },
        setHidden(h) { document.hidden = h; listeners.slice().forEach(f => f()); }
    };
}
const make = (e, over) => { const calls = { n: 0, busy: false };
    const p = e.scope.bbPoller(Object.assign({ name: 'x', fastMs: 5000, slowMs: 30000, graceMs: 60000,
        run: () => { calls.n++; return calls.busy; } }, over));
    return { p, calls }; };
(async () => {
%BODY%
})().catch(e => { console.error(e); process.exitCode = 1; });
"""

BODIES = {
    "idle_uses_slow_interval": """
    const e = env(); const { calls } = make(e);
    await e.flush(); assert.equal(calls.n, 1);
    await e.advance(29000); assert.equal(calls.n, 1);
    await e.advance(1000); assert.equal(calls.n, 2);
    await e.advance(60000); assert.equal(calls.n, 4);
    """,
    "active_uses_fast_interval_then_backs_off": """
    const e = env(); const { calls } = make(e); calls.busy = true;
    await e.flush(); await e.advance(30000);
    assert.equal(calls.n, 7, 'initial + 6 fast ticks in 30s');
    calls.busy = false; await e.advance(5000);
    const after = calls.n; await e.advance(20000);
    assert.equal(calls.n, after, 'idle result backs off');
    """,
    "hidden_makes_no_requests_and_visible_refreshes_at_once": """
    const e = env(); const { calls } = make(e); await e.flush();
    e.setHidden(true); const before = calls.n;
    await e.advance(300000); assert.equal(calls.n, before);
    e.setHidden(false); await e.flush(); assert.equal(calls.n, before + 1);
    await e.advance(30000); assert.equal(calls.n, before + 2);
    """,
    "kick_checks_now_and_goes_fast": """
    const e = env(); const { p, calls } = make(e); await e.flush();
    p.kick(); await e.flush(); assert.equal(calls.n, 2);
    await e.advance(5000); assert.equal(calls.n, 3);
    await e.advance(5000); assert.equal(calls.n, 4);
    await e.advance(60000); const n = calls.n;
    await e.advance(19000); assert.equal(calls.n, n, "grace expired: slow again");
    """,
    "double_init_leaves_one_timer": """
    const e = env(); make(e); await e.flush(); const { calls } = make(e); await e.flush();
    assert.equal(e.timers.size, 1); assert.equal(e.listeners.length, 1);
    await e.advance(30000); assert.equal(calls.n, 2);
    """,
    "delayed_start_does_not_fetch_immediately": """
    const e = env(); const { calls } = make(e, { immediate: false });
    await e.flush(); assert.equal(calls.n, 0);
    await e.advance(30000); assert.equal(calls.n, 1);
    """,
    "stuck_request_does_not_stop_polling": """
    const e = env(); let calls = 0, release;
    const stuck = new Promise(r => { release = r; });
    e.scope.bbPoller({ name: 'x', fastMs: 5000, slowMs: 30000, run: () => { calls++; return calls === 1 ? stuck : false; } });
    await e.flush(); assert.equal(calls, 1);
    await e.advance(600000);
    assert.ok(calls >= 2, 'watchdog must free the poller; got ' + calls);
    assert.equal(e.timers.size, 1, 'exactly one timer');
    e.setHidden(true); e.setHidden(false); await e.flush();
    const n = calls; e.scope.__bbPollers.x.kick(); await e.flush();
    assert.equal(calls, n + 1, 'kick works after a stuck run');
    release(true); await e.flush();
    assert.equal(e.timers.size, 1, 'late result neither double-schedules nor flips state');
    """,
    "stuck_request_is_aborted": """
    const e = env(); let signal;
    e.scope.AbortController = AbortController;
    e.scope.bbPoller({ name: 'x', fastMs: 5000, slowMs: 30000, timeoutMs: 20000, run: s => { signal = signal || s; return new Promise(() => {}); } });
    await e.flush(); assert.equal(signal.aborted, false);
    await e.advance(20000); assert.equal(signal.aborted, true);
    """,
    "failed_run_keeps_active_state": """
    const e = env(); let mode = 'busy', calls = 0;
    e.scope.bbPoller({ name: 'x', fastMs: 5000, slowMs: 30000, run: () => {
        calls++; if (mode === 'busy') return true; if (mode === 'reject') return Promise.reject(new Error('x')); return undefined; } });
    await e.flush(); await e.advance(5000); assert.equal(calls, 2);
    mode = 'reject'; await e.advance(5000); assert.equal(calls, 3);
    await e.advance(5000); assert.equal(calls, 4, 'still on the fast cadence after a rejection');
    mode = 'undefined'; await e.advance(5000); assert.equal(calls, 5);
    await e.advance(5000); assert.equal(calls, 6, 'non-boolean result keeps active');
    """,
    "forge_check_failure_is_not_reported_idle": """
    const src = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
    const a = src.indexOf('function checkForgeStatus('), b = src.indexOf('\\n            }', a);
    const code = src.slice(a, b + 14).replace(/\\{%[\\s\\S]*?%\\}/g, '').replace(/\\{\\{[\\s\\S]*?\\}\\}/g, '""');
    const ctx = { document: { getElementById: () => ({ style: {} }) }, console, bbFormat: s => s, bbPlural: s => s,
        fetch: () => Promise.reject(new Error('net')) };
    vm.createContext(ctx); vm.runInContext(code, ctx);
    const result = await ctx.checkForgeStatus();
    assert.notEqual(typeof result, 'boolean', 'a failed check must not report idle');
    ctx.fetch = () => Promise.resolve({ json: async () => ['t'] });
    assert.equal(await ctx.checkForgeStatus(), true);
    """,
}


@unittest.skipUnless(shutil.which("node"), "Node.js required for JS behavioral check")
class TestNextPoller(unittest.TestCase):
    def _run(self, name):
        script = HARNESS.replace("%BODY%", BODIES[name])
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                                text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


for _name in BODIES:
    setattr(TestNextPoller, "test_" + _name, lambda self, n=_name: self._run(n))


class TestLibraryPollingWiring(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "templates/next/_library_scripts.html").read_text(encoding="utf-8")

    def test_no_raw_interval_and_pollers_registered(self):
        self.assertNotIn("setInterval(checkForgeStatus", self.src)
        self.assertIn("name: 'forge'", self.src)
        self.assertIn("name: 'library-progress'", self.src)
        self.assertIn("poller.js", (ROOT / "templates/next/index.html").read_text(encoding="utf-8"))

    def test_progress_refresh_uses_one_index_not_per_book_selector(self):
        self.assertNotIn("document.querySelectorAll(`.book-card[data-abs-id=", self.src)
        self.assertIn("cardsById", self.src)
