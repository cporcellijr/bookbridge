// The Library has one search box: wide screens move the filter input into the top bar
// (before the Ctrl K launcher); phones keep it in the toolbar. Runs the real script.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('templates/next/_library_scripts.html', 'utf8');
const start = source.indexOf('// One search box:');
const end = source.indexOf('}());', start) + '}());'.length;
assert(start >= 0 && end > start, 'search placement script not found');
const script = source.slice(start, end);

function node(name) {
  return {
    name, children: [], parentElement: null,
    appendChild(child) { return this.insertBefore(child, null); },
    insertBefore(child, before) {
      if (child.parentElement) {
        const siblings = child.parentElement.children;
        siblings.splice(siblings.indexOf(child), 1);
      }
      const at = before ? this.children.indexOf(before) : -1;
      if (before) assert(at >= 0, 'insertBefore reference must be a child');
      this.children.splice(at >= 0 ? at : this.children.length, 0, child);
      child.parentElement = this;
      return child;
    },
  };
}

const home = node('toolbar label');
const slot = node('top bar slot');
const launch = node('Ctrl K launcher');
const input = node('dashboard-search');
slot.appendChild(launch);
home.appendChild(input);

let changeHandler = null;
const media = {
  matches: true,
  addEventListener(type, handler) { assert.equal(type, 'change'); changeHandler = handler; },
};
const document = {
  querySelector(selector) { return selector === '[data-library-search-slot]' ? slot : null; },
  getElementById(id) { return ({'dashboard-search': input, 'bb-search-open': launch})[id] ?? null; },
};
const window = {matchMedia(query) { assert.equal(query, '(min-width: 761px)'); return media; }};

vm.runInNewContext(script, {document, window});
assert.equal(input.parentElement, slot, 'wide screens put the filter in the top bar');
assert.deepEqual(slot.children.map(child => child.name), ['dashboard-search', 'Ctrl K launcher']);
assert.equal(home.children.length, 0);

media.matches = false;
changeHandler();
assert.equal(input.parentElement, home, 'phones keep the filter in the toolbar');
assert.deepEqual(slot.children.map(child => child.name), ['Ctrl K launcher']);

media.matches = true;
changeHandler();
changeHandler();
assert.deepEqual(slot.children.map(child => child.name), ['dashboard-search', 'Ctrl K launcher']);

// Other pages have no slot: nothing moves.
const otherInput = node('dashboard-search');
const otherHome = node('toolbar label');
otherHome.appendChild(otherInput);
vm.runInNewContext(script, {
  document: {querySelector() { return null; }, getElementById() { return otherInput; }},
  window,
});
assert.equal(otherInput.parentElement, otherHome);
console.log('library search placement ok');
