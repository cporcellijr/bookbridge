/* Cover masonry uses the same measured 4px grid rows as Classic. */
(function () {
    'use strict';
    const ROW_PX = 4;
    if (!('ResizeObserver' in window)) return;

    const grids = Array.from(document.querySelectorAll('.library-page .book-grid'));
    if (!grids.length) return;
    const pending = new Set();
    const observed = new WeakSet();
    let frame = 0;

    function inCovers() {
        return document.getElementById('library-page')?.dataset.view === 'covers';
    }

    function layoutGrid(grid) {
        const covers = inCovers();
        const gap = parseFloat(getComputedStyle(grid).columnGap) || 0;
        grid.classList.toggle('masonry', covers);
        Array.from(grid.children).forEach(function (item) {
            if (!covers || item.offsetParent === null) {
                item.style.gridRowEnd = '';
                return;
            }
            const height = item.getBoundingClientRect().height;
            item.style.gridRowEnd = 'span ' + Math.max(1, Math.ceil((height + gap) / ROW_PX));
        });
    }

    function schedule(grid) {
        pending.add(grid);
        if (frame) return;
        frame = requestAnimationFrame(function () {
            frame = 0;
            pending.forEach(layoutGrid);
            pending.clear();
        });
    }

    const resizeObserver = new ResizeObserver(function (entries) {
        entries.forEach(function (entry) {
            const grid = entry.target.parentElement;
            if (grid && grids.includes(grid)) schedule(grid);
        });
    });

    function watchChildren(grid) {
        Array.from(grid.children).forEach(function (child) {
            if (!observed.has(child)) {
                observed.add(child);
                resizeObserver.observe(child);
            }
        });
    }

    function initMasonry() {
        grids.forEach(function (grid) {
            watchChildren(grid);
            new MutationObserver(function () {
                watchChildren(grid);
                schedule(grid);
            }).observe(grid, { childList: true });
            schedule(grid);
        });
    }

    window.bbLayoutMasonry = function () {
        grids.forEach(schedule);
    };
    // Filtering, sorting, series folding and view switches change the row layout.
    // ResizeObserver handles measured heights; these events also cover display:none.
    document.addEventListener('change', function (event) {
        if (event.target.closest('.library-page')) window.bbLayoutMasonry();
    });
    document.addEventListener('click', function (event) {
        if (event.target.closest('.library-page')) window.bbLayoutMasonry();
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initMasonry);
    else initMasonry();
}());
