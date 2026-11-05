/* New-only Settings navigation; Classic's copied Settings handlers remain unchanged. */
(function () {
    'use strict';

    let requestedField = '';
    try { requestedField = decodeURIComponent(location.hash.slice(1)); }
    catch (error) { requestedField = ''; }

    const serviceTests = {
        audiobookshelf: 'abs',
        koreader: 'kosync',
        storyteller: 'storyteller',
        grimmory: 'booklore',
        bookfusion: 'bookfusion',
        bookorbit: 'bookorbit',
        kavita: 'kavita',
        readest: 'readest',
        cwa: 'cwa',
        hardcover: 'hardcover',
        storygraph: 'storygraph',
        goodreads: 'goodreads'
    };
    const aliases = {
        integrations: 'audiobookshelf',
        general: 'audiobookshelf',
        reading_apps: 'audiobookshelf',
        trackers: 'hardcover',
        features: 'library',
        tools: 'maintenance'
    };
    const serviceNames = Object.keys(serviceTests);
    const pageNames = ['sync', 'library', 'alignment', 'ai', 'system', 'maintenance', 'users', 'logs'];
    let alignHealthLoaded = false;

    function selectedPage(name) {
        if (/^[A-Z][A-Z0-9_]*$/.test(name)) {
            const field = Array.from(document.querySelectorAll('#settingsForm [name]'))
                .find(function (node) { return node.name === name && node.type !== 'hidden'; });
            if (field) {
                const region = field.closest('[data-service-page]');
                if (region) return region.dataset.servicePage;
                const panel = field.closest('.tab-panel');
                if (panel) return panel.dataset.panel;
            }
        }
        const canonical = aliases[name] || name;
        return serviceNames.includes(canonical) || pageNames.includes(canonical)
            ? canonical : 'audiobookshelf';
    }

    function mountSettingsNavigation() {
        const menu = document.getElementById('settings-navigation');
        const rail = document.getElementById('settings-rail-slot');
        const mobile = document.getElementById('settings-mobile-slot');
        if (!menu || !rail || !mobile) return;
        const viewport = window.matchMedia('(max-width: 760px)');
        const move = function () {
            const target = viewport.matches ? mobile : rail;
            if (menu.parentElement !== target) target.appendChild(menu);
            keepSelectedMenuLinkInView();
        };
        move();
        if (viewport.addEventListener) viewport.addEventListener('change', move);
        else viewport.addListener(move);
    }

    function disableServiceHeaderCollapse() {
        document.querySelectorAll('.settings-section[data-svc]').forEach(function (section) {
            const head = section.querySelector(':scope > .section-header');
            if (!head) return;
            section.addEventListener('click', function (event) {
                if (head.contains(event.target) && !event.target.closest('.header-tail')) {
                    // Capture before the Classic-copied header click handler runs.
                    event.stopPropagation();
                }
            }, true);
        });
    }

    function keepSelectedMenuLinkInView() {
        const menu = document.querySelector('.settings-menu');
        const active = menu && menu.querySelector('.settings-tab.active');
        if (!active || menu.scrollHeight <= menu.clientHeight) return;
        const menuBounds = menu.getBoundingClientRect();
        const linkBounds = active.getBoundingClientRect();
        if (linkBounds.top < menuBounds.top) {
            menu.scrollTop -= menuBounds.top - linkBounds.top + 8;
        } else if (linkBounds.bottom > menuBounds.bottom) {
            menu.scrollTop += linkBounds.bottom - menuBounds.bottom + 8;
        }
    }

    function showSettingsTab(name, preserveFieldHash) {
        const main = document.querySelector('.settings-main');
        if (main && main.classList.contains('filtering')) {
            main.classList.remove('filtering');
            const search = document.getElementById('settings_search');
            const changed = document.getElementById('changed_only');
            if (search) search.value = '';
            if (changed) changed.checked = false;
        }
        const page = selectedPage(name);
        const servicePage = serviceNames.includes(page);
        document.querySelectorAll('.settings-tab').forEach(function (link) {
            const active = link.dataset.tab === page;
            link.classList.toggle('active', active);
            if (active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
        keepSelectedMenuLinkInView();
        document.querySelectorAll('.tab-panel').forEach(function (panel) {
            const active = panel.dataset.panel === (servicePage ? 'integrations' : page);
            panel.classList.toggle('active', active);
            panel.style.display = '';
        });
        document.querySelectorAll('.settings-section[data-service-page]').forEach(function (section) {
            section.style.display = section.dataset.servicePage === page ? '' : 'none';
        });
        const form = document.getElementById('settingsForm');
        if (form) form.style.display = (page === 'users' || page === 'logs') ? 'none' : '';
        const bar = document.querySelector('.submit-bar');
        // Maintenance has nothing to save (Classic hides the bar there too).
        if (bar) bar.style.display = page === 'maintenance' ? 'none' : '';
        const returnTab = document.getElementById('return_tab');
        if (returnTab) returnTab.value = page;
        const fragment = preserveFieldHash ? name : page;
        if (history.replaceState) history.replaceState(null, '', '#' + encodeURIComponent(fragment));
        else location.hash = encodeURIComponent(fragment);

        if (page === 'alignment' && !alignHealthLoaded) {
            alignHealthLoaded = true;
            if (typeof window.loadAlignmentHealth === 'function') window.loadAlignmentHealth();
        }
    }
    window.showSettingsTab = showSettingsTab;

    function connectionState(section) {
        const header = section.querySelector(':scope > .section-header');
        const pill = header && header.querySelector('.pill');
        const enabled = header && header.querySelector('input[type="checkbox"]');
        const menu = document.querySelector('.settings-menu');
        const disabled = enabled && !enabled.checked;
        return {
            label: pill ? pill.textContent.trim() :
                (disabled ? menu.dataset.disabledLabel : menu.dataset.enabledLabel),
            state: disabled || (pill && pill.classList.contains('off')) ? 'off' : 'ok'
        };
    }

    function buildServiceNavigation() {
        document.querySelectorAll('.settings-service-tab[data-svc]').forEach(function (link) {
            const key = link.dataset.svc;
            const section = document.querySelector('.settings-section[data-service-page="' + key + '"]');
            if (!section) return;
            const status = link.querySelector('.next-connection-status');
            const refresh = function () {
                const state = connectionState(section);
                status.textContent = state.label;
                status.dataset.state = state.state;
            };
            refresh();
            const switcher = section.querySelector(':scope > .section-header input[type="checkbox"]');
            if (switcher) switcher.addEventListener('change', refresh);
            // Test sits in the service's own header so each page names the service once.
            const tail = section.querySelector(':scope > .section-header .header-tail');
            if (tail) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'next-connection-test';
                button.textContent = document.querySelector('.settings-menu').dataset.testLabel;
                button.addEventListener('click', function () {
                    if (typeof window.testConnection === 'function') {
                        window.testConnection(serviceTests[key], button);
                    }
                });
                tail.prepend(button);
            }
        });
    }

    // Safari must not depend on Classic's non-standard implicit Window.event.
    window.bbNextFindShelves = function (evt, kind) {
        const watch = kind === 'watch';
        const button = evt.currentTarget;
        populateLibraryPicker({
            endpoint: watch ? '/api/watch-shelves' : '/api/booklore/shelves',
            button: button,
            inputName: watch ? 'ALL_APPS_SHELF_WATCH_NAME' : 'DEVICE_SYNC_EXCLUDED_SHELVES',
            selectId: watch ? 'booklore_watch_shelf_picker' : 'booklore_shelf_picker',
            statusId: watch ? 'booklore_watch_shelf_picker_status' : 'booklore_shelf_picker_status',
            emptyMessage: button.dataset.emptyMessage,
            labelBuilder: watch
                ? (shelf) => shelf.name
                : (shelf) => bbPlural(SHELF_BOOK_COUNT, shelf.count, { name: shelf.name }),
        });
    };

    function focusRequestedField(fieldName) {
        if (!/^[A-Z][A-Z0-9_]*$/.test(fieldName)) return false;
        const field = Array.from(document.querySelectorAll('#settingsForm [name]'))
            .find(function (node) { return node.name === fieldName && node.type !== 'hidden'; });
        if (!field) return false;
        showSettingsTab(fieldName, true);
        const section = field.closest('.settings-section');
        if (section) {
            section.querySelectorAll('.section-body.collapsed').forEach(function (body) {
                body.classList.remove('collapsed');
            });
        }
        field.closest('details')?.setAttribute('open', '');
        field.scrollIntoView({ block: 'center' });
        field.classList.add('bb-search-setting-target');
        field.focus({ preventScroll: true });
        return true;
    }

    function applySettingsFilter() {
        const main = document.querySelector('.settings-main');
        const search = document.getElementById('settings_search');
        const q = (search ? search.value : '').trim().toLocaleLowerCase();
        const changedOnly = document.getElementById('changed_only').checked;
        const filtering = !!q || changedOnly;
        main.classList.toggle('filtering', filtering);
        if (!filtering) {
            document.querySelectorAll('.settings-main .settings-section').forEach(function (section) {
                section.style.display = '';
            });
            showSettingsTab(selectedPage((location.hash || '').slice(1)));
            return;
        }
        // Search spans every connection and category, without detaching form fields.
        document.querySelectorAll('.tab-panel').forEach(function (panel) {
            let matches = 0;
            panel.querySelectorAll('.settings-section').forEach(function (section) {
                const text = ((section.dataset.svc || '') + ' ' + section.textContent).toLocaleLowerCase();
                const hit = (!q || text.includes(q))
                    && (!changedOnly || section.classList.contains('has-changed'));
                section.style.display = hit ? '' : 'none';
                if (!hit) return;
                matches += 1;
                if (q) {
                    section.querySelectorAll('details').forEach(function (details) {
                        if (details.textContent.toLocaleLowerCase().includes(q)) details.open = true;
                    });
                    section.querySelectorAll(':scope > .section-body.collapsed').forEach(function (body) {
                        if (body.textContent.toLocaleLowerCase().includes(q)) body.classList.remove('collapsed');
                    });
                }
            });
            panel.style.display = matches ? 'block' : 'none';
        });
        const form = document.getElementById('settingsForm');
        if (form) form.style.display = '';
        const bar = document.querySelector('.submit-bar');
        if (bar) bar.style.display = '';
    }
    window.applySettingsFilter = applySettingsFilter;

    document.addEventListener('DOMContentLoaded', function () {
        mountSettingsNavigation();
        disableServiceHeaderCollapse();
        buildServiceNavigation();
        showSettingsTab(requestedField || 'audiobookshelf',
            /^[A-Z][A-Z0-9_]*$/.test(requestedField));
        focusRequestedField(requestedField);
        window.addEventListener('hashchange', function () {
            let fragment = '';
            try { fragment = decodeURIComponent(location.hash.slice(1)); }
            catch (error) { fragment = ''; }
            if (!focusRequestedField(fragment)) showSettingsTab(fragment || 'audiobookshelf');
        });
    });
}());
