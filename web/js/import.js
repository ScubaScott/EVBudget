/* Import tab: pick bank files, review how each row was classified, then save. */

let importAccountNames = [];      // card / account names already in use, offered when naming a file
let importFiles = [];            // inspected files: {filename, headers, mapping, account_type, is_mapped, selected, imported_rows, ...}
let importRecords = [];          // preview rows
let importRenderLimit = 300;
let importFilter = 'all';
let importSort = { col: 'date', asc: false };
let importFileSort = { col: 'modified', asc: false };   // the file list: newest first by default
let importSummaryData = null;
let importMappingIndex = null;
let importEnvelopes = [];
let importLoaded = false;

const IMPORT_STATUS_LABELS = { new: 'New', existing: 'Already saved', deleted: 'Deleted earlier' };

document.addEventListener('tabchange', (e) => {
    if (e.detail.tab === 'import' && !importLoaded) {
        importLoaded = true;
        loadImportFolder();
        importScanFolder();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    const dz = document.getElementById('importDropzone');
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
        e.preventDefault();
        dz.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) importUploadFiles(e.dataTransfer.files);
    });
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeMappingModal();
            closeImportFolderModal();
            if (review && !document.querySelector('.modal-overlay.active')) closeReview();
        }
    });
});

// ---------------------------------------------------------------------
// Which folder bank files are read from (saved per budget)
// ---------------------------------------------------------------------
let importFolder = null;       // {path, default, is_default, exists}
let importBrowseTimer = null;

async function loadImportFolder() {
    try {
        importFolder = await api('/api/import/folder');
    } catch (err) { return; }
    const label = document.getElementById('importFolderLabel');
    label.textContent = importFolder.path;
    label.title = importFolder.is_default ? "This budget's own input folder" : 'A folder you chose. Use Change folder to go back to the built-in one.';
}

async function openImportFolderModal() {
    await loadImportFolder();
    if (!importFolder) return;
    document.getElementById('importFolderInput').value = importFolder.path;
    document.getElementById('btnImportFolderDefault').hidden = importFolder.is_default;
    document.getElementById('importFolderNote').textContent = importFolder.is_default
        ? "This is the budget's built-in folder." : `The built-in folder is ${importFolder.default}`;
    document.getElementById('importFolderOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('importFolderInput').focus();
}

function closeImportFolderModal() {
    clearTimeout(importBrowseTimer);
    importBrowseTimer = null;
    document.getElementById('btnImportBrowse').disabled = false;
    document.getElementById('importFolderOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

// Opens the operating system's folder chooser on this computer, then waits for the answer
async function importFolderBrowse() {
    const btn = document.getElementById('btnImportBrowse');
    const note = document.getElementById('importFolderNote');
    btn.disabled = true;
    try {
        await apiPost('/api/import/folder/browse');
    } catch (err) {
        btn.disabled = false;
        note.textContent = err.message;
        return;
    }
    note.textContent = 'Choose a folder in the window that just opened (it may be behind this one).';
    const started = Date.now();
    const poll = async () => {
        let st;
        try { st = await api('/api/import/folder/browse'); } catch (err) { st = { state: 'error', message: err.message }; }
        if (st.state === 'waiting' && Date.now() - started < 10 * 60 * 1000) { importBrowseTimer = setTimeout(poll, 700); return; }
        importBrowseTimer = null;
        btn.disabled = false;
        if (st.state === 'done') {
            document.getElementById('importFolderInput').value = st.path;
            note.textContent = 'Press Use this folder to switch to it.';
        } else if (st.state === 'unavailable') {
            note.textContent = "This computer can't show a folder chooser. Paste or type the folder path instead.";
        } else if (st.state === 'error') {
            note.textContent = 'Could not open a folder chooser' + (st.message ? ': ' + st.message : '') + '. Paste or type the folder path instead.';
        } else {
            note.textContent = '';
        }
    };
    poll();
}

async function saveImportFolder(path) {
    const value = path === undefined ? document.getElementById('importFolderInput').value : path;
    try {
        importFolder = await apiPost('/api/import/folder', { path: value });
    } catch (err) {
        document.getElementById('importFolderNote').textContent = err.message;
        return;
    }
    closeImportFolderModal();
    await loadImportFolder();
    importFiles = [];   // the old folder's files (and their ticks) don't apply to the new one
    renderImportFiles();
    await importScanFolder();
    showToast(importFolder.is_default ? 'Using the built-in import folder.' : 'Import folder changed.');
}

function useDefaultImportFolder() {
    saveImportFolder('');
}

// ---------------------------------------------------------------------
// Step 1: files
// ---------------------------------------------------------------------
async function importScanFolder() {
    try {
        const scan = await api('/api/import/scan');
        const previous = new Map(importFiles.map(f => [f.filename, f]));
        if (scan.files.length === 0) {
            importFiles = [];
            renderImportFiles();
            return;
        }
        const inspected = await apiPost('/api/import/inspect', { filenames: scan.files.map(f => f.filename) });
        const meta = new Map(scan.files.map(f => [f.filename, f]));
        importAccountNames = inspected.accounts || [];
        importFiles = inspected.files.map(info => {
            const old = previous.get(info.filename);
            const m = meta.get(info.filename) || {};
            info.modified = m.modified || '';
            info.size = m.size || 0;
            // Keep earlier choices (selection, account type, edited mapping) across a refresh
            if (old) {
                info.selected = old.selected;
                info.account_type = old.account_type;
                info.account_name = old.account_name;
                info.mapping = old.mapping;
                info.is_mapped = old.is_mapped;
            } else {
                info.selected = false;
            }
            return info;
        });
        renderImportFiles();
    } catch (err) {
        showToast('Could not read the input folder: ' + err.message, true);
    }
}

async function importUploadFiles(fileList) {
    if (!fileList || fileList.length === 0) return;
    const form = new FormData();
    Array.from(fileList).forEach(f => form.append('files', f));
    try {
        const res = await fetch('/api/import/upload', { method: 'POST', body: form });
        const data = await res.json();
        if (!res.ok || data.success === false) throw new Error(data.error || 'Upload failed');
        showToast(`Added ${data.saved.length} file${data.saved.length === 1 ? '' : 's'} to the input folder.`);
        await importScanFolder();
        // Pre-select what was just added
        importFiles.forEach(f => { if (data.saved.includes(f.filename)) f.selected = true; });
        renderImportFiles();
    } catch (err) {
        showToast('Upload failed: ' + err.message, true);
    }
}

// Display order for the file list. The row handlers address files by their place in importFiles, so
// sort a list of those places instead of reordering the array.
function importFileOrder() {
    const key = {
        name: f => (f.filename || '').toLowerCase(),
        modified: f => f.modified || '',
        imported: f => f.imported_rows || 0,
        account: f => (f.account_name || '').toLowerCase(),
        type: f => f.account_type || '',
        columns: f => (f.valid && f.is_mapped ? 1 : 0)
    }[importFileSort.col];
    const dir = importFileSort.asc ? 1 : -1;
    return importFiles.map((f, i) => i).sort((a, b) => {
        const x = key(importFiles[a]), y = key(importFiles[b]);
        if (x < y) return -dir;
        if (x > y) return dir;
        const n1 = (importFiles[a].filename || '').toLowerCase(), n2 = (importFiles[b].filename || '').toLowerCase();
        return n1 < n2 ? -dir : n1 > n2 ? dir : 0;
    });
}

function importFilesSortBy(col) {
    if (importFileSort.col === col) importFileSort.asc = !importFileSort.asc;
    else importFileSort = { col, asc: col !== 'modified' && col !== 'imported' };
    renderImportFiles();
}

function renderImportFiles() {
    const wrap = document.getElementById('importFileListWrap');
    const body = document.getElementById('importFileBody');
    wrap.hidden = importFiles.length === 0;
    document.getElementById('importDropzone').classList.toggle('dropzone-compact', importFiles.length > 0);
    ['name', 'modified', 'imported', 'account', 'type', 'columns'].forEach(c => {
        const el = document.getElementById('ifsort_' + c);
        if (el) el.textContent = c === importFileSort.col ? (importFileSort.asc ? '▲' : '▼') : '';
    });
    body.innerHTML = importFileOrder().map(i => {
        const f = importFiles[i];
        if (!f.valid) {
            return `<tr class="import-file-bad"><td></td><td>${escapeHtml(f.filename)}</td><td colspan="5">Can't read this file: ${escapeHtml(f.error || 'unknown problem')}</td></tr>`;
        }
        const isPdf = f.kind === 'pdf';
        const mapped = isPdf
            ? `<span class="status-pill ${f.reconciled ? 'status-included' : 'status-excluded'}">${escapeHtml(f.bank)} statement${f.reconciled ? '' : ': check totals'}</span>`
            : f.is_mapped
            ? `<span class="status-pill status-included">${f.from_preset ? 'Remembered' : 'Detected'}</span>`
            : `<span class="status-pill status-excluded">Needs mapping</span>`;
        const fileSub = isPdf
            ? `${f.row_count} transactions${f.account_last4 ? ', card ending ' + escapeHtml(f.account_last4) : ''}`
              + `${f.period_start ? ', ' + escapeHtml(formatDisplayDate(f.period_start)) + ' to ' + escapeHtml(formatDisplayDate(f.period_end)) : ''}`
            : `${(f.size / 1024).toFixed(0)} KB`;
        const warn = (f.warnings || []).map(w => `<div class="import-file-sub" style="color:var(--accent-red);">${escapeHtml(w)}</div>`).join('');
        const editBtn = isPdf ? '' : `<button type="button" class="btn-inline-action btn-inline-cancel" onclick="openMappingModal(${i})">Edit</button>`;
        const imported = f.imported_rows
            ? `<span class="import-imported">${f.imported_rows.toLocaleString()} rows</span>`
            : '<span class="import-not-yet">Not yet</span>';
        return `<tr class="${f.imported_rows ? 'import-file-done' : ''}">
            <td style="text-align:center;"><input type="checkbox" class="txn-checkbox" ${f.selected ? 'checked' : ''} ${f.is_mapped ? '' : 'disabled'} onchange="importToggleFile(${i}, this.checked)"></td>
            <td><div style="font-weight:500;">${escapeHtml(f.filename)}</div><div class="import-file-sub">${fileSub}</div>${warn}</td>
            <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(f.modified))}</td>
            <td>${imported}</td>
            <td><input type="text" class="input-text import-mini" list="importAccountList" maxlength="60" placeholder="Card name"
                       value="${escapeHtml(f.account_name || '')}" onchange="importFiles[${i}].account_name = this.value.trim()"></td>
            <td>
                <select class="select-input import-mini" onchange="importFiles[${i}].account_type = this.value">
                    <option value="Bank / Checking" ${f.account_type === 'Bank / Checking' ? 'selected' : ''}>Bank / Checking</option>
                    <option value="Credit / Debit" ${f.account_type === 'Credit / Debit' ? 'selected' : ''}>Credit / Debit</option>
                </select>
            </td>
            <td>${mapped} ${editBtn}</td>
        </tr>`;
    }).join('');
    document.getElementById('importAccountList').innerHTML = importAccountNames.map(n => `<option value="${escapeHtml(n)}">`).join('');
    updateImportSelectionHint();
}

function importToggleFile(i, checked) {
    importFiles[i].selected = checked;
    updateImportSelectionHint();
}

function importSelectFiles(mode) {
    importFiles.forEach(f => {
        if (!f.valid || !f.is_mapped) { f.selected = false; return; }
        f.selected = mode === 'all' || (mode === 'new' && !f.imported_rows);
    });
    renderImportFiles();
}

function updateImportSelectionHint() {
    const n = importFiles.filter(f => f.selected).length;
    document.getElementById('importFileCount').textContent = `${importFiles.length} file${importFiles.length === 1 ? '' : 's'} in folder`;
    document.getElementById('importProcessHint').textContent = n ? `${n} file${n === 1 ? '' : 's'} selected` : 'Select at least one file';
    document.getElementById('btnImportProcess').disabled = n === 0;
}

// ---------------------------------------------------------------------
// Column mapping dialog
// ---------------------------------------------------------------------
function openMappingModal(i) {
    importMappingIndex = i;
    const f = importFiles[i];
    document.getElementById('mappingModalFile').textContent = f.filename;
    const opts = (selected, allowNone) => (allowNone ? '<option value="">(None)</option>' : '') +
        (f.headers || []).map(h => `<option value="${escapeHtml(h)}" ${h === selected ? 'selected' : ''}>${escapeHtml(h)}</option>`).join('');
    const field = (id, label, selected, allowNone) =>
        `<div class="field-group"><label for="${id}">${label}</label><select id="${id}" class="select-input">${opts(selected, allowNone)}</select></div>`;
    document.getElementById('mappingModalBody').innerHTML =
        field('mapDate', 'Date column *', f.mapping.date_col, false) +
        field('mapName', 'Payee / description column *', f.mapping.name_col, false) +
        field('mapAmount', 'Amount column (one column)', f.mapping.amount_col, true) +
        field('mapCheck', 'Check number column', f.mapping.check_col, true) +
        field('mapDebit', 'Debit column (money out)', f.mapping.debit_col, true) +
        field('mapCredit', 'Credit column (money in)', f.mapping.credit_col, true) +
        field('mapRef', 'Bank reference ID column', f.mapping.ref_col, true) +
        field('mapType', 'Transaction type column', f.mapping.type_col, true) +
        `<div class="field-group full-col"><label for="mapSign">Amount sign</label>
            <select id="mapSign" class="select-input">
                <option value="standard" ${f.mapping.sign_mode === 'standard' ? 'selected' : ''}>Standard: negative is spending, positive is a deposit (Chase, Wells Fargo)</option>
                <option value="inverted" ${f.mapping.sign_mode === 'inverted' ? 'selected' : ''}>Inverted: positive is a charge, negative is a payment (Synchrony, Apple Card)</option>
            </select></div>`;
    document.getElementById('mappingModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeMappingModal() {
    document.getElementById('mappingModalOverlay').classList.remove('active');
    importMappingIndex = null;
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

function saveMappingModal() {
    if (importMappingIndex === null) return;
    const f = importFiles[importMappingIndex];
    const val = id => document.getElementById(id).value;
    f.mapping = Object.assign({}, f.mapping, {
        date_col: val('mapDate'), name_col: val('mapName'), amount_col: val('mapAmount'),
        check_col: val('mapCheck'), debit_col: val('mapDebit'), credit_col: val('mapCredit'),
        ref_col: val('mapRef'), type_col: val('mapType'), sign_mode: val('mapSign')
    });
    f.mapping.is_dual = Boolean(f.mapping.debit_col && f.mapping.credit_col);
    f.is_mapped = Boolean(f.mapping.date_col && f.mapping.name_col && (f.mapping.amount_col || f.mapping.is_dual));
    f.from_preset = false;
    closeMappingModal();
    renderImportFiles();
    showToast(f.is_mapped ? 'Column mapping updated.' : 'Choose a date, payee, and amount (or debit and credit) column.', !f.is_mapped);
}

// ---------------------------------------------------------------------
// Step 2: preview and review
// ---------------------------------------------------------------------
async function importProcess() {
    const chosen = importFiles.filter(f => f.selected && f.is_mapped);
    if (chosen.length === 0) return;
    const btn = document.getElementById('btnImportProcess');
    btn.disabled = true;
    btn.textContent = 'Reading files…';
    try {
        const data = await apiPost('/api/import/preview', {
            files: chosen.map(f => ({
                filename: f.filename, mapping: f.mapping, account_type: f.account_type, headers: f.headers,
                account_name: f.account_name, memory_key: f.memory_key
            }))
        });
        importRecords = data.records.map(r => Object.assign({ skip: !!(r.likely_duplicate && r.status === 'new'), restore: false }, r));
        importSummaryData = data.summary;
        importEnvelopes = data.envelopes || configuredEnvelopesList;
        peopleList = data.people || peopleList;
        incomeSourcesList = data.income_sources || incomeSourcesList;
        importFilter = importSummaryData.new > 0 ? 'new' : 'all';
        importRenderLimit = 300;
        document.getElementById('importDoneBanner').hidden = true;
        document.getElementById('importReviewSection').hidden = false;
        renderImportSummary();
        importRenderGrid();
        document.getElementById('importReviewSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
        showToast('Could not read the files: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Process & review →';
        updateImportSelectionHint();
    }
}

function renderImportSummary() {
    const s = importSummaryData;
    const card = (cls, title, value, sub) => `
        <div class="kpi-card ${cls}"><div class="kpi-header"><span class="kpi-title">${title}</span></div>
        <div class="kpi-value ${cls}">${value}</div><div class="kpi-subtitle">${sub}</div></div>`;
    renderImportDupNote();
    document.getElementById('importSummary').innerHTML =
        card('income', 'New rows', (s.new - (s.likely_duplicates || 0)).toLocaleString(), 'Will be saved') +
        (s.likely_duplicates ? card('net', 'Look already saved', s.likely_duplicates.toLocaleString(), 'Same date, amount and merchant. Left unticked') : '') +
        card('count', 'Already saved', s.existing.toLocaleString(), 'Left exactly as they are') +
        card('net', 'Need review', s.needs_review_new.toLocaleString(), 'Unmatched deposits and reimbursements') +
        card('spend', 'Excluded', s.excluded_new.toLocaleString(), 'Transfers and card payments') +
        card('count', 'Deleted earlier', (s.deleted || 0).toLocaleString(), 'Not brought back') +
        card('count', 'Skipped', ((s.skipped_bad_date || 0) + (s.duplicates_across_files || 0)).toLocaleString(),
            `${s.skipped_bad_date || 0} unreadable date, ${s.duplicates_across_files || 0} repeated across files`);
}

function importRowMatchesFilter(r) {
    switch (importFilter) {
        case 'new': return r.status === 'new';
        case 'existing': return r.status === 'existing';
        case 'excluded': return !r.is_included;
        case 'duplicate': return !!r.likely_duplicate && r.status === 'new';
        case 'review': return !!r.needs_review && r.status !== 'existing';
        case 'deleted': return r.status === 'deleted';
        default: return true;
    }
}

function importVisibleRows() {
    const q = (document.getElementById('importSearch').value || '').trim().toLowerCase();
    const rows = importRecords
        .map((r, idx) => ({ r, idx }))
        .filter(({ r }) => importRowMatchesFilter(r))
        .filter(({ r }) => !q || [r.name, r.raw_name, r.envelope, r.match_source, r.income_label, String(r.amount), r.date, KIND_LABELS[r.txn_kind]]
            .some(v => (v || '').toString().toLowerCase().includes(q)));
    const dir = importSort.asc ? 1 : -1;
    rows.sort((a, b) => {
        let x = a.r[importSort.col], y = b.r[importSort.col];
        if (importSort.col === 'amount') { x = +x; y = +y; }
        else { x = (x || '').toString().toLowerCase(); y = (y || '').toString().toLowerCase(); }
        if (x < y) return -dir;
        if (x > y) return dir;
        return 0;
    });
    return rows;
}

function importSortBy(col) {
    if (importSort.col === col) importSort.asc = !importSort.asc;
    else importSort = { col, asc: col !== 'date' };
    ['date', 'name', 'amount', 'status', 'match_source'].forEach(c => {
        const el = document.getElementById('isort_' + c);
        if (el) el.textContent = c === importSort.col ? (importSort.asc ? '▲' : '▼') : '';
    });
    importRenderGrid();
}

function setImportFilter(f) {
    importFilter = f;
    importRenderLimit = 300;
    importRenderGrid();
}

function renderImportChips() {
    const count = key => importRecords.filter(r => {
        const saved = importFilter; importFilter = key;
        const ok = importRowMatchesFilter(r);
        importFilter = saved;
        return ok;
    }).length;
    const chips = [
        ['all', 'All'], ['new', 'New'], ...(importSummaryData && importSummaryData.likely_duplicates ? [['duplicate', 'Look already saved']] : []),
        ['existing', 'Already saved'], ['review', 'Needs review'],
        ['excluded', 'Excluded'], ['deleted', 'Deleted earlier']
    ];
    document.getElementById('importChips').innerHTML = chips.map(([key, label]) =>
        `<button type="button" class="preset-btn ${importFilter === key ? 'active' : ''}" onclick="setImportFilter('${key}')">${label} <span class="chip-count">${count(key)}</span></button>`
    ).join('');
}

function importEnvelopeOptions(selected) {
    const names = importEnvelopes.length ? importEnvelopes : configuredEnvelopesList;
    return '<option value="">(No envelope)</option>' + names.map(n =>
        `<option value="${escapeHtml(n)}" ${n === selected ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('');
}

function importKindOptions(selected) {
    return Object.keys(KIND_LABELS).map(k => `<option value="${k}" ${k === selected ? 'selected' : ''}>${KIND_LABELS[k]}</option>`).join('');
}

function importPersonOptions(selected) {
    return '<option value="">(Person?)</option>' + peopleList.map(p =>
        `<option value="${p.id}" ${String(p.id) === String(selected) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('');
}

// What the bank sent, under the cleaned-up name, whenever the two differ
function importOriginalText(r) {
    const bank = (r.raw_name || '').replace(/\s+/g, ' ').trim();
    if (!bank || bank === (r.name || '').trim()) return '';
    return `<div class="import-original" title="What the bank sent: ${escapeHtml(bank)}">${escapeHtml(bank)}</div>`;
}

// One chip per rule that did something to this row; click one to open it. Built-in steps (paycheck, refund match) are plain chips.
function importRuleChips(r) {
    if (r.edited) return '<span class="rule-chip rule-chip-note">You changed this</span>';
    const chips = (r.rules || []).map(x =>
        `<button type="button" class="rule-chip rule-chip-link" title="${escapeHtml(x.did.join(', '))}" onclick="importOpenRule(${x.id})">${escapeHtml(x.name)}</button>`);
    (r.notes || []).forEach(n => chips.push(`<span class="rule-chip rule-chip-note">${escapeHtml(n)}</span>`));
    if (chips.length) return chips.join('');
    return r.match_source ? `<span class="import-source">${escapeHtml(r.match_source)}</span>` : '<span class="import-muted">No rule</span>';
}

async function importOpenRule(id) {
    await showImportSub('rules');
    editImportRule(id);
}

// Under the payee of a row that looks like one already saved, say which one
function importDupHint(r) {
    const d = r.likely_duplicate;
    if (!d || r.status !== 'new') return '';
    const where = [d.account_name, d.source_file].filter(Boolean).join(', ');
    return `<div class="import-dup-hint">Looks like ${escapeHtml(formatDisplayDate(d.date))} &ldquo;${escapeHtml(d.name)}&rdquo;${where ? ' already saved from ' + escapeHtml(where) : ' already saved'}</div>`;
}

// What to do about rows that look like ones already saved, and the tip for a replaced card
function renderImportDupNote() {
    const box = document.getElementById('importDupNote');
    const n = importSummaryData ? (importSummaryData.likely_duplicates || 0) : 0;
    if (!n) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<strong>${n.toLocaleString()} row${n === 1 ? '' : 's'} look like transactions you already saved</strong> (same date, amount and merchant), probably under another account. This happens when a card is reissued with a new number, or a statement covers days an earlier file already had. They start unticked so they are not saved twice.
        Tick one if it really is a separate purchase.
        <button type="button" class="btn-inline-action btn-inline-cancel" onclick="importTickDuplicates(true)">Tick them anyway</button>
        <button type="button" class="btn-inline-action btn-inline-cancel" onclick="setImportFilter('duplicate')">Show only these</button>
        <div class="import-hint" style="margin-top: 6px;">Replacing a card? After saving, Import &rarr; Accounts &rarr; Merge puts the old and new card in one account.</div>`;
}

function importTickDuplicates(tick) {
    importRecords.forEach(r => { if (r.likely_duplicate && r.status === 'new') r.skip = !tick; });
    importRenderGrid();
}

// The Save box in the header: every new row that is currently shown
function importToggleSaveAll(checked) {
    importVisibleRows().forEach(({ r }) => { if (r.status === 'new') r.skip = !checked; });
    importRenderGrid();
}

function updateImportSaveAll() {
    const box = document.getElementById('importSaveAll');
    if (!box) return;
    const shown = importVisibleRows().filter(({ r }) => r.status === 'new');
    const ticked = shown.filter(({ r }) => !r.skip).length;
    box.disabled = shown.length === 0;
    box.checked = shown.length > 0 && ticked === shown.length;
    box.indeterminate = ticked > 0 && ticked < shown.length;
}

function importRowHtml(r, idx) {
    const locked = r.status !== 'new';
    const dis = locked ? 'disabled' : '';
    const isNeg = r.amount < 0;
    const amtClass = INCOME_KINDS.includes(r.txn_kind) ? 'amount-income' : (r.txn_kind === 'refund' ? 'amount-refund' : (EXCLUDED_KINDS.includes(r.txn_kind) ? 'amount-muted' : 'amount-spend'));
    const classes = [];
    if (locked) classes.push('import-row-locked');
    if (r.skip) classes.push('import-row-skipped');
    if (!r.is_included) classes.push('excluded-row');
    if (r.needs_review && r.status === 'new') classes.push('import-row-review');
    if (r.likely_duplicate && r.status === 'new') classes.push('import-row-dup');

    let target = '';
    if (INCOME_KINDS.includes(r.txn_kind)) {
        target = `<select class="select-input import-mini" ${dis} onchange="importSetField(${idx}, 'person_id', this.value ? parseInt(this.value, 10) : null)">${importPersonOptions(r.person_id)}</select>`;
        if (r.txn_kind === 'paycheck') {
            target += `<select class="select-input import-mini" ${dis} onchange="importSetField(${idx}, 'income_subtype', this.value)">
                <option value="regular" ${r.income_subtype !== 'bonus' ? 'selected' : ''}>Regular</option>
                <option value="bonus" ${r.income_subtype === 'bonus' ? 'selected' : ''}>Bonus</option></select>`;
        } else {
            target += `<input type="text" class="input-text import-mini" ${dis} placeholder="Label" value="${escapeHtml(r.income_label || '')}" onchange="importSetField(${idx}, 'income_label', this.value)">`;
        }
    } else if (EXCLUDED_KINDS.includes(r.txn_kind)) {
        target = `<span class="import-muted">${escapeHtml(r.exclusion_reason || 'Excluded')}</span>`;
    } else {
        target = `<select class="select-input import-mini" ${dis} onchange="importSetEnvelope(${idx}, this.value)">${importEnvelopeOptions(r.envelope)}</select>`;
        if (r.txn_kind === 'refund' && !locked) {
            target += `<button type="button" class="btn-inline-action btn-inline-save" title="Pick the expense this money pays back" onclick="importMatchExpense(${idx})">Match…</button>`;
        }
    }

    let statusCell = `<span class="import-status import-status-${r.status}">${IMPORT_STATUS_LABELS[r.status] || r.status}</span>`;
    if (r.needs_review && r.status === 'new') statusCell += ' <span class="kind-badge kind-review" title="Needs a decision">Review</span>';
    if (r.likely_duplicate && r.status === 'new') statusCell += ' <span class="kind-badge kind-review" title="This looks like a transaction you already saved. Tick the box if it is a different one.">Looks saved</span>';
    if (r.status === 'deleted') statusCell += ` <button type="button" class="btn-inline-action btn-inline-save" onclick="importRestore(${idx})">Restore</button>`;

    const saveCell = r.status === 'new'
        ? `<input type="checkbox" class="txn-checkbox" ${r.skip ? '' : 'checked'} title="Save this row" onchange="importSetSkip(${idx}, !this.checked)">`
        : '';
    const inclCell = `<input type="checkbox" class="txn-checkbox" ${r.is_included ? 'checked' : ''} ${dis} onchange="importSetIncluded(${idx}, this.checked)">`;

    return `<tr class="${classes.join(' ')}" data-idx="${idx}">
        <td style="text-align:center;">${saveCell}</td>
        <td style="text-align:center;">${inclCell}</td>
        <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(r.date))}</td>
        <td><input type="text" class="input-text import-name" ${dis} value="${escapeHtml(r.name)}" title="${escapeHtml(r.raw_name)}" onchange="importSetField(${idx}, 'name', this.value)">${importOriginalText(r)}${importDupHint(r)}</td>
        <td class="amount-cell ${amtClass}">${isNeg ? '-' : '+'}${formatCurrency(Math.abs(r.amount))}</td>
        <td><select class="select-input import-mini import-kind" ${dis} onchange="importSetKind(${idx}, this.value)">${importKindOptions(r.txn_kind)}</select></td>
        <td><div class="import-target">${target}</div></td>
        <td>${statusCell}</td>
        <td>${importRuleChips(r)}</td>
        <td>${locked ? '' : `<button type="button" class="btn-inline-action btn-inline-cancel" title="Look this merchant up online" onclick="importLookup(${idx})">&#128269;</button>`}</td>
    </tr>`;
}

function importRenderGrid() {
    renderImportChips();
    const rows = importVisibleRows();
    const shown = rows.slice(0, importRenderLimit);
    let html = shown.map(({ r, idx }) => importRowHtml(r, idx)).join('');
    if (rows.length > shown.length) {
        html += `<tr><td colspan="10" class="import-more"><button type="button" class="btn-bulk-action" onclick="importRenderLimit += 400; importRenderGrid();">Show ${Math.min(400, rows.length - shown.length)} more (${rows.length - shown.length} hidden)</button></td></tr>`;
    }
    if (rows.length === 0) {
        html = '<tr><td colspan="10" class="import-more">No rows match this filter.</td></tr>';
    }
    document.getElementById('importGridBody').innerHTML = html;
    updateImportSaveBar();
    updateImportSaveAll();
}

function updateImportSaveBar() {
    const toSave = importRecords.filter(r => (r.status === 'new' && !r.skip) || (r.status === 'deleted' && r.restore));
    const review = toSave.filter(r => r.needs_review).length;
    const btn = document.getElementById('btnImportCommit');
    btn.disabled = toSave.length === 0;
    btn.textContent = toSave.length === 0 ? 'Nothing new to save' : `Save ${toSave.length.toLocaleString()} new transaction${toSave.length === 1 ? '' : 's'}`;
    document.getElementById('importSaveHint').textContent = review
        ? `${review} of these still need a decision. You can save now and finish them later under Analytics → Needs review.`
        : (toSave.length ? 'Everything looks decided.' : '');
}

// ---------------------------------------------------------------------
// Editing rows
// ---------------------------------------------------------------------
function importTouch(r) {
    r.edited = true;
    if (!/^User/.test(r.match_source || '')) r.match_source = 'User Override';
    r.rules = [];
    r.notes = [];
}

function importSetSkip(idx, skip) {
    importRecords[idx].skip = skip;
    importRenderGrid();
}

function importRestore(idx) {
    const r = importRecords[idx];
    r.status = 'new';
    r.restore = true;
    importTouch(r);
    importRenderGrid();
}

function importSetIncluded(idx, included) {
    const r = importRecords[idx];
    r.is_included = included;
    r.exclusion_reason = included ? '' : (r.exclusion_reason || 'User Excluded');
    importTouch(r);
    importRenderGrid();
}

function importSetField(idx, field, value) {
    const r = importRecords[idx];
    r[field] = value;
    if (field === 'person_id' && value) r.needs_review = 0;
    importTouch(r);
    importRenderGrid();
}

function importSetKind(idx, kind) {
    const r = importRecords[idx];
    r.txn_kind = kind;
    if (EXCLUDED_KINDS.includes(kind)) {
        r.is_included = false;
        r.exclusion_reason = kind === 'transfer' ? 'Internal Account Transfer' : 'Credit Card Payment / Autopay';
        r.envelope = '';
    } else {
        r.is_included = true;
        r.exclusion_reason = '';
    }
    if (kind !== 'paycheck') r.income_subtype = null;
    if (!INCOME_KINDS.includes(kind)) { r.person_id = null; r.income_label = null; }
    if (kind === 'paycheck' && !r.income_subtype) r.income_subtype = 'regular';
    if (INCOME_KINDS.includes(kind)) r.envelope = '';
    if (kind !== 'refund') r.reimburses_txn_id = null;
    importTouch(r);
    importRenderGrid();
}

async function importSetEnvelope(idx, envelope) {
    const r = importRecords[idx];
    r.envelope = envelope;
    if (r.txn_kind === 'refund' && envelope) r.needs_review = 0;
    importTouch(r);
    importRenderGrid();

    // Offer to remember the choice for spending rows
    if (r.txn_kind !== 'expense' || !envelope) return;
    const phrase = (r.name || '').replace(/CHECK #[0-9]+/g, '').trim();
    if (!phrase) return;
    if (!confirm(`Always send "${phrase}" to "${envelope}" from now on?`)) return;
    try {
        const learned = await apiPost('/api/rules/learn', { phrase, envelope });
        let updated = 0;
        importRecords.forEach(o => {
            if (o.status === 'new' && !o.edited && o.txn_kind === 'expense' && (o.name || '').toUpperCase().includes(phrase.toUpperCase())) {
                o.envelope = envelope;
                o.match_source = phrase;
                o.rules = [{ id: learned.id, name: phrase, did: ['→ ' + envelope] }];
                o.notes = [];
                updated++;
            }
        });
        importRenderGrid();
        showToast(`Rule saved: "${phrase}" → ${envelope}` + (updated ? ` (${updated} other row${updated === 1 ? '' : 's'} updated)` : ''));
    } catch (err) {
        showToast('Could not save the rule: ' + err.message, true);
    }
}

function importMatchExpense(idx) {
    const r = importRecords[idx];
    openMatchModal({ date: r.date, amount: r.amount, label: r.name }, (cand) => {
        r.envelope = cand.envelope || '';
        r.reimburses_txn_id = cand.txn_id;
        r.needs_review = 0;
        importTouch(r);
        importRenderGrid();
    });
}

async function importLookup(idx) {
    const r = importRecords[idx];
    showToast(`Looking up "${r.name}"…`);
    try {
        const data = await apiPost('/api/import/vendor-lookup', { query: r.name });
        if (!data.envelope) { showToast('No category suggestion found for ' + r.name); return; }
        if (confirm(`Web research suggests:\nEnvelope: ${data.envelope}\nConfidence: ${data.confidence}\n${data.snippet || ''}\n\nUse this envelope for "${r.name}"?`)) {
            if (r.txn_kind !== 'expense') importSetKind(idx, 'expense');
            importSetEnvelope(idx, data.envelope);
        }
    } catch (err) {
        showToast('Lookup failed: ' + err.message, true);
    }
}

// ---------------------------------------------------------------------
// Save
// ---------------------------------------------------------------------
async function importCommit() {
    const toSend = importRecords.filter(r =>
        (r.status === 'new' && !r.skip) || (r.status === 'deleted' && r.restore) || r.status === 'existing');
    const newCount = toSend.filter(r => r.status === 'new' || r.status === 'deleted').length;
    if (newCount === 0) return;
    const btn = document.getElementById('btnImportCommit');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
        const files = importFiles.filter(f => f.selected).map(f => f.filename);
        const result = await apiPost('/api/import/commit', { records: toSend, files });
        const banner = document.getElementById('importDoneBanner');
        banner.hidden = false;
        banner.innerHTML = `
            <div><strong>Saved ${result.inserted.toLocaleString()} new transaction${result.inserted === 1 ? '' : 's'}.</strong>
            ${result.existing ? `${result.existing.toLocaleString()} were already in the database and were left alone. ` : ''}
            ${result.rejected ? `${result.rejected} row${result.rejected === 1 ? ' was' : 's were'} skipped because the data was invalid. ` : ''}
            ${result.skipped_deleted ? `${result.skipped_deleted} deleted earlier, not brought back. ` : ''}
            ${result.needs_review ? `${result.needs_review} still need review.` : ''}</div>
            <div class="import-done-actions">
                ${result.needs_review ? '<button type="button" class="btn-modal-save" onclick="goToNeedsReview()">Review them</button>' : ''}
                <button type="button" class="btn-modal-cancel" onclick="showTab(\'analytics\')">Open Analytics</button>
            </div>`;
        showToast(`Saved ${result.inserted} new transaction${result.inserted === 1 ? '' : 's'}.`);
        const flagged = result.audit ? result.audit.total : 0;
        if (flagged > 0) {
            banner.innerHTML += `<div class="import-done-actions audit-alert"><strong>&#9888; ${flagged} thing${flagged === 1 ? '' : 's'} in this import worth a look.</strong>
                <button type="button" class="btn-modal-save" onclick="openAuditDialog({ mode: 'import' })">Review</button></div>`;
        }
        loadAuditBadge();
        if (flagged > 0 && result.audit_auto_open) openAuditDialog({ mode: 'import', results: result.audit });
        await importScanFolder();
        await refreshTransactions();
        // Re-run the preview so the grid now shows these as saved
        await importProcess();
        banner.hidden = false;
    } catch (err) {
        showToast('Save failed: ' + err.message, true);
        btn.disabled = false;
        updateImportSaveBar();
    }
}

// ---------------------------------------------------------------------
// Rules dialog
// ---------------------------------------------------------------------
let rulesData = [];            // every rule, in the order they run
let rulesEnvelopes = [];
let ruleFilter = 'all';
let editingRuleId = null;
let rulePreviewTimer = null;
let rulePreviewSeq = 0;

const RULE_KIND_LABELS = {
    expense: 'spending', refund: 'a refund', other_income: 'other income',
    transfer: 'a transfer (left out)', cc_payment: 'a card payment (left out)', paycheck: 'a paycheck'
};

// ---------------------------------------------------------------------
// Import | Rules | History
// ---------------------------------------------------------------------
let importSub = 'import';

// keepForm: the caller is about to fill the rule form itself, so don't clear it
async function showImportSub(name, keepForm) {
    importSub = name;
    ['import', 'accounts', 'rules', 'history'].forEach(n => {
        document.getElementById('importSub' + n.charAt(0).toUpperCase() + n.slice(1)).hidden = (n !== name);
        const btn = document.getElementById('importSubBtn-' + n);
        btn.classList.toggle('active', n === name);
        btn.setAttribute('aria-selected', String(n === name));
    });
    if (name !== 'rules') closeReview();
    if (name === 'rules' && !keepForm) {
        await loadImportRules();
        resetRuleForm();
    }
    if (name === 'history') loadImportHistory();
    if (name === 'accounts') loadAccounts();
}

async function loadImportRules() {
    try {
        const data = await api('/api/rules');
        rulesData = data.rules;
        rulesEnvelopes = data.envelopes;
        document.getElementById('ruleEnvelope').innerHTML = rulesEnvelopes.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
        renderImportRules();
    } catch (err) {
        document.getElementById('rulesBody').innerHTML = `<tr><td colspan="5">Could not load rules: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function setRuleFilter(filter) {
    ruleFilter = filter;
    document.querySelectorAll('#ruleFilterToggle .segmented-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
    renderImportRules();
}

function ruleFinds(r) {
    const code = (t) => `<code>${escapeHtml(t)}</code>`;
    let what;
    if (r.filter_type === 'check') what = 'any check';
    else if (r.filter_type === 'payment') what = 'anything the bank marks as a payment';
    else if (r.filter_type === 'all') what = 'everything';
    else if (r.match_mode === 'regex') what = 'pattern ' + code(r.phrase);
    else if (r.match_mode === 'word') what = 'whole word ' + code(r.phrase);
    else what = 'contains ' + code(r.phrase);
    const dir = { out: 'money out', in: 'money in' }[r.direction];
    return what + (dir ? ` <span class="rule-dir">${dir}</span>` : '');
}

function ruleDoes(r) {
    const chips = [];
    if (r.set_payee) chips.push(`rename to <b>${escapeHtml(r.set_payee)}</b>`);
    if (r.set_envelope) chips.push(`&rarr; <b>${escapeHtml(r.set_envelope)}</b>`);
    if (r.set_kind) chips.push(`treat as ${RULE_KIND_LABELS[r.set_kind] || escapeHtml(r.set_kind)}${r.set_income_label ? ' (' + escapeHtml(r.set_income_label) + ')' : ''}`);
    if (r.set_needs_review) chips.push('flag for review');
    return chips.map(c => `<span class="rule-chip">${c}</span>`).join(' ') || '<span class="import-muted">Nothing</span>';
}

function ruleGroupMatches(r) {
    switch (ruleFilter) {
        case 'rename': return !!r.set_payee;
        case 'envelope': return !!r.set_envelope;
        case 'kind': return !!r.set_kind;
        case 'attention': return !!r.problem;
        default: return true;
    }
}

function renderImportRules() {
    const q = (document.getElementById('ruleSearch').value || '').trim().toLowerCase();
    const attention = rulesData.filter(r => r.problem).length;
    document.getElementById('ruleAttentionCount').textContent = attention ? `(${attention})` : '';
    const last = rulesData.length - 1;
    const rows = rulesData.map((r, i) => ({ r, i }))
        .filter(({ r }) => ruleGroupMatches(r))
        .filter(({ r }) => !q || [r.name, r.phrase, r.set_payee, r.set_envelope, r.set_kind].some(v => (v || '').toLowerCase().includes(q)));
    document.getElementById('rulesBody').innerHTML = rows.length === 0
        ? '<tr><td colspan="5" class="import-more">No rules here.</td></tr>'
        : rows.slice(0, 400).map(({ r, i }) => `<tr class="${r.active ? '' : 'excluded-row'}">
            <td style="text-align:right; color: var(--text-muted);">${i + 1}</td>
            <td><div class="rule-name">${escapeHtml(r.name || r.phrase)}</div>
                <div class="rule-finds">${ruleFinds(r)}</div>
                ${r.problem ? `<div class="rule-problem">${escapeHtml(r.problem)}</div>` : ''}</td>
            <td>${ruleDoes(r)}</td>
            <td style="text-align:center;"><input type="checkbox" class="txn-checkbox" ${r.active ? 'checked' : ''} onchange="toggleImportRule(${r.id}, this.checked)"></td>
            <td style="white-space:nowrap;">
                <button type="button" class="btn-env-icon" title="Move to the top" ${i === 0 ? 'disabled' : ''} onclick="moveImportRule(${r.id}, 'top')">&#10514;</button>
                <button type="button" class="btn-env-icon" title="Move up" ${i === 0 ? 'disabled' : ''} onclick="moveImportRule(${r.id}, 'up')">&uarr;</button>
                <button type="button" class="btn-env-icon" title="Move down" ${i === last ? 'disabled' : ''} onclick="moveImportRule(${r.id}, 'down')">&darr;</button>
                <button type="button" class="btn-env-icon" title="Move to the bottom" ${i === last ? 'disabled' : ''} onclick="moveImportRule(${r.id}, 'bottom')">&#10515;</button>
                <button type="button" class="btn-inline-action btn-inline-cancel" title="Preview what this rule would change in saved transactions, and choose which to update" onclick="reviewSavedRule(${r.id})">Apply&hellip;</button>
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="editImportRule(${r.id})">Edit</button>
                <button type="button" class="btn-env-icon delete" title="Delete rule" onclick="deleteImportRule(${r.id})">&times;</button></td>
        </tr>`).join('') + (rows.length > 400 ? `<tr><td colspan="5" class="import-more">Showing 400 of ${rows.length}. Use search to narrow.</td></tr>` : '');
}

// ----- the form -------------------------------------------------------

const ruleVal = (id) => document.getElementById(id).value;
const ruleChecked = (id) => document.getElementById(id).checked;

function ruleFormData() {
    const text = ruleVal('ruleFindType') === 'text';
    const kind = ruleChecked('ruleDoKind') ? ruleVal('ruleKind') : null;
    const orig = editingRuleId ? rulesData.find(x => x.id === editingRuleId) : null;
    return {
        name: ruleVal('ruleName').trim(),
        filter_type: ruleVal('ruleFindType'),
        phrase: text ? ruleVal('ruleFindText').trim() : '',
        match_mode: ruleVal('ruleMatchMode'),
        direction: ruleVal('ruleDirection'),
        set_payee: text && ruleChecked('ruleDoRename') ? ruleVal('ruleRenameTo').trim() : null,
        set_envelope: ruleChecked('ruleDoEnvelope') ? ruleVal('ruleEnvelope') : null,
        set_kind: kind,
        set_income_label: kind === 'other_income' ? ruleVal('ruleIncomeLabel').trim() : null,
        exclusion_reason: kind && orig && orig.set_kind === kind ? orig.exclusion_reason : null,
        set_needs_review: ruleChecked('ruleDoReview') ? 1 : 0
    };
}

// Shows only what applies, and keeps the name in step with what you type until you give it your own
function onRuleFormChange() {
    const text = ruleVal('ruleFindType') === 'text';
    document.getElementById('ruleFindText').hidden = !text;
    document.getElementById('ruleMatchMode').hidden = !text;
    document.getElementById('ruleRegexHint').hidden = !(text && ruleVal('ruleMatchMode') === 'regex');
    document.getElementById('ruleFindText').placeholder = ruleVal('ruleMatchMode') === 'regex' ? 'Pattern (e.g. SQ\\s?\\*\\s?(.*))' : 'Text to look for (e.g. SAMS CLUB)';
    const rename = document.getElementById('ruleDoRename');
    rename.disabled = !text;
    if (!text) rename.checked = false;
    document.getElementById('ruleRenameTo').disabled = !rename.checked;
    document.getElementById('ruleRenameTo').placeholder = ruleVal('ruleMatchMode') === 'regex' ? 'Replace with (e.g. \\1)' : 'New name (e.g. Sam\'s Club)';
    document.getElementById('ruleEnvelope').disabled = !ruleChecked('ruleDoEnvelope');
    document.getElementById('ruleKind').disabled = !ruleChecked('ruleDoKind');
    document.getElementById('ruleIncomeLabel').hidden = !(ruleChecked('ruleDoKind') && ruleVal('ruleKind') === 'other_income');
    const nameBox = document.getElementById('ruleName');
    if (nameBox.dataset.touched !== '1') {
        nameBox.value = (rename.checked && ruleVal('ruleRenameTo').trim() && ruleVal('ruleMatchMode') !== 'regex' ? ruleVal('ruleRenameTo') : ruleVal('ruleFindText')).trim();
    }
    scheduleRulePreview();
}

function scheduleRulePreview() {
    clearTimeout(rulePreviewTimer);
    const box = document.getElementById('ruleEditorPreview');
    const seq = ++rulePreviewSeq;
    rulePreviewTimer = setTimeout(async () => {
        try {
            const p = await apiPost('/api/rules/preview', ruleFormData());
            if (seq !== rulePreviewSeq) return;
            if (p.count === null) { box.innerHTML = `<span class="import-muted">${escapeHtml(p.message || '')}</span>`; return; }
            const parts = [`Matches <strong>${p.count}</strong> saved transaction${p.count === 1 ? '' : 's'}`];
            if (p.would_change) parts.push(`would change <strong>${p.would_change}</strong>`);
            if (p.locked) parts.push(`${p.locked} you edited by hand ${p.locked === 1 ? 'is' : 'are'} left alone`);
            const ex = p.examples.slice(0, 4).map(e => {
                const bits = [];
                if (e.after_name) bits.push(`<code>${escapeHtml(e.name)}</code> &rarr; <code>${escapeHtml(e.after_name)}</code>`);
                if (e.after_envelope) bits.push(`${escapeHtml(e.after_name || e.name)}: ${escapeHtml(e.envelope || 'no envelope')} &rarr; ${escapeHtml(e.after_envelope)}`);
                if (e.after_kind) bits.push(`${escapeHtml(e.after_name || e.name)}: becomes ${escapeHtml(KIND_LABELS[e.after_kind] || e.after_kind)}`);
                return bits.join(', ');
            }).filter(Boolean);
            box.innerHTML = parts.join(' &middot; ') + (ex.length ? '<br>' + ex.join('<br>') : '');
        } catch (err) {
            if (seq === rulePreviewSeq) box.innerHTML = `<span class="tpl-short">${escapeHtml(err.message)}</span>`;
        }
    }, 300);
}

function resetRuleForm() {
    editingRuleId = null;
    document.getElementById('ruleName').value = '';
    document.getElementById('ruleName').dataset.touched = '';
    document.getElementById('ruleFindType').value = 'text';
    document.getElementById('ruleFindText').value = '';
    document.getElementById('ruleMatchMode').value = 'contains';
    document.getElementById('ruleDirection').value = 'any';
    ['ruleDoRename', 'ruleDoEnvelope', 'ruleDoKind', 'ruleDoReview'].forEach(id => { document.getElementById(id).checked = false; });
    document.getElementById('ruleRenameTo').value = '';
    document.getElementById('ruleIncomeLabel').value = '';
    document.getElementById('ruleKind').value = 'expense';
    document.getElementById('btnRuleSave').textContent = 'Add rule';
    document.getElementById('ruleEditorTitle').textContent = 'New rule';
    document.getElementById('btnRuleCancel').hidden = true;
    document.getElementById('ruleEditorPreview').innerHTML = '';
    onRuleFormChange();
}

function editImportRule(id) {
    const r = rulesData.find(x => x.id === id);
    if (!r) return;
    resetRuleForm();
    editingRuleId = id;
    document.getElementById('ruleName').value = r.name || '';
    document.getElementById('ruleName').dataset.touched = '1';
    document.getElementById('ruleFindType').value = r.filter_type || 'text';
    document.getElementById('ruleFindText').value = r.phrase || '';
    document.getElementById('ruleMatchMode').value = r.match_mode || 'contains';
    document.getElementById('ruleDirection').value = r.direction || 'any';
    document.getElementById('ruleDoRename').checked = !!r.set_payee;
    document.getElementById('ruleRenameTo').value = r.set_payee || '';
    document.getElementById('ruleDoEnvelope').checked = !!r.set_envelope;
    if (r.set_envelope && !rulesEnvelopes.includes(r.set_envelope)) {
        document.getElementById('ruleEnvelope').insertAdjacentHTML('beforeend', `<option value="${escapeHtml(r.set_envelope)}">${escapeHtml(r.set_envelope)} (no such envelope)</option>`);
    }
    if (r.set_envelope) document.getElementById('ruleEnvelope').value = r.set_envelope;
    document.getElementById('ruleDoKind').checked = !!r.set_kind;
    if (r.set_kind) document.getElementById('ruleKind').value = r.set_kind;
    document.getElementById('ruleIncomeLabel').value = r.set_income_label || '';
    document.getElementById('ruleDoReview').checked = !!r.set_needs_review;
    document.getElementById('btnRuleSave').textContent = 'Save changes';
    document.getElementById('ruleEditorTitle').textContent = 'Edit rule';
    document.getElementById('btnRuleCancel').hidden = false;
    onRuleFormChange();
    document.getElementById('ruleName').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    document.getElementById('ruleName').focus();
}

// ----- review: what a rule (or every rule) would change, with a tick box on each row -----

// A stored rule in the shape the review and apply calls expect
function ruleBodyFromStored(r) {
    return {
        name: r.name || r.phrase || '', filter_type: r.filter_type || 'text', phrase: r.phrase || '',
        match_mode: r.match_mode || 'contains', direction: r.direction || 'any', set_payee: r.set_payee || null,
        set_envelope: r.set_envelope || null, set_kind: r.set_kind || null, set_income_label: r.set_income_label || null,
        exclusion_reason: r.exclusion_reason || null, set_needs_review: r.set_needs_review ? 1 : 0
    };
}

let review = null;   // {scope, rule, label, changes, picked: Set, show, limit}
const REVIEW_CHUNK = 150;

const REVIEW_FIELD_NAMES = { txn_kind: 'Kind', is_included: 'Included', envelope: 'Envelope', name: 'Payee', person_id: 'Person',
    income_subtype: 'Paycheck type', income_label: 'Label', needs_review: 'Review flag', exclusion_reason: 'Reason',
    income_source_id: 'Income source', match_source: 'Matched by' };

function reviewDiff(c) {
    return c.changed_fields.map(f => {
        const show = v => v === null || v === '' || v === undefined ? '(none)' : (f === 'txn_kind' ? (KIND_LABELS[v] || v) : f === 'person_id' ? (personName(v) || v) : f === 'is_included' || f === 'needs_review' ? (v ? 'yes' : 'no') : v);
        return `<span class="diff-part"><b>${REVIEW_FIELD_NAMES[f] || f}:</b> ${escapeHtml(String(show(c.before[f])))} → ${escapeHtml(String(show(c.after[f])))}</span>`;
    }).join('');
}

// scope: 'rule' (with the rule body) or 'all'
async function openReview(scope, rule, label) {
    await showImportSub('rules', true);
    const box = document.getElementById('rulesReview');
    document.getElementById('rulesBrowse').hidden = true;
    box.hidden = false;
    document.getElementById('reviewTitle').textContent = scope === 'all' ? 'Apply all rules' : `Apply "${label}"`;
    document.getElementById('reviewSub').textContent = 'Checking your saved transactions…';
    document.getElementById('reviewBody').innerHTML = '';
    document.getElementById('btnReviewApply').disabled = true;
    try {
        const res = await apiPost('/api/rules/review', { scope, rule });
        if (res.changes.length === 0) {
            closeReview();
            showToast(scope === 'all' ? `Checked ${res.checked.toLocaleString()} transactions. Nothing would change.`
                : `"${label}" has nothing to change in your saved transactions.`);
            return;
        }
        review = { scope, rule, label, changes: res.changes, picked: new Set(), show: 'all', limit: REVIEW_CHUNK, checked: res.checked };
        // Rows you never edited start ticked. Rows you edited by hand start unticked: tick them to let the rule replace your edit.
        res.changes.forEach(c => { if (!c.edited_by_hand) review.picked.add(c.txn_id); });
        document.querySelectorAll('#reviewFilterToggle .segmented-btn').forEach(b => b.classList.toggle('active', b.dataset.show === 'all'));
        renderReview();
        box.scrollIntoView({ block: 'nearest' });
    } catch (err) {
        closeReview();
        showToast('Could not check the rule: ' + err.message, true);
    }
}

function reviewVisible() {
    if (!review) return [];
    return review.changes.filter(c => review.show === 'all' || (review.show === 'edited') === !!c.edited_by_hand);
}

function reviewRowHtml(c) {
    const edited = c.edited_by_hand;
    const badge = edited ? ' <span class="kind-badge kind-review" title="You changed this row yourself. Tick it to let the rules replace your edit.">Edited by hand</span>' : '';
    return `<tr${edited ? ' class="review-edited"' : ''}>
        <td style="text-align:center;"><input type="checkbox" class="txn-checkbox review-pick" data-id="${escapeHtml(c.txn_id)}" ${review.picked.has(c.txn_id) ? 'checked' : ''} onchange="reviewPick(this.dataset.id, this.checked)" aria-label="Apply this change"></td>
        <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(c.date))}</td>
        <td>${escapeHtml(c.after.name)}${badge}<div class="import-file-sub">${escapeHtml(c.raw_name || '')}</div></td>
        <td class="amount-cell" style="text-align:right;">${formatSigned(c.amount)}</td>
        <td>${reviewDiff(c)}</td><td>${escapeHtml(c.rule || '')}</td></tr>`;
}

// Redraw the list, the counts, the select-all box and the Apply button from what is picked
function renderReview() {
    if (!review) return;
    const vis = reviewVisible();
    const shown = vis.slice(0, review.limit);
    document.getElementById('reviewBody').innerHTML = shown.map(reviewRowHtml).join('') ||
        '<tr><td colspan="6" class="import-more">Nothing in this view.</td></tr>';
    const more = document.getElementById('reviewMore');
    more.hidden = vis.length <= shown.length;
    more.innerHTML = vis.length > shown.length
        ? `<button type="button" class="btn-bulk-action" onclick="review.limit += REVIEW_CHUNK * 2; renderReview();">Show more (${(vis.length - shown.length).toLocaleString()} hidden)</button>` : '';

    const total = review.changes.length, n = review.picked.size;
    const edited = review.changes.filter(c => c.edited_by_hand);
    const editedPicked = edited.filter(c => review.picked.has(c.txn_id)).length;
    document.getElementById('reviewSub').textContent = review.scope === 'all'
        ? `Checked ${review.checked.toLocaleString()} transactions with every rule. ${total.toLocaleString()} would change. Untick anything you don't want changed.`
        : `Matches ${review.checked.toLocaleString()} saved transaction${review.checked === 1 ? '' : 's'}; ${total.toLocaleString()} would change. Untick anything you don't want changed.`;
    document.getElementById('reviewEditedCount').textContent = edited.length ? `(${edited.length.toLocaleString()})` : '';
    document.getElementById('reviewCount').textContent = `${n.toLocaleString()} of ${total.toLocaleString()} selected`;
    const all = document.getElementById('reviewSelectAll');
    const visPicked = vis.filter(c => review.picked.has(c.txn_id)).length;
    all.checked = vis.length > 0 && visPicked === vis.length;
    all.indeterminate = visPicked > 0 && visPicked < vis.length;
    document.getElementById('reviewWarn').textContent = editedPicked
        ? `${editedPicked.toLocaleString()} hand-edited row${editedPicked === 1 ? ' is' : 's are'} ticked and will be overwritten.` : '';
    const apply = document.getElementById('btnReviewApply');
    apply.textContent = `Apply ${n.toLocaleString()} selected`;
    apply.disabled = n === 0;
}

function reviewPick(id, checked) {
    if (!review) return;
    checked ? review.picked.add(id) : review.picked.delete(id);
    renderReview();
}

// Select all (or none) of what the current filter shows, including rows not drawn yet
function reviewSelectAll(checked) {
    if (!review) return;
    reviewVisible().forEach(c => { checked ? review.picked.add(c.txn_id) : review.picked.delete(c.txn_id); });
    renderReview();
}

function reviewSetShow(mode) {
    if (!review) return;
    review.show = mode;
    review.limit = REVIEW_CHUNK;
    document.querySelectorAll('#reviewFilterToggle .segmented-btn').forEach(b => b.classList.toggle('active', b.dataset.show === mode));
    renderReview();
}

function closeReview() {
    review = null;
    const box = document.getElementById('rulesReview');
    if (box) box.hidden = true;
    const browse = document.getElementById('rulesBrowse');
    if (browse) browse.hidden = false;
}

async function applyReview() {
    if (!review || review.picked.size === 0) return;
    const apply = document.getElementById('btnReviewApply');
    apply.disabled = true;
    try {
        const res = await apiPost('/api/rules/review/apply', { scope: review.scope, rule: review.rule, txn_ids: [...review.picked] });
        const label = review.scope === 'all' ? 'Rules' : `"${review.label}"`;
        closeReview();
        await refreshTransactions();
        showToast(`${label} updated ${res.changed.toLocaleString()} saved transaction${res.changed === 1 ? '' : 's'}.`);
    } catch (err) {
        showToast('Could not apply: ' + err.message, true);
        apply.disabled = false;
    }
}

// The three ways in
function reviewAllRules() { openReview('all', null, ''); }

function reviewRuleFromForm() {
    const body = ruleFormData();
    openReview('rule', body, body.name || body.phrase || 'This rule');
}

function reviewSavedRule(id) {
    const r = rulesData.find(x => x.id === id);
    if (r) openReview('rule', ruleBodyFromStored(r), r.name || r.phrase || 'This rule');
}

// Adds a new rule, or saves the one being edited
async function saveRuleFromForm() {
    const body = ruleFormData();
    const wasEdit = !!editingRuleId;
    if (wasEdit) {
        body.id = editingRuleId;
        const cur = rulesData.find(x => x.id === editingRuleId);
        body.active = cur ? cur.active : 1;
    }
    try {
        const saved = await apiPost('/api/rules/save', body);
        const p = await apiPost('/api/rules/preview', body);
        resetRuleForm();
        await loadImportRules();
        showToast(`Rule ${wasEdit ? 'updated' : 'added'}.`);
        // Offer the same preview-and-choose screen when the rule would change saved transactions; Back skips it
        if (p.would_change > 0) await openReview('rule', body, body.name || body.phrase || 'This rule');
        return saved;
    } catch (err) {
        showToast('Could not save the rule: ' + err.message, true);
    }
}

async function moveImportRule(id, to) {
    try {
        await apiPost('/api/rules/move', { id, to });
        await loadImportRules();
    } catch (err) {
        showToast('Could not move the rule: ' + err.message, true);
    }
}

async function toggleImportRule(id, on) {
    const r = rulesData.find(x => x.id === id);
    if (!r) return;
    try {
        await apiPost('/api/rules/save', Object.assign({}, r, { active: on ? 1 : 0 }));
        r.active = on ? 1 : 0;
        renderImportRules();
    } catch (err) {
        showToast('Could not update the rule: ' + err.message, true);
        renderImportRules();
    }
}

async function deleteImportRule(id) {
    const r = rulesData.find(x => x.id === id);
    if (!r || !confirm(`Delete the rule "${r.name || r.phrase}"?`)) return;
    try {
        await apiPost('/api/rules/delete', { id });
        if (editingRuleId === id) resetRuleForm();
        rulesData = rulesData.filter(x => x.id !== id);
        renderImportRules();
    } catch (err) {
        showToast('Could not delete the rule: ' + err.message, true);
    }
}

// ---------------------------------------------------------------------
// History
// ---------------------------------------------------------------------
async function loadImportHistory() {
    const body = document.getElementById('historyBody');
    body.innerHTML = '<tr><td colspan="5">Loading…</td></tr>';
    try {
        const data = await api('/api/import/history');
        body.innerHTML = data.batches.length === 0
            ? '<tr><td colspan="5" class="import-more">Nothing saved yet.</td></tr>'
            : data.batches.map(b => {
                let files = [];
                try { files = JSON.parse(b.files || '[]'); } catch (e) { /* ignore */ }
                return `<tr><td style="white-space:nowrap;">${escapeHtml((b.created_at || '').replace('T', ' ').slice(0, 16))}</td>
                    <td>${escapeHtml(files.join(', '))}</td>
                    <td style="text-align:right;">${b.rows_new}</td><td style="text-align:right;">${b.rows_existing}</td>
                    <td style="text-align:right;">${b.rows_needs_review}</td></tr>`;
            }).join('');
    } catch (err) {
        body.innerHTML = `<tr><td colspan="5">Could not load history: ${escapeHtml(err.message)}</td></tr>`;
    }
}
