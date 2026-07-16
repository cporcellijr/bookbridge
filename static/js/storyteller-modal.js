/**
 * Storyteller Legacy Link Fix Modal
 * Handles searching and linking Storyteller books to existing ABS books.
 */

let currentAbsId = null;

function openStorytellerModal(absId, title) {
    currentAbsId = absId;
    document.getElementById('st-modal-title').textContent = `Link Storyteller: ${title}`;
    document.getElementById('st-modal').classList.remove('hidden');
    document.getElementById('st-search-input').value = title; // Pre-fill with title
    document.getElementById('st-search-input').focus();
    document.getElementById('st-results').innerHTML = ''; // Clear results

    // Auto-search if title is present
    if (title) searchStoryteller();
}

function closeStorytellerModal() {
    document.getElementById('st-modal').classList.add('hidden');
    currentAbsId = null;
}

async function searchStoryteller() {
    const query = document.getElementById('st-search-input').value;
    if (!query) return;

    const resultsDiv = document.getElementById('st-results');
    resultsDiv.innerHTML = '<div class="st-loading">Searching...</div>';

    try {
        const response = await fetch(`/api/storyteller/search?q=${encodeURIComponent(query)}`);
        const books = await response.json();

        resultsDiv.innerHTML = '';

        // [NEW] Always show "None" option to allow unlinking
        const noneCard = document.createElement('div');
        noneCard.className = 'st-result-card st-none-option';
        noneCard.style.border = '1px dashed #666';
        const noneInfo = document.createElement('div');
        noneInfo.className = 'st-card-info';
        const noneTitle = document.createElement('div');
        noneTitle.className = 'st-card-title';
        noneTitle.textContent = 'None - Do not link';
        const noneAuthor = document.createElement('div');
        noneAuthor.className = 'st-card-author';
        noneAuthor.style.fontStyle = 'italic';
        noneAuthor.style.color = '#888';
        noneAuthor.textContent = 'Unlink current Storyteller book';
        noneInfo.appendChild(noneTitle);
        noneInfo.appendChild(noneAuthor);
        noneCard.appendChild(noneInfo);
        const unlinkBtn = document.createElement('button');
        unlinkBtn.className = 'action-btn secondary';
        unlinkBtn.textContent = 'Unlink';
        unlinkBtn.addEventListener('click', function() { linkStoryteller('none'); });
        noneCard.appendChild(unlinkBtn);
        resultsDiv.appendChild(noneCard);

        if (books.length === 0) {
            const noRes = document.createElement('div');
            noRes.className = 'st-no-results';
            noRes.textContent = 'No matching books found via search.';
            resultsDiv.appendChild(noRes);
            return;
        }

        books.forEach(book => {
            const card = document.createElement('div');
            card.className = 'st-result-card';
            const info = document.createElement('div');
            info.className = 'st-card-info';
            const title = document.createElement('div');
            title.className = 'st-card-title';
            title.textContent = book.title || '';
            const author = document.createElement('div');
            author.className = 'st-card-author';
            author.textContent = Array.isArray(book.authors) ? book.authors.join(', ') : '';
            const button = document.createElement('button');
            button.className = 'action-btn success';
            button.textContent = 'Link';
            button.addEventListener('click', () => linkStoryteller(String(book.uuid || '')));
            info.append(title, author);
            card.append(info, button);
            resultsDiv.appendChild(card);
        });

    } catch (e) {
        resultsDiv.textContent = `Error: ${e.message}`;
    }
}

async function linkStoryteller(uuid) {
    if (!currentAbsId) return;

    const resultsDiv = document.getElementById('st-results');
    while (resultsDiv.firstChild) resultsDiv.removeChild(resultsDiv.firstChild);
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'st-loading';
    loadingDiv.textContent = 'Linking and downloading...';
    resultsDiv.appendChild(loadingDiv);

    try {
        const response = await fetch(`/api/storyteller/link/${currentAbsId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ uuid: uuid })
        });

        if (response.ok) {
            window.location.reload();
        } else {
            const err = await response.json();
            throw new Error(err.error || 'Failed to link');
        }
    } catch (e) {
        resultsDiv.textContent = `Link Failed: ${e.message}`;
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    // Close on click outside
    document.getElementById('st-modal').addEventListener('click', (e) => {
        if (e.target === document.getElementById('st-modal')) {
            closeStorytellerModal();
        }
    });

    // Enter key in search
    document.getElementById('st-search-input').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            searchStoryteller();
        }
    });
});
