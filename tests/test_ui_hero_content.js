'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const mutations = {
  order: ["const heroOrder = ['synopsis', 'stats', 'streak'];",
          "const heroOrder = ['synopsis', 'streak'];"],
  missing: ['stats.hidden = groups.length === 0;', 'stats.hidden = false;'],
  swap: ["heroSlots = heroSlotRows(row, card);\n                loadHeroContent();",
         "heroSlots = heroSlotRows(row, card);\n                void 0;"],
  safe: ['element.textContent = String(value);', 'element.innerHTML = String(value);'],
  marks: ['known.filter(slot => slot !== leader).map(slot => {', 'known.map(slot => {'],
  position: ['const amount = Math.round(pages * (1 - heroClamp(reading.pct) / 100));',
             'const amount = Math.round(pages);'],
  quiet: ['if (Number(listened.week) > 0 || Number(listened.today) > 0) {', 'if (true) {'],
  pace: ['const daysThisWeek = ((today.getDay() + 6) % 7) + 1;', 'const daysThisWeek = 7;'],
  square: ['if (image.naturalWidth / image.naturalHeight < 0.85) return;', ''],
  repaint: ['heroUpdateSlots(row);', ''],
};
// A Wednesday at noon: three days into the Monday-start week the stats count.
const FIXED_NOW = new Date(2026, 10, 18, 12).getTime();
class FixedDate extends Date {
  constructor(...args) { if (args.length) super(...args); else super(FIXED_NOW); }
  static now() { return FIXED_NOW; }
}
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
  listened: {seconds: 17220, lengthSeconds: 42000, leftSeconds: 24780, week: 12000, today: 2700},
  pages: {read: 96, total: 448, left: 282, week: 41, today: 12},
  streak: {days: 6},
};
const fixtureSlots = [
  {key: 'ebook', linked: true, service: 'BookOrbit', pct: 37, last: false},
  {key: 'audio', linked: true, service: 'BookOrbit', pct: 41, last: true},
  {key: 'koreader', linked: true, service: 'KOReader', pct: 39, last: false},
  {key: 'readalong', linked: false, pct: null, last: false},
];
function element(tag) {
  return {
    tag, className: '', textContent: '', hidden: false, children: [], dataset: {}, attributes: {},
    style: {setProperty(name, value) { this[name] = value; }},
    classList: {add() {}, remove() {}},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    querySelector() { return null; },
  };
}
function coverStub(width, height, complete) {
  const classes = new Set();
  const image = Object.assign(element('img'), {
    className: 'cover-image', isConnected: true, naturalWidth: width, naturalHeight: height, complete,
    addEventListener(event, handler) { this.onload = handler; },
    cloneNode() { return element('img'); },
  });
  const cover = {
    children: [image],
    classList: {add: name => classes.add(name), remove: name => classes.delete(name), has: name => classes.has(name)},
    prepend(node) { this.children.unshift(node); },
    querySelector(selector) {
      if (selector === '.cover-image') return image;
      if (selector === '.hero-cover-backdrop') {
        const backdrop = this.children.find(node => node.className === 'hero-cover-backdrop');
        return backdrop ? Object.assign(backdrop, {remove: () => this.children.splice(this.children.indexOf(backdrop), 1)}) : null;
      }
      return null;
    },
  };
  return {cover, image, classes};
}
function setup(choice, {slots = fixtureSlots, data = fixturePieces, duration = '42000', pages = '448'} = {}) {
  const fill = element('span');
  const track = element('span');
  track.querySelector = name => name === '.hero-track-fill' ? fill : null;
  const ui = {};
  for (const name of ['.hero-synopsis', '.hero-stats', '.hero-moved', '.hero-left', '.hero-legend',
    '.hero-cover', '.hero-title', '.hero-author', '.hero-percent', '.hero-series',
    '.hero-rating', '.hero-phone-rating']) ui[name] = element('span');
  ui['.hero-track'] = track;
  ui['.library-hero-link'] = {href: '/book/a', setAttribute(name, value) {this[name] = value;}};
  ui['.hero-synopsis'].hidden = true;
  ui['.hero-stats'].hidden = true;
  const hero = {
    dataset: {absId: 'a', heroContent: choice, heroSlots: JSON.stringify(slots),
      movementAt: String(Math.floor(FIXED_NOW / 1000) - 7300), markerAt: '100',
      service: 'BookOrbit', leaderSlot: 'audio', duration, pages, percent: '41.6'},
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
    AbortController, Date: FixedDate,
    filterBooks() {}, sortBooks() {}, updateSelectionBar() {},
    bbFormat: (tpl, args) => tpl.replace(/\{(\w+)\}/g, (_, key) => args[key]),
  };
  vm.runInNewContext(code, context);
  return {context, hero, requests, ui, fill, track};
}
const text = node => [node.textContent, ...node.children.map(text)].join('');
const statKeys = fixture => fixture.ui['.hero-stats'].children.map(child => child.className.split(' ').at(-1));
const settled = async () => {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
};

// The real Display controller: the streak needs book stats; nothing else is capped.
const displaySource = fs.readFileSync('static/js/next/display.js', 'utf8');
const displayStart = displaySource.indexOf('function syncHeroChoices(form, value) {');
const displayStop = displaySource.indexOf('function setSelected(form, value) {', displayStart);
assert(displayStart >= 0 && displayStop > displayStart, 'Display checkbox controller missing');
const displayContext = {};
vm.runInNewContext(displaySource.slice(displayStart, displayStop), displayContext);
const checks = ['synopsis', 'stats', 'streak'].map(value => ({value, checked: false, disabled: false}));
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
displayContext.syncHeroChoices(selectionForm, 'synopsis,stats,streak');
assert(checks.every(check => check.checked && !check.disabled), 'All three pieces can be on together');
assert.equal(hiddenValue.value, 'synopsis,stats,streak');
displayContext.syncHeroChoices(selectionForm, 'synopsis,streak');
assert.deepEqual(checks.filter(check => check.disabled).map(check => check.value), ['streak'],
  'Without book stats the streak cannot be chosen');
assert.equal(checks.find(check => check.value === 'streak').checked, false);
assert.equal(hiddenValue.value, 'synopsis', 'A streak without stats is dropped');
displayContext.syncHeroChoices(selectionForm, '');
assert.equal(hiddenValue.value, '', 'Empty selection means the base-only hero');

(async function () {
  // Base hero: painted from first-paint data, no network.
  const base = setup('');
  base.context.loadHeroContent();
  await settled();
  assert.equal(base.requests.length, 0, 'Nothing chosen: no fetch');
  assert.equal(base.ui['.hero-moved'].textContent, 'Listening on BookOrbit · 2h ago');
  assert.equal(base.ui['.hero-left'].textContent, '6h 53m left', 'Time left from the audio position');
  assert.equal(base.fill.style.width, '41.6%');
  assert.equal(base.track.dataset.leader, 'audio');
  const marks = base.track.children.slice(1);
  assert.deepEqual(marks.map(mark => [mark.dataset.slot, mark.style.left]),
    [['ebook', '37%'], ['koreader', '39%']], 'One mark per other app, never the leader');
  const legend = base.ui['.hero-legend'];
  assert.equal(legend.hidden, false);
  assert.deepEqual(legend.children.map(text), ['Audio 41%', 'Ebook 37%', 'KOReader 39%']);
  assert.equal(base.ui['.hero-stats'].hidden, true);
  assert.equal(base.ui['.hero-synopsis'].hidden, true);

  // Book stats: both groups in their own units, plus the streak, plus a finish date.
  const stats = setup('synopsis,stats,streak');
  stats.context.loadHeroContent();
  await settled();
  assert.deepEqual(stats.requests, ['/api/library/hero/a']);
  assert.deepEqual(statKeys(stats), ['hero-stat-listened', 'hero-stat-pages', 'hero-stat-streak']);
  assert.deepEqual(stats.ui['.hero-stats'].children.map(text), [
    'Listening4h 47mof 11h 40m · 6h 53m leftThis week 3h 20m · Today 45m',
    'Reading96 pagesof 448 · about 282 to goThis week 41 · Today 12',
    'Streak6 daysAll books',
  ]);
  // 24780s left at 12000s over three days of this week: seven days from Wednesday.
  const finish = new Date(2026, 10, 25).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
  assert.equal(stats.ui['.hero-left'].textContent, '6h 53m left · done ~' + finish,
    'Finish date from this week\'s daily pace');
  assert.equal(stats.ui['.hero-synopsis'].textContent, fixturePieces.synopsis.text,
    'Synopsis is plain text, never HTML');
  assert.equal(stats.ui['.hero-synopsis'].hidden, false);

  // A quiet week drops the zero line; a one-day streak is singular; no data, no group.
  const quiet = setup('stats,streak', {data: {
    listened: {seconds: 4320, lengthSeconds: 30780, leftSeconds: 24960, week: 0, today: 0},
    streak: {days: 1},
  }});
  quiet.context.loadHeroContent();
  await settled();
  assert.deepEqual(quiet.ui['.hero-stats'].children.map(text), [
    'Listening1h 12mof 8h 33m · 6h 56m left', 'Streak1 dayAll books',
  ]);
  assert.equal(quiet.ui['.hero-left'].textContent, '6h 53m left', 'No pace this week: no finish date');

  // Ebook-only: pages left from where the reader is; one app means no legend.
  const ebook = setup('', {slots: [{key: 'koreader', linked: true, pct: 50, last: true}],
    duration: '0', pages: '400'});
  ebook.context.loadHeroContent();
  await settled();
  assert.equal(ebook.ui['.hero-left'].textContent, '200 pages left');
  assert.equal(ebook.ui['.hero-legend'].hidden, true);
  assert(ebook.ui['.hero-moved'].textContent.startsWith('Reading on BookOrbit'),
    'The leader slot, not the stale data attribute, picks the verb');

  // A quiet poll (no swap) repaints the bar, the time left, the marks and the legend.
  const poll = setup('');
  poll.context.updateHeroProgress({
    abs_id: 'a', last_slot: 'audio', last_slot_client: 'abs', last_slot_service: 'Audiobookshelf',
    last_slot_unix: 200, unified_progress: 50, states: {
      abs: {percentage: 50, last_updated: 3}, bookorbit: {percentage: 45, last_updated: 2},
      kosync: {percentage: 39, last_updated: 1}},
  });
  assert.equal(poll.ui['.hero-percent'].textContent, '50%');
  assert.equal(poll.fill.style.width, '50%');
  assert.equal(poll.ui['.hero-left'].textContent, '5h 50m left');
  assert.equal(poll.ui['.hero-moved'].textContent.split(' · ')[0], 'Listening on Audiobookshelf');
  assert.deepEqual(poll.track.children.slice(1).map(mark => [mark.dataset.slot, mark.style.left]),
    [['ebook', '45%'], ['koreader', '39%']]);
  assert.deepEqual(poll.ui['.hero-legend'].children.map(text), ['Audio 50%', 'Ebook 45%', 'KOReader 39%']);

  // Square art gets the blurred backdrop; portrait art does not; a late thumbnail is fitted on load.
  for (const [width, height, square] of [[600, 600, true], [400, 600, false]]) {
    const fit = setup('');
    const {cover, classes} = coverStub(width, height, true);
    fit.ui['.hero-cover'] = cover;
    fit.context.heroFitCover();
    assert.equal(classes.has('is-square'), square, width + 'x' + height + ' square fit');
    assert.equal(cover.children.length, square ? 2 : 1);
  }
  const late = setup('');
  const pending = coverStub(0, 0, false);
  late.ui['.hero-cover'] = pending.cover;
  late.context.heroFitCover();
  assert.equal(pending.classes.has('is-square'), false, 'Nothing to measure before the thumbnail loads');
  Object.assign(pending.image, {naturalWidth: 720, naturalHeight: 720, complete: true});
  pending.image.onload();
  assert.equal(pending.classes.has('is-square'), true, 'Fitted once the thumbnail arrives');
  assert.equal(pending.cover.children[0].className, 'hero-cover-backdrop');
  late.context.heroFitCover();
  assert.equal(pending.cover.children.filter(node => node.className === 'hero-cover-backdrop').length, 1,
    'Refitting (a swap) never stacks backdrops');

  const empty = setup('synopsis,stats', {data: {}});
  empty.context.loadHeroContent();
  await settled();
  assert.equal(empty.ui['.hero-stats'].hidden, true, 'No pieces: the base hero alone');
  assert.equal(empty.ui['.hero-synopsis'].hidden, true);

  // Swapping books refetches and carries the card's series, rating and length.
  const swap = setup('synopsis,stats');
  swap.context.loadHeroContent();
  await settled();
  const card = {
    dataset: {absId: 'b', title: 'Second book', author: 'Author B', seriesName: 'Saga',
      seriesSequence: '2.0', rating: '3.75', duration: '3600', pages: ''},
    querySelector(selector) {
      if (selector === '.book-open') return {href: '/book/b'};
      if (selector === '.cover-wrap') return {querySelector: () => null};
      return null;
    },
  };
  swap.context.populateHeroFromCard(card, {
    abs_id: 'b', last_slot_unix: 300, unified_progress: 51, last_slot_service: 'Audiobookshelf',
    last_slot: 'audio', states: {abs: {percentage: 51, last_updated: 123}},
  });
  await settled();
  assert.deepEqual(swap.requests, ['/api/library/hero/a', '/api/library/hero/b'],
    'Swapping hero refetches reader-scoped content for the new book');
  assert.equal(swap.hero.dataset.absId, 'b');
  assert.equal(swap.ui['.hero-title'].textContent, 'Second book');
  assert.equal(swap.ui['.hero-series'].textContent, 'Saga #2');
  assert.equal(swap.ui['.hero-rating'].attributes['aria-label'], 'Rated 3.8 of 5');
  assert.equal(swap.ui['.hero-phone-rating'].textContent, '★ 3.8');
  assert.equal(swap.hero.dataset.duration, '3600');
  assert.equal(swap.ui['.library-hero-link'].href, '/book/b');
  console.log('Hero: base paint, marks, stats, quiet week, ebook-only, empty, swap passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
