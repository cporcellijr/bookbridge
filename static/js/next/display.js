(function () {
    'use strict';
    var page = document.querySelector('.display-page');
    if (!page) return;
    page.classList.add('js-display');
    var stage = document.getElementById('display-preview-stage');
    var statuses = new WeakMap();
    var sizeRow = document.querySelector('.display-size-row');
    var finishRow = document.querySelector('.display-finish-row');
    var previewSwitches = {
        UI_SHOW_PROGRESS: 'showProgress',
        UI_SHOW_FINISHED: 'showFinished',
        UI_SHOW_FORMAT_STRIP: 'showFormatStrip',
        UI_SHOW_RATINGS: 'showRatings',
        UI_SHOW_TITLES: 'showTitles'
    };
    var sizeHelp = ['4 per row on a phone', '3 per row on a phone',
        '2 per row on a phone', '2 per row on a phone', '2 per row on a phone'];

    function selected(form) {
        if (form.dataset.displaySetting === 'UI_HERO_CONTENT') {
            return Array.from(form.querySelectorAll('[data-hero-piece]:checked')).map(input => input.value).join(',');
        }
        if (form.dataset.displaySetting === 'UI_COVER_SIZE') {
            return form.querySelector('input[name="UI_COVER_SIZE"]').value;
        }
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
        if (!finishRow) return;
        var styleForm = document.querySelector('[data-display-setting="UI_LIBRARY_STYLE"]');
        var style = styleForm ? selected(styleForm) : (stage && stage.dataset.libraryStyle);
        finishRow.hidden = style !== 'spines' && style !== 'faceout';
    }

    function refreshSizeStepper(form, value) {
        var input = form.querySelector('input[name="UI_COVER_SIZE"]');
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
        if (help) help.textContent = window.bbT(sizeHelp[n - 1]);
    }

    function syncHeroChoices(form, value) {
        var values = value ? value.split(',') : [];
        var checks = form.querySelectorAll('[data-hero-piece]');
        checks.forEach(function (check) {
            check.checked = values.includes(check.value);
            check.disabled = !check.checked && values.length >= 2;
        });
        var hidden = form.querySelector('input[name="UI_HERO_CONTENT"]');
        if (hidden) hidden.value = values.join(',');
    }

    function setSelected(form, value) {
        if (form.dataset.displaySetting === 'UI_HERO_CONTENT') syncHeroChoices(form, value);
        form.querySelectorAll('[data-display-value]').forEach(function (button) {
            button.setAttribute('aria-pressed', String(button.dataset.displayValue === value));
        });
        var toggle = form.querySelector('[role="switch"]');
        if (toggle) {
            toggle.checked = value === 'true';
            toggle.setAttribute('aria-checked', String(toggle.checked));
        }
        var key = form.dataset.displaySetting;
        if (key === 'UI_COVER_SIZE') refreshSizeStepper(form, value);
        if (key === 'UI_LIBRARY_STYLE') {
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
            if (previewSwitches[key]) stage.dataset[previewSwitches[key]] = value;
        }
        if (key === 'UI_THEME' && window.bbApplyThemePreference) window.bbApplyThemePreference(value);
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
            if (key === 'UI_HERO_CONTENT') form.dataset.lastSavedValue = before;
            showStatus(form, window.bbT("Couldn't save"), true);
        }).finally(function () {
            controls.forEach(function (control) { control.disabled = false; });
            if (key === 'UI_COVER_SIZE') refreshSizeStepper(form, selected(form));
            if (key === 'UI_HERO_CONTENT') syncHeroChoices(form, selected(form));
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
        if (form.dataset.displaySetting === 'UI_HERO_CONTENT') {
            var initial = selected(form);
            syncHeroChoices(form, initial);
            form.addEventListener('change', function (event) {
                if (!event.target.matches('[data-hero-piece]')) return;
                var before = form.dataset.lastSavedValue ?? initial;
                var next = selected(form);
                initial = next;
                form.dataset.lastSavedValue = next;
                syncHeroChoices(form, next);
                save(form, next, before);
            });
        }
        var toggle = form.querySelector('[role="switch"]');
        if (toggle) toggle.addEventListener('change', function () {
            save(form, toggle.checked ? 'true' : 'false', toggle.checked ? 'false' : 'true');
        });
        form.addEventListener('submit', function (event) { event.preventDefault(); });
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
}());
