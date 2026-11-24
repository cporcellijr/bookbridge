const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

class Element {
    constructor() { this.children = []; this.events = {}; this.isConnected = true; }
    addEventListener(name, callback) { this.events[name] = callback; }
    setAttribute(name, value) { this[name] = value; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    replaceChildren(...children) { this.children = children; }
    focus() { this.focused = true; }
    click() { this.events.click(); }
}
class Dialog extends Element {
    constructor() { super(); this.nodes = new Map(); this.open = false; this.openCount = 0; }
    querySelector(key) { if (!this.nodes.has(key)) this.nodes.set(key, new Element()); return this.nodes.get(key); }
    querySelectorAll() { return []; }
    showModal() { this.open = true; this.openCount++; }
    close(value = '') { this.open = false; this.returnValue = value; this.events.close(); }
}
const labels = { loadError: 'Could not load editions', noFiles: 'No editions', emptySelection: 'Choose', text: 'Text EPUB', epub: 'EPUB', readAlong: 'Read-along', primary: 'Primary' };
const files = [{ id: 10, filename: 'text.epub', primary: false }, { id: 20, filename: 'readalong.epub', primary: true, media_overlay: true }];
const response = (rows) => ({ ok: true, json: async () => ({ files: rows }) });
function setup(fetcher = async () => response(files)) {
    const context = { window: {}, document: { createElement: () => new Element() }, AbortController };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/provider-edition-picker.js'), 'utf8'), context);
    const dialog = new Dialog();
    const Picker = context.window.BookBridgeEditionPicker;
    return { dialog, Picker, picker: new Picker(dialog, labels, fetcher) };
}
const options = () => ({ source: 'BookOrbit', sourceId: 7, title: 'A book', opener: new Element() });
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('single compatible file selects directly, without opening a dialog', async () => {
    const { picker, dialog } = setup(async () => response([files[0]]));
    assert.equal((await picker.choose(options())).id, 10);
    assert.equal(dialog.openCount, 0);
});
test('multiple editions require explicit confirmation and return the chosen ID', async () => {
    const { picker, dialog } = setup();
    const result = picker.choose(options());
    await tick();
    assert.equal(dialog.open, true);
    assert.equal(dialog.querySelector('[data-edition-confirm]').disabled, true);
    const rows = dialog.querySelector('[data-edition-options]').children;
    assert.equal(rows.length, 2);
    const radio = rows[1].children[2];
    radio.checked = true;
    radio.events.change();
    dialog.querySelector('[data-edition-confirm]').click();
    assert.equal((await result).id, 20);
});
test('Escape/cancel does not commit a highlighted file and restores focus', async () => {
    const { picker, dialog } = setup();
    const args = { ...options(), currentFileId: 20 };
    const result = picker.choose(args);
    await tick();
    assert.equal(picker.selected.id, 20);
    dialog.close();
    assert.equal(await result, null);
    assert.equal(args.opener.focused, true);
});
test('a stale metadata response cannot override a newer selection', async () => {
    const pending = [];
    const { picker } = setup(() => new Promise((resolve) => pending.push(resolve)));
    const old = picker.choose(options());
    const current = picker.choose({ ...options(), sourceId: 8 });
    pending[1](response([files[1]]));
    assert.equal((await current).id, 20);
    pending[0](response([files[0]]));
    assert.equal(await old, null);
});
test('failed/empty lookups never silently select the primary', async () => {
    const failed = setup(async () => ({ ok: false }));
    await assert.rejects(failed.picker.choose(options()), /Could not load editions/);
    const empty = setup(async () => response([]));
    await assert.rejects(empty.picker.choose(options()), /No editions/);
});
test('comic files cannot silently turn an audiobook match into ebook-only', async () => {
    const { picker, dialog } = setup(async () => response([...files.slice(0, 1), { id: 30, filename: 'comic.cbz' }]));
    assert.equal((await picker.choose({ ...options(), allowComics: false })).id, 10);
    assert.equal(dialog.openCount, 0);
});
test('one book card per provider entry; local files and other providers remain distinct', () => {
    const { Picker } = setup();
    const original = [
        { source: 'BookOrbit', source_id: 7, score: 60 },
        { source: 'BookOrbit', source_id: 7, score: 90, linked: true },
        { source: 'Booklore', source_id: 7 },
        { source: 'Grimmory', source_id: '7' },
        { source: 'Local File', ebook_filename: 'text.epub' },
    ];
    const grouped = Picker.groupMatches(original);
    assert.equal(grouped.length, 3);
    assert.equal(grouped[0].score, 90);
    assert.equal(grouped[0].linked, true);
    assert.equal(original[0].linked, undefined);
});
