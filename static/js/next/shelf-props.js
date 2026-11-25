/* Shelf props (UI_SHELF_PROPS): bookends around the visible books and a plant, candle or
   book stack every few books, as the reader chose (UI_SHELF_PROP_KINDS, UI_SHELF_PROP_AMOUNT).
   Plants, candles, bookends and the reader's uploaded pictures are decoration; a book stack is the next few real books lying
   flat, wrapped in .shelf-stack, so they stay links in their sorted place. CSS shows props and
   piles only on shelves with props switched on; elsewhere the wrapper is display: contents. */
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
    var PILES = [3, 2];
    var SHIFTS = [0, 10, 4];
    var STACK_VARS = ['--stack-below', '--stack-shift'];
    var UPLOAD = /^upload:([0-9a-f]{12}\.(?:svg|png|webp))$/;

    function clear(grid) {
        if (!grid) return;
        Array.from(grid.children).forEach(function (child) {
            if (child.classList.contains('shelf-prop')) child.remove();
            if (!child.classList.contains('shelf-stack')) return;
            var books = Array.from(child.children);
            books.forEach(function (book) {
                STACK_VARS.forEach(function (name) { book.style.removeProperty(name); });
            });
            child.replaceWith.apply(child, books);
        });
    }

    // A reader's own picture ("upload:<file>") is drawn from its owner-only route.
    function prop(kind) {
        var element = document.createElement('span');
        var upload = UPLOAD.exec(kind);
        element.className = 'shelf-prop';
        element.dataset.prop = upload ? 'upload' : kind;
        if (upload) element.style.setProperty('--prop-art', 'url("/account/trinkets/' + upload[1] + '")');
        element.setAttribute('aria-hidden', 'true');
        return element;
    }

    // The hero's own card stays in the grid but is hidden by CSS.
    function visible(card) {
        return card.classList.contains('book-card') && !card.hidden
            && !card.classList.contains('hidden') && card.style.display !== 'none'
            && card.dataset.heroExcluded !== 'true';
    }

    function px(card, name, fallback) {
        return parseFloat(card.style.getPropertyValue(name)) || fallback;
    }

    // Lay up to `count` books from `first` flat, first in sort order on top. Only adjacent
    // standing spines go in, so unwrapping puts every book back exactly where it was.
    function pile(first, count) {
        var books = [];
        for (var book = first; book && books.length < count && visible(book) && !('reading' in book.dataset);
            book = book.nextElementSibling) books.push(book);
        if (books.length < 2) return 0;
        var stack = document.createElement('div');
        stack.className = 'shelf-stack';
        first.before(stack);
        var below = 0;
        var width = 0;
        books.slice().reverse().forEach(function (book, index) {
            var shift = SHIFTS[(books.length - 1 - index) % SHIFTS.length];
            book.style.setProperty('--stack-below', below + 'px');
            book.style.setProperty('--stack-shift', shift + 'px');
            below += px(book, '--spine-w', 42);
            width = Math.max(width, px(book, '--spine-h', 214) + shift);
        });
        books.forEach(function (book) { stack.appendChild(book); });
        stack.style.setProperty('--stack-w', width + 'px');
        return books.length;
    }

    // The reader's choices sit on the page root (data-shelf-prop-kinds / -amount).
    function choices(grid) {
        var page = grid.closest ? grid.closest('.library-page') : null;
        var data = page ? page.dataset : {};
        var chosen = (data.shelfPropKinds ?? DEFAULT_KINDS).split(',');
        return {
            bookends: chosen.includes('bookends'),
            kinds: KINDS.filter(function (kind) { return chosen.includes(kind); })
                .concat(chosen.filter(function (kind) { return UPLOAD.test(kind); })),
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
        var piles = 0;
        if (options.kinds.length) books.forEach(function (card, index) {
            if (index + 1 !== next || index === books.length - 1) return;
            var kind = options.kinds[placed % options.kinds.length];
            var stacked = 0;
            // The last book stays standing so the closing bookend has a spine to lean on.
            if (kind === 'stack') stacked = pile(books[index + 1], Math.min(PILES[piles % PILES.length], books.length - 2 - index));
            else card.after(prop(kind));
            if (stacked) piles += 1;
            placed += 1;
            next = Math.max(next + gaps[placed % gaps.length], index + stacked + 2);
        });
        if (options.bookends) books[books.length - 1].after(prop('bookend-end'));
    }

    window.bbShelfProps = {place: place, clear: clear};
}());
