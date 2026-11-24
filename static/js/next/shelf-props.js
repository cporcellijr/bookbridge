/* Decorative shelf props (UI_SHELF_PROPS): bookends around the visible books and a
   plant, book stack or candle every few books, as the reader chose (UI_SHELF_PROP_KINDS,
   UI_SHELF_PROP_AMOUNT). CSS shows them only on shelves with
   props switched on, so placing them never changes the Cards or List views. */
(function () {
    'use strict';
    // Books between props for each "How many" step (UI_SHELF_PROP_AMOUNT); 3 is the original spacing.
    var GAPS = {
        1: [8, 14, 10, 16, 12],
        2: [5, 10, 7, 11, 8],
        3: [3, 7, 5, 8, 6],
        4: [2, 5, 3, 5, 4],
        5: [1, 3, 2, 3, 2]
    };
    var KINDS = ['plant', 'stack', 'candle'];
    var DEFAULT_KINDS = 'bookends,plant,stack,candle';

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

    // The reader's choices sit on the page root (data-shelf-prop-kinds / -amount).
    function choices(grid) {
        var page = grid.closest ? grid.closest('.library-page') : null;
        var data = page ? page.dataset : {};
        var chosen = (data.shelfPropKinds ?? DEFAULT_KINDS).split(',');
        return {
            bookends: chosen.includes('bookends'),
            kinds: KINDS.filter(function (kind) { return chosen.includes(kind); }),
            gaps: GAPS[data.shelfPropAmount] || GAPS[3]
        };
    }

    function place(grid) {
        if (!grid) return;
        clear(grid);
        var books = Array.from(grid.children).filter(visible);
        if (!books.length) return;
        var options = choices(grid);
        var gaps = options.gaps;
        if (options.bookends) books[0].before(prop('bookend-start'));
        var next = gaps[0];
        var placed = 0;
        if (options.kinds.length) books.forEach(function (card, index) {
            if (index + 1 !== next || index === books.length - 1) return;
            card.after(prop(options.kinds[placed % options.kinds.length]));
            placed += 1;
            next += gaps[placed % gaps.length];
        });
        if (options.bookends) books[books.length - 1].after(prop('bookend-end'));
    }

    window.bbShelfProps = {place: place, clear: clear};
}());
