/* Audit dialog: find double charges and charges that look wrong, on demand and after every import. */

let auditCfg = null;
let auditFindings = [];      // what is currently shown
let auditMode = 'manual';    // 'manual' (full scan) or 'import' (just the last import)
let auditReturnTo = null;    // set while the editor or compare window is open over the dialog

const AUDIT_GROUPS = { duplicates: 'Duplicates', unusual: 'Unusual charges', tidy: 'Housekeeping' };
const DUP_CHECKS = ['dup_same_day', 'dup_near_days', 'dup_import_twins', 'dup_amount_date'];

document.addEventListener('DOMContentLoaded', () => {
    loadAuditBadge();
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAuditDialog(); });
});
document.addEventListener('editclosed', resumeAuditDialog);
document.addEventListener('comparedclosed', resumeAuditDialog);

async function loadAuditBadge() {
    try {
        const r = await api('/api/audit/pending');
        setAuditBadge(r.total);
    } catch (err) { /* the badge is optional */ }
}

function setAuditBadge(n) {
    const b = document.getElementById('auditBadge');
    b.textContent = n;
    b.hidden = !(n > 0);
}

// ---------------------------------------------------------------------
// Open, close, set up
// ---------------------------------------------------------------------
async function openAuditDialog(opts = {}) {
    try {
        auditCfg = await api('/api/audit/config');
    } catch (err) { showToast('Could not open the audit: ' + err.message, true); return; }
    renderAuditSetup();
    document.getElementById('auditDlgOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    auditMode = opts.mode || 'manual';
    const results = document.getElementById('auditResults');
    if (auditMode === 'import') {
        document.getElementById('auditDlgTitle').textContent = 'Audit: this import';
        document.getElementById('auditDlgSub').textContent = 'What the checks you chose for every import found in the rows that were just saved.';
        document.getElementById('auditSetup').open = false;
        const data = opts.results || await api('/api/audit/pending');
        showAuditResults(data);
    } else {
        document.getElementById('auditDlgTitle').textContent = 'Audit';
        document.getElementById('auditDlgSub').textContent = 'Checks your whole history for double charges and charges that look wrong.';
        document.getElementById('auditSetup').open = true;
        results.innerHTML = '<div class="audit-empty">Pick the checks and press <strong>Run audit</strong>.</div>';
        document.getElementById('auditFooter').textContent = '';
        auditFindings = [];
    }
}

function closeAuditDialog() {
    const o = document.getElementById('auditDlgOverlay');
    if (!o.classList.contains('active')) return;
    o.classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
    loadAuditBadge();
}

function renderAuditSetup() {
    const c = auditCfg;
    let html = '<div class="audit-check-cols"><span>Check</span><span>Run now</span><span>Every import</span></div>';
    Object.entries(AUDIT_GROUPS).forEach(([group, label]) => {
        const rows = c.checks.filter(x => x.group === group);
        if (!rows.length) return;
        html += `<div class="audit-group">${label}</div>` + rows.map(x => `<div class="audit-check">
            <div><div class="audit-check-title">${escapeHtml(x.title)}</div><div class="audit-check-help">${escapeHtml(x.help)}</div></div>
            <input type="checkbox" class="txn-checkbox" data-run="${x.id}" ${c.manual.includes(x.id) ? 'checked' : ''} aria-label="Run ${escapeHtml(x.title)} now">
            <input type="checkbox" class="txn-checkbox" data-import="${x.id}" ${c.import.includes(x.id) ? 'checked' : ''} aria-label="Run ${escapeHtml(x.title)} on every import">
        </div>`).join('');
    });
    document.getElementById('auditChecks').innerHTML = html;
    document.getElementById('auditLarge').value = c.large_amount;
    document.getElementById('auditLookback').value = c.lookback_days;
    document.getElementById('auditAutoOpen').checked = c.auto_open;
}

function auditChoices() {
    const pick = (attr) => Array.from(document.querySelectorAll(`#auditChecks input[${attr}]:checked`)).map(i => i.getAttribute(attr));
    return {
        manual: pick('data-run'), import: pick('data-import'),
        large_amount: parseFloat(document.getElementById('auditLarge').value) || 0,
        lookback_days: parseInt(document.getElementById('auditLookback').value, 10) || 0,
        auto_open: document.getElementById('auditAutoOpen').checked
    };
}

// ---------------------------------------------------------------------
// Run and show
// ---------------------------------------------------------------------
async function runAudit() {
    const ch = auditChoices();
    if (ch.manual.length === 0) { showToast('Tick at least one check to run.', true); return; }
    const btn = document.getElementById('btnRunAudit');
    btn.disabled = true;
    btn.textContent = 'Checking…';
    try {
        await apiPost('/api/audit/settings', ch);
        auditMode = 'manual';
        document.getElementById('auditDlgTitle').textContent = 'Audit';
        const data = await apiPost('/api/audit/run', { checks: ch.manual, large_amount: ch.large_amount, lookback_days: ch.lookback_days });
        showAuditResults(data);
        document.getElementById('auditSetup').open = false;
    } catch (err) {
        showToast(err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Run audit';
    }
}

function showAuditResults(data) {
    auditFindings = data.findings || [];
    renderAuditResults(data);
}

function txnLine(t) {
    return `<div class="audit-txn">${escapeHtml(formatDisplayDate(t.date))} &middot; ${escapeHtml(t.name)} &middot; <strong>${formatCurrency(Math.abs(t.amount))}</strong>${t.envelope ? ' &middot; ' + escapeHtml(t.envelope) : ''}</div>`;
}

function renderAuditResults(data) {
    const box = document.getElementById('auditResults');
    const checks = (auditCfg && auditCfg.checks) || [];
    const title = (id) => (checks.find(c => c.id === id) || { title: id }).title;
    if (auditFindings.length === 0) {
        box.innerHTML = `<div class="audit-empty"><div style="font-size:30px;">&#10003;</div><strong>Nothing looks wrong.</strong><br>
            ${(data.ran || []).length} check${(data.ran || []).length === 1 ? '' : 's'} run${data.hidden_reviewed ? `, ${data.hidden_reviewed} you already marked OK left out` : ''}.</div>`;
        document.getElementById('auditFooter').textContent = '';
        return;
    }
    const order = (auditCfg ? auditCfg.checks.map(c => c.id) : []);
    const byCheck = {};
    auditFindings.forEach((f, idx) => { (byCheck[f.check] = byCheck[f.check] || []).push([f, idx]); });
    let html = '';
    Object.keys(byCheck).sort((a, b) => order.indexOf(a) - order.indexOf(b)).forEach(cid => {
        const list = byCheck[cid];
        const isDup = DUP_CHECKS.includes(cid);
        html += `<div class="audit-section-head"><div class="audit-section-title">${escapeHtml(title(cid))} &mdash; ${list.length}</div>
            <div class="audit-actions">${isDup ? `<button type="button" class="btn-bulk-action" onclick="compareFindings('${cid}')">Compare side by side</button>` : ''}
            <button type="button" class="btn-bulk-action" onclick="dismissFindings('${cid}')">Mark all OK</button></div></div>`;
        html += list.map(([f, idx]) => `<div class="audit-finding" id="af-${idx}">
            <div class="audit-sev audit-sev-${f.severity}">${f.severity}</div>
            <div><div class="audit-reason">${escapeHtml(f.reason)}</div>${f.txns.map(txnLine).join('')}</div>
            <div class="audit-actions">
                ${isDup ? `<button type="button" class="btn-inline-action btn-inline-save" onclick="compareFindings('${cid}', ${idx})">Compare</button>` : ''}
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="openFindingTxn(${idx})">Open</button>
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="dismissFinding(${idx})">Mark OK</button>
            </div></div>`).join('');
    });
    box.innerHTML = html;
    document.getElementById('auditFooter').textContent =
        `${auditFindings.length} to look at${data.hidden_reviewed ? `, ${data.hidden_reviewed} already marked OK` : ''}. "Mark OK" is remembered.`;
}

// ---------------------------------------------------------------------
// Acting on findings
// ---------------------------------------------------------------------
async function dismissItems(findings) {
    await apiPost('/api/audit/dismiss', { items: findings.map(f => ({ check: f.check, key: f.key })) });
    auditFindings = auditFindings.filter(f => !findings.includes(f));
    renderAuditResults({ ran: [], hidden_reviewed: 0 });
    if (auditMode === 'import') loadAuditBadge();
}

async function dismissFinding(idx) {
    try { await dismissItems([auditFindings[idx]]); } catch (err) { showToast(err.message, true); }
}

async function dismissFindings(checkId) {
    const list = auditFindings.filter(f => f.check === checkId);
    if (!confirm(`Mark all ${list.length} as OK? They won't show up again.`)) return;
    try { await dismissItems(list); } catch (err) { showToast(err.message, true); }
}

function suspendAuditDialog() {
    auditReturnTo = { mode: auditMode, checks: auditChoices().manual };
    const o = document.getElementById('auditDlgOverlay');
    o.classList.remove('active');
}

async function resumeAuditDialog() {
    if (!auditReturnTo) return;
    const back = auditReturnTo;
    auditReturnTo = null;
    await refreshTransactions();
    await openAuditDialog({ mode: back.mode });
    if (back.mode === 'manual' && back.checks.length) await runAudit();
}

function openFindingTxn(idx) {
    const f = auditFindings[idx];
    const id = f.txns[f.txns.length - 1].txn_id;
    suspendAuditDialog();
    openEditModal(id);
}

// Opens the existing side-by-side window for a duplicate (or for every duplicate of one kind)
function compareFindings(checkId, onlyIdx) {
    const list = onlyIdx === undefined ? auditFindings.filter(f => f.check === checkId) : [auditFindings[onlyIdx]];
    const queue = [];
    list.forEach(f => {
        const items = f.txns.map(t => allTransactions.find(x => x.txn_id === t.txn_id)).filter(Boolean);
        for (let i = 0; i < items.length - 1; i++) {
            queue.push({ type: 'duplicate_pair', itemA: items[i], itemB: items[i + 1] });
        }
    });
    if (queue.length === 0) { showToast('Those transactions are no longer in your data. Run the audit again.', true); return; }
    suspendAuditDialog();
    auditCandidatesQueue = queue;
    currentAuditIndex = 0;
    openAuditModal();
}
