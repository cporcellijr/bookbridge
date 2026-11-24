// Shelf props: bookends around the visible books, decor every few books, and a clean re-place.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element(classes, parent) {
    const node = {
        classList: {
            items: new Set(classes),
            contains(name) { return this.items.has(name); },
        },
        dataset: {}, style: {}, hidden: false, attrs: {},
        setAttribute(name, value) { this.attrs[name] = value; },
        remove() { parent.children.splice(parent.children.indexOf(this), 1); },
        before(other) { other.remove = node.remove.bind(other); parent.children.splice(parent.children.indexOf(this), 0, other); },
        after(other) { other.remove = node.remove.bind(other); parent.children.splice(parent.children.indexOf(this) + 1, 0, other); },
    };
    Object.defineProperty(node, 'className', {set(value) { node.classList.items = new Set(value.split(' ')); }});
    return node;
}

const grid = {children: []};
for (let i = 0; i < 20; i++) {
    const card = element(['book-card'], grid);
    card.dataset.id = String(i);
    grid.children.push(card);
}
grid.children[4].style.display = 'none';
grid.children[9].classList.items.add('hidden');

const window = {};
const document = {createElement() { return element([], grid); }};
vm.runInNewContext(fs.readFileSync('static/js/next/shelf-props.js', 'utf8'), {window, document, Array});
const layout = () => grid.children.map(node => node.classList.contains('shelf-prop') ? node.dataset.prop : node.dataset.id);

window.bbShelfProps.place(grid);
const first = layout();
assert.equal(first[0], 'bookend-start', 'a bookend opens the shelf');
assert.equal(first[first.length - 1], 'bookend-end', 'a bookend closes it');
// 18 visible books (4 and 9 are hidden): decor after the 3rd, 10th and 15th visible book (gaps 3, 7, 5).
assert.deepEqual(first, ['bookend-start', '0', '1', '2', 'plant', '3', '4', '5', '6', '7', '8', '9', '10', '11',
    'stack', '12', '13', '14', '15', '16', 'candle', '17', '18', '19', 'bookend-end']);
assert.equal(grid.children.find(node => node.dataset.prop).attrs['aria-hidden'], 'true');

window.bbShelfProps.place(grid);
assert.deepEqual(layout(), first, 'placing again replaces the props instead of adding more');

window.bbShelfProps.clear(grid);
assert.equal(grid.children.length, 20, 'clear leaves only the books');
grid.children.reverse();
window.bbShelfProps.place(grid);
assert.equal(layout()[1], '19', 'props follow the new book order');

// The reader's choices: only candles, no bookends, the most props (gaps 1, 3, 2, 3, 2).
const page = {dataset: {shelfPropKinds: 'candle', shelfPropAmount: '5'}};
grid.closest = selector => (selector === '.library-page' ? page : null);
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
delete grid.closest;

grid.children.forEach(card => { card.hidden = true; });
window.bbShelfProps.place(grid);
assert.equal(grid.children.length, 20, 'no visible books means no props');
console.log('Shelf props placement ok');
