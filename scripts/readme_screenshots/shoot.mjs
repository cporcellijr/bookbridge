// Headless Chrome screenshots of harness.py, written straight into docs/assets/screenshots.
// Usage: node shoot.mjs [outDir] [shots.json]. Each shot may set display settings first
// (pre), run page actions, and clip to one element. The first shot waits for thumbnails.
// shortcut: Chrome path is the Windows default install; pass CHROME=... to run elsewhere.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outDir = process.argv[2] || resolve(here, '../../docs/assets/screenshots');
const shotsFile = process.argv[3] || join(here, 'shots.json');
const shots = JSON.parse(readFileSync(shotsFile, 'utf8'));
const BASE = 'http://127.0.0.1:5799';
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), 'bbshot-'))}`,
  '--hide-scrollbars', '--no-first-run', '--no-default-browser-check', 'about:blank',
], { stdio: 'ignore' });

let wsUrl;
for (let i = 0; i < 50 && !wsUrl; i++) {
  try { wsUrl = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page')?.webSocketDebuggerUrl; } catch { await sleep(200); }
}
const ws = new WebSocket(wsUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));
let id = 0; const pending = new Map();
ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise(r => { const n = ++id; pending.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
const evaluate = async expr => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;

await send('Page.enable');
await send('Page.navigate', { url: `${BASE}/login` }); await sleep(1500);
await evaluate(`fetch('/login', {method: 'POST', body: new URLSearchParams({username: 'demo', password: 'demo'})}).then(r => r.status)`);

for (const s of shots) {
  await send('Emulation.setDeviceMetricsOverride', { width: s.width, height: s.height, deviceScaleFactor: s.dsf || 1, mobile: !!s.mobile });
  await send('Emulation.setTouchEmulationEnabled', { enabled: !!s.mobile });
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: s.theme || 'dark' }] });
  for (const js of s.pre || []) console.log(s.name, 'pre ->', await evaluate(js));
  await send('Page.navigate', { url: BASE + s.path }); await sleep(s.wait || 2500);
  for (const js of s.actions || []) { console.log(s.name, 'action ->', await evaluate(js)); await sleep(s.actionWait || 1500); }
  let clip;
  if (s.clip) {
    const r = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(s.clip)}); e.scrollIntoView({block: 'center'}); const b = e.getBoundingClientRect(); return {x: b.x + scrollX, y: b.y + scrollY, width: b.width, height: b.height}; })()`);
    await sleep(800);
    const pad = s.pad ?? 16;
    clip = { x: r.x - pad, y: r.y - pad, width: r.width + 2 * pad, height: r.height + 2 * pad, scale: 1 };
  }
  const shot = await send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip, captureBeyondViewport: true } : {}) });
  writeFileSync(join(outDir, `${s.name}.png`), Buffer.from(shot.result.data, 'base64'));
  console.log('saved', s.name);
}
ws.close(); chrome.kill();
process.exit(0);
