(function () {
    'use strict';
    // About this book: render cached text, or fetch it after first paint; stay hidden without text.
    function initBookAbout(section) {
        const text = section.querySelector('[data-about-text]');
        const toggle = section.querySelector('[data-about-toggle]');
        function show() {
            section.hidden = false;
            text.classList.add('is-clamped');
            if (text.scrollHeight > text.clientHeight + 1) {
                toggle.hidden = false;
            } else {
                text.classList.remove('is-clamped');
            }
        }
        toggle.addEventListener('click', function () {
            const open = toggle.getAttribute('aria-expanded') !== 'true';
            toggle.setAttribute('aria-expanded', String(open));
            text.classList.toggle('is-clamped', !open);
            toggle.textContent = open ? toggle.dataset.less : toggle.dataset.more;
        });
        if (!section.dataset.synopsisUrl) {
            if (text.textContent.trim()) show();
            return;
        }
        fetch(section.dataset.synopsisUrl).then(function (response) {
            return response.ok ? response.json() : null;
        }).then(function (data) {
            if (!data || !data.text) return;
            text.textContent = data.text;
            show();
        }).catch(function () {});
    }
    const about = document.querySelector('[data-book-about]');
    if (about) initBookAbout(about);
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
        // Safari paints a loading frame white; keep it hidden until the book page has drawn.
        frame.style.visibility = 'hidden';
        dialog.append(frame);
        document.body.append(dialog);
        const restore = trigger && trigger.isConnected ? trigger : document.activeElement;
        const close = function () {
            dialog.close(); dialog.remove();
            if (restore && restore.isConnected) {
                restore.focus();
                // Native dialog focus restoration can run after close() in Chrome/Safari.
                requestAnimationFrame(function () {
                    if (restore.isConnected) restore.focus({preventScroll: true});
                });
            }
        };
        dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
        dialog.addEventListener('click', function (event) { if (event.target === dialog) close(); });
        frame.addEventListener('load', function () {
            const doc = frame.contentDocument;
            // A 403/404/error answer has no drawer markup: fall back to the full page.
            if (!doc || !doc.querySelector('[data-book-close]')) { close(); location.assign(url); return; }
            frame.style.visibility = '';
            frame.contentWindow.addEventListener('pagehide', function () { frame.style.visibility = 'hidden'; });
            doc.querySelector('[data-book-close]')?.addEventListener('click', function (event) {
                event.preventDefault(); close();
            });
            doc.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') { event.preventDefault(); close(); }
                if (event.key !== 'Tab') return;
                const items = Array.from(doc.querySelectorAll('a[href],button:not(:disabled),input,summary,[tabindex="0"]')).filter(node => node.getClientRects().length > 0 && node.tabIndex >= 0 && getComputedStyle(node).visibility !== 'hidden');
                const first = items[0], last = items[items.length - 1];
                if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last?.focus(); }
                else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first?.focus(); }
            });
            doc.querySelector('[data-book-close]')?.focus();
        });
        frame.src = url + (url.includes('?') ? '&' : '?') + 'fragment=1';
        dialog.showModal();
        dialog.tabIndex = -1;
        dialog.focus();
    };
})();
