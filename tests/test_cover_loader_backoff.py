"""Execute the real New Library cover loader against bounded 202 responses."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required for JS behavioral check")
class TestCoverLoaderBackoff(unittest.TestCase):
    def test_bounded_202_retries_share_one_probe_and_fall_back(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const imgs = [0,1].map(() => {
    const img = {
        dataset: { thumbUrl: '/covers/thumb/book.jpg' },
        hidden: true, isConnected: true,
        addEventListener(event, callback) {
            if (event === 'load') this.onload = callback;
        },
        closest() { return this; }
    };
    Object.defineProperty(img, 'src', { set(src) {
        this.loadedSrc = src;
        this.onload?.();
    }});
    return img;
});
const library = { querySelectorAll() { return imgs; } };
let probes = 0;
const delays = [];
const document = {
    hidden: false,
    getElementById(id) { return id === 'library-page' ? library : null; },
    addEventListener() {}, removeEventListener() {}
};
const scope = {
    document, window: { fetch: true }, Promise, Map,
    fetch: async () => { probes++; return { status: 202 }; },
    setTimeout(fn, delay) { delays.push(delay); queueMicrotask(fn); }
};
vm.runInNewContext(fs.readFileSync(
    'static/js/next/cover-loader.js','utf8'),scope);
(async () => {
    for (let i=0; i<1000 && imgs.some(x => !x.loadedSrc); i++)
        await new Promise(resolve => setImmediate(resolve));
    assert.equal(probes, 15, 'duplicate URLs should share readiness probes');
    assert.equal(delays[0], 75, 'initial readiness delay should be short');
    assert.ok(delays.every(x => x <= 1800), 'backoff must stay bounded');
    assert.deepEqual(imgs.map(x => x.loadedSrc),
                     ['/covers/book.jpg', '/covers/book.jpg']);
})().catch(e => { console.error(e); process.exitCode=1; });
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=root, capture_output=True,
            text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_remembered_thumb_skips_head_but_stale_hint_falls_back(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function scenario(missing) {
    let headCalls=0, removed=false, providerCalls=0;
    const visited=[];
    const thumb='/covers/thumb/book.jpg';
    const img = {
        dataset:{thumbUrl:thumb},hidden:true,isConnected:true,
        onerror(){providerCalls++},
        addEventListener(name,callback){if(name==='load')this.onload=callback;},
        closest(){return this;}
    };
    Object.defineProperty(img,'src',{set(value){
        visited.push(value);
        if(missing && value===thumb) this.onerror?.();
        else this.onload?.();
    }});
    const library={querySelectorAll(){return [img]}};
    const storage={
        getItem(key){return key.endsWith(thumb)?String(Date.now()):null;},
        setItem(){},
        removeItem(){removed=true;}
    };
    const scope={
        document:{hidden:false,getElementById(){return library;},
                  addEventListener(){},removeEventListener(){}},
        window:{fetch:true,sessionStorage:storage},
        fetch:async()=>{headCalls++;return {status:200}},
        Promise,Map,Date,setTimeout
    };
    vm.runInNewContext(fs.readFileSync('static/js/next/cover-loader.js','utf8'),scope);
    for(let i=0;i<1000&&!visited.length;i++)
        await new Promise(r=>setImmediate(r));
    assert.equal(headCalls,0,'recent thumbnail must avoid a redundant HEAD');
    if(missing){
        assert.deepEqual(visited,[thumb,'/covers/book.jpg']);
        assert.equal(removed,true,'discard stale readiness hint');
    }else{
        assert.deepEqual(visited,[thumb]);
        assert.equal(removed,false);
    }
    assert.equal(img.hidden,false);
    assert.equal(providerCalls,0);
}
(async()=>{await scenario(false);await scenario(true)})()
    .catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=root, capture_output=True,
            text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_cover_goes_to_provider_fallback_without_fetching_original(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const events = [];
const img = {
    dataset: { thumbUrl: '/covers/thumb/gone.jpg' }, hidden: true, isConnected: true,
    addEventListener() {}, closest() { return this; },
    dispatchEvent(event) { events.push(event.type); },
};
Object.defineProperty(img, 'src', { set(src) { this.loadedSrc = src; } });
const document = {
    hidden: false,
    getElementById(id) { return id === 'library-page' ? { querySelectorAll() { return [img]; } } : null; },
    addEventListener() {}, removeEventListener() {}
};
let probes = 0;
const scope = {
    document, window: { fetch: true }, Promise, Map,
    Event: class { constructor(type) { this.type = type; } },
    fetch: async () => { probes++; return { status: 404 }; },
    setTimeout(fn) { queueMicrotask(fn); }
};
vm.runInNewContext(fs.readFileSync('static/js/next/cover-loader.js','utf8'),scope);
(async () => {
    for (let i=0; i<50; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(probes, 1, 'a missing cover is probed once');
    assert.deepEqual(events, ['error'], 'the provider onerror fallback runs');
    assert.equal(img.loadedSrc, undefined, 'the original cover is never requested');
})().catch(e => { console.error(e); process.exitCode=1; });
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=root, capture_output=True,
            text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
