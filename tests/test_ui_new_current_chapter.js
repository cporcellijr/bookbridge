const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/js/next/current-chapter.js', 'utf8');

async function scenario(kind) {
    const fields = {
        '[data-chapter-title]': {hidden: true, textContent: ''},
        '[data-chapter-position]': {textContent: ''},
        '[data-chapter-time]': {textContent: ''}
    };
    const panel = {
        hidden: true, dataset: {bookId: 'book-one', of: 'of'},
        querySelector(selector) {return fields[selector];}
    };
    let abort, callback, requested, cleared = false;
    class AbortController {
        constructor() {this.signal = {aborted: false};}
        abort() {this.signal.aborted = true; abort?.();}
    }
    const data = {
        available: true, title: 'Chapter title', index: 2, count: 9,
        elapsed: 75, length: 120, source: 'ABS'
    };
    const fetch = (url, options) => {
        requested = {url, options};
        if (kind === 'timeout') return new Promise((_resolve, reject) => {
            abort = () => reject(new Error('AbortError'));
        });
        if (kind === 'http-error') return Promise.resolve({ok: false});
        return Promise.resolve({
            ok: true, json: () => Promise.resolve(kind === 'unavailable' ? {available: false} : data),
        });
    };
    const context = {
        document: {querySelector(selector) {return selector === '[data-current-chapter]' ? panel : null;}},
        AbortController, fetch, encodeURIComponent,
        setTimeout(handler, delay) {assert.equal(delay, 5000); callback = handler; return 1;},
        clearTimeout(id) {assert.equal(id, 1); cleared = true;},
        console
    };
    vm.runInNewContext(source, context, {filename: 'current-chapter.js'});
    assert.equal(requested.url, '/api/books/book-one/current-chapter');
    assert.equal(panel.hidden, true, 'starts hidden');
    if (kind === 'timeout') {
        callback();
        assert.equal(requested.options.signal.aborted, true, 'timeout aborts request');
    }
    for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(cleared, true, kind + ' clears timer');
    if (kind === 'success') {
        assert.equal(panel.hidden, false, 'successful chapter is shown');
        assert.equal(fields['[data-chapter-title]'].textContent, 'Chapter title');
        assert.equal(fields['[data-chapter-title]'].hidden, false);
        assert.equal(fields['[data-chapter-position]'].textContent, '2 of 9');
        assert.equal(fields['[data-chapter-time]'].textContent, '0:01:15 / 0:02:00');
    } else {
        assert.equal(panel.hidden, true, kind + ' must stay hidden');
        assert.equal(fields['[data-chapter-title]'].textContent, '');
    }
}

(async () => {
    for (const kind of ['success', 'unavailable', 'http-error', 'timeout']) {
        await scenario(kind);
    }
    console.log('current chapter fake DOM: 4/4');
})().catch(error => {console.error(error); process.exitCode = 1;});
