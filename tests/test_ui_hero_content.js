'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const mutations = {
  order: ["const heroOrder = ['synopsis', 'bars', 'listened', 'pages'];",
          "const heroOrder = ['pages', 'listened', 'bars', 'synopsis'];"],
  missing: ["if (!data || (Array.isArray(data) && !data.length)) return;",
            "if (!data) return;"],
  swap: ["heroSlots = heroSlotRows(row, card);\n                loadHeroContent();",
         "heroSlots = heroSlotRows(row, card);\n                void 0;"],
  safe: ["element.textContent = String(value);", "element.innerHTML = String(value);"],
};
const mutation = process.env.P16D_MUTATE;
if (mutation) {
  assert(mutations[mutation], 'Unknown mutation: ' + mutation);
  const [before, after] = mutations[mutation];
  assert(source.includes(before), 'Missing mutation target: ' + mutation);
  source = source.replace(before, after);
}
const start = source.indexOf("const heroRoot = document.getElementById('library-hero');");
const stop = source.indexOf('let refreshPaused = false;', start);
assert(start >= 0 && stop > start, 'Cannot find actual hero lifecycle');
const code = source.slice(start, stop).replace(
  /\{\{\s*_\('([^']*)'\)\|tojson\s*\}\}/g, (_, value) => JSON.stringify(value)
);

const fixturePieces = {
  synopsis: {text: '<script>Just untrusted plain text</script> & an excellent book'},
  listened: {seconds: 9000, lengthSeconds: 36000, leftSeconds: 27000, week: 3600, today: 300},
  pages: {read: 91, total: 280, left: 189, week: 27, today: 8},
};
const fixtureSlots = [
  {key: 'ebook', linked: true, service: 'Grimmory', pct: 24, last: false},
  {key: 'audio', linked: true, service: 'Audiobookshelf', pct: 25, last: true},
  {key: 'readalong', linked: false, pct: null, last: false},
];
function element(tag) {
  return {
    tag, className: '', textContent: '', hidden: false, style: {}, children: [], dataset: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    querySelector() { return null; },
  };
}
function setup(choice, slots = fixtureSlots, data = fixturePieces) {
  const content = element('div');
  content.hidden = true;
  const cover = element('div');
  const mini = element('span');
  const fill = element('span');
  mini.querySelector = name => name === 'span' ? fill : null;
  const link = {href: '/book/a', setAttribute(name, value) {this[name] = value;}};
  const title = element('h2');
  const author = element('span');
  const pct = element('span'); pct.textContent = '25.0%';
  const service = element('span');
  const ui = {
    '.hero-content': content, '.hero-mini-track': mini, '.hero-cover': cover,
    '.library-hero-link': link, '.hero-title': title, '.hero-author': author,
    '.hero-percent': pct, '.hero-service': service,
  };
  const hero = {
    dataset: {absId: 'a', heroContent: choice, heroSlots: JSON.stringify(slots),
      movementAt: '100', markerAt: '100'},
    hidden: false,
    querySelector(name) { return ui[name] || null; },
  };
  const requests = [];
  const context = {
    document: {getElementById: () => hero, querySelectorAll: () => [], createElement: element},
    window: {matchMedia: () => ({matches: false}), requestAnimationFrame() {},
      addEventListener() {}, setTimeout() {}},
    fetch(url) {
      requests.push(url);
      const id = decodeURIComponent(url.split('/').pop());
      return Promise.resolve({ok: true, json: () => Promise.resolve({absId: id, pieces: data})});
    },
    AbortController,
    filterBooks() {}, sortBooks() {}, updateSelectionBar() {},
    bbFormat: (tpl, args) => tpl.replace(/\{(\w+)\}/g, (_, key) => args[key]),
  };
  vm.runInNewContext(code, context);
  return {context, hero, content, requests, ui, fill};
}
const classes = fixture => fixture.content.children.map(child => child.className.split(' ').at(-1).replace('hero-piece-', ''));
const settled = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
};
// Check the real Display controller disables a third choice and re-enables
// choices when a reader unticks an existing one.
const displaySource = fs.readFileSync('static/js/next/display.js', 'utf8');
const displayStart = displaySource.indexOf('function syncHeroChoices(form, value) {');
const displayStop = displaySource.indexOf('function setSelected(form, value) {', displayStart);
assert(displayStart >= 0 && displayStop > displayStart, 'Display checkbox controller missing');
const displayContext = {};
vm.runInNewContext(displaySource.slice(displayStart, displayStop), displayContext);
const checks = ['synopsis', 'bars', 'listened', 'pages'].map(value => ({
  value, checked: false, disabled: false,
}));
const hiddenValue = {value: ''};
const selectionForm = {
  querySelectorAll(selector) {
    assert.equal(selector, '[data-hero-piece]');
    return checks;
  },
  querySelector(selector) {
    return selector === 'input[name="UI_HERO_CONTENT"]' ? hiddenValue : null;
  },
};
displayContext.syncHeroChoices(selectionForm, 'bars,listened');
assert.deepEqual(checks.filter(check => check.disabled).map(check => check.value),
  ['synopsis', 'pages'], 'Third and fourth checkboxes cannot be selected');
assert.equal(hiddenValue.value, 'bars,listened');
displayContext.syncHeroChoices(selectionForm, 'bars');
assert(checks.every(check => !check.disabled), 'Removing one choice re-enables all');
displayContext.syncHeroChoices(selectionForm, '');
assert.equal(hiddenValue.value, '', 'Empty selection means the base-only hero');

(async function () {
  const canonical = ['synopsis', 'bars', 'listened', 'pages'];
  const pairs = [];
  for (let i = 0; i < canonical.length; i++) {
    for (let j = i + 1; j < canonical.length; j++) {
      pairs.push([canonical[i], canonical[j]]);
    }
  }
  for (const chosen of [...pairs, ...canonical.map(key => [key]), []]) {
    const fixture = setup(chosen.join(','));
    fixture.context.loadHeroContent();
    await settled();
    assert.equal(fixture.hero.dataset.absId, 'a', 'Always keeps base book id');
    assert.equal(fixture.requests.length, chosen.some(key => key !== 'bars') ? 1 : 0,
      'Network is used only when a data piece needs it');
    assert.deepEqual(classes(fixture), chosen, 'Canonical selection renders in order');
    assert.equal(fixture.content.hidden, chosen.length === 0, 'Empty hero has no content');
    assert.equal(fixture.content.dataset.visibleCount, String(chosen.length));
    if (chosen.includes('bars')) {
      const bar = fixture.content.children.find(piece => piece.className.includes('hero-piece-bars'));
      assert.equal(bar.children.filter(row => row.className.includes('hero-app-row')).length, 2);
      const leader = bar.children.find(row => row.className.includes('is-leader'));
      assert(leader && leader.children[2].children[0].style.width === '25%',
        'Leader bar keeps its own accent position');
    } else {
      assert.equal(fixture.ui['.hero-mini-track'].hidden, false,
        'When bars omitted, leader mini-track remains');
      assert.equal(fixture.fill.style.width, '25%');
    }
    if (chosen.includes('synopsis')) {
      const paragraph = fixture.content.children[0].children.at(-1);
      assert.equal(paragraph.textContent, fixturePieces.synopsis.text,
        'Synopsis must only be rendered as plain text, never HTML');
    }
  }
  const empty = setup('bars,pages', [], {});
  empty.context.loadHeroContent();
  await settled();
  assert.equal(empty.content.hidden, true, 'Empty pieces leave only P16c base');

  const swap = setup('synopsis,bars');
  swap.context.loadHeroContent();
  await settled();
  const card = {
    dataset: {absId: 'b', title: 'Second book', author: 'Author B'},
    querySelector(selector) {
      if (selector === '.book-open') return {href: '/book/b'};
      if (selector === '.cover-wrap') return {querySelector: () => null};
      return null;
    },
  };
  swap.context.populateHeroFromCard(card, {
    abs_id: 'b', last_slot_unix: 300, unified_progress: 51,
    last_slot: 'audio', states: {abs: {percentage: 51, last_updated: 123}},
  });
  await settled();
  assert.deepEqual(swap.requests,
    ['/api/library/hero/a', '/api/library/hero/b'],
    'Swapping hero refetches reader-scoped content for the new book');
  assert.equal(swap.hero.dataset.absId, 'b');
  assert.equal(swap.ui['.hero-title'].textContent, 'Second book');
  assert.equal(swap.ui['.library-hero-link'].href, '/book/b');
  assert.equal(swap.content.hidden, false);
  console.log('P16d: six pairings, four single choices, empty, safe text, missing data, refetch passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
