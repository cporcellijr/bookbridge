(function () {
    'use strict';
    document.querySelectorAll('[data-bookfusion-unlink]').forEach(function (button) {
        button.addEventListener('click', async function () {
            if (!confirm(bbT('Unlink BookFusion?'))) return;
            button.disabled = true;
            try {
                const response = await fetch('/api/bookfusion/link/' + encodeURIComponent(button.closest('[data-abs-id]').dataset.absId), {method: 'DELETE'});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || bbT('Link failed'));
                location.reload();
            } catch (error) {
                alert(error.message);
                button.disabled = false;
            }
        });
    });
    // P3 can use ordinary book links: failure keeps their full-page destination.
    // The frame loads the fragment by URL (not srcdoc) so the shared actions' location.reload() refetches fresh state.
    window.openBookDrawer = function (url, trigger) {
        const dialog = document.createElement('dialog');
        dialog.className = 'book-drawer-host';
        dialog.setAttribute('aria-label', trigger?.textContent?.trim() || document.title);
        const frame = document.createElement('iframe');
        frame.title = trigger?.textContent?.trim() || document.title;
        dialog.append(frame);
        document.body.append(dialog);
        const restore = document.activeElement;
        const close = function () {
            dialog.close(); dialog.remove(); restore?.focus();
        };
        dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
        dialog.addEventListener('click', function (event) { if (event.target === dialog) close(); });
        frame.addEventListener('load', function () {
            const doc = frame.contentDocument;
            // A 403/404/error answer has no drawer markup: fall back to the full page.
            if (!doc || !doc.querySelector('[data-book-close]')) { close(); location.assign(url); return; }
            doc.querySelector('[data-book-close]')?.addEventListener('click', function (event) {
                event.preventDefault(); close();
            });
            doc.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') { event.preventDefault(); close(); }
                if (event.key !== 'Tab') return;
                const items = Array.from(doc.querySelectorAll('a[href],button:not(:disabled),input,summary,[tabindex="0"]')).filter(node => node.checkVisibility() && node.tabIndex >= 0);
                const first = items[0], last = items[items.length - 1];
                if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last?.focus(); }
                else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first?.focus(); }
            });
            doc.querySelector('[data-book-close]')?.focus();
        });
        frame.src = url + (url.includes('?') ? '&' : '?') + 'fragment=1';
        dialog.showModal();
    };
})();
