(function () {
    let actionActive = false;
    let providerOptions = null;
    window.acquisitionJobActionActive = false;
    window.acquisitionJobEditorActive = false;

    function panel() {
        return document.getElementById('acquisition-jobs');
    }

    function activeEditor() {
        const root = panel();
        return root && root.querySelector('[data-provider-editor]:not([hidden])');
    }

    function focusedInsidePanel() {
        const root = panel();
        return !!(root && document.activeElement && root.contains(document.activeElement));
    }

    function showFeedback(message) {
        const target = document.getElementById('acquisition-job-feedback');
        if (!target) return;
        target.textContent = message;
        target.hidden = false;
    }

    function messages() {
        try { return JSON.parse(document.getElementById('acquisition-job-i18n')?.textContent || '{}'); }
        catch (_) { return {}; }
    }

    function explain(data, fallback, kind) {
        const code = data && (data.error || data.reason || (kind && data.failures && data.failures[kind])
            || (data.failures && Object.values(data.failures)[0]));
        return messages().errors?.[code] || fallback || messages().statusUnconfirmed || '';
    }

    async function jsonFetch(url, options) {
        const response = await fetch(url, { cache: 'no-store', ...options,
            headers: { Accept: 'application/json', ...(options && options.headers || {}) } });
        const data = await response.json().catch(() => null);
        return { response, data };
    }

    function setEditor(editor, open) {
        if (!editor) return;
        editor.hidden = !open;
        window.acquisitionJobEditorActive = !!activeEditor();
    }

    function makeButton(label, action, data) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'job-queue-action';
        button.textContent = label;
        button.dataset.providerAction = action;
        Object.entries(data || {}).forEach(([key, value]) => { button.dataset[key] = value; });
        return button;
    }

    function releaseText(release) {
        const values = [release.title, release.author, release.format, release.size, release.source].filter(Boolean);
        return values.join(' · ') || messages().availableCopy || '';
    }

    async function showShelfmarkChoices(controls) {
        const kind = controls.dataset.mediaKind;
        const editor = controls.querySelector('[data-provider-editor]');
        const list = editor && editor.querySelector('[data-release-list]');
        if (!editor || !list) return;
        setEditor(editor, true);
        list.textContent = messages().loading || '';
        const query = new URLSearchParams({
            title: controls.dataset.title || '', author: controls.dataset.author || '', format: kind,
        });
        try {
            const { response, data } = await jsonFetch(`/api/acquisition/shelfmark/releases?${query}`);
            if (!response.ok || !data || !Array.isArray(data.releases)) {
                list.textContent = explain(data, messages().shelfmarkChoicesFailed);
                return;
            }
            list.replaceChildren();
            if (!data.releases.length) {
                list.textContent = messages().noShelfmark || '';
                return;
            }
            data.releases.forEach((release) => {
                const button = makeButton(releaseText(release), 'shelfmark-submit', {
                    jobId: controls.dataset.jobId, mediaKind: kind, releaseKey: release.key,
                });
                list.appendChild(button);
            });
        } catch (error) {
            console.warn('Could not load Shelfmark choices.', error);
            list.textContent = messages().shelfmarkChoicesFailed || '';
        }
    }

    async function showBookOrbitChoices(button) {
        const receipt = panel()?.querySelector(`.acquisition-receipt[data-receipt-id="${CSS.escape(button.dataset.receiptId)}"]`);
        if (!receipt || receipt.dataset.decisionAction !== 'pick_release') return;
        const editor = document.createElement('div');
        editor.className = 'acquisition-provider-editor acquisition-bookorbit-editor';
        editor.dataset.providerEditor = '';
        const list = document.createElement('div');
        list.dataset.releaseList = '';
        list.textContent = messages().loading || '';
        editor.append(list, makeButton(messages().close || '', 'close-editor'));
        receipt.appendChild(editor);
        setEditor(editor, true);
        try {
            const { response, data } = await jsonFetch(`/api/acquisition/${encodeURIComponent(button.dataset.receiptId)}/releases`);
            if (!response.ok || !data || !Array.isArray(data.releases)) {
                list.textContent = explain(data, messages().bookorbitChoicesFailed);
                return;
            }
            list.replaceChildren();
            if (!data.releases.length) {
                list.textContent = messages().noBookOrbit || '';
                return;
            }
            data.releases.forEach((release) => {
                list.appendChild(makeButton(releaseText(release), 'bookorbit-grab', {
                    receiptId: button.dataset.receiptId, releaseKey: release.key,
                }));
            });
        } catch (error) {
            console.warn('Could not load BookOrbit choices.', error);
            list.textContent = messages().bookorbitChoicesFailed || '';
        }
    }

    async function refreshProviderOptions() {
        function showHelp(controls, message) {
            const help = controls.querySelector('[data-provider-help]');
            const link = controls.querySelector('[data-provider-settings]');
            if (!help || !link) return;
            help.textContent = message || '';
            help.hidden = !message;
            link.hidden = !message;
        }
        try {
            const { response, data } = await jsonFetch('/api/acquisition/options');
            if (!response.ok || !data || !data.services) {
                document.querySelectorAll('[data-provider-controls]').forEach((controls) => {
                    showHelp(controls, messages().providerOptionsUnavailable);
                });
                return;
            }
            providerOptions = data.services;
            document.querySelectorAll('[data-provider-controls]').forEach((controls) => {
                const kind = controls.dataset.mediaKind;
                const bo = controls.querySelector('[data-provider-action="bookorbit-submit"]');
                const smFind = controls.querySelector('[data-provider-action="shelfmark-open"]');
                const smBook = controls.querySelector('[data-provider-action="shelfmark-request-book"]');
                if (bo) bo.hidden = !(providerOptions.bookorbit?.usable && providerOptions.bookorbit.formats?.includes(kind));
                const mode = providerOptions.shelfmark?.formats?.[kind];
                if (smFind) smFind.hidden = !(providerOptions.shelfmark?.usable
                    && providerOptions.shelfmark.find_formats?.includes(kind));
                if (smBook) smBook.hidden = !(providerOptions.shelfmark?.usable && mode === 'request_book');
                const eligible = (bo && !bo.hidden) || (smFind && !smFind.hidden) || (smBook && !smBook.hidden);
                if (eligible) {
                    showHelp(controls, '');
                } else {
                    const allDisabled = providerOptions.bookorbit?.reason === 'toggle_off'
                        && providerOptions.shelfmark?.reason === 'toggle_off';
                    showHelp(controls, allDisabled ? messages().providerAccountDisabled : messages().providerUnavailable);
                }
            });
        } catch (error) {
            console.warn('Could not load provider options.', error);
            document.querySelectorAll('[data-provider-controls]').forEach((controls) => {
                showHelp(controls, messages().providerOptionsUnavailable);
            });
        }
    }

    async function validateBookOrbitEditors(mine) {
        const root = panel();
        if (!root) return false;
        const editor = root.querySelector('.acquisition-bookorbit-editor');
        if (!editor) return false;
        const receipt = editor.closest('.acquisition-receipt');
        const fresh = (mine.requests || []).find((row) => String(row.id) === String(receipt?.dataset.receiptId));
        if (!fresh || fresh.decision_action !== 'pick_release' || fresh.status === 'cancelled' || fresh.status === 'paired') {
            editor.remove();
            window.acquisitionJobEditorActive = !!activeEditor();
            return true;
        }
        return false;
    }

    async function refreshPanel(force) {
        const root = panel();
        if (!root || (!force && (document.hidden || actionActive))) return;
        try {
            const { response: mineResponse, data: mine } = await jsonFetch('/api/acquisition/mine');
            let closedStalePicker = false;
            if (mineResponse.ok && mine) {
                closedStalePicker = await validateBookOrbitEditors(mine);
                if (activeEditor() && !force) return;
            } else if (!force && activeEditor()) {
                return;
            }
            if (!force && focusedInsidePanel() && !closedStalePicker) return;
            const response = await fetch(root.dataset.fragmentUrl, {
                headers: { Accept: 'text/html', 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store',
            });
            if (!response.ok) return;
            const text = await response.text();
            const parsed = new DOMParser().parseFromString(text, 'text/html');
            const updated = parsed.getElementById('acquisition-jobs');
            const current = panel();
            if (!updated || !current || (!force && activeEditor())) return;
            current.replaceWith(updated);
            window.acquisitionJobEditorActive = false;
            await refreshProviderOptions();
        } catch (error) {
            console.warn('Could not refresh acquisition job status.', error);
        }
    }

    async function performProviderAction(button) {
        const action = button.dataset.providerAction;
        if (action === 'close-editor') {
            const editor = button.closest('[data-provider-editor]');
            setEditor(editor, false);
            if (editor.classList.contains('acquisition-bookorbit-editor')) editor.remove();
            window.acquisitionJobEditorActive = !!activeEditor();
            return;
        }
        if (action === 'shelfmark-open') {
            const controls = button.closest('[data-provider-controls]');
            if (controls) await showShelfmarkChoices(controls);
            return;
        }
        if (action === 'bookorbit-picker') {
            await showBookOrbitChoices(button);
            return;
        }

        let url;
        let body;
        const controls = button.closest('[data-provider-controls]');
        if (action === 'bookorbit-submit') {
            url = '/api/acquisition/bookorbit';
            body = { job_id: button.closest('[data-provider-controls]').dataset.jobId,
                submit_formats: [button.closest('[data-provider-controls]').dataset.mediaKind] };
        } else if (action === 'shelfmark-request-book') {
            url = '/api/acquisition/shelfmark';
            body = { job_id: controls.dataset.jobId, submit_formats: [controls.dataset.mediaKind] };
        } else if (action === 'shelfmark-submit') {
            url = '/api/acquisition/shelfmark';
            body = { job_id: button.dataset.jobId, submit_formats: [button.dataset.mediaKind],
                format: button.dataset.mediaKind, release_key: button.dataset.releaseKey };
        } else if (action === 'bookorbit-grab') {
            url = `/api/acquisition/${encodeURIComponent(button.dataset.receiptId)}/grab`;
            body = { key: button.dataset.releaseKey };
        } else if (action === 'cancel-receipt') {
            url = `/api/acquisition/${encodeURIComponent(button.dataset.receiptId)}/cancel`;
            body = {};
        } else if (action === 'dismiss-receipt') {
            url = `/api/acquisition/${encodeURIComponent(button.dataset.receiptId)}/dismiss`;
            body = {};
        } else {
            return;
        }

        actionActive = true;
        window.acquisitionJobActionActive = true;
        const root = panel();
        if (root) root.querySelectorAll('[data-provider-action], [data-acquisition-action]').forEach((item) => { item.disabled = true; });
        try {
            const { response, data } = await jsonFetch(url, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            if (!response.ok || !data) {
                const message = explain(data, messages().statusUnconfirmed, body.submit_formats?.[0] || body.format);
                if (action === 'bookorbit-grab' || action === 'shelfmark-submit') {
                    const editor = button.closest('[data-provider-editor]');
                    if (editor) editor.remove();
                    window.acquisitionJobEditorActive = !!activeEditor();
                }
                await refreshPanel(true);
                showFeedback(message);
                return;
            }
            if (action === 'shelfmark-submit' || action === 'bookorbit-grab') {
                const editor = button.closest('[data-provider-editor]');
                if (editor) editor.remove();
                window.acquisitionJobEditorActive = !!activeEditor();
            }
            let feedback = messages().updated;
            if (data.failures && Object.keys(data.failures).length) {
                const code = Object.values(data.failures)[0];
                feedback = explain({ error: code }, messages().actionUnconfirmed,
                    body.submit_formats?.[0] || body.format);
            }
            showFeedback(action === 'cancel-receipt' ? messages().cancelled
                : action === 'dismiss-receipt' ? messages().hidden
                    : feedback);
            await refreshPanel(true);
        } catch (error) {
            console.warn('Provider action could not be confirmed.', error);
            showFeedback(messages().statusUnconfirmed || '');
            await refreshPanel(true);
        } finally {
            actionActive = false;
            window.acquisitionJobActionActive = false;
            const fresh = panel();
            if (fresh) fresh.querySelectorAll('[data-provider-action], [data-acquisition-action]').forEach((item) => { item.disabled = false; });
        }
    }

    document.addEventListener('click', async function (event) {
        const providerButton = event.target.closest('[data-provider-action]');
        if (providerButton && !actionActive) {
            await performProviderAction(providerButton);
            return;
        }
        const button = event.target.closest('[data-acquisition-action]');
        if (!button || actionActive) return;
        const action = button.dataset.acquisitionAction;
        const jobId = button.dataset.jobId;
        if (!jobId) return;
        let url;
        let body;
        if (action === 'dismiss') {
            url = `/api/acquisition/jobs/${encodeURIComponent(jobId)}/dismiss`;
        } else if (action === 'review') {
            const kind = button.dataset.mediaKind;
            url = `/api/acquisition/jobs/${encodeURIComponent(jobId)}/${encodeURIComponent(kind)}/review`;
            body = { source: button.dataset.source, source_id: button.dataset.sourceId };
            if (kind === 'ebook') body.filename = button.dataset.filename;
        } else if (action === 'scan-retry') {
            url = `/api/acquisition/jobs/${encodeURIComponent(jobId)}/scan/retry`;
        } else return;
        actionActive = true;
        window.acquisitionJobActionActive = true;
        try {
            const { response, data } = await jsonFetch(url, {
                method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
            });
            if (!response.ok || !data) showFeedback(explain(data, messages().actionUnconfirmed));
            else await refreshPanel(true);
        } catch (error) {
            console.warn('Acquisition job action failed.', error);
            showFeedback(messages().actionUnconfirmed || '');
        } finally {
            actionActive = false;
            window.acquisitionJobActionActive = false;
        }
    });

    const requested = document.querySelector('[data-requested-focus="true"]');
    if (requested) requested.scrollIntoView({ block: 'center' });
    refreshProviderOptions();
    window.setInterval(() => refreshPanel(false), 20000);
})();
