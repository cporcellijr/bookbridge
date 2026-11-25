(function () {
    'use strict';
    var page = document.querySelector('.display-page');
    if (!page) return;
    page.classList.add('js-display');
    var stage = document.getElementById('display-preview-stage');
    var statuses = new WeakMap();
    var sizeRow = document.querySelector('.display-size-row');
    var finishRow = document.querySelector('.display-finish-row');
    var propsRows = document.querySelectorAll('.display-props-row');
    var faceoutRow = document.querySelector('.display-faceout-row');
    var previewSwitches = {
        UI_SHOW_PROGRESS: 'showProgress',
        UI_SHOW_FINISHED: 'showFinished',
        UI_SHOW_FORMAT_STRIP: 'showFormatStrip',
        UI_SHOW_RATINGS: 'showRatings',
        UI_SHOW_TITLES: 'showTitles',
        UI_SHELF_PROPS: 'shelfProps',
        UI_SHELF_PROP_KINDS: 'shelfPropKinds',
        UI_SHELF_PROP_AMOUNT: 'shelfPropAmount'
    };
    var sizeHelp = ['4 per row on a phone', '3 per row on a phone',
        '2 per row on a phone', '2 per row on a phone', '2 per row on a phone'];

    // Forms whose value is a comma list of ticked pieces (hero content, trinkets).
    function hasPieces(form) {
        return !!form.querySelector('[data-hero-piece], [data-display-piece]');
    }

    function hasStepper(form) {
        return form.querySelectorAll('[data-display-step]').length > 0;
    }

    function stored(form) {
        return form.querySelector('input[name="' + form.dataset.displaySetting + '"]');
    }

    function selected(form) {
        if (hasPieces(form)) {
            return Array.from(form.querySelectorAll('[data-hero-piece]:checked, [data-display-piece]:checked')).map(input => input.value).join(',');
        }
        if (hasStepper(form)) return stored(form).value;
        if (form.querySelector('[role="switch"]')) {
            return form.querySelector('[role="switch"]').checked ? 'true' : 'false';
        }
        var button = form.querySelector('[data-display-value][aria-pressed="true"]');
        return button ? button.dataset.displayValue : '';
    }

    function syncSizeRow() {
        if (!sizeRow) return;
        var styleForm = document.querySelector('[data-display-setting="UI_LIBRARY_STYLE"]');
        var style = styleForm ? selected(styleForm) : (stage && stage.dataset.libraryStyle);
        sizeRow.hidden = style === 'spines' || style === 'list';
    }

    function syncFinishRow() {
        var styleForm = document.querySelector('[data-display-setting="UI_LIBRARY_STYLE"]');
        var style = styleForm ? selected(styleForm) : (stage && stage.dataset.libraryStyle);
        var shelves = style === 'spines' || style === 'faceout';
        var propsSwitch = document.getElementById('display-switch-ui_shelf_props');
        if (finishRow) finishRow.hidden = !shelves;
        // Trinkets and How many only matter while Shelf props is on.
        propsRows.forEach(function (row) {
            row.hidden = !shelves || (row.classList.contains('display-props-detail') && !(propsSwitch && propsSwitch.checked));
        });
        if (faceoutRow) faceoutRow.hidden = style !== 'faceout';
    }

    function refreshSizeStepper(form, value) {
        var input = stored(form);
        if (!input) return;
        var n = Math.min(5, Math.max(1, Number(value) || 2));
        input.value = String(n);
        form.querySelectorAll('[data-display-step]').forEach(function (button) {
            var down = button.dataset.displayStep === '-1';
            button.value = String(Math.min(5, Math.max(1, n + (down ? -1 : 1))));
            button.disabled = down ? n === 1 : n === 5;
        });
        form.querySelectorAll('[data-size-dot]').forEach(function (dot) {
            dot.classList.toggle('is-active', Number(dot.dataset.sizeDot) <= n);
        });
        var help = form.querySelector('.display-size-help');
        if (help && form.dataset.displaySetting === 'UI_COVER_SIZE') help.textContent = window.bbT(sizeHelp[n - 1]);
    }

    function syncHeroChoices(form, value) {
        var values = value ? value.split(',') : [];
        var checks = form.querySelectorAll('[data-hero-piece]');
        // The streak sits in the book stats row, so it needs book stats.
        if (!values.includes('stats')) values = values.filter(function (key) { return key !== 'streak'; });
        checks.forEach(function (check) {
            check.checked = values.includes(check.value);
            check.disabled = check.value === 'streak' && !values.includes('stats');
        });
        var hidden = form.querySelector('input[name="UI_HERO_CONTENT"]');
        if (hidden) hidden.value = values.join(',');
    }

    function syncPieces(form, value) {
        if (form.dataset.displaySetting === 'UI_HERO_CONTENT') return syncHeroChoices(form, value);
        var values = value ? value.split(',') : [];
        form.querySelectorAll('[data-display-piece]').forEach(function (check) {
            check.checked = values.includes(check.value);
        });
        stored(form).value = value;
    }

    function setSelected(form, value) {
        if (hasPieces(form)) syncPieces(form, value);
        form.querySelectorAll('[data-display-value]').forEach(function (button) {
            button.setAttribute('aria-pressed', String(button.dataset.displayValue === value));
        });
        var toggle = form.querySelector('[role="switch"]');
        if (toggle) {
            toggle.checked = value === 'true';
            toggle.setAttribute('aria-checked', String(toggle.checked));
        }
        var key = form.dataset.displaySetting;
        if (hasStepper(form)) refreshSizeStepper(form, value);
        if (key === 'UI_LIBRARY_STYLE' || key === 'UI_SHELF_PROPS') {
            syncSizeRow();
            syncFinishRow();
        }
        if (stage) {
            if (key === 'UI_LIBRARY_STYLE') {
                stage.dataset.libraryStyle = value;
                stage.dataset.view = value === 'list' ? 'list' : (value === 'spines' || value === 'faceout' ? 'spines' : 'covers');
                if (stage.dataset.view === 'spines') paintPreviewSpines();
            }
            if (key === 'SERIES_SHOW_CURRENT_BOOK_DETAIL') stage.dataset.seriesOn = value;
            if (key === 'UI_COVER_SIZE') stage.dataset.coverSize = value;
            if (key === 'UI_SHELF_FINISH') stage.dataset.shelfFinish = value;
            if (key === 'UI_COVER_FINISH') stage.dataset.coverFinish = value;
            if (previewSwitches[key]) stage.dataset[previewSwitches[key]] = value;
            if ((key === 'UI_SHELF_PROP_KINDS' || key === 'UI_SHELF_PROP_AMOUNT') && window.bbShelfProps) {
                window.bbShelfProps.place(stage.querySelector('.display-preview-flat'));
            }
        }
        if (key === 'UI_THEME' && window.bbApplyThemePreference) window.bbApplyThemePreference(value);
        if (key === 'UI_ACCENT') document.documentElement.dataset.accent = value;
    }

    function paintPreviewSpines() {
        if (!stage) return;
        stage.querySelectorAll('.library-book-card:not([data-spine-painted])').forEach(function (card) {
            var img = card.querySelector('.cover-image');
            if (!img) return;
            var sample = function () {
                if (!img.naturalWidth) return;
                try {
                    var canvas = document.createElement('canvas');
                    canvas.width = canvas.height = 1;
                    var context = canvas.getContext('2d');
                    context.drawImage(img, 0, 0, 1, 1);
                    var pixels = context.getImageData(0, 0, 1, 1).data;
                    var colour = 'rgb(' + pixels[0] + ',' + pixels[1] + ',' + pixels[2] + ')';
                    card.style.setProperty('--spine', colour);
                    card.style.setProperty('--spine-band', colour);
                    card.style.setProperty('--spine-ink', (pixels[0]*.299+pixels[1]*.587+pixels[2]*.114 > 150) ? '#1c1a17' : '#f3eee4');
                    card.dataset.spinePainted = '';
                } catch (error) { /* Cross-origin covers use the normal spine fallback. */ }
            };
            if (img.complete) sample();
            else img.addEventListener('load', sample, {once: true});
            img.loading = 'eager';
        });
    }

    function showStatus(form, value, error) {
        var group = form.closest('[data-display-group]');
        var label = group && group.querySelector('.display-save-status');
        if (!label) return;
        var prior = statuses.get(group);
        if (prior) window.clearTimeout(prior);
        label.textContent = value;
        label.toggleAttribute('data-visible', true);
        label.toggleAttribute('data-error', !!error);
        statuses.set(group, window.setTimeout(function () {
            label.removeAttribute('data-visible');
            label.textContent = '';
        }, 1500));
    }

    function save(form, value, before) {
        var key = form.dataset.displaySetting;
        if (!key || value === before) return;
        setSelected(form, value);
        var controls = form.querySelectorAll('button, input[type="checkbox"]');
        controls.forEach(function (control) { control.disabled = true; });
        window.fetch('/account/display', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify({[key]: value})
        }).then(function (response) {
            if (!response.ok) throw new Error('Save failed');
            showStatus(form, window.bbT('Saved'));
        }).catch(function () {
            setSelected(form, before);
            if (hasPieces(form)) form.dataset.lastSavedValue = before;
            showStatus(form, window.bbT("Couldn't save"), true);
        }).finally(function () {
            controls.forEach(function (control) { control.disabled = false; });
            if (hasStepper(form)) refreshSizeStepper(form, selected(form));
            if (hasPieces(form)) syncPieces(form, selected(form));
        });
    }

    document.querySelectorAll('.display-page form[data-display-setting]').forEach(function (form) {
        form.addEventListener('click', function (event) {
            var step = event.target.closest('[data-display-step]');
            if (step && form.contains(step)) {
                event.preventDefault();
                if (!step.disabled) save(form, step.value, selected(form));
                return;
            }
            var button = event.target.closest('[data-display-value]');
            if (!button || !form.contains(button)) return;
            event.preventDefault();
            save(form, button.dataset.displayValue, selected(form));
        });
        if (hasPieces(form)) {
            var initial = selected(form);
            syncPieces(form, initial);
            form.addEventListener('change', function (event) {
                if (!event.target.matches('[data-hero-piece], [data-display-piece]')) return;
                var before = form.dataset.lastSavedValue ?? initial;
                var next = selected(form);
                initial = next;
                form.dataset.lastSavedValue = next;
                syncPieces(form, next);
                save(form, next, before);
            });
        }
        var toggle = form.querySelector('[role="switch"]');
        if (toggle) toggle.addEventListener('change', function () {
            save(form, toggle.checked ? 'true' : 'false', toggle.checked ? 'false' : 'true');
        });
        form.addEventListener('submit', function (event) { event.preventDefault(); });
    });
    // Picking a trinket picture uploads it straight away; the page comes back with it listed.
    document.querySelectorAll('[data-trinket-file]').forEach(function (input) {
        input.addEventListener('change', function () { if (input.files.length) input.form.submit(); });
    });
    window.bbDisplaySaveTheme = function (value, previous) {
        var form = document.querySelector('[data-display-setting="UI_THEME"]');
        if (form) save(form, value, previous);
    };
    var initialThemeForm = document.querySelector('[data-display-setting="UI_THEME"]');
    // Only mirror the buttons: setSelected would re-apply the theme and re-fire this event.
    document.addEventListener('bb:theme-preference', function (event) {
        if (!initialThemeForm) return;
        initialThemeForm.querySelectorAll('[data-display-value]').forEach(function (button) {
            button.setAttribute('aria-pressed', String(button.dataset.displayValue === event.detail.value));
        });
    });
    syncSizeRow();
    syncFinishRow();
    if (stage && stage.dataset.view === 'spines') paintPreviewSpines();
    if (stage && window.bbShelfProps) window.bbShelfProps.place(stage.querySelector('.display-preview-flat'));
}());
