(function () {
    'use strict';

    const launch = document.getElementById('bb-search-open');
    const overlay = document.getElementById('bb-search-overlay');
    const input = document.getElementById('bb-search-input');
    const results = document.getElementById('bb-search-results');
    const empty = document.getElementById('bb-search-empty');
    const message = document.getElementById('bb-search-message');
    const catalog = window.BB_SEARCH_CATALOG;
    if (!launch || !overlay || !input || !results || !catalog) return;

    const labels = catalog.labels;
    let books = [];
    let settings = [];
    let loading = false;
    let loaded = false;
    let active = -1;
    let shown = [];
    let previousFocus = null;
    let previousOverflow = '';
    const background = Array.from(document.querySelectorAll('.app-shell, .tabbar'));

    function adjustViewport() {
        if (overlay.hidden) return;
        const viewport = window.visualViewport;
        overlay.style.setProperty('--bb-search-height', (viewport ? viewport.height : window.innerHeight) + 'px');
        overlay.style.setProperty('--bb-search-top', (viewport ? viewport.offsetTop : 0) + 'px');
        overlay.style.setProperty('--bb-search-left', (viewport ? viewport.offsetLeft : 0) + 'px');
    }

    function setActive(index) {
        const buttons = results.querySelectorAll('[role="option"]');
        active = buttons.length ? (index + buttons.length) % buttons.length : -1;
        buttons.forEach(function (button, i) {
            const selected = i === active;
            button.setAttribute('aria-selected', selected ? 'true' : 'false');
            if (selected) button.scrollIntoView({ block: 'nearest' });
        });
        if (active < 0) input.removeAttribute('aria-activedescendant');
        else input.setAttribute('aria-activedescendant', buttons[active].id);
    }

    function labelForField(doc, field) {
        const labelsForField = field.labels ? Array.from(field.labels) : [];
        const ownLabel = labelsForField.find(function (label) {
            return !label.classList.contains('toggle-switch');
        });
        const container = field.closest('.poll-row, .checkbox-wrapper, .form-group, .toggle-switch-wrapper, .section-header, .form-row, .setting-row')
            || field.parentElement;
        const label = ownLabel || (container && container.querySelector(
            '.poll-name, label:not(.toggle-switch):not(.poll-settle), .toggle-label, h3'));
        let text = (label ? label.textContent : '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 140) {
            text = field.getAttribute('aria-label') || field.getAttribute('placeholder') || field.name;
        }
        const section = field.closest('.settings-section');
        const sectionTitle = section && section.querySelector('.section-header h3');
        const sectionText = sectionTitle ? sectionTitle.textContent.replace(/\s+/g, ' ').trim() : '';
        if (sectionText && sectionText !== text && text.length < 100) text = sectionText + ' · ' + text;
        return text;
    }

    function indexSettings(html) {
        // Parse the actual rendered Settings form, not a duplicated registry. This
        // includes translated labels and newly added fields automatically.
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const indexed = new Set();
        return Array.from(doc.querySelectorAll('#settingsForm [name]')).filter(function (field) {
            if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(field.tagName)) return false;
            if (/^(hidden|button|submit|reset)$/i.test(field.type || '')) return false;
            if (!/^[A-Z][A-Z0-9_]*$/.test(field.name) || indexed.has(field.name)) return false;
            indexed.add(field.name);
            return true;
        }).map(function (field) {
            return {
                type: 'setting',
                title: labelForField(doc, field),
                detail: field.name,
                url: '/settings#' + encodeURIComponent(field.name)
            };
        });
    }

    async function loadSources() {
        if (loading || loaded) return;
        loading = true;
        message.textContent = labels.loading;
        const sources = [
            fetch('/api/me/books', { credentials: 'same-origin' })
                .then(function (response) { if (!response.ok) throw new Error('books unavailable'); return response.json(); })
                .then(function (data) {
                    books = (data.books || []).filter(function (book) { return book.abs_id && book.title; });
                })
        ];
        // GET /settings is protected by the server's existing admin-only guard.
        // Readers are not even offered this request or any settings index.
        if (catalog.maySearchSettings) {
            sources.push(fetch('/settings', { credentials: 'same-origin', cache: 'no-store' })
                .then(function (response) { if (!response.ok) throw new Error('settings unavailable'); return response.text(); })
                .then(function (html) { settings = indexSettings(html); }));
        }
        const settled = await Promise.allSettled(sources);
        loading = false;
        loaded = true;
        message.textContent = settled.some(function (item) { return item.status === 'rejected'; }) ? labels.failed : '';
        render();
    }

    function score(item, query) {
        if (!query) return item.type === 'page' ? 15 : item.type === 'action' ? 12 : item.type === 'book' ? 7 : 0;
        const name = item.title.toLocaleLowerCase();
        const haystack = (item.title + ' ' + (item.detail || '')).toLocaleLowerCase();
        const words = query.split(/\s+/).filter(Boolean);
        if (!words.every(function (word) { return haystack.includes(word); })) return -1;
        if (name === query) return 110;
        if (name.startsWith(query)) return 95;
        if (name.includes(query)) return 80;
        return 45;
    }

    function searchItems(query) {
        const pages = (catalog.pages || []).map(function (page) {
            return { type: 'page', title: page.title, detail: '', url: page.url };
        });
        const actions = (catalog.actions || []).map(function (action) {
            return { type: 'action', title: action.title, detail: '', url: action.url, action: action.action };
        });
        const ownBooks = books.map(function (book) {
            return { type: 'book', title: book.title, detail: book.author || book.ebook_filename || '', absId: book.abs_id };
        });
        const syncItems = query ? books.map(function (book) {
            return {
                type: 'action', title: labels.sync + ' · ' + book.title,
                detail: book.author || '', absId: book.abs_id, action: 'sync'
            };
        }) : [];
        return pages.concat(actions, ownBooks, settings, syncItems)
            .map(function (item, order) { return { item: item, order: order, rank: score(item, query) }; })
            .filter(function (candidate) { return candidate.rank >= 0; })
            .sort(function (a, b) { return b.rank - a.rank || a.order - b.order; })
            .slice(0, 32).map(function (candidate) { return candidate.item; });
    }

    function render() {
        const query = input.value.trim().toLocaleLowerCase();
        shown = searchItems(query);
        results.replaceChildren();
        shown.forEach(function (item, index) {
            const button = document.createElement('button');
            button.type = 'button';
            button.id = 'bb-search-result-' + index;
            button.className = 'bb-search-result';
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', 'false');
            const category = document.createElement('span');
            category.className = 'bb-search-result-type';
            category.textContent = labels[item.type];
            const text = document.createElement('span');
            text.className = 'bb-search-result-body';
            const title = document.createElement('strong');
            title.textContent = item.title;
            text.appendChild(title);
            if (item.detail) {
                const detail = document.createElement('small');
                detail.textContent = item.detail;
                text.appendChild(detail);
            }
            button.append(category, text);
            button.addEventListener('click', function () { activate(item, button); });
            results.appendChild(button);
        });
        empty.hidden = !!shown.length || loading;
        input.setAttribute('aria-expanded', shown.length ? 'true' : 'false');
        setActive(shown.length ? 0 : -1);
    }

    function submitExistingAction(item) {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = item.url;
        const field = document.createElement('input');
        field.type = 'hidden';
        field.name = 'action';
        field.value = item.action;
        form.appendChild(field);
        document.body.appendChild(form);
        // The existing CSRF form-submit wrapper adds the session token.
        form.submit();
    }

    async function activate(item, trigger) {
        if (item.action === 'sync') {
            try {
                const response = await fetch('/api/sync-now/' + encodeURIComponent(item.absId), {
                    method: 'POST', headers: { Accept: 'application/json' }
                });
                const body = await response.json();
                if (!response.ok || body.success !== true) throw new Error(body.error || labels.syncFailed);
                message.textContent = labels.syncStarted + ': ' + item.title.slice((labels.sync + ' · ').length);
            } catch (error) {
                message.textContent = labels.syncFailed + ': ' + error.message;
            }
            return;
        }
        if (item.action) { submitExistingAction(item); return; }
        if (item.absId) {
            const url = '/book/' + encodeURIComponent(item.absId);
            close();
            if (typeof window.openBookDrawer === 'function' && location.pathname === '/') {
                window.openBookDrawer(url, trigger);
            } else {
                location.assign(url);
            }
            return;
        }
        close();
        location.assign(item.url);
    }

    function open() {
        if (!overlay.hidden) return;
        previousFocus = document.activeElement;
        previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        overlay.hidden = false;
        background.forEach(function (element) { element.inert = true; });
        launch.setAttribute('aria-expanded', 'true');
        input.value = '';
        adjustViewport();
        render();
        // Synchronous focus matters on iPad: an asynchronous focus may not
        // trigger Safari's on-screen keyboard after a tap.
        input.focus();
        loadSources();
    }

    function close() {
        if (overlay.hidden) return;
        overlay.hidden = true;
        background.forEach(function (element) { element.inert = false; });
        document.body.style.overflow = previousOverflow;
        launch.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        const restore = previousFocus && previousFocus.isConnected &&
            previousFocus !== document.body && previousFocus !== document.documentElement &&
            !overlay.contains(previousFocus) ? previousFocus : launch;
        restore.focus();
    }

    launch.addEventListener('click', open);
    overlay.querySelectorAll('[data-search-close]').forEach(function (element) {
        element.addEventListener('click', close);
    });
    input.addEventListener('input', render);
    input.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            setActive(active + (event.key === 'ArrowDown' ? 1 : -1));
        } else if (event.key === 'Enter' && active >= 0) {
            event.preventDefault();
            activate(shown[active], results.children[active]);
        }
    });
    document.addEventListener('keydown', function (event) {
        const modifier = event.ctrlKey || event.metaKey;
        if ((modifier && event.key.toLowerCase() === 'k') ||
            (event.key === '/' && !modifier && !event.altKey &&
                !event.target.closest('input, textarea, select, [contenteditable="true"]'))) {
            event.preventDefault();
            if (overlay.hidden) open();
            else input.focus();
            return;
        }
        if (overlay.hidden) return;
        if (event.key === 'Escape') { event.preventDefault(); close(); return; }
        if (event.key === 'Tab') {
            const focusable = [input, overlay.querySelector('.bb-search-close')].concat(
                Array.from(results.querySelectorAll('button')));
            const first = focusable[0], last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault(); first.focus();
            }
        }
    });
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', adjustViewport);
        window.visualViewport.addEventListener('scroll', adjustViewport);
    }
    window.addEventListener('resize', adjustViewport);
}());
