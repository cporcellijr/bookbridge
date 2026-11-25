// Shelf props: bookends around the visible books, decor every few books, and a clean re-place.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// A small DOM: enough tree moves (before/after/append/replaceWith) for piles of real books.
function element(classes) {
    const vars = {};
    const node = {
        parent: null, children: [], dataset: {}, hidden: false, attrs: {},
        classList: {items: new Set(classes), contains(name) { return this.items.has(name); }},
        style: {display: '', vars, setProperty(name, value) { vars[name] = value; },
            getPropertyValue(name) { return vars[name] || ''; }, removeProperty(name) { delete vars[name]; }},
        setAttribute(name, value) { this.attrs[name] = value; },
        get nextElementSibling() { return this.parent ? this.parent.children[this.parent.children.indexOf(this) + 1] || null : null; },
        remove() {
            if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
            this.parent = null;
        },
        before(...nodes) { nodes.forEach(other => { other.remove(); other.parent = this.parent; this.parent.children.splice(this.parent.children.indexOf(this), 0, other); }); },
        after(...nodes) { nodes.reverse().forEach(other => { other.remove(); other.parent = this.parent; this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, other); }); },
        appendChild(other) { other.remove(); other.parent = this; this.children.push(other); return other; },
        replaceWith(...nodes) { this.before(...nodes); this.remove(); },
    };
    Object.defineProperty(node, 'className', {set(value) { node.classList.items = new Set(value.split(' ')); }});
    return node;
}

const grid = element([]);
for (let i = 0; i < 20; i++) {
    const card = element(['book-card']);
    card.dataset.id = String(i);
    grid.appendChild(card);
}
grid.children[4].style.display = 'none';
grid.children[9].classList.items.add('hidden');

const window = {};
const document = {createElement() { return element([]); }};
vm.runInNewContext(fs.readFileSync('static/js/next/shelf-props.js', 'utf8'), {window, document, Array, Math, parseFloat});
const layout = () => grid.children.map(node => node.classList.contains('shelf-prop') ? node.dataset.prop
    : node.classList.contains('shelf-stack') ? '[' + node.children.map(book => book.dataset.id).join(',') + ']' : node.dataset.id);

window.bbShelfProps.place(grid);
const first = layout();
assert.equal(first[0], 'bookend-start', 'a bookend opens the shelf');
assert.equal(first[first.length - 1], 'bookend-end', 'a bookend closes it');
// 18 visible books (4 and 9 are hidden): decor after the 3rd, 10th and 15th visible book (gaps 3, 7, 5).
// The stack is the next three real books (12-14) lying in a pile, in their sorted place.
assert.deepEqual(first, ['bookend-start', '0', '1', '2', 'plant', '3', '4', '5', '6', '7', '8', '9', '10', '11',
    '[12,13,14]', '15', '16', 'candle', '17', '18', '19', 'bookend-end']);
assert.equal(grid.children.find(node => node.dataset.prop).attrs['aria-hidden'], 'true');
assert.equal(grid.children.find(node => node.classList.contains('shelf-stack')).attrs['aria-hidden'], undefined,
    'a pile holds real links, so it is never hidden from assistive tech');

window.bbShelfProps.place(grid);
assert.deepEqual(layout(), first, 'placing again replaces the props instead of adding more');

window.bbShelfProps.clear(grid);
assert.deepEqual(layout(), Array.from({length: 20}, (_, i) => String(i)), 'clear unwraps the pile back into place');
assert.equal(grid.children[13].parent, grid);
grid.children.reverse();
window.bbShelfProps.place(grid);
assert.equal(layout()[1], '19', 'props follow the new book order');

// The reader's choices: only candles, no bookends, the most props (gaps 1, 3, 2, 3, 2).
const page = {dataset: {shelfPropKinds: 'candle', shelfPropAmount: '5'}};
grid.closest = selector => (selector === '.library-page' ? page : null);
window.bbShelfProps.clear(grid);
grid.children.sort((a, b) => Number(a.dataset.id) - Number(b.dataset.id));
grid.children.forEach(card => { card.hidden = false; card.style.display = ''; card.classList.items.delete('hidden'); });
window.bbShelfProps.place(grid);
assert.deepEqual(layout(), ['0', 'candle', '1', '2', '3', 'candle', '4', '5', 'candle', '6', '7', '8', 'candle',
    '9', '10', 'candle', '11', 'candle', '12', '13', '14', 'candle', '15', '16', 'candle', '17', '18', '19'],
    'only the chosen trinket, at the chosen amount, and no bookends');
page.dataset = {shelfPropKinds: 'bookends,plant,candle', shelfPropAmount: '2'};
window.bbShelfProps.place(grid);
const decor = () => layout().filter(item => !/^\d+$/.test(item));
assert.deepEqual(decor(), ['bookend-start', 'plant', 'candle', 'bookend-end'], 'gaps 5, 10 cycle only the chosen kinds');
assert.equal(layout().indexOf('candle'), 17, 'second decor after the 15th book');
page.dataset.shelfPropAmount = '1';
window.bbShelfProps.place(grid);
assert.deepEqual(decor(), ['bookend-start', 'plant', 'bookend-end'], 'fewest: one decor in 20 books');
assert.equal(layout().indexOf('plant'), 9, 'after the 8th book');
page.dataset = {shelfPropKinds: '', shelfPropAmount: '3'};
window.bbShelfProps.place(grid);
assert.equal(grid.children.length, 20, 'no trinkets chosen means a bare shelf');

// Real-book piles: only adjacent standing spines, never a face-out reading book, never the last book.
page.dataset = {shelfPropKinds: 'stack', shelfPropAmount: '5'};
grid.children[9].dataset.reading = '';
grid.children[5].style.setProperty('--spine-w', '50px');
grid.children[6].style.setProperty('--spine-w', '36px');
grid.children[6].style.setProperty('--spine-h', '200px');
window.bbShelfProps.place(grid);
assert.deepEqual(layout(), ['0', '[1,2,3]', '4', '[5,6]', '7', '8', '9', '10', '[11,12,13]', '14', '[15,16]', '17', '18', '19']);
const pair = grid.children[3];
assert.equal(pair.style.vars['--stack-w'], '214px', 'as wide as its longest book plus its nudge');
assert.deepEqual([grid.children[3].children[0].style.vars, grid.children[3].children[1].style.vars].map(v => [v['--stack-below'], v['--stack-shift']]),
    [['36px', '0px'], ['0px', '10px']], 'the first book lies on top of the second');
window.bbShelfProps.place(grid);
assert.deepEqual(layout(), ['0', '[1,2,3]', '4', '[5,6]', '7', '8', '9', '10', '[11,12,13]', '14', '[15,16]', '17', '18', '19'],
    'placing again rebuilds the same piles');
window.bbShelfProps.clear(grid);
assert.deepEqual(layout(), Array.from({length: 20}, (_, i) => String(i)));
assert.deepEqual(grid.children[5].style.vars, {'--spine-w': '50px'}, 'clear drops the pile offsets');
delete grid.children[9].dataset.reading;
delete grid.closest;

grid.children.forEach(card => { card.hidden = true; });
window.bbShelfProps.place(grid);
assert.equal(grid.children.length, 20, 'no visible books means no props');
console.log('Shelf props placement ok');
