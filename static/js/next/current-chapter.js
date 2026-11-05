(function () {
    'use strict';
    var panel = document.querySelector('[data-current-chapter]');
    if (!panel || !panel.dataset.bookId) return;

    var title = panel.querySelector('[data-chapter-title]');
    var ordinal = panel.querySelector('[data-chapter-position]');
    var time = panel.querySelector('[data-chapter-time]');
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 5000);

    function formatTime(value) {
        var seconds = Math.max(0, Math.floor(Number(value) || 0));
        var hours = Math.floor(seconds / 3600);
        var minutes = String(Math.floor(seconds % 3600 / 60)).padStart(2, '0');
        var remainder = String(seconds % 60).padStart(2, '0');
        return hours + ':' + minutes + ':' + remainder;
    }

    fetch('/api/books/' + encodeURIComponent(panel.dataset.bookId) + '/current-chapter', {
        signal: controller.signal
    }).then(function (response) {
        if (!response.ok) throw new Error('Chapter request failed');
        return response.json();
    }).then(function (data) {
        if (!data.available) return;
        if (!Number.isFinite(data.index) || !Number.isFinite(data.count) ||
                !Number.isFinite(data.elapsed) || !Number.isFinite(data.length)) return;
        if (data.index < 1 || data.count < data.index || data.length <= 0) return;
        if (data.title) {
            title.textContent = data.title;
            title.hidden = false;
        }
        ordinal.textContent = data.index + ' ' + panel.dataset.of + ' ' + data.count;
        time.textContent = formatTime(data.elapsed) + ' / ' + formatTime(data.length);
        panel.hidden = false;
    }).catch(function () {
        // Optional enhancement: missing metadata, network failures, and aborts stay hidden.
    }).finally(function () {
        clearTimeout(timeout);
    });
})();
