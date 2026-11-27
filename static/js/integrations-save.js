// Integrations Save is enabled only while the form differs from what the server rendered (#632).
(function () {
    const isToggle = field => field.type === 'checkbox' || field.type === 'radio';

    // The rendered (saved) value; read once, because library lookups replace select options.
    function savedValue(field) {
        if (isToggle(field)) return field.defaultChecked;
        if (field.tagName === 'SELECT') {
            const options = Array.from(field.options);
            const chosen = options.find(option => option.defaultSelected) || options[0];
            return chosen ? chosen.value : '';
        }
        return field.defaultValue;
    }

    const currentValue = field => (isToggle(field) ? field.checked : field.value);

    function watch(bar) {
        const form = bar.closest('form');
        const button = bar.querySelector('button[type="submit"]');
        const note = bar.querySelector('.integrations-dirty');
        if (!form || !button) return;
        const fields = Array.from(form.elements).filter(field =>
            field.name && !['submit', 'button', 'reset'].includes(field.type));
        const saved = new Map(fields.map(field => [field, savedValue(field)]));
        const refresh = function () {
            const dirty = fields.some(field => currentValue(field) !== saved.get(field));
            button.disabled = !dirty;
            if (note) note.hidden = !dirty;
        };
        form.addEventListener('input', refresh);
        form.addEventListener('change', refresh);
        // A field persisted by its own flow (BookFusion linking) becomes the new saved value.
        form.addEventListener('integrations-saved', function (event) {
            if (saved.has(event.target)) saved.set(event.target, currentValue(event.target));
            refresh();
        });
        refresh();
    }

    function init() {
        document.querySelectorAll('.integrations-save-bar').forEach(watch);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
