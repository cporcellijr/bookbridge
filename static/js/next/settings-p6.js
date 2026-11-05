/* New Settings presentation. Classic Settings handlers are included unchanged. */
(function () {
    'use strict';

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
        storygraph: 'storygraph'
    };

    if (typeof SETTINGS_TABS !== 'undefined' && !SETTINGS_TABS.includes('logs')) {
        SETTINGS_TABS.push('logs');
    }
    if (typeof window.showSettingsTab === 'function') {
        const classicShowSettingsTab = window.showSettingsTab;
        window.showSettingsTab = function (name) {
            classicShowSettingsTab(name);
            if (name === 'logs') {
                const form = document.getElementById('settingsForm');
                const bar = document.querySelector('.submit-bar');
                if (form) form.style.display = 'none';
                if (bar) bar.style.display = 'none';
            }
        };
    }

    function buildServiceTiles() {
        const grid = document.getElementById('next-service-tiles');
        if (!grid) return;
        const testLabel = grid.dataset.testLabel;
        const configureLabel = grid.dataset.configureLabel;
        const regions = document.querySelectorAll('.settings-section[data-svc]');

        regions.forEach(function (region) {
            const key = region.dataset.svc.split(/\s+/)[0];
            const header = region.querySelector(':scope > .section-header');
            if (!header) return;
            const title = header.querySelector('.svc-title h3');
            const initials = header.querySelector('.svc-chip');
            const status = header.querySelector('.pill');
            const enabled = header.querySelector('input[type="checkbox"]');
            const body = region.querySelector(':scope > .section-body');
            if (!title || !serviceTests[key]) return;

            const tile = document.createElement('div');
            tile.className = 'next-service-tile';
            const top = document.createElement('div');
            top.className = 'next-service-name';
            const emblem = document.createElement('span');
            emblem.className = 'next-service-initial';
            emblem.setAttribute('aria-hidden', 'true');
            emblem.textContent = initials ? initials.textContent : title.textContent.slice(0, 2);
            const name = document.createElement('span');
            name.className = 'next-service-title';
            name.textContent = (title.firstChild && title.firstChild.textContent || title.textContent).trim();
            top.append(emblem, name);
            tile.appendChild(top);

            const state = document.createElement('div');
            state.className = 'next-service-state';
            function refreshState() {
                const disabled = enabled && !enabled.checked;
                state.textContent = status ? status.textContent.trim() : (disabled ? grid.dataset.disabledLabel : grid.dataset.enabledLabel);
                state.dataset.state = disabled || (status && status.classList.contains('off')) ? 'off' : 'ok';
            }
            refreshState();
            if (enabled) enabled.addEventListener('change', refreshState);
            tile.appendChild(state);

            const actions = document.createElement('div');
            actions.className = 'next-service-actions';
            const test = document.createElement('button');
            test.type = 'button';
            test.textContent = testLabel;
            test.addEventListener('click', function () {
                if (typeof window.testConnection === 'function') {
                    window.testConnection(serviceTests[key], test);
                }
            });
            const configure = document.createElement('button');
            configure.type = 'button';
            configure.textContent = configureLabel;
            configure.addEventListener('click', function () {
                const search = document.getElementById('settings_search');
                const changed = document.getElementById('changed_only');
                if (search) search.value = '';
                if (changed) changed.checked = false;
                if (typeof window.applySettingsFilter === 'function') window.applySettingsFilter();
                if (typeof window.showSettingsTab === 'function') window.showSettingsTab('integrations');
                if (body) body.classList.remove('collapsed');
                region.scrollIntoView({ behavior: 'smooth', block: 'start' });
                region.classList.add('next-service-section-flash');
                window.setTimeout(function () {
                    region.classList.remove('next-service-section-flash');
                }, 1200);
            });
            actions.append(test, configure);
            tile.appendChild(actions);
            grid.appendChild(tile);
        });
    }

    // Safari must not depend on the non-standard implicit Window.event used by Classic.
    // Delegate to Classic's shared picker helper with the actual click target.
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

    document.addEventListener('DOMContentLoaded', buildServiceTiles);
}());
