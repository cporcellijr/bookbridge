(function () {
    var root = document.documentElement;
    var toggle = document.querySelector('[data-theme-toggle]');
    var theme = 'dark';
    try {
        var saved = window.localStorage.getItem('bookbridge-theme');
        if (saved === 'light' || saved === 'dark') theme = saved;
    } catch (error) { /* Storage can be blocked; keep the dark default. */ }

    function applyTheme(value) {
        theme = value;
        root.dataset.theme = value;
        if (!toggle) return;
        var light = value === 'light';
        toggle.setAttribute('aria-pressed', light ? 'true' : 'false');
        var label = light ? 'Night theme' : 'Day theme';
        var action = light ? 'Switch to night theme' : 'Switch to day theme';
        var strings = window.BB_STRINGS || {};
        toggle.setAttribute('aria-label', strings[action] || action);
        toggle.setAttribute('title', strings[action] || action);
        var text = toggle.querySelector('.theme-label');
        if (text) text.textContent = strings[label] || label;
    }

    applyTheme(theme);
    if (toggle) toggle.addEventListener('click', function () {
        var next = theme === 'dark' ? 'light' : 'dark';
        applyTheme(next);
        try { window.localStorage.setItem('bookbridge-theme', next); } catch (error) { /* Theme still works for this page. */ }
    });

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
