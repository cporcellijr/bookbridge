(function () {
    'use strict';

    function makeElement(tag, className, dataAttribute) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (dataAttribute) element.setAttribute(dataAttribute, '');
        return element;
    }

    function createPreviewUi(card) {
        if (!card || card.querySelector('[data-position-preview-toggle]')) return;
        if (card.dataset.syncMode === 'audiobook_only') return;
        // No ebook file on the card means nothing can be resolved; offering the
        // button would only ever return the "file is not available" state.
        if (!card.dataset.filename) return;
        const absId = card.dataset.absId || '';
        if (!absId) return;
        const info = card.querySelector('.book-info');
        if (!info) return;
        const panelId = `position-preview-${absId.replace(/[^A-Za-z0-9_-]/g, '-')}`;

        const button = makeElement('button', 'position-preview-toggle', 'data-position-preview-toggle');
        button.type = 'button';
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', panelId);
        button.textContent = bbT('Show position');

        const panel = makeElement('div', 'position-preview-panel', 'data-position-preview');
        panel.id = panelId;
        panel.hidden = true;
        panel.setAttribute('role', 'status');
        panel.setAttribute('aria-live', 'polite');

        const header = makeElement('div', 'position-preview-header');
        const title = makeElement('strong', 'position-preview-title', 'data-position-preview-title');
        title.textContent = bbT('Current reading position');
        const meta = makeElement('span', 'position-preview-meta', 'data-position-preview-meta');
        header.append(title, meta);

        const text = makeElement('div', 'position-preview-text');
        const before = makeElement('span', '', 'data-position-preview-before');
        const marker = makeElement('span', 'position-preview-marker', 'data-position-preview-marker');
        marker.hidden = true;
        marker.textContent = '▌';
        const after = makeElement('span', '', 'data-position-preview-after');
        text.append(before, marker, after);

        const message = makeElement('div', 'position-preview-message', 'data-position-preview-message');
        panel.append(header, text, message);
        info.append(button, panel);
    }

    function initPreviewUi() {
        document.querySelectorAll('.book-card[data-abs-id]').forEach(createPreviewUi);
    }

    function applyGridPreviewLayout(grid) {
        if (!grid) return;
        const hasExpandedPreview = Boolean(
            grid.querySelector('[data-position-preview-toggle][aria-expanded="true"]')
        );
        grid.classList.toggle('position-preview-expanded', hasExpandedPreview);
    }

    function syncGridPreviewLayout(button) {
        applyGridPreviewLayout(button.closest('.book-grid'));
    }

    // Series grouping moves .book-card nodes between grids at runtime, which
    // otherwise strands `position-preview-expanded` on the grid a card just left
    // and denies it to the grid the card just joined.
    function syncAllGridPreviewLayouts() {
        document.querySelectorAll('.book-grid').forEach(applyGridPreviewLayout);
    }

    window.bookbridgeSyncPreviewLayout = syncAllGridPreviewLayouts;

    function setExpanded(button, panel, expanded) {
        button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        button.textContent = expanded ? bbT('Hide position') : bbT('Show position');
        panel.hidden = !expanded;
        syncGridPreviewLayout(button);
    }

    function setLoading(panel) {
        panel.dataset.state = 'loading';
        panel.querySelector('[data-position-preview-title]').textContent = bbT('Current reading position');
        panel.querySelector('[data-position-preview-meta]').textContent = bbT('Resolving saved position…');
        panel.querySelector('[data-position-preview-before]').textContent = '';
        panel.querySelector('[data-position-preview-after]').textContent = '';
        panel.querySelector('[data-position-preview-marker]').hidden = true;
        panel.querySelector('[data-position-preview-message]').textContent = '';
    }

    function renderPayload(panel, payload) {
        const state = payload && payload.status ? payload.status : 'error';
        const source = payload && payload.source ? String(payload.source) : 'BookBridge';
        const confidence = payload && payload.confidence ? String(payload.confidence) : bbT('Unavailable');
        const percentage = payload && Number.isFinite(Number(payload.percentage))
            ? `${Number(payload.percentage).toFixed(1)}%`
            : '';
        const meta = [source, percentage, confidence].filter(Boolean).join(' · ');
        const before = payload && payload.before ? String(payload.before) : '';
        const after = payload && payload.after ? String(payload.after) : '';
        const hasText = Boolean(before || after);

        panel.dataset.state = state;
        panel.querySelector('[data-position-preview-title]').textContent =
            state === 'approximate' ? bbT('Approximate reading position') :
            state === 'unavailable' ? bbT('Reading position unavailable') :
            bbT('Current reading position');
        panel.querySelector('[data-position-preview-meta]').textContent = meta;
        panel.querySelector('[data-position-preview-before]').textContent = before ? `…${before}` : '';
        panel.querySelector('[data-position-preview-after]').textContent = after ? `${after}…` : '';
        panel.querySelector('[data-position-preview-marker]').hidden = !hasText;
        panel.querySelector('[data-position-preview-message]').textContent =
            payload && payload.message ? String(payload.message) : '';
    }

    function renderError(panel) {
        panel.dataset.state = 'error';
        panel.querySelector('[data-position-preview-title]').textContent = bbT('Reading position unavailable');
        panel.querySelector('[data-position-preview-meta]').textContent = bbT('Could not load the preview');
        panel.querySelector('[data-position-preview-before]').textContent = '';
        panel.querySelector('[data-position-preview-after]').textContent = '';
        panel.querySelector('[data-position-preview-marker]').hidden = true;
        panel.querySelector('[data-position-preview-message]').textContent =
            bbT('Try again. Your saved progress was not changed.');
    }

    async function loadPreview(panel, absId) {
        setLoading(panel);
        try {
            const response = await fetch(
                `/api/books/${encodeURIComponent(absId)}/position-preview`,
                { cache: 'no-store', headers: { Accept: 'application/json' } }
            );
            const payload = await response.json().catch(function () { return {}; });
            if (!response.ok) throw new Error('preview request failed');
            renderPayload(panel, payload);
        } catch (_error) {
            renderError(panel);
        }
    }

    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-position-preview-toggle]');
        if (!button) return;
        const card = button.closest('.book-card');
        if (!card) return;
        const panelId = button.getAttribute('aria-controls');
        const panel = panelId ? document.getElementById(panelId) : null;
        const absId = card.dataset.absId || '';
        if (!panel || !absId) return;
        const isExpanded = button.getAttribute('aria-expanded') === 'true';
        if (isExpanded) return setExpanded(button, panel, false);
        setExpanded(button, panel, true);
        loadPreview(panel, absId);
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPreviewUi, { once: true });
    } else {
        initPreviewUi();
    }
})();
