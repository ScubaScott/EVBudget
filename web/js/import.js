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
            closeImportRulesModal();
            closeReclassifyModal();
            closeImportHistoryModal();
        }
    });
});

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
        importRecords = data.records.map(r => Object.assign({ skip: false, restore: false }, r));
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
    document.getElementById('importSummary').innerHTML =
        card('income', 'New rows', s.new.toLocaleString(), 'Will be saved') +
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
        ['all', 'All'], ['new', 'New'], ['existing', 'Already saved'], ['review', 'Needs review'],
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
    await openImportRulesModal();
    editImportRule(id);
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
    if (r.status === 'deleted') statusCell += ` <button type="button" class="btn-inline-action btn-inline-save" onclick="importRestore(${idx})">Restore</button>`;

    const saveCell = r.status === 'new'
        ? `<input type="checkbox" class="txn-checkbox" ${r.skip ? '' : 'checked'} title="Save this row" onchange="importSetSkip(${idx}, !this.checked)">`
        : '';
    const inclCell = `<input type="checkbox" class="txn-checkbox" ${r.is_included ? 'checked' : ''} ${dis} onchange="importSetIncluded(${idx}, this.checked)">`;

    return `<tr class="${classes.join(' ')}" data-idx="${idx}">
        <td style="text-align:center;">${saveCell}</td>
        <td style="text-align:center;">${inclCell}</td>
        <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(r.date))}</td>
        <td><input type="text" class="input-text import-name" ${dis} value="${escapeHtml(r.name)}" title="${escapeHtml(r.raw_name)}" onchange="importSetField(${idx}, 'name', this.value)">${importOriginalText(r)}</td>
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

async function openImportRulesModal() {
    document.getElementById('rulesModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    await loadImportRules();
    resetRuleForm();
}

function closeImportRulesModal() {
    document.getElementById('rulesModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
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
    document.getElementById('btnRuleCancel').hidden = false;
    onRuleFormChange();
    document.getElementById('ruleName').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    document.getElementById('ruleName').focus();
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
        let note = '';
        if (p.would_change > 0 && confirm(`This rule would also change ${p.would_change} saved transaction${p.would_change === 1 ? '' : 's'}.\n\nApply it to ${p.would_change === 1 ? 'it' : 'them'} now? Ones you edited by hand are left alone.`)) {
            const done = await apiPost('/api/rules/apply', body);
            note = ` ${done.changed} saved transaction${done.changed === 1 ? '' : 's'} updated.`;
            await refreshTransactions();
        }
        resetRuleForm();
        await loadImportRules();
        showToast(`Rule ${wasEdit ? 'updated' : 'added'}.` + note);
        if (!wasEdit) document.querySelector('.rules-table-wrap').scrollTop = 0;
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
// Re-run rules
// ---------------------------------------------------------------------
async function openReclassifyModal() {
    document.getElementById('reclassifyModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    await runReclassifyPreview();
}

function closeReclassifyModal() {
    document.getElementById('reclassifyModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function runReclassifyPreview() {
    const summary = document.getElementById('reclassifySummary');
    const apply = document.getElementById('btnReclassifyApply');
    summary.textContent = 'Checking…';
    apply.disabled = true;
    try {
        const res = await apiPost('/api/transactions/reclassify', {
            apply: false, include_edited: document.getElementById('reclassifyIncludeEdited').checked
        });
        summary.innerHTML = res.changed_count === 0
            ? `Checked ${res.checked.toLocaleString()} transactions. <strong>Nothing would change.</strong>`
            : `Checked ${res.checked.toLocaleString()} transactions. <strong>${res.changed_count.toLocaleString()} would change</strong>${res.truncated ? ' (first ' + res.changes.length + ' shown)' : ''}.`;
        document.getElementById('reclassifyBody').innerHTML = res.changes.map(c => {
            const parts = c.changed_fields.map(f => {
                const nice = { txn_kind: 'Kind', is_included: 'Included', envelope: 'Envelope', name: 'Payee', person_id: 'Person',
                    income_subtype: 'Paycheck type', income_label: 'Label', needs_review: 'Review flag', exclusion_reason: 'Reason',
                    income_source_id: 'Income source', match_source: 'Matched by' }[f] || f;
                const show = v => v === null || v === '' || v === undefined ? '(none)' : (f === 'txn_kind' ? (KIND_LABELS[v] || v) : f === 'person_id' ? (personName(v) || v) : f === 'is_included' || f === 'needs_review' ? (v ? 'yes' : 'no') : v);
                return `<span class="diff-part"><b>${nice}:</b> ${escapeHtml(String(show(c.before[f])))} → ${escapeHtml(String(show(c.after[f])))}</span>`;
            }).join('');
            return `<tr><td>${escapeHtml(formatDisplayDate(c.date))}</td><td>${escapeHtml(c.after.name)}<div class="import-file-sub">${escapeHtml(c.raw_name || '')}</div></td><td class="amount-cell" style="text-align:right;">${formatSigned(c.amount)}</td><td>${parts}</td><td>${escapeHtml(c.rule || '')}</td></tr>`;
        }).join('');
        apply.disabled = res.changed_count === 0;
    } catch (err) {
        summary.textContent = 'Could not check: ' + err.message;
    }
}

async function applyReclassify() {
    const apply = document.getElementById('btnReclassifyApply');
    apply.disabled = true;
    try {
        const res = await apiPost('/api/transactions/reclassify', {
            apply: true, include_edited: document.getElementById('reclassifyIncludeEdited').checked
        });
        showToast(`Updated ${res.changed_count.toLocaleString()} transaction${res.changed_count === 1 ? '' : 's'}.`);
        closeReclassifyModal();
        await refreshTransactions();
    } catch (err) {
        showToast('Could not apply: ' + err.message, true);
        apply.disabled = false;
    }
}

// ---------------------------------------------------------------------
// History
// ---------------------------------------------------------------------
async function openImportHistoryModal() {
    document.getElementById('historyModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
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

function closeImportHistoryModal() {
    document.getElementById('historyModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}
