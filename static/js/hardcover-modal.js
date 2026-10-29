/**
 * Hardcover Edition Picker Modal
 * Allows users to select which edition to use when linking a book to Hardcover.
 */

// Modal state
var hardcoverModalState = {
    absId: null,
    bookData: null,
    selectedEditionId: null,
    linkedEditionId: null
};

function linkHardcover(event) {
    event.stopPropagation();
    hardcoverModalState.absId = event.currentTarget.dataset.absId;
    hardcoverModalState.bookData = null;
    hardcoverModalState.selectedEditionId = null;
    openHardcoverModal();
    autoResolveBook();
}

function openHardcoverModal() {
    document.getElementById('hardcover-modal').style.display = 'flex';
    showHcState('loading');
}

function closeHardcoverModal() {
    document.getElementById('hardcover-modal').style.display = 'none';
}

function showHcState(state) {
    ['loading', 'found', 'manual', 'error'].forEach(function(s) {
        document.getElementById('hc-' + s).style.display = (s === state) ? 'block' : 'none';
    });
    document.getElementById('hc-link-btn').disabled = (state !== 'found');
}

async function autoResolveBook() {
    showHcState('loading');
    try {
        const resp = await fetch('/api/hardcover/resolve?abs_id=' + hardcoverModalState.absId);
        const data = await resp.json();
        if (data && data.found) {
            displayBookWithEditions(data);
            return;
        }
        if (!resp.ok) {
            document.getElementById('hc-error-msg').textContent = (data && data.message) || bbT('Search failed');
            showHcState('error');
            return;
        }
        showHcState('manual');
    } catch (err) {
        showHcState('manual');
    }
}

function showManualInput() {
    showHcState('manual');
    document.getElementById('hc-input').value = '';
    document.getElementById('hc-input').focus();
}

async function resolveManualInput() {
    const input = document.getElementById('hc-input').value.trim();
    if (!input) return;
    showHcState('loading');
    try {
        const resp = await fetch('/api/hardcover/resolve?abs_id=' + hardcoverModalState.absId + '&input=' + encodeURIComponent(input));
        const data = await resp.json();
        if (data && data.found) {
            displayBookWithEditions(data);
            return;
        }
        document.getElementById('hc-error-msg').textContent = (data && data.message) || bbT('Book not found');
        showHcState('error');
    } catch (err) {
        document.getElementById('hc-error-msg').textContent = bbT('Search failed');
        showHcState('error');
    }
}

function getFormatIcon(format) {
    var f = (format || '').toLowerCase();
    if (f.includes('audio') || f.includes('audible')) return '🎧';
    if (f.includes('hard')) return '📕';
    if (f.includes('paper') || f.includes('soft')) return '📖';
    if (f.includes('ebook') || f.includes('kindle') || f.includes('digital')) return '📱';
    return '📚';
}

function createSvgIcon(type) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');

    if (type === 'clock') {
        var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', '12');
        circle.setAttribute('cy', '12');
        circle.setAttribute('r', '10');
        var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M12 6v6l4 2');
        svg.appendChild(circle);
        svg.appendChild(path);
    } else if (type === 'book') {
        var path1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path1.setAttribute('d', 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20');
        var path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path2.setAttribute('d', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z');
        svg.appendChild(path1);
        svg.appendChild(path2);
    }
    return svg;
}

function displayBookWithEditions(data) {
    hardcoverModalState.bookData = data;
    hardcoverModalState.linkedEditionId = data.linked_edition_id || null;
    document.getElementById('hc-title').textContent = data.title || bbT('Unknown Title');
    document.getElementById('hc-author').textContent = data.author || bbT('Unknown Author');

    var container = document.getElementById('hc-editions');
    container.replaceChildren();

    var hasEditions = data.editions && data.editions.length > 0;

    if (hasEditions) {
        // Determine which edition to pre-select (linked edition or first)
        var linkedId = data.linked_edition_id ? String(data.linked_edition_id) : null;
        var preSelectId = linkedId || String(data.editions[0].id);

        // Sort editions so linked edition appears first
        var sortedEditions = data.editions.slice();
        if (linkedId) {
            sortedEditions.sort(function(a, b) {
                if (String(a.id) === linkedId) return -1;
                if (String(b.id) === linkedId) return 1;
                return 0;
            });
        }

        sortedEditions.forEach(function(ed) {
            var edId = String(ed.id);
            var isSelected = (edId === preSelectId);
            var isLinked = (edId === linkedId);

            var div = document.createElement('div');
            div.className = 'hc-edition-option' + (isSelected ? ' selected' : '');
            div.dataset.editionId = ed.id;
            div.dataset.pages = ed.pages || '';
            div.dataset.audioSeconds = ed.audio_seconds || '';
            div.onclick = function() { selectEdition(div); };

            // Icon section
            var iconDiv = document.createElement('div');
            iconDiv.className = 'hc-edition-icon';
            iconDiv.textContent = getFormatIcon(ed.format);

            // Main content section
            var mainDiv = document.createElement('div');
            mainDiv.className = 'hc-edition-main';

            var formatSpan = document.createElement('span');
            formatSpan.className = 'hc-edition-format';
            formatSpan.textContent = ed.format || bbT('Unknown');

            // Add "Currently linked" badge if this is the linked edition
            if (isLinked) {
                var linkedBadge = document.createElement('span');
                linkedBadge.className = 'hc-edition-linked';
                linkedBadge.textContent = bbT('Linked');
                formatSpan.appendChild(linkedBadge);
            }

            var detailsDiv = document.createElement('div');
            detailsDiv.className = 'hc-edition-details';

            // Add duration or pages
            if (ed.audio_seconds && ed.audio_seconds > 0) {
                var hours = Math.floor(ed.audio_seconds / 3600);
                var mins = Math.floor((ed.audio_seconds % 3600) / 60);
                var durDetail = document.createElement('span');
                durDetail.className = 'hc-edition-detail';
                durDetail.appendChild(createSvgIcon('clock'));
                durDetail.appendChild(document.createTextNode(bbT('{hours}h {minutes}m', { hours: hours, minutes: mins })));
                detailsDiv.appendChild(durDetail);
            } else if (ed.pages && ed.pages > 0) {
                var pageDetail = document.createElement('span');
                pageDetail.className = 'hc-edition-detail';
                pageDetail.appendChild(createSvgIcon('book'));
                pageDetail.appendChild(document.createTextNode(bbT('{count} pp', { count: ed.pages })));
                detailsDiv.appendChild(pageDetail);
            }

            // Add year
            if (ed.year) {
                var yearDetail = document.createElement('span');
                yearDetail.className = 'hc-edition-detail';
                yearDetail.textContent = ed.year;
                detailsDiv.appendChild(yearDetail);
            }

            mainDiv.appendChild(formatSpan);
            mainDiv.appendChild(detailsDiv);

            div.appendChild(iconDiv);
            div.appendChild(mainDiv);
            container.appendChild(div);

            if (isSelected) {
                hardcoverModalState.selectedEditionId = ed.id;
            }
        });
    }

    // "No specific edition" option — always appended at the end
    var noneDiv = document.createElement('div');
    noneDiv.className = 'hc-edition-option hc-edition-none' + (!hasEditions ? ' selected' : '');
    noneDiv.dataset.editionId = '';
    noneDiv.dataset.pages = '';
    noneDiv.dataset.audioSeconds = '';
    noneDiv.onclick = function() { selectEdition(noneDiv); };

    var noneIcon = document.createElement('div');
    noneIcon.className = 'hc-edition-icon';
    noneIcon.textContent = '\u2014';

    var noneMain = document.createElement('div');
    noneMain.className = 'hc-edition-main';

    var noneFormat = document.createElement('span');
    noneFormat.className = 'hc-edition-format';
    noneFormat.textContent = bbT('No specific edition');

    var noneDetails = document.createElement('div');
    noneDetails.className = 'hc-edition-details';
    noneDetails.textContent = bbT('Track on Hardcover without progress sync');

    noneMain.appendChild(noneFormat);
    noneMain.appendChild(noneDetails);
    noneDiv.appendChild(noneIcon);
    noneDiv.appendChild(noneMain);
    container.appendChild(noneDiv);

    if (!hasEditions) {
        hardcoverModalState.selectedEditionId = null;
    }

    document.getElementById('hc-link-btn').disabled = false;
    showHcState('found');
}

function formatEditionDetails(ed) {
    var parts = [];
    if (ed.audio_seconds && ed.audio_seconds > 0) {
        var hours = Math.floor(ed.audio_seconds / 3600);
        var mins = Math.floor((ed.audio_seconds % 3600) / 60);
        parts.push(bbT('{hours}h {minutes}m', { hours: hours, minutes: mins }));
    } else if (ed.pages && ed.pages > 0) {
        parts.push(bbT('{count} pp', { count: ed.pages }));
    }
    if (ed.year) {
        parts.push(ed.year);
    }
    return parts.join('  ·  ') || '—';
}

function selectEdition(div) {
    document.querySelectorAll('.hc-edition-option').forEach(function(el) {
        el.classList.remove('selected');
    });
    div.classList.add('selected');
    hardcoverModalState.selectedEditionId = div.dataset.editionId || null;
}

async function linkSelectedEdition() {
    const data = hardcoverModalState.bookData;
    const editionId = hardcoverModalState.selectedEditionId;
    const edition = data.editions.find(function(e) { return e.id == editionId; });

    try {
        const resp = await fetch('/link-hardcover/' + hardcoverModalState.absId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                book_id: data.book_id,
                edition_id: editionId,
                pages: edition ? edition.pages : null,
                audio_seconds: edition ? edition.audio_seconds : null,
                title: data.title,
                slug: data.slug
            })
        });

        if (resp.ok) {
            closeHardcoverModal();
            location.reload();
        } else {
            document.getElementById('hc-error-msg').textContent = bbT('Failed to link book');
            showHcState('error');
        }
    } catch (err) {
        document.getElementById('hc-error-msg').textContent = bbT('Failed to link book');
        showHcState('error');
    }
}
