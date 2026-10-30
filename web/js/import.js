/* Import tab: pick bank files, review how each row was classified, then save. */

let importAccountNames = [];      // card / account names already in use, offered when naming a file
let importFiles = [];            // inspected files: {filename, headers, mapping, account_type, is_mapped, selected, imported_rows, ...}
let importRecords = [];          // preview rows
let importRenderLimit = 300;
let importFilter = 'all';
let importSort = { col: 'date', asc: false };
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

function renderImportFiles() {
    const wrap = document.getElementById('importFileListWrap');
    const body = document.getElementById('importFileBody');
    wrap.hidden = importFiles.length === 0;
    body.innerHTML = importFiles.map((f, i) => {
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
        return `<tr>
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
        <td><input type="text" class="input-text import-name" ${dis} value="${escapeHtml(r.name)}" title="${escapeHtml(r.raw_name)}" onchange="importSetField(${idx}, 'name', this.value)"></td>
        <td class="amount-cell ${amtClass}">${isNeg ? '-' : '+'}${formatCurrency(Math.abs(r.amount))}</td>
        <td><select class="select-input import-mini import-kind" ${dis} onchange="importSetKind(${idx}, this.value)">${importKindOptions(r.txn_kind)}</select></td>
        <td><div class="import-target">${target}</div></td>
        <td>${statusCell}</td>
        <td><span class="import-source">${escapeHtml(r.match_source || '')}</span></td>
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
    const scope = await askRuleScope(`Always send "${phrase}" to "${envelope}" from now on?`);
    if (!scope) return;
    try {
        await apiPost('/api/rules/save', { phrase, stage: 'envelope', set_envelope: envelope, scope });
        let updated = 0;
        importRecords.forEach(o => {
            if (o.status === 'new' && !o.edited && o.txn_kind === 'expense' && (o.name || '').toUpperCase().includes(phrase.toUpperCase())) {
                o.envelope = envelope;
                o.match_source = 'User Learned Rule';
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
let importRulesData = [];
let ruleFilter = 'all';

async function openImportRulesModal() {
    document.getElementById('rulesModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('ruleNewEnvelope').innerHTML = importEnvelopeOptions('').replace('(No envelope)', 'Envelope…');
    onRuleActionChange();
    await loadImportRules();
}

function closeImportRulesModal() {
    if (editingRuleId !== null) cancelRuleEdit();
    document.getElementById('rulesModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function loadImportRules() {
    try {
        importRulesData = (await api('/api/rules')).rules;
        renderImportRules();
    } catch (err) {
        document.getElementById('rulesBody').innerHTML = `<tr><td colspan="5">Could not load rules: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function setRuleFilter(action) {
    ruleFilter = action;
    document.querySelectorAll('#ruleOriginToggle .segmented-btn').forEach(b => b.classList.toggle('active', b.dataset.action === action));
    renderImportRules();
}

// The filter groups a rule by what it does, whoever supplied it
function ruleGroupOf(r) {
    if (r.stage === 'envelope') return 'envelope';
    if (r.stage === 'payee') return 'payee';
    if (r.stage === 'exclude') return 'exclude';
    if (r.stage === 'include_override') return 'include';
    return 'income';
}
const RULE_ORIGIN_LABELS = { user: 'Mine', vendor: 'Vendor list', core: 'Built-in' };

const STAGE_LABELS = {
    include_override: 'Keep (never exclude)', exclude: 'Exclude', income: 'Positive amounts',
    refund: 'Positive amounts', envelope: 'Spending', payee: 'Any'
};

function describeRule(r) {
    const bits = [];
    if (r.stage === 'include_override') bits.push('Keep as ' + (KIND_LABELS[r.set_kind] || 'expense').toLowerCase());
    if (r.stage === 'exclude') bits.push(`Exclude as ${(KIND_LABELS[r.set_kind] || 'excluded').toLowerCase()}`);
    if (r.set_kind && ['income', 'refund'].includes(r.stage)) bits.push('Treat as ' + KIND_LABELS[r.set_kind].toLowerCase());
    if (r.set_envelope) bits.push('→ ' + r.set_envelope);
    if (r.set_income_label) bits.push(`label "${r.set_income_label}"`);
    if (r.set_payee) bits.push(r.match_mode === 'regex' ? `replace the match with "${r.set_payee}"` : `rename to "${r.set_payee}"`);
    if (r.set_needs_review) bits.push('flag for review');
    if (r.match_mode && r.match_mode !== 'contains') bits.push(`(${r.match_mode === 'regex' ? 'pattern' : r.match_mode === 'word' ? 'whole word' : 'vendor match'})`);
    return bits.join(' ') || '—';
}

function renderImportRules() {
    const q = (document.getElementById('ruleSearch').value || '').trim().toLowerCase();
    const rows = importRulesData
        .filter(r => ruleFilter === 'all' || ruleGroupOf(r) === ruleFilter)
        .filter(r => !q || (r.phrase + ' ' + (r.description || '') + ' ' + describeRule(r)).toLowerCase().includes(q));
    document.getElementById('rulesBody').innerHTML = rows.length === 0
        ? '<tr><td colspan="5" class="import-more">No rules here yet.</td></tr>'
        : rows.slice(0, 400).map(r => `<tr class="${r.active ? '' : 'excluded-row'}">
            <td>${r.description ? `<div class="rule-desc">${escapeHtml(r.description)}</div>` : ''}<code>${escapeHtml(r.phrase)}</code> <span class="rule-origin">${RULE_ORIGIN_LABELS[r.origin] || ''}</span><span class="rule-scope ${r.scope === 'shared' ? 'shared' : ''}">${r.scope === 'shared' ? 'Everyone' : 'Just me'}</span></td>
            <td>${escapeHtml(STAGE_LABELS[r.stage] || r.stage)}</td>
            <td>${escapeHtml(describeRule(r))}</td>
            <td style="text-align:center;"><input type="checkbox" class="txn-checkbox" ${r.active ? 'checked' : ''} onchange="toggleImportRule(${r.id}, this.checked)"></td>
            <td style="white-space:nowrap; text-align:right;">
                <button type="button" class="btn-inline-action btn-inline-cancel" ${ruleActionOf(r) ? '' : 'disabled title="A special built-in rule. Turn it off or add your own."'} onclick="editImportRule(${r.id})">Edit</button>
                <button type="button" class="btn-env-icon delete" title="Delete rule" onclick="deleteImportRule(${r.id})">&times;</button></td>
        </tr>`).join('') + (rows.length > 400 ? `<tr><td colspan="5" class="import-more">Showing 400 of ${rows.length}. Use search to narrow.</td></tr>` : '');
}

function onRuleActionChange() {
    const action = document.getElementById('ruleNewAction').value;
    const regex = document.getElementById('ruleNewRegex').checked;
    document.getElementById('ruleNewEnvelope').style.display = ['envelope', 'refund'].includes(action) ? '' : 'none';
    const payee = document.getElementById('ruleNewPayee');
    payee.style.display = action === 'payee' ? '' : 'none';
    payee.placeholder = regex ? 'Replace with (e.g. \\1)' : 'New payee name';
    document.getElementById('ruleNewPhrase').placeholder = regex ? 'Pattern (e.g. SQ\\s?\\*\\s?(.*))' : 'Text to look for (e.g. TARGET)';
    document.getElementById('ruleRegexHint').hidden = !(regex && action === 'payee');
    previewNewRule();
}

// Shows what a rename rule would do to saved transactions before it is added
let rulePreviewTimer = null;
function previewNewRule() {
    clearTimeout(rulePreviewTimer);
    const box = document.getElementById('ruleNewPreview');
    const phrase = document.getElementById('ruleNewPhrase').value.trim();
    const payee = document.getElementById('ruleNewPayee').value.trim();
    if (document.getElementById('ruleNewAction').value !== 'payee' || !phrase || !payee) { box.innerHTML = ''; return; }
    rulePreviewTimer = setTimeout(async () => {
        try {
            const r = await apiPost('/api/rules/preview', { phrase, set_payee: payee, match_mode: document.getElementById('ruleNewRegex').checked ? 'regex' : 'contains' });
            box.innerHTML = r.count === 0
                ? 'No saved transactions would change.' + (r.locked ? ` (${r.locked} you edited by hand are left alone.)` : '')
                : `Would rename <strong>${r.count}</strong> saved transaction${r.count === 1 ? '' : 's'}: ` +
                  r.examples.slice(0, 4).map(e => `<code>${escapeHtml(e.before)}</code> &rarr; <code>${escapeHtml(e.after)}</code>`).join(', ') + (r.examples.length > 4 ? ', ...' : '');
        } catch (err) {
            box.innerHTML = `<span class="tpl-short">${escapeHtml(err.message)}</span>`;
        }
    }, 300);
}

// Which action in the form a stored rule corresponds to (null = a special built-in rule the form can't show)
function ruleActionOf(r) {
    if (r.stage === 'envelope' && r.set_envelope) return 'envelope';
    if (r.stage === 'refund' && r.set_kind === 'refund') return 'refund';
    if (r.stage === 'income' && r.set_kind === 'other_income') return 'other_income';
    if (r.stage === 'exclude' && ['transfer', 'cc_payment'].includes(r.set_kind)) return r.set_kind;
    if (r.stage === 'payee') return 'payee';
    if (r.stage === 'include_override') return 'include';
    return null;
}

// Every column the action decides, so switching a rule to a different action clears what no longer applies
function ruleFieldsFor(action, envelope, payee, orig) {
    const base = { stage: 'envelope', set_kind: null, set_envelope: null, set_payee: orig ? orig.set_payee : null, exclusion_reason: null };
    if (action === 'envelope') return Object.assign(base, { stage: 'envelope', set_envelope: envelope });
    if (action === 'refund') return Object.assign(base, { stage: 'refund', set_kind: 'refund', set_envelope: envelope });
    if (action === 'other_income') return Object.assign(base, { stage: 'income', set_kind: 'other_income' });
    if (action === 'include') return Object.assign(base, { stage: 'include_override', set_kind: 'expense', set_envelope: envelope || null });
    if (action === 'payee') return Object.assign(base, { stage: 'payee', set_payee: payee });
    return Object.assign(base, { stage: 'exclude', set_kind: action, exclusion_reason: action === 'transfer' ? 'Internal Account Transfer' : 'Credit Card Payment / Autopay' });
}

let editingRuleId = null;

function editImportRule(id) {
    const r = importRulesData.find(x => x.id === id);
    const action = r && ruleActionOf(r);
    if (!action) { showToast('This built-in rule has options the form can\'t show. Turn it off, or add your own rule.', true); return; }
    editingRuleId = id;
    document.getElementById('ruleNewDescription').value = r.description || '';
    document.getElementById('ruleNewPhrase').value = r.phrase;
    document.getElementById('ruleNewAction').value = action;
    document.getElementById('ruleNewRegex').checked = r.match_mode === 'regex';
    document.getElementById('ruleNewEnvelope').value = r.set_envelope || '';
    document.getElementById('ruleNewPayee').value = r.set_payee || '';
    document.getElementById('ruleNewScope').value = r.scope || 'personal';
    onRuleActionChange();
    document.getElementById('btnRuleSave').textContent = 'Save changes';
    document.getElementById('btnRuleCancel').hidden = false;
    document.getElementById('ruleNewDescription').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    document.getElementById('ruleNewDescription').focus();
}

function cancelRuleEdit() {
    editingRuleId = null;
    ['ruleNewDescription', 'ruleNewPhrase', 'ruleNewPayee'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('ruleNewRegex').checked = false;
    document.getElementById('ruleNewScope').value = '';
    document.getElementById('ruleNewPreview').innerHTML = '';
    document.getElementById('btnRuleSave').textContent = 'Add rule';
    document.getElementById('btnRuleCancel').hidden = true;
    onRuleActionChange();
}

// Adds a new rule, or saves the one being edited
async function addImportRule() {
    const phrase = document.getElementById('ruleNewPhrase').value.trim();
    const action = document.getElementById('ruleNewAction').value;
    const envelope = document.getElementById('ruleNewEnvelope').value;
    const payee = document.getElementById('ruleNewPayee').value.trim();
    const orig = editingRuleId ? importRulesData.find(x => x.id === editingRuleId) : null;
    const scope = document.getElementById('ruleNewScope').value;
    if (!phrase) { showToast('Enter the text to look for.', true); return; }
    if (['envelope', 'refund'].includes(action) && !envelope) { showToast('Choose an envelope.', true); return; }
    if (action === 'payee' && !payee) { showToast('Enter the new payee name.', true); return; }
    if (!scope) { showToast('Choose who this rule applies to: just you, or everyone.', true); return; }

    // Matching mode: pattern if ticked; otherwise keep a whole-word or vendor-style match the form can't show
    const keepMode = orig && ['word', 'vendor'].includes(orig.match_mode) ? orig.match_mode : 'contains';
    const body = Object.assign({ phrase, scope, match_mode: document.getElementById('ruleNewRegex').checked ? 'regex' : keepMode,
        description: document.getElementById('ruleNewDescription').value.trim() }, ruleFieldsFor(action, envelope, payee, orig));
    if (orig) {
        body.id = orig.id;
        body.active = orig.active;
        const flagged = ['refund', 'income'].includes(body.stage) && ['refund', 'income'].includes(orig.stage);
        body.set_needs_review = flagged ? orig.set_needs_review : 0;
        body.set_income_label = body.stage === 'income' ? orig.set_income_label : null;
    }
    try {
        await apiPost('/api/rules/save', body);
        let renamedNote = '';
        if (action === 'payee') {
            const probe = { phrase, set_payee: payee, match_mode: body.match_mode };
            const pv = await apiPost('/api/rules/preview', probe);
            if (pv.count > 0 && confirm(`Also rename the ${pv.count} saved transaction${pv.count === 1 ? '' : 's'} this matches?\n\nFor example: ${pv.examples[0].before} -> ${pv.examples[0].after}`)) {
                const done = await apiPost('/api/rules/apply-rename', probe);
                renamedNote = ` ${done.renamed} saved transaction${done.renamed === 1 ? '' : 's'} renamed.`;
                await refreshTransactions();
            }
        }
        const wasEdit = !!orig;
        cancelRuleEdit();
        if (!wasEdit) setRuleFilter('all');
        await loadImportRules();
        showToast(`Rule ${wasEdit ? 'updated' : 'added'} for "${phrase}".` + renamedNote);
    } catch (err) {
        showToast('Could not save the rule: ' + err.message, true);
    }
}

async function toggleImportRule(id, on) {
    const r = importRulesData.find(x => x.id === id);
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
    const r = importRulesData.find(x => x.id === id);
    if (!r || !confirm(`Delete the rule for "${r.phrase}"?`)) return;
    try {
        await apiPost('/api/rules/delete', { id });
        importRulesData = importRulesData.filter(x => x.id !== id);
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
