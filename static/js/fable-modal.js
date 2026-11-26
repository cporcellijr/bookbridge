var fableModal = {absId: null, book: null};

function showFableState(state) {
    ['loading', 'found', 'manual', 'error'].forEach(function(name) {
        document.getElementById('fb-' + name).style.display = name === state ? 'block' : 'none';
    });
    document.getElementById('fb-link-btn').disabled = state !== 'found';
}

function linkFable(event) {
    event.stopPropagation();
    fableModal.absId = event.currentTarget.dataset.absId;
    fableModal.book = null;
    document.getElementById('fable-modal').style.display = 'flex';
    resolveFableBook('');
}

function closeFableModal() {
    document.getElementById('fable-modal').style.display = 'none';
}

function showFableManualInput() {
    showFableState('manual');
    document.getElementById('fb-input').focus();
}

async function resolveFableManualInput() {
    var value = document.getElementById('fb-input').value.trim();
    if (value) await resolveFableBook(value);
}

async function resolveFableBook(input) {
    showFableState('loading');
    try {
        var query = new URLSearchParams({abs_id: fableModal.absId});
        if (input) query.set('input', input);
        var response = await fetch('/api/fable/resolve?' + query.toString());
        var data = await response.json();
        if (!response.ok || !data.found) {
            throw new Error(data.message || data.error || 'Book not found');
        }
        fableModal.book = data;
        document.getElementById('fb-title').textContent = data.title || '';
        document.getElementById('fb-author').textContent = data.author || '';
        document.getElementById('fb-found-url').href = data.url;
        document.getElementById('fb-link-btn').textContent = data.linked ? 'Linked' : 'Link Book';
        showFableState('found');
    } catch (error) {
        document.getElementById('fb-error-msg').textContent = error.message || 'Search failed';
        showFableState(input ? 'error' : 'manual');
    }
}

async function linkSelectedFableBook() {
    if (!fableModal.book || !fableModal.book.book_id) return;
    var button = document.getElementById('fb-link-btn');
    button.disabled = true;
    button.textContent = bbT('Linking...');
    try {
        var response = await fetch('/link-fable/' + encodeURIComponent(fableModal.absId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({book_id: fableModal.book.book_id})
        });
        var data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || bbT('Link failed'));
        window.location.reload();
    } catch (error) {
        document.getElementById('fb-error-msg').textContent = error.message || bbT('Link failed');
        showFableState('error');
        button.textContent = bbT('Link Book');
    }
}
