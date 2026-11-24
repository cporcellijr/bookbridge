/* Decorative shelf props (UI_SHELF_PROPS): bookends around the visible books and a
   plant, book stack or candle every few books. CSS shows them only on shelves with
   props switched on, so placing them never changes the Cards or List views. */
(function () {
    'use strict';
    var GAPS = [3, 7, 5, 8, 6];
    var KINDS = ['plant', 'stack', 'candle'];

    function clear(grid) {
        if (!grid) return;
        Array.from(grid.children).forEach(function (child) {
            if (child.classList.contains('shelf-prop')) child.remove();
        });
    }

    function prop(kind) {
        var element = document.createElement('span');
        element.className = 'shelf-prop';
        element.dataset.prop = kind;
        element.setAttribute('aria-hidden', 'true');
        return element;
    }

    function visible(card) {
        return card.classList.contains('book-card') && !card.hidden
            && !card.classList.contains('hidden') && card.style.display !== 'none';
    }

    function place(grid) {
        if (!grid) return;
        clear(grid);
        var books = Array.from(grid.children).filter(visible);
        if (!books.length) return;
        books[0].before(prop('bookend-start'));
        var next = GAPS[0];
        var placed = 0;
        books.forEach(function (card, index) {
            if (index + 1 !== next || index === books.length - 1) return;
            card.after(prop(KINDS[placed % KINDS.length]));
            placed += 1;
            next += GAPS[placed % GAPS.length];
        });
        books[books.length - 1].after(prop('bookend-end'));
    }

    window.bbShelfProps = {place: place, clear: clear};
}());
