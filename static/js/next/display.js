(function () {
    'use strict';
    var page = document.querySelector('.display-page');
    if (!page) return;
    page.classList.add('js-display');
    var stage = document.getElementById('display-preview-stage');
    var statuses = new WeakMap();

    function selected(form) {
        if (form.dataset.displaySetting === 'SERIES_SHOW_CURRENT_BOOK_DETAIL') {
            return form.querySelector('[role="switch"]').checked ? 'true' : 'false';
        }
        var button = form.querySelector('[data-display-value][aria-pressed="true"]');
        return button ? button.dataset.displayValue : '';
    }

    function setSelected(form, value) {
        form.querySelectorAll('[data-display-value]').forEach(function (button) {
            button.setAttribute('aria-pressed', String(button.dataset.displayValue === value));
        });
        var toggle = form.querySelector('[role="switch"]');
        if (toggle) {
            toggle.checked = value === 'true';
            toggle.setAttribute('aria-checked', String(toggle.checked));
        }
        var key = form.dataset.displaySetting;
        if (stage) {
            if (key === 'UI_LIBRARY_STYLE') {
                stage.dataset.libraryStyle = value;
                stage.dataset.view = value === 'list' ? 'list' : (value === 'spines' || value === 'faceout' ? 'spines' : 'covers');
                if (stage.dataset.view === 'spines') paintPreviewSpines();
            }
            if (key === 'SERIES_SHOW_CURRENT_BOOK_DETAIL') stage.dataset.seriesOn = value;
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
            showStatus(form, window.bbT("Couldn't save"), true);
        }).finally(function () {
            controls.forEach(function (control) { control.disabled = false; });
        });
    }

    document.querySelectorAll('.display-page form[data-display-setting]').forEach(function (form) {
        form.addEventListener('click', function (event) {
            var button = event.target.closest('[data-display-value]');
            if (!button || !form.contains(button)) return;
            event.preventDefault();
            save(form, button.dataset.displayValue, selected(form));
        });
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
    if (stage && stage.dataset.view === 'spines') paintPreviewSpines();
}());
