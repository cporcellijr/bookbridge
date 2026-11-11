/* Load New Library covers only once a small thumbnail is ready.
 * A HEAD probe cannot transfer the original image; the CSS typographic cover
 * remains visible during background conversion. Classic remains unchanged.
 */
(function () {
    'use strict';
    const library = document.getElementById('library-page');
    if (!library || !window.fetch) return;

    const readiness = new Map();
    const MAX_ATTEMPTS = 15;
    const READY_HINT_MS = 120000; // Stay below the server's 300s private image cache.
    const READY_PREFIX = 'bb-cover-thumb-ready:';

    function rememberedReady(url) {
        try {
            const timestamp = Number(window.sessionStorage?.getItem(READY_PREFIX + url));
            const age = Date.now() - timestamp;
            return timestamp > 0 && age >= 0 && age < READY_HINT_MS;
        } catch (e) {
            return false; // Private browsing/storage policy must never break covers.
        }
    }

    function rememberReady(url) {
        try { window.sessionStorage?.setItem(READY_PREFIX + url, String(Date.now())); }
        catch (e) { /* Storage may be disabled. */ }
    }

    function forgetReady(url) {
        try { window.sessionStorage?.removeItem(READY_PREFIX + url); }
        catch (e) { /* Storage may be disabled. */ }
    }

    function whenVisible() {
        if (!document.hidden) return Promise.resolve();
        return new Promise(resolve => {
            document.addEventListener('visibilitychange', function resumed() {
                if (document.hidden) return;
                document.removeEventListener('visibilitychange', resumed);
                resolve();
            });
        });
    }

    async function checkReady(url) {
        if (rememberedReady(url)) return true;
        for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
            await whenVisible();
            let response;
            try {
                response = await fetch(url, {
                    method: 'HEAD', credentials: 'same-origin', cache: 'no-store',
                });
            } catch (e) {
                return false;
            }
            if (response.status === 200) {
                rememberReady(url);
                return true;
            }
            // No extracted cover at all: skip the original and go straight to the provider image.
            if (response.status === 404) return null;
            if (response.status !== 202) return false;
            await new Promise(resolve => setTimeout(resolve,
                Math.min(1800, Math.round(75 * Math.pow(1.65, attempt)))));
        }
        return false;
    }

    function loadCover(img) {
        const url = img.dataset.thumbUrl;
        if (!url) return;
        if (!readiness.has(url)) readiness.set(url, checkReady(url));
        readiness.get(url).then(ready => {
            if (!img.isConnected) return;
            img.loading = 'eager'; // The observed cover may still be hidden.
            img.addEventListener('load', () => { img.hidden = false; }, { once: true });
            if (ready === null) { img.dispatchEvent(new Event('error')); return; }
            // A stored readiness hint can outlive an invalidated or evicted
            // thumbnail. Fall back to the original, then to the existing
            // provider onerror handler, without leaving a broken image.
            if (ready) {
                const providerError = img.onerror;
                img.onerror = function () {
                    forgetReady(url);
                    img.onerror = providerError;
                    img.src = url.replace('/covers/thumb/', '/covers/');
                };
            }
            img.src = ready ? url : url.replace('/covers/thumb/', '/covers/');
        });
    }

    const hosts = new Map();
    library.querySelectorAll('img[data-thumb-url]').forEach(img => {
        const host = img.closest('.cover-wrap, .series-stack') || img;
        if (!hosts.has(host)) hosts.set(host, []);
        hosts.get(host).push(img);
    });

    if (!('IntersectionObserver' in window)) {
        hosts.forEach(images => images.forEach(loadCover));
        return;
    }
    const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            observer.unobserve(entry.target);
            (hosts.get(entry.target) || []).forEach(loadCover);
            hosts.delete(entry.target);
        });
    }, { rootMargin: '250px 0px' });
    hosts.forEach((_images, host) => observer.observe(host));
}());
