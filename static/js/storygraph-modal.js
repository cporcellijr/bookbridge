/**
 * StoryGraph Edition Picker Modal
 * Mirrors the Hardcover flow to allow selecting specific editions.
 */

var storygraphModalState = {
    absId: null,
    bookData: null,
    selectedEditionId: null,
    linkedEditionId: null
};

function linkStorygraph(event) {
    event.stopPropagation();
    storygraphModalState.absId = event.currentTarget.dataset.absId;
    storygraphModalState.bookData = null;
    storygraphModalState.selectedEditionId = null;
    openStorygraphModal();
    autoResolveStorygraphBook();
}

function openStorygraphModal() {
    document.getElementById('storygraph-modal').style.display = 'flex';
    showStorygraphState('loading');
}

function closeStorygraphModal() {
    document.getElementById('storygraph-modal').style.display = 'none';
}

function showStorygraphState(state) {
    ['loading', 'found', 'manual', 'error'].forEach(function(s) {
        const el = document.getElementById('sg-' + s);
        if (el) el.style.display = (s === state) ? 'block' : 'none';
    });
    const btn = document.getElementById('sg-link-btn');
    if (btn) btn.disabled = (state !== 'found');
}

async function autoResolveStorygraphBook() {
    showStorygraphState('loading');
    try {
        const resp = await fetch('/api/storygraph/resolve?abs_id=' + storygraphModalState.absId);
        const data = await resp.json();
        if (data && data.found) {
            displayStorygraphBook(data);
            return;
        }
        if (!resp.ok) {
            document.getElementById('sg-error-msg').textContent = (data && data.message) || bbT('Search failed');
            showStorygraphState('error');
            return;
        }
        showStorygraphState('manual');
    } catch (err) {
        showStorygraphState('manual');
    }
}

function showStorygraphManualInput() {
    showStorygraphState('manual');
    document.getElementById('sg-input').value = '';
    document.getElementById('sg-input').focus();
}

async function resolveStorygraphManualInput() {
    const input = document.getElementById('sg-input').value.trim();
    if (!input) return;
    showStorygraphState('loading');
    try {
        const resp = await fetch('/api/storygraph/resolve?abs_id=' + storygraphModalState.absId + '&input=' + encodeURIComponent(input));
        const data = await resp.json();
        if (data && data.found) {
            displayStorygraphBook(data);
            return;
        }
        document.getElementById('sg-error-msg').textContent = (data && data.message) || bbT('Book not found');
        showStorygraphState('error');
    } catch (err) {
        document.getElementById('sg-error-msg').textContent = bbT('Search failed');
        showStorygraphState('error');
    }
}

function getSgFormatIcon(format, isAudio) {
    if (isAudio) return '🎧';
    var f = (format || '').toLowerCase();
    if (f.indexOf('audio') !== -1) return '🎧';
    if (f.indexOf('kindle') !== -1) return '📱';
    if (f.indexOf('ebook') !== -1 || f.indexOf('e-book') !== -1 || f.indexOf('digital') !== -1) return '📱';
    if (f.indexOf('hard') !== -1) return '📕';
    if (f.indexOf('mass market') !== -1) return '📖';
    if (f.indexOf('paper') !== -1 || f.indexOf('soft') !== -1) return '📖';
    return '📚';
}

function formatAudioDuration(seconds) {
    if (!seconds || seconds <= 0) return null;
    var hours = Math.floor(seconds / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    if (hours > 0 && mins > 0) return bbT('{hours}h {minutes}m', { hours: hours, minutes: mins });
    if (hours > 0) return bbT('{hours}h', { hours: hours });
    return bbT('{minutes}m', { minutes: mins });
}

function displayStorygraphBook(data) {
    storygraphModalState.bookData = data;
    storygraphModalState.linkedEditionId = data.linked_edition_id || null;
    
    document.getElementById('sg-title').textContent = data.title || bbT('Unknown Title');
    document.getElementById('sg-author').textContent = data.author || bbT('Unknown Author');

    var link = document.getElementById('sg-found-url');
    if (link) {
        link.href = data.url || '#';
        link.textContent = data.linked ? bbT('Open current StoryGraph link') : bbT('Open in StoryGraph');
    }

    var container = document.getElementById('sg-editions');
    if (container) {
        container.replaceChildren();

        var hasEditions = data.editions && data.editions.length > 0;
        if (hasEditions) {
            var linkedId = data.linked_edition_id ? String(data.linked_edition_id) : null;
            var preSelectId = linkedId || String(data.editions[0].id);

            data.editions.forEach(function(ed) {
                var edId = String(ed.id);
                var isSelected = (edId === preSelectId);
                var isLinked = (edId === linkedId);

                var edIsAudio = (ed.is_audio === true) ||
                    (ed.is_audio === undefined && ed.format && ed.format.toLowerCase().indexOf('audio') !== -1);

                var div = document.createElement('div');
                div.className = 'hc-edition-option' + (isSelected ? ' selected' : '');
                div.dataset.editionId = ed.id;
                div.dataset.pages = ed.pages || '';
                div.dataset.audioSeconds = ed.audio_seconds || '';
                div.dataset.isAudio = edIsAudio ? '1' : '0';
                div.onclick = function() { selectSgEdition(div); };

                var iconDiv = document.createElement('div');
                iconDiv.className = 'hc-edition-icon';
                iconDiv.textContent = getSgFormatIcon(ed.format, edIsAudio);

                var mainDiv = document.createElement('div');
                mainDiv.className = 'hc-edition-main';

                var formatSpan = document.createElement('span');
                formatSpan.className = 'hc-edition-format';
                formatSpan.textContent = ed.format && ed.format !== 'Unknown'
                    ? ed.format
                    : (edIsAudio ? bbT('Audiobook') : bbT('Unknown format'));

                if (isLinked) {
                    var linkedBadge = document.createElement('span');
                    linkedBadge.className = 'hc-edition-linked';
                    linkedBadge.style.marginLeft = '8px';
                    linkedBadge.style.fontSize = '0.7rem';
                    linkedBadge.style.padding = '2px 6px';
                    linkedBadge.style.background = 'rgba(76, 175, 80, 0.2)';
                    linkedBadge.style.color = '#4CAF50';
                    linkedBadge.style.borderRadius = '4px';
                    linkedBadge.textContent = bbT('Linked');
                    formatSpan.appendChild(linkedBadge);
                }

                var detailsDiv = document.createElement('div');
                detailsDiv.className = 'hc-edition-details';

                var detailParts = [];
                if (edIsAudio) {
                    var dur = formatAudioDuration(ed.audio_seconds);
                    if (dur) detailParts.push(dur);
                } else if (ed.pages && ed.pages > 0) {
                    detailParts.push(bbPlural(bbT('{count} pages'), ed.pages));
                }
                if (ed.language) detailParts.push(ed.language);
                detailsDiv.textContent = detailParts.join('  ·  ') || bbT('No details');

                mainDiv.appendChild(formatSpan);
                mainDiv.appendChild(detailsDiv);
                div.appendChild(iconDiv);
                div.appendChild(mainDiv);
                container.appendChild(div);

                if (isSelected) {
                    storygraphModalState.selectedEditionId = ed.id;
                }
            });
        }

        // "No specific edition" option
        var noneDiv = document.createElement('div');
        noneDiv.className = 'hc-edition-option hc-edition-none' + (!hasEditions ? ' selected' : '');
        noneDiv.dataset.editionId = '';
        noneDiv.onclick = function() { selectSgEdition(noneDiv); };

        var noneIcon = document.createElement('div');
        noneIcon.className = 'hc-edition-icon';
        noneIcon.textContent = '—';

        var noneMain = document.createElement('div');
        noneMain.className = 'hc-edition-main';

        var noneFormat = document.createElement('span');
        noneFormat.className = 'hc-edition-format';
        noneFormat.textContent = bbT('No specific edition');

        var noneDetails = document.createElement('div');
        noneDetails.className = 'hc-edition-details';
        noneDetails.textContent = bbT('Track on StoryGraph without progress sync');

        noneMain.appendChild(noneFormat);
        noneMain.appendChild(noneDetails);
        noneDiv.appendChild(noneIcon);
        noneDiv.appendChild(noneMain);
        container.appendChild(noneDiv);

        if (!hasEditions) {
            storygraphModalState.selectedEditionId = null;
        }
    }

    showStorygraphState('found');
}

function selectSgEdition(div) {
    document.querySelectorAll('#sg-editions .hc-edition-option').forEach(function(el) {
        el.classList.remove('selected');
    });
    div.classList.add('selected');
    storygraphModalState.selectedEditionId = div.dataset.editionId || null;
}

async function linkSelectedStorygraphBook() {
    const data = storygraphModalState.bookData;
    const editionId = storygraphModalState.selectedEditionId;
    
    var pages = null;
    var audioSeconds = null;
    if (data.editions) {
        const ed = data.editions.find(function(e) { return e.id == editionId; });
        if (ed) {
            pages = ed.pages;
            audioSeconds = ed.audio_seconds;
        }
    }

    if (!data || !data.book_id) return;

    const button = document.getElementById('sg-link-btn');
    button.disabled = true;
    button.textContent = bbT('Linking...');

    try {
        const resp = await fetch('/link-storygraph/' + storygraphModalState.absId, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                book_id: data.book_id,
                edition_id: editionId,
                pages: pages,
                audio_seconds: audioSeconds,
                title: data.title || '',
                url: data.url || ''
            })
        });

        if (!resp.ok) {
            const payload = await resp.json().catch(function() { return {}; });
            throw new Error(payload.error || bbT('Link failed'));
        }

        closeStorygraphModal();
        window.location.reload();
    } catch (err) {
        const errEl = document.getElementById('sg-error-msg');
        if (errEl) errEl.textContent = err.message || bbT('Link failed');
        showStorygraphState('error');
    } finally {
        button.disabled = false;
        button.textContent = bbT('Link Book');
    }
}
