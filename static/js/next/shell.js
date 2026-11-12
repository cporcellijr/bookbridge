(function () {
    var root = document.documentElement;
    var toggle = document.querySelector('[data-theme-toggle]');
    var preference = root.dataset.themePref || 'auto';
    var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
    var theme = 'dark';

    function applyTheme() {
        theme = preference === 'day' || (preference === 'auto' && media && media.matches) ? 'light' : 'dark';
        root.dataset.theme = theme;
        if (!toggle) return;
        var light = theme === 'light';
        toggle.setAttribute('aria-pressed', light ? 'true' : 'false');
        var label = light ? 'Night theme' : 'Day theme';
        var action = light ? 'Switch to night theme' : 'Switch to day theme';
        var strings = window.BB_STRINGS || {};
        toggle.setAttribute('aria-label', strings[action] || action);
        toggle.setAttribute('title', strings[action] || action);
        var text = toggle.querySelector('.theme-label');
        if (text) text.textContent = strings[label] || label;
    }

    applyTheme();
    if (media) {
        var onDeviceThemeChange = function () {
            if (preference === 'auto') applyTheme();
        };
        if (media.addEventListener) media.addEventListener('change', onDeviceThemeChange);
        else if (media.addListener) media.addListener(onDeviceThemeChange);
    }
    if (toggle) toggle.addEventListener('click', function () {
        preference = theme === 'dark' ? 'day' : 'night';
        root.dataset.themePref = preference;
        applyTheme();
        var themeSelect = document.querySelector('select[name="UI_THEME"]');
        if (themeSelect) themeSelect.value = preference;
        try {
            window.fetch('/account/display', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify({UI_THEME: preference})
            }).catch(function () { /* Keep the theme on this page if saving fails. */ });
        } catch (error) { /* Keep the theme on this page if saving fails. */ }
    });

    // Collapsible rail: icons only, remembered per browser; collapsed links get their label as a tooltip.
    var railToggle = document.querySelector('[data-rail-toggle]');
    function applyRail(collapsed) {
        if (collapsed) root.dataset.rail = 'collapsed';
        else delete root.dataset.rail;
        var label = collapsed ? railToggle.dataset.expandLabel : railToggle.dataset.collapseLabel;
        railToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        railToggle.setAttribute('aria-label', label);
        railToggle.setAttribute('title', label);
        document.querySelectorAll('.rail-nav > a').forEach(function (link) {
            if (collapsed) link.setAttribute('title', (link.querySelector('span:not(.count)') || link).textContent.trim());
            else link.removeAttribute('title');
        });
    }
    if (railToggle) {
        applyRail(root.dataset.rail === 'collapsed');
        railToggle.addEventListener('click', function () {
            var collapsed = root.dataset.rail !== 'collapsed';
            applyRail(collapsed);
            try { window.localStorage.setItem('bookbridge-rail', collapsed ? 'collapsed' : 'expanded'); } catch (error) { /* Still applies to this page. */ }
        });
    }

    document.querySelectorAll('details.user-menu, details.mobile-user-menu, details.mobile-app-menu').forEach(function (menu) {
        var summary = menu.querySelector('summary');
        if (!summary) return;
        menu.addEventListener('toggle', function () {
            summary.setAttribute('aria-expanded', menu.open ? 'true' : 'false');
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && menu.open) {
                menu.open = false;
                summary.focus();
                event.stopPropagation();
            }
        });
        document.addEventListener('click', function (event) {
            if (menu.open && !menu.contains(event.target)) menu.open = false;
        });
    });
}());
