/* Budget tab: envelope settings and suggestions, bills, fill templates, and the go-live setup. */

let budgetData = null;          // {envelopes, suggestions, summary, unallocated_id}
let budgetTemplates = [];       // [{income_source_id, ..., lines}]
let templateDraft = {};         // income_source_id -> lines being edited
let templateDirty = {};
let setupStatus = null;
let billsEnvelopeId = null;
let budgetLoaded = false;

const KIND_OPTIONS = { spending: 'Spending', bill: 'Bills', savings: 'Savings' };
const ROLLOVER_OPTIONS = { reset: 'Start fresh', rollover: 'Carry over' };

document.addEventListener('tabchange', (e) => {
    if (e.detail.tab === 'budget') loadBudget();
});
document.addEventListener('settingsopen', loadBudgetSettings);
document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { closeBillsModal(); closeGoLiveModal(); }
    });
});

function money(v) { return formatCurrency(v || 0); }

// ---------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------
async function loadBudget() {
    try {
        const [b, t, s] = await Promise.all([api('/api/budget/envelopes'), api('/api/templates'), api('/api/setup/status')]);
        budgetData = b;
        budgetTemplates = t.templates;
        setupStatus = s;
        templateDraft = {};
        templateDirty = {};
        budgetTemplates.forEach(x => { templateDraft[x.income_source_id] = x.lines.map(l => Object.assign({}, l)); });
        budgetLoaded = true;
        renderBudgetKpis();
        renderGoLive();
        renderBudgetTable();
        renderTemplates();
        loadDiscovery();
    } catch (err) {
        showToast('Could not load the budget: ' + err.message, true);
    }
}

async function reloadBudgetKeepTemplates() {
    // Refresh envelopes/suggestions without throwing away template edits in progress
    const b = await api('/api/budget/envelopes');
    budgetData = b;
    renderBudgetKpis();
    renderBudgetTable();
    renderTemplates();
}

// ---------------------------------------------------------------------
// Summary and go-live
// ---------------------------------------------------------------------
function renderBudgetKpis() {
    const s = budgetData.summary;
    const card = (cls, title, value, sub) => `<div class="kpi-card ${cls}"><div class="kpi-header"><span class="kpi-title">${title}</span></div>
        <div class="kpi-value ${cls}">${value}</div><div class="kpi-subtitle">${sub}</div></div>`;
    const people = Object.entries(s.planning_by_person || {}).map(([k, v]) => `${escapeHtml(k)} ${money(v)}`).join(' &bull; ');
    document.getElementById('budgetKpis').innerHTML =
        card('income', 'Income to plan around', money(s.planning_income) + '<span class="kpi-unit"> / month</span>', people || 'Add pay sources on the Income tab') +
        card('net', 'Budgeted', money(s.total_budgeted) + '<span class="kpi-unit"> / month</span>', 'Spending, bills and savings') +
        card(s.surplus >= 0 ? 'income' : 'spend', s.surplus >= 0 ? 'Left over' : 'Over budget', money(Math.abs(s.surplus)) + '<span class="kpi-unit"> / month</span>',
            s.surplus >= 0 ? 'Income minus everything budgeted' : 'Budgets add up to more than income') +
        card('count', 'Actually spent', s.actual_avg_spend === null ? '—' : money(s.actual_avg_spend) + '<span class="kpi-unit"> / month</span>', 'Average over recent complete months');
}

function renderGoLive() {
    const live = setupStatus.go_live_date;
    document.getElementById('goLiveText').innerHTML = live
        ? `Envelope balances start on <strong>${formatDisplayDate(live)}</strong> with ${money(setupStatus.opening_total)} across all envelopes.`
        : 'Not set up yet. Balances stay off until you choose a go-live date and enter opening balances.';
    document.getElementById('btnGoLive').textContent = live ? 'Re-run setup' : 'Set up go-live';
}

// ---------------------------------------------------------------------
// Envelope table
// ---------------------------------------------------------------------
function suggestionCell(e) {
    const s = budgetData.suggestions[String(e.id)];
    if (!s || s.basis === 'none') return '<span class="import-muted">You choose the monthly amount</span>';
    const conf = s.confidence ? `<span class="conf conf-${s.confidence}">${s.confidence}</span>` : '';
    if (s.basis === 'bills') {
        return `Bills add up to <strong>${money(s.suggested)}</strong> a month ${s.actual_12mo_avg !== undefined ? `<div class="import-file-sub">Actually paid ${money(s.actual_12mo_avg)} a month over the last year</div>` : ''}`;
    }
    const use = (!e.budget_derived && s.suggested > 0)
        ? ` <button type="button" class="btn-inline-action btn-inline-save" onclick="acceptSuggestion(${e.id}, ${s.suggested})">Use ${money(s.suggested)}</button>` : '';
    if (s.basis === 'annualized') {
        return `${money(s.total)} paid in the last 12 months &rarr; <strong>${money(s.suggested)}</strong> a month ${conf}${use}` +
            (s.partial_history ? `<div class="import-file-sub">Only ${s.months} months of history, so this may be low</div>` : '');
    }
    return `Avg <strong>${money(s.avg)}</strong> &bull; median ${money(s.median)} &bull; ${money(s.min)}&ndash;${money(s.max)} ${conf}${use}` +
        `<div class="import-file-sub">${s.months} complete month${s.months === 1 ? '' : 's'} of data</div>`;
}

function renderBudgetTable() {
    const sel = (opts, cur, field, id) => `<select class="select-input import-mini" onchange="saveEnvelopeField(${id}, '${field}', this.value)">` +
        Object.entries(opts).map(([k, v]) => `<option value="${k}" ${k === cur ? 'selected' : ''}>${v}</option>`).join('') + '</select>';
    document.getElementById('budgetBody').innerHTML = budgetData.envelopes.map(e => {
        const budgetCell = e.budget_derived
            ? `<div class="derived-budget">${money(e.effective_budget)}</div><div class="import-file-sub">from bills</div>`
            : `<input type="number" step="0.01" min="0" class="input-number import-mini" value="${e.monthly_budget || ''}" placeholder="0" onchange="saveEnvelopeField(${e.id}, 'monthly_budget', this.value || 0)">`;
        const activeBills = e.bills.filter(b => b.active).length;
        const overdue = e.bills.some(b => b.overdue);
        return `<tr class="${e.active ? '' : 'excluded-row'}">
            <td><div style="font-weight:600;">${escapeHtml(e.name)}</div></td>
            <td>${sel(KIND_OPTIONS, e.kind, 'kind', e.id)}</td>
            <td>${sel(ROLLOVER_OPTIONS, e.rollover_mode, 'rollover_mode', e.id)}</td>
            <td>${budgetCell}</td>
            <td>${suggestionCell(e)}</td>
            <td><button type="button" class="btn-inline-action btn-inline-cancel" onclick="openBillsModal(${e.id})">${activeBills ? activeBills + ' bill' + (activeBills === 1 ? '' : 's') : 'Add bills'}</button>${overdue ? ' <span class="kind-badge kind-review">Overdue</span>' : ''}</td>
            <td style="text-align:center;"><input type="checkbox" class="txn-checkbox" ${e.active ? 'checked' : ''} onchange="saveEnvelopeField(${e.id}, 'active', this.checked ? 1 : 0)"></td></tr>`;
    }).join('');
}

async function saveEnvelopeField(id, field, value) {
    try {
        await apiPost('/api/envelopes/save', { id, [field]: value });
        await reloadBudgetKeepTemplates();
        await refreshTransactions();
    } catch (err) {
        showToast(err.message, true);
        await reloadBudgetKeepTemplates();
    }
}

async function acceptSuggestion(id, amount) {
    try {
        await apiPost('/api/budget/accept', { items: [{ envelope_id: id, amount }] });
        await reloadBudgetKeepTemplates();
        showToast('Budget set.');
    } catch (err) { showToast(err.message, true); }
}

async function acceptAllSuggestions() {
    const items = budgetData.envelopes
        .filter(e => e.active && !e.budget_derived && e.kind !== 'savings')
        .map(e => ({ e, s: budgetData.suggestions[String(e.id)] }))
        .filter(({ s }) => s && s.confidence === 'high' && s.suggested > 0)
        .map(({ e, s }) => ({ envelope_id: e.id, amount: s.suggested }));
    if (items.length === 0) { showToast('No high-confidence suggestions to accept.'); return; }
    if (!confirm(`Set ${items.length} envelope budget${items.length === 1 ? '' : 's'} to the suggested amounts?`)) return;
    try {
        const res = await apiPost('/api/budget/accept', { items });
        await reloadBudgetKeepTemplates();
        showToast(`Updated ${res.updated} budget${res.updated === 1 ? '' : 's'}.`);
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Bills
// ---------------------------------------------------------------------
function billScheduleText(b) {
    if (b.freq === 'once') return 'One time';
    const unit = { weekly: 'week', monthly: 'month', yearly: 'year' }[b.freq];
    return b.interval === 1 ? `Every ${unit}` : `Every ${b.interval} ${unit}s`;
}

async function openBillsModal(envId) {
    billsEnvelopeId = envId;
    const env = budgetData.envelopes.find(e => e.id === envId);
    document.getElementById('billsModalTitle').textContent = `Bills: ${env.name}`;
    document.getElementById('billsModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    resetBillForm();
    renderBillsList();
    loadBillHistory();
}

function closeBillsModal() {
    if (!document.getElementById('billsModalOverlay').classList.contains('active')) return;
    document.getElementById('billsModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
    billsEnvelopeId = null;
    if (budgetLoaded) reloadBudgetKeepTemplates();
}

function currentEnvelope() { return budgetData.envelopes.find(e => e.id === billsEnvelopeId); }

function renderBillsList() {
    const env = currentEnvelope();
    const body = document.getElementById('billsBody');
    body.innerHTML = env.bills.length === 0
        ? '<tr><td colspan="6" class="import-more">No bills yet. Add one below, or start from a payee you already pay.</td></tr>'
        : env.bills.map(b => `<tr class="${b.active ? '' : 'excluded-row'}">
            <td><div style="font-weight:500;">${escapeHtml(b.name)}</div>${b.match_phrase ? `<div class="import-file-sub"><code>${escapeHtml(b.match_phrase)}</code></div>` : ''}</td>
            <td>${billScheduleText(b)}</td>
            <td style="text-align:right;">${money(b.amount)}${b.amount_mode === 'estimated' ? '<div class="import-file-sub">estimate</div>' : ''}</td>
            <td style="text-align:right;">${b.freq === 'once' ? '—' : money(b.monthly_amount)}</td>
            <td style="white-space:nowrap;">${b.next_due_date ? formatDisplayDate(b.next_due_date) : '—'}${b.overdue ? ' <span class="kind-badge kind-review">Overdue</span>' : ''}</td>
            <td style="text-align:right; white-space:nowrap;">
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="editBill(${b.id})">Edit</button>
                <button type="button" class="btn-env-icon delete" title="Delete bill" onclick="deleteBill(${b.id})">&times;</button></td></tr>`).join('');
    document.getElementById('billsFooter').textContent = env.budget_derived
        ? `This envelope's budget is ${money(env.effective_budget)} a month, from its bills.` : '';
}

async function loadBillHistory() {
    const box = document.getElementById('billHistory');
    box.innerHTML = '';
    try {
        const data = await api('/api/bills/history?envelope_id=' + billsEnvelopeId);
        // Leave out payees that already have a bill in this envelope
        const bills = currentEnvelope().bills;
        const norm = (t) => (t || '').toUpperCase().replace(/\s+/g, ' ').trim();
        data.payees = data.payees.filter(p => !bills.some(b =>
            norm(b.name) === norm(p.name) || (norm(b.match_phrase) && norm(p.name).includes(norm(b.match_phrase)))));
        window._billHistory = data.payees;
        if (!data.payees.length) return;
        box.innerHTML = '<span class="control-label">Start from a payee:</span> ' + data.payees.map((p, i) =>
            `<button type="button" class="preset-btn" onclick="fillBillFromHistory(${i})" title="${p.payments} payment${p.payments === 1 ? '' : 's'}, last ${formatDisplayDate(p.last_date)}">${escapeHtml(p.name)} &middot; ${money(p.typical_amount)}</button>`).join('');
    } catch (err) { /* history is optional */ }
}

function fillBillFromHistory(i) {
    const p = window._billHistory[i];
    resetBillForm();
    document.getElementById('billName').value = p.name;
    document.getElementById('billPhrase').value = p.name;
    document.getElementById('billAmount').value = p.typical_amount;
    document.getElementById('billFreq').value = p.freq;
    document.getElementById('billInterval').value = p.interval;
    document.getElementById('billAnchor').value = p.last_date;
    previewBillDates();
}

function resetBillForm() {
    ['billId', 'billName', 'billPhrase', 'billAmount', 'billAnchor', 'billNext', 'billTolerance'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('billAmountMode').value = 'fixed';
    document.getElementById('billFreq').value = 'monthly';
    document.getElementById('billInterval').value = 1;
    document.getElementById('billActive').checked = true;
    document.getElementById('billFormTitle').textContent = 'Add a bill';
    document.getElementById('billPreview').textContent = '';
}

function editBill(id) {
    const b = currentEnvelope().bills.find(x => x.id === id);
    document.getElementById('billId').value = b.id;
    document.getElementById('billName').value = b.name;
    document.getElementById('billPhrase').value = b.match_phrase || '';
    document.getElementById('billAmount').value = b.amount;
    document.getElementById('billAmountMode').value = b.amount_mode;
    document.getElementById('billFreq').value = b.freq;
    document.getElementById('billInterval').value = b.interval;
    document.getElementById('billAnchor').value = b.anchor_date || '';
    document.getElementById('billNext').value = b.next_due_date || '';
    document.getElementById('billTolerance').value = b.amount_tolerance_pct || '';
    document.getElementById('billActive').checked = !!b.active;
    document.getElementById('billFormTitle').textContent = 'Edit bill';
    previewBillDates();
    document.getElementById('billForm').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function previewBillDates() {
    const anchor = document.getElementById('billAnchor').value;
    const box = document.getElementById('billPreview');
    if (!anchor) { box.textContent = ''; return; }
    try {
        const r = await api(`/api/bills/preview?freq=${document.getElementById('billFreq').value}&interval=${document.getElementById('billInterval').value || 1}&anchor=${anchor}`);
        box.textContent = r.dates.length ? 'Coming up: ' + r.dates.map(formatDisplayDate).join(', ') : 'No future dates for this schedule.';
        if (r.dates.length && !document.getElementById('billId').value) document.getElementById('billNext').value = r.dates[0];
    } catch (err) { box.textContent = ''; }
}

async function saveBill() {
    try {
        await apiPost('/api/bills/save', {
            id: document.getElementById('billId').value || null, envelope_id: billsEnvelopeId,
            name: document.getElementById('billName').value, match_phrase: document.getElementById('billPhrase').value,
            amount: document.getElementById('billAmount').value || 0, amount_mode: document.getElementById('billAmountMode').value,
            freq: document.getElementById('billFreq').value, interval: document.getElementById('billInterval').value,
            anchor_date: document.getElementById('billAnchor').value, next_due_date: document.getElementById('billNext').value,
            amount_tolerance_pct: document.getElementById('billTolerance').value,
            active: document.getElementById('billActive').checked ? 1 : 0
        });
        const b = await api('/api/budget/envelopes');
        budgetData = b;
        renderBillsList();
        resetBillForm();
        loadBillHistory();
        showToast('Bill saved.');
    } catch (err) { showToast(err.message, true); }
}

async function deleteBill(id) {
    if (!confirm('Delete this bill?')) return;
    try {
        await apiPost('/api/bills/delete', { id });
        budgetData = await api('/api/budget/envelopes');
        renderBillsList();
        loadBillHistory();
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Fill templates
// ---------------------------------------------------------------------
function templateTotal(sid) {
    return (templateDraft[sid] || []).reduce((s, l) => s + (parseFloat(l.amount) || 0), 0);
}

function renderTemplates() {
    const wrap = document.getElementById('templatesWrap');
    if (budgetTemplates.length === 0) {
        wrap.innerHTML = '<div class="import-more">Add a pay source on the Income tab first.</div>';
        return;
    }
    const envs = budgetData.envelopes.filter(e => e.active);
    wrap.innerHTML = budgetTemplates.map(t => {
        const sid = t.income_source_id;
        const lines = templateDraft[sid] || [];
        const used = new Set(lines.map(l => l.envelope_id));
        const rows = lines.map((l, i) => `<tr>
            <td><select class="select-input import-mini" onchange="templateSetEnvelope(${sid}, ${i}, parseInt(this.value, 10))">
                ${envs.filter(e => e.id === l.envelope_id || !used.has(e.id)).map(e => `<option value="${e.id}" ${e.id === l.envelope_id ? 'selected' : ''}>${escapeHtml(e.name)}</option>`).join('')}</select></td>
            <td><input type="number" step="1" min="0" class="input-number import-mini" value="${l.amount}" oninput="templateSetAmount(${sid}, ${i}, this.value)"></td>
            <td><button type="button" class="btn-env-icon delete" onclick="templateRemoveLine(${sid}, ${i})" title="Remove">&times;</button></td></tr>`).join('');
        const free = envs.filter(e => !used.has(e.id));
        return `<div class="template-card" id="tpl-${sid}">
            <div class="template-head">
                <div><div class="table-title">${escapeHtml(t.person_name || '')} &middot; ${escapeHtml(t.source_name)}</div>
                <div class="table-subtitle">${t.typical ? `Typical paycheck ${money(t.typical)}, ${Math.round(t.periods_per_year)} a year` : 'No paychecks yet to base this on'}</div></div>
                <div class="table-header-actions">
                    <button type="button" class="btn-refresh" onclick="templateGenerate(${sid})">Generate from budget</button>
                    <button type="button" class="btn-modal-save" onclick="templateSave(${sid})" ${templateDirty[sid] || !t.saved ? '' : 'disabled'}>Save template</button>
                </div>
            </div>
            <table class="txn-table income-table template-table"><thead><tr><th>Envelope</th><th style="width:140px;">Per paycheck ($)</th><th style="width:50px;"></th></tr></thead>
                <tbody>${rows || '<tr><td colspan="3" class="import-more">No lines yet.</td></tr>'}</tbody></table>
            <div class="template-foot">
                <button type="button" class="btn-bulk-action" onclick="templateAddLine(${sid})" ${free.length ? '' : 'disabled'}>+ Add envelope</button>
                <div class="template-total" id="tpl-total-${sid}"></div>
            </div>
        </div>`;
    }).join('');
    budgetTemplates.forEach(t => updateTemplateTotal(t.income_source_id));
}

function updateTemplateTotal(sid) {
    const t = budgetTemplates.find(x => x.income_source_id === sid);
    const total = templateTotal(sid);
    const el = document.getElementById('tpl-total-' + sid);
    if (!el) return;
    if (!t.typical) { el.innerHTML = `Template total <strong>${money(total)}</strong>`; return; }
    const diff = t.typical - total;
    el.innerHTML = `Template total <strong>${money(total)}</strong> of ${money(t.typical)} &middot; ` +
        (diff >= 0 ? `<span class="tpl-ok">${money(diff)} stays in Unallocated</span>` : `<span class="tpl-short">Short by ${money(-diff)}</span>`);
}

function markTemplateDirty(sid) {
    templateDirty[sid] = true;
    const btn = document.querySelector(`#tpl-${sid} .btn-modal-save`);
    if (btn) btn.disabled = false;
    updateTemplateTotal(sid);
}

function templateSetAmount(sid, i, v) { templateDraft[sid][i].amount = v === '' ? 0 : parseFloat(v); markTemplateDirty(sid); }
function templateSetEnvelope(sid, i, id) { templateDraft[sid][i].envelope_id = id; markTemplateDirty(sid); renderTemplates(); }
function templateRemoveLine(sid, i) { templateDraft[sid].splice(i, 1); markTemplateDirty(sid); renderTemplates(); }

function templateAddLine(sid) {
    const used = new Set(templateDraft[sid].map(l => l.envelope_id));
    const free = budgetData.envelopes.find(e => e.active && !used.has(e.id));
    if (!free) return;
    templateDraft[sid].push({ envelope_id: free.id, amount: 0 });
    markTemplateDirty(sid);
    renderTemplates();
}

async function templateGenerate(sid) {
    try {
        const g = await apiPost('/api/templates/generate', { income_source_id: sid });
        if (g.lines.length === 0) { showToast('Set some envelope budgets first, then generate.', true); return; }
        if (templateDraft[sid].length && !confirm('Replace the lines you have now with suggested ones? Nothing is saved until you press Save.')) return;
        templateDraft[sid] = g.lines;
        markTemplateDirty(sid);
        renderTemplates();
        showToast(`Suggested ${money(g.total)} per paycheck. Adjust, then save.`);
    } catch (err) { showToast(err.message, true); }
}

async function templateSave(sid) {
    try {
        await apiPost('/api/templates/save', { income_source_id: sid, lines: templateDraft[sid] });
        const t = budgetTemplates.find(x => x.income_source_id === sid);
        t.saved = true;
        t.lines = templateDraft[sid].map(l => Object.assign({}, l));
        templateDirty[sid] = false;
        renderTemplates();
        showToast('Template saved.');
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Go-live wizard
// ---------------------------------------------------------------------
function openGoLiveModal() {
    const live = setupStatus.go_live_date;
    let date = live;
    if (!date) {
        const t = new Date();
        date = toInputDateFormat(new Date(t.getFullYear(), t.getMonth() + 1, 1));
    }
    document.getElementById('goLiveDate').value = date;
    const rows = budgetData.envelopes.filter(e => e.active).map(e => ({ id: e.id, name: e.name, budget: e.effective_budget }));
    rows.push({ id: setupStatus.unallocated_id, name: 'Unallocated', budget: null });
    document.getElementById('goLiveBody').innerHTML = rows.map(r => `<tr>
        <td style="font-weight:${r.budget === null ? 700 : 500};">${escapeHtml(r.name)}</td>
        <td style="text-align:right;">${r.budget === null ? '<span class="import-muted">money not in an envelope</span>' : money(r.budget)}</td>
        <td><input type="number" step="0.01" class="input-number import-mini go-live-input" data-id="${r.id}" value="${setupStatus.opening[String(r.id)] ?? ''}" placeholder="0" oninput="updateGoLiveTotal()"></td></tr>`).join('');
    document.getElementById('goLiveWarning').textContent = live ? 'Re-running replaces the opening balances you entered before.' : '';
    updateGoLiveTotal();
    document.getElementById('goLiveModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeGoLiveModal() {
    document.getElementById('goLiveModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

function updateGoLiveTotal() {
    const total = Array.from(document.querySelectorAll('.go-live-input')).reduce((s, i) => s + (parseFloat(i.value) || 0), 0);
    document.getElementById('goLiveTotal').innerHTML = `Total across all envelopes: <strong>${money(total)}</strong>`;
}

async function confirmGoLive() {
    const opening = {};
    document.querySelectorAll('.go-live-input').forEach(i => { if (i.value !== '') opening[i.dataset.id] = parseFloat(i.value); });
    const reset = !!setupStatus.go_live_date;
    if (reset && !confirm('Replace the existing opening balances?')) return;
    try {
        const res = await apiPost('/api/setup/go-live', { date: document.getElementById('goLiveDate').value, opening, reset });
        closeGoLiveModal();
        await refreshTransactions();   // the Spending tab needs the new go-live date too
        await loadBudget();
        showToast(`Go-live set for ${formatDisplayDate(res.go_live_date)} with ${money(res.opening_total)} in envelopes.`);
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Bills we noticed (steady payees without a bill yet)
// ---------------------------------------------------------------------
let discoverList = [];

async function loadDiscovery() {
    try {
        discoverList = (await api('/api/bills/discover')).candidates;
    } catch (err) { discoverList = []; }
    const card = document.getElementById('discoverCard');
    card.hidden = discoverList.length === 0;
    document.getElementById('discoverBody').innerHTML = discoverList.map((c, i) => `<tr>
        <td><div style="font-weight:500;">${escapeHtml(c.name)}</div><div class="import-file-sub">${c.payments} payments, last ${formatDisplayDate(c.last_date)} <span class="conf conf-${c.confidence}">${c.confidence}</span></div></td>
        <td>${escapeHtml(c.envelope)}</td><td>${escapeHtml(billScheduleText(c))}</td>
        <td style="text-align:right;">${money(c.amount)}</td>
        <td>${c.next_due ? formatDisplayDate(c.next_due) : '—'}</td>
        <td style="text-align:right; white-space:nowrap;">
            <button type="button" class="btn-modal-save" onclick="addDiscovered(${i})">Add bill</button>
            <button type="button" class="btn-env-icon delete" title="Not a bill" onclick="dismissDiscovered(${i})">&times;</button></td></tr>`).join('');
}

async function addDiscovered(i) {
    const c = discoverList[i];
    try {
        await apiPost('/api/bills/save', {
            envelope_id: c.envelope_id, name: c.name, match_phrase: c.name, amount: c.amount, freq: c.freq,
            interval: c.interval, anchor_date: c.last_date
        });
        showToast(`Added a bill for ${c.name}.`);
        await reloadBudgetKeepTemplates();
        loadDiscovery();
    } catch (err) { showToast(err.message, true); }
}

async function dismissDiscovered(i) {
    try {
        await apiPost('/api/bills/dismiss', { key: discoverList[i].key });
        loadDiscovery();
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Settings dialog section
// ---------------------------------------------------------------------
async function loadBudgetSettings() {
    try {
        const s = await api('/api/settings');
        document.getElementById('setBonusPct').value = s.bonus_threshold_pct;
        document.getElementById('setSpendMonths').value = s.suggest_window_spending_months;
        document.getElementById('setBillMonths').value = s.suggest_window_bill_months;
        document.getElementById('setBillWindow').value = s.bill_match_window_days;
        document.getElementById('setBillTol').value = s.bill_amount_tolerance_pct;
        document.getElementById('setRefundDays').value = s.refund_lookback_days;
        document.getElementById('setGoLive').textContent = s.go_live_date ? `Go-live: ${formatDisplayDate(s.go_live_date)} (change it on the Budget tab)` : 'Go-live not set yet';
    } catch (err) { /* settings are optional to view */ }
}

async function saveBudgetSettings() {
    try {
        await apiPost('/api/settings', {
            bonus_threshold_pct: document.getElementById('setBonusPct').value,
            suggest_window_spending_months: document.getElementById('setSpendMonths').value,
            suggest_window_bill_months: document.getElementById('setBillMonths').value,
            bill_match_window_days: document.getElementById('setBillWindow').value,
            bill_amount_tolerance_pct: document.getElementById('setBillTol').value,
            refund_lookback_days: document.getElementById('setRefundDays').value
        });
        showToast('Budget settings saved.');
        if (budgetLoaded) await reloadBudgetKeepTemplates();
    } catch (err) { showToast(err.message, true); }
}
