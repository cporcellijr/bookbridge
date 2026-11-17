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
  // marks: [{sel, n}] draws an amber outline and a numbered badge on each element, for the docs' callouts.
  // badge: 'left' (per shot or per mark) puts the badge beside the element instead of on its top-left corner.
  if (s.marks) console.log(s.name, 'marks ->', await evaluate(`(() => {
    const missing = [];
    ${JSON.stringify(s.marks)}.forEach(({ sel, n, badge = ${JSON.stringify(s.badge || 'corner')} }) => {
      const e = document.querySelector(sel); if (!e) { missing.push(sel); return; }
      const b = e.getBoundingClientRect(), x = b.left + scrollX, y = b.top + scrollY;
      const box = document.createElement('div');
      box.style.cssText = 'position:absolute;z-index:99999;pointer-events:none;border:3px solid #F2B544;border-radius:10px;box-shadow:0 0 0 5px rgba(242,181,68,.2);'
        + 'left:' + (x - 7) + 'px;top:' + (y - 7) + 'px;width:' + (b.width + 14) + 'px;height:' + (b.height + 14) + 'px';
      const dot = document.createElement('div');
      dot.textContent = n;
      dot.style.cssText = 'position:absolute;z-index:100000;pointer-events:none;width:34px;height:34px;border-radius:50%;background:#F2B544;color:#1A1204;'
        + 'font:700 19px/34px "Instrument Sans",system-ui,sans-serif;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.5);'
        + (badge === 'left' ? 'left:' + (x - 50) + 'px;top:' + (y + b.height / 2 - 17) + 'px' : 'left:' + (x - 24) + 'px;top:' + (y - 24) + 'px');
      document.body.append(box, dot);
    });
    return missing.length ? 'MISSING ' + missing.join(' | ') : 'ok';
  })()`));
  if (s.probeOnly) continue;
  // region is in viewport coordinates, so it follows any scrolling the actions did.
  let clip = s.region && { ...s.region, y: s.region.y + await evaluate('scrollY'), scale: 1 };
  if (s.clip) {
    // clipMax: pin the element's top under the header and keep only its first clipMax pixels.
    const scroll = s.clipMax ? 'scrollTo(0, e.getBoundingClientRect().top + scrollY - 90)' : "e.scrollIntoView({block: 'center'})";
    const r = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(s.clip)}); ${scroll}; const b = e.getBoundingClientRect(); return {x: b.x + scrollX, y: b.y + scrollY, width: b.width, height: b.height}; })()`);
    await sleep(800);
    const pad = s.pad ?? 16, padY = s.padY ?? pad;
    clip = { x: r.x - pad, y: r.y - padY, width: r.width + 2 * pad, height: Math.min(r.height + 2 * padY, s.clipMax || Infinity), scale: 1 };
  }
  const shot = await send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip, captureBeyondViewport: true } : {}) });
  writeFileSync(join(outDir, `${s.name}.png`), Buffer.from(shot.result.data, 'base64'));
  console.log('saved', s.name);
}
ws.close(); chrome.kill();
process.exit(0);
