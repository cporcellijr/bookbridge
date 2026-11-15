/* Shared by Classic and New Discover. Native dialog supplies focus trapping and Escape. */
(function () {
    'use strict';
    class ProviderEditionPicker {
        constructor(dialog, labels, fetcher = window.fetch.bind(window)) {
            this.dialog = dialog;
            this.labels = labels;
            this.fetcher = fetcher;
            this.sequence = 0;
            this.confirm = dialog.querySelector('[data-edition-confirm]');
            dialog.querySelectorAll('[data-edition-cancel]').forEach((button) => {
                button.addEventListener('click', () => dialog.close('cancel'));
            });
            this.confirm.addEventListener('click', () => {
                if (this.selected) dialog.close('confirm');
            });
            dialog.addEventListener('close', () => {
                if (dialog.open) return;
                const result = dialog.returnValue === 'confirm' ? this.selected : null;
                this.finish(result);
            });
            dialog.addEventListener('click', (event) => {
                if (event.target !== dialog) return;
                const bounds = dialog.getBoundingClientRect();
                if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close('cancel');
            });
            dialog.addEventListener('keydown', (event) => {
                if (event.key !== 'Tab') return;
                const controls = Array.from(dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), [href], [tabindex="0"]')).filter((node) => node.getClientRects().length);
                const first = controls[0], last = controls[controls.length - 1];
                if (first && ((!event.shiftKey && document.activeElement === last) || (event.shiftKey && document.activeElement === first))) {
                    event.preventDefault();
                    (event.shiftKey ? last : first).focus();
                }
            });
        }

        static isProvider(source) {
            return ['bookorbit', 'booklore', 'grimmory'].includes(String(source || '').toLowerCase());
        }

        static groupMatches(matches) {
            const result = [];
            const books = new Map();
            for (const match of matches) {
                if (!this.isProvider(match.source) || !match.source_id) {
                    result.push(match);
                    continue;
                }
                const source = String(match.source).toLowerCase() === 'bookorbit' ? 'bookorbit' : 'grimmory';
                const key = `${source}:${match.source_id}`;
                if (!books.has(key)) {
                    books.set(key, result.length);
                    result.push(match);
                } else {
                    const index = books.get(key);
                    const previous = result[index];
                    const best = (match.score || 0) > (previous.score || 0) ? match : previous;
                    result[index] = { ...best, linked: !!(previous.linked || match.linked) };
                }
            }
            return result;
        }

        finish(result) {
            const resolve = this.resolve;
            this.resolve = null;
            if (resolve) {
                resolve(result);
                if (this.opener && this.opener.isConnected) this.opener.focus({ preventScroll: true });
            }
        }

        cancel() {
            this.sequence += 1;
            if (this.controller) this.controller.abort();
            this.finish(null);
            if (this.dialog.open) this.dialog.close('cancel');
        }

        async choose({ source, sourceId, title, currentFileId, opener, allowComics = true }) {
            this.cancel();
            const sequence = this.sequence;
            this.controller = new AbortController();
            this.opener = opener;
            const response = await this.fetcher(`/api/ebook-files/${encodeURIComponent(source)}/${encodeURIComponent(sourceId)}`, { signal: this.controller.signal });
            if (sequence !== this.sequence) return null;
            if (!response.ok) throw new Error(this.labels.loadError);
            const data = await response.json();
            if (sequence !== this.sequence) return null;
            const files = (data.files || []).filter((file) => file && typeof file.filename === 'string' && (allowComics || !file.filename.toLowerCase().endsWith('.cbz')));
            if (!files.length) throw new Error(this.labels.noFiles);
            if (files.length === 1) return files[0];
            this.selected = files.find((file) => currentFileId != null && String(file.id) === String(currentFileId)) || null;
            this.dialog.querySelector('[data-edition-provider]').textContent = String(source).toLowerCase() === 'bookorbit' ? 'BookOrbit' : 'Grimmory';
            this.dialog.querySelector('[data-edition-book]').textContent = title;
            const list = this.dialog.querySelector('[data-edition-options]');
            list.replaceChildren(...files.map((file, index) => this.option(file, index, source, files)));
            this.updateSelection(source, files);
            this.dialog.returnValue = '';
            return new Promise((resolve) => {
                this.resolve = resolve;
                this.dialog.showModal();
            });
        }

        updateSelection(source, files) {
            this.confirm.disabled = !this.selected;
            this.dialog.querySelector('[data-edition-selection]').textContent = this.selected ? this.selected.filename : this.labels.emptySelection;
            const ambiguous = this.selected && (!this.selected.primary || files.filter((file) => file.filename.split('.').pop().toLowerCase() === this.selected.filename.split('.').pop().toLowerCase()).length > 1);
            this.dialog.querySelector('[data-edition-warning]').hidden = String(source).toLowerCase() === 'bookorbit' || !ambiguous;
        }

        option(file, index, source, files) {
            const row = document.createElement('label');
            row.className = 'edition-picker-option';
            const icon = document.createElement('span');
            icon.className = 'edition-picker-icon';
            icon.setAttribute('aria-hidden', 'true');
            const path = file.media_overlay ? 'M3 14v-3a9 9 0 0 1 18 0v3M3 13h4v8H5a2 2 0 0 1-2-2zM21 13h-4v8h2a2 2 0 0 0 2-2z' : 'M12 7v14M3 3h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3z';
            icon.innerHTML = `<svg viewBox="0 0 24 24"><path d="${path}"/></svg>`;
            const body = document.createElement('span');
            body.className = 'edition-picker-option-body';
            const heading = document.createElement('span');
            heading.className = 'edition-picker-option-heading';
            const kind = document.createElement('span');
            kind.id = `edition-option-${index}`;
            kind.textContent = file.media_overlay ? this.labels.readAlong : file.filename.toLowerCase().endsWith('.cbz') ? this.labels.comic : file.media_overlay === false ? this.labels.text : this.labels.epub;
            heading.appendChild(kind);
            if (file.primary) {
                const tag = document.createElement('span');
                tag.className = 'edition-picker-tag';
                tag.textContent = this.labels.primary;
                heading.appendChild(tag);
            }
            const description = document.createElement('span');
            description.className = 'edition-picker-description';
            const meta = [file.media_overlay ? this.labels.audioTimings : file.media_overlay === false ? this.labels.textOnly : this.labels.publication];
            if (typeof file.size_bytes === 'number' && file.size_bytes > 0) meta.push(`${(file.size_bytes / 1048576).toLocaleString(undefined, { maximumFractionDigits: 1 })} MiB`);
            description.textContent = meta.join(' · ');
            const filename = document.createElement('span');
            filename.className = 'edition-picker-filename';
            filename.id = `edition-filename-${index}`;
            filename.textContent = file.filename;
            body.append(heading, description, filename);
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = 'provider-edition';
            radio.value = String(file.id);
            radio.checked = file === this.selected;
            radio.setAttribute('aria-labelledby', kind.id);
            radio.setAttribute('aria-describedby', filename.id);
            radio.addEventListener('change', () => {
                if (radio.checked) {
                    this.selected = file;
                    this.updateSelection(source, files);
                }
            });
            row.append(icon, body, radio);
            return row;
        }
    }
    window.BookBridgeEditionPicker = ProviderEditionPicker;
})();
