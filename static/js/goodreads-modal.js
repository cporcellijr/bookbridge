var goodreadsModal = {absId: null, book: null};

function showGoodreadsState(state) {
    ['loading', 'found', 'manual', 'error'].forEach(function(name) {
        document.getElementById('gr-' + name).style.display = name === state ? 'block' : 'none';
    });
    document.getElementById('gr-link-btn').disabled = state !== 'found';
}

function linkGoodreads(event) {
    event.stopPropagation();
    goodreadsModal.absId = event.currentTarget.dataset.absId;
    goodreadsModal.book = null;
    document.getElementById('goodreads-modal').style.display = 'flex';
    resolveGoodreadsBook('');
}

function closeGoodreadsModal() {
    document.getElementById('goodreads-modal').style.display = 'none';
}

function showGoodreadsManualInput() {
    showGoodreadsState('manual');
    document.getElementById('gr-input').focus();
}

async function resolveGoodreadsManualInput() {
    var value = document.getElementById('gr-input').value.trim();
    if (value) await resolveGoodreadsBook(value);
}

async function resolveGoodreadsBook(input) {
    showGoodreadsState('loading');
    try {
        var query = new URLSearchParams({abs_id: goodreadsModal.absId});
        if (input) query.set('input', input);
        var response = await fetch('/api/goodreads/resolve?' + query.toString());
        var data = await response.json();
        if (!response.ok || !data.found) {
            throw new Error(data.message || data.error || 'Book not found');
        }
        goodreadsModal.book = data;
        document.getElementById('gr-title').textContent = data.title || '';
        document.getElementById('gr-author').textContent = data.author || '';
        document.getElementById('gr-found-url').href = data.url;
        document.getElementById('gr-link-btn').textContent = data.linked ? 'Linked' : 'Link Book';
        showGoodreadsState('found');
    } catch (error) {
        document.getElementById('gr-error-msg').textContent = error.message || 'Search failed';
        showGoodreadsState(input ? 'error' : 'manual');
    }
}

async function linkSelectedGoodreadsBook() {
    if (!goodreadsModal.book || !goodreadsModal.book.book_id) return;
    var button = document.getElementById('gr-link-btn');
    button.disabled = true;
    button.textContent = bbT('Linking...');
    try {
        var response = await fetch('/link-goodreads/' + encodeURIComponent(goodreadsModal.absId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({book_id: goodreadsModal.book.book_id})
        });
        var data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || bbT('Link failed'));
        window.location.reload();
    } catch (error) {
        document.getElementById('gr-error-msg').textContent = error.message || bbT('Link failed');
        showGoodreadsState('error');
        button.textContent = bbT('Link Book');
    }
}
