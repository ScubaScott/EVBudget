/* Envelopes tab: how full each envelope is, paycheck fills, transfers, and what's coming due. */

let envState = null;     // /api/envelopes/balances
let envQueue = [];
let envUpcoming = [];
let allocCtx = null;     // the allocation screen's working state
let drawerEnvId = null;

const GROUPS = [['spending', 'Spending'], ['bill', 'Bills'], ['savings', 'Savings']];
const STATUS_LABELS = { on_track: 'On track', behind: 'Behind', overdrawn: 'Overdrawn', funded: 'Funded' };

document.addEventListener('tabchange', (e) => { if (e.detail.tab === 'envelopes') loadEnvelopesTab(); });
document.addEventListener('txnsrefreshed', () => { if (typeof activeTab !== 'undefined' && activeTab === 'envelopes') loadEnvelopesTab(); });
document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { closeAllocModal(); closeTransferModal(); closeEnvDrawer(); closeUnallocModal(); }
    });
});

const todayIso = () => toInputDateFormat(new Date());
const m$ = (v) => formatCurrency(v || 0);

// ---------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------
async function loadEnvelopesTab() {
    try {
        const [bal, queue, up] = await Promise.all([
            api('/api/envelopes/balances'), api('/api/allocations/queue'), api('/api/bills/upcoming?days=30')
        ]);
        envState = bal;
        envQueue = queue.queue;
        envUpcoming = up.bills;
        renderEnvelopesTab();
    } catch (err) {
        showToast('Could not load envelopes: ' + err.message, true);
    }
}

function renderEnvelopesTab() {
    const live = envState.enabled;
    document.getElementById('envNotLive').hidden = live;
    document.getElementById('envLive').hidden = !live;
    if (!live) return;
    renderEnvWarnings();
    renderEnvTimeline();
    renderEnvQueue();
    renderEnvUpcoming();
    if (drawerEnvId) renderEnvDrawer();
}

// ---------------------------------------------------------------------
// Time slider: show balances as of an earlier day (the summary cards and bars only; the rest stays live)
// ---------------------------------------------------------------------
const TIMELINE_DAYS = 30;
let envDaysBack = 0;       // 0 = today
let envHistSeq = 0;        // guards against out-of-order responses while dragging

function envTimelineDate(daysBack) {
    const d = new Date();
    d.setDate(d.getDate() - daysBack);
    return toInputDateFormat(d);
}

// Never offer days before go-live: there is nothing to show there
function envTimelineSpan() {
    const gl = envState && envState.go_live_date ? new Date(envState.go_live_date + 'T00:00:00') : null;
    if (!gl) return TIMELINE_DAYS;
    const sinceGoLive = Math.floor((Date.now() - gl.getTime()) / 86400000);
    return Math.max(0, Math.min(TIMELINE_DAYS, sinceGoLive));
}

function renderEnvTimeline() {
    const span = envTimelineSpan();
    envDaysBack = Math.min(envDaysBack, span);
    const slider = document.getElementById('envTimeline');
    slider.min = -span;
    slider.disabled = span === 0;
    slider.value = -envDaysBack;
    document.getElementById('envTimelineStart').textContent = span ? formatDisplayDate(envTimelineDate(span)).slice(0, 5) : '';
    updateEnvTimeline();
}

function onEnvTimeline(v) {
    envDaysBack = -parseInt(v, 10);
    updateEnvTimeline();
}

function resetEnvTimeline() {
    envDaysBack = 0;
    document.getElementById('envTimeline').value = 0;
    updateEnvTimeline();
}

async function updateEnvTimeline() {
    const seq = ++envHistSeq;
    const back = envDaysBack;
    document.getElementById('envTimelineReset').disabled = back === 0;
    document.getElementById('envTimelineLabel').textContent = back === 0 ? 'Today'
        : `${formatDisplayDate(envTimelineDate(back))} (${back} day${back === 1 ? '' : 's'} ago)`;
    if (back === 0) { drawEnvBalances(envState); return; }
    try {
        const hist = await api('/api/envelopes/balances?as_of=' + envTimelineDate(back));
        if (seq === envHistSeq) drawEnvBalances(hist);
    } catch (err) {
        if (seq === envHistSeq) showToast('Could not load that day: ' + err.message, true);
    }
}

function drawEnvBalances(state) {
    renderEnvSummary(state, state !== envState);
    renderEnvChart(state);
}

function renderEnvWarnings() {
    const s = envState, w = [];
    if (s.over_allocated) {
        w.push(`<div class="warn-banner warn-red"><div><strong>Over-allocated.</strong> Unallocated is ${m$(s.unallocated)}: more has been assigned to envelopes than you have.
            Undo a fill, or move money back into Unallocated.</div>
            <div><button type="button" class="btn-bulk-action" onclick="openTransferModal(undefined, envState.unallocated_id)">Move money in</button></div></div>`);
    }
    const u = s.uncategorized;
    if (u && u.count > 0) {
        w.push(`<div class="warn-banner warn-amber"><div><strong>${u.count} transaction${u.count === 1 ? '' : 's'} since go-live ${u.count === 1 ? 'has' : 'have'} no envelope.</strong>
            ${u.spent ? `${m$(u.spent)} of spending` : ''}${u.spent && u.refunded ? ' and ' : ''}${u.refunded ? `${m$(u.refunded)} of refunds or reimbursements` : ''}
            ${u.count === 1 ? 'comes' : 'come'} out of (or go into) Unallocated instead of an envelope.</div>
            <div><button type="button" class="btn-bulk-action" onclick="goToUncategorized()">Assign envelopes</button></div></div>`);
    }
    document.getElementById('envWarnings').innerHTML = w.join('');
}

function exportEnvelopeCsv() {
    if (drawerEnvId) window.location.href = '/api/envelopes/ledger.csv?envelope_id=' + drawerEnvId;
}

function renderEnvSummary(s = envState, historical = false) {
    // Queue and bills-due counts only exist for today, and the Unallocated card opens a live screen
    const card = (cls, title, value, sub, attrs = '') => `<div class="kpi-card ${cls}" ${attrs}><div class="kpi-header"><span class="kpi-title">${title}</span></div>
        <div class="kpi-value ${cls}">${value}</div><div class="kpi-subtitle">${sub}</div></div>`;
    document.getElementById('envSummary').innerHTML =
        card(s.over_allocated ? 'spend' : 'net', 'Unallocated', m$(s.unallocated),
            (s.over_allocated ? 'Over-allocated: more has been assigned than you have' : 'Income not yet put in an envelope') + (historical ? '' : ' &bull; click to fill'),
            historical ? '' : 'role="button" tabindex="0" style="cursor:pointer" onclick="openUnallocModal()" onkeydown="if (event.key === \'Enter\') openUnallocModal()"') +
        card(s.total_in_envelopes < 0 ? 'spend' : 'income', 'In envelopes', m$(s.total_in_envelopes), 'Across all active envelopes') +
        (historical ? '' : card('count', 'To fill', String(s.queue_count), 'Deposits waiting to be split')) +
        card(s.overdrawn_count ? 'spend' : 'count', 'Overdrawn', String(s.overdrawn_count), 'Envelopes below zero') +
        (historical ? '' : card('count', 'Bills due soon', String(s.bills_due_soon), 'Due in the next 7 days or overdue'));
    const warn = document.getElementById('envInvariant');
    warn.hidden = !s.invariant || s.invariant.ok;
    if (!warn.hidden) warn.textContent = 'Balances do not add up to opening money + income − spending. Please report this.';
}

// With no positive target (a carried-in deficit), any money in the envelope is more than it needs: a full bar.
function pctOf(value, cap) { return cap > 0 ? Math.max(0, value / cap) : (value > 0 ? 1 : 0); }

function renderEnvChart(state = envState) {
    const envs = state.envelopes.filter(e => e.active);
    const anyNeg = envs.some(e => e.balance < 0);
    const neg = anyNeg ? 12 : 0;        // width of the left-hand "overdrawn" zone, in % of the track
    const pos = 100 - neg;
    let html = '';
    GROUPS.forEach(([kind, label]) => {
        const rows = envs.filter(e => e.kind === kind);
        if (!rows.length) return;
        html += `<div class="env-group">${label}</div>`;
        rows.forEach(e => {
            const frac = pctOf(e.balance, e.capacity);
            const fillW = Math.min(1, frac) * pos;
            const over = frac > 1.0001 || (e.capacity <= 0 && e.balance > 0);
            const negW = e.balance < 0 ? Math.min(1, Math.abs(e.balance) / Math.max(e.capacity, Math.abs(e.balance), 1)) * neg : 0;
            const markerLeft = e.marker !== null && e.capacity > 0 ? neg + Math.min(1, Math.max(0, e.marker / e.capacity)) * pos : null;
            const tip = [
                `${e.name}: ${m$(e.balance)} of ${m$(e.capacity)}`,
                e.marker !== null ? `${e.marker_mode === 'pace_down' ? 'Target moves down as the month goes on. ' : 'Target builds up over time. '}Should have about ${m$(e.marker)} by now${e.gap > 0 ? ` (${m$(e.gap)} short)` : ' (on pace)'}` : 'No target marker for this envelope',
                e.carry_in ? `Carried in: ${m$(e.carry_in)}` : '', e.next_due ? `Next bill due ${formatDisplayDate(e.next_due)}` : ''
            ].filter(Boolean).join('\n');
            html += `<div class="env-row" onclick="openEnvDrawer(${e.id})" title="${escapeHtml(tip)}" tabindex="0" onkeydown="if (event.key==='Enter') openEnvDrawer(${e.id})">
                <div class="env-label"><span class="env-name">${escapeHtml(e.name)}</span>
                    <span class="env-nums">${m$(e.balance)} <span class="env-of">/ ${m$(e.capacity)}</span></span></div>
                <div class="env-track">
                    ${anyNeg ? `<div class="env-zero" style="left:${neg}%"></div>` : ''}
                    ${negW ? `<div class="env-fill st-overdrawn" style="left:${neg - negW}%; width:${negW}%"></div>` : ''}
                    ${fillW ? `<div class="env-fill st-${e.status}${over ? ' env-over' : ''}" style="left:${neg}%; width:${fillW}%"></div>` : ''}
                    ${markerLeft !== null ? `<div class="env-marker" style="left:${markerLeft}%"><span class="env-arrow ${e.marker_mode === 'pace_down' ? 'env-arrow-left' : 'env-arrow-right'}">${e.marker_mode === 'pace_down' ? '&#9664;' : '&#9654;'}</span></div>` : ''}
                </div>
                <div class="env-status"><span class="env-chip st-${e.status}">${STATUS_LABELS[e.status]}</span>${e.overdue ? ' <span class="kind-badge kind-review">Bill overdue</span>' : (e.next_due ? `<span class="env-due">due ${formatDisplayDate(e.next_due).slice(0, 5)}</span>` : '')}</div>
            </div>`;
        });
    });
    document.getElementById('envChart').innerHTML = html || '<div class="import-more">No active envelopes.</div>';
}

function renderEnvQueue() {
    document.getElementById('envQueueCount').textContent = envQueue.length ? `${envQueue.length} waiting` : 'All caught up';
    document.getElementById('envQueueBody').innerHTML = envQueue.length === 0
        ? '<tr><td colspan="4" class="import-more">Every deposit since go-live has been split across envelopes.</td></tr>'
        : envQueue.map(q => `<tr>
            <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(q.date))}</td>
            <td><div style="font-weight:500;">${escapeHtml(q.person_name || q.name)}${q.source_name ? ' &middot; ' + escapeHtml(q.source_name) : ''}</div>
                <div class="import-file-sub">${q.txn_kind === 'paycheck' ? (q.income_subtype === 'bonus' ? 'Bonus' : 'Paycheck') : 'Other income' + (q.income_label ? ' · ' + escapeHtml(q.income_label) : '')}</div></td>
            <td class="amount-cell amount-income">${queueAmountMarkup(q)}</td>
            <td style="text-align:right;"><button type="button" class="btn-modal-save" onclick="openAllocModal('${escapeHtml(q.txn_id)}')">${q.partial ? 'Finish' : 'Allocate'}</button></td></tr>`).join('');
}

// A deposit's amount, plus how much of it is still unassigned when only part has been split
function queueAmountMarkup(q) {
    return formatSigned(q.amount) + (q.partial
        ? `<div class="import-file-sub"><span class="kind-badge kind-review">Partially funded</span> ${m$(q.remaining)} left of ${m$(q.amount)}</div>` : '');
}

function renderEnvUpcoming() {
    document.getElementById('envUpcomingBody').innerHTML = envUpcoming.length === 0
        ? '<tr><td colspan="5" class="import-more">No bills due in the next 30 days. Add bills on the Budget tab.</td></tr>'
        : envUpcoming.map(b => `<tr class="${b.overdue ? 'import-row-review' : ''}">
            <td style="font-weight:500;">${escapeHtml(b.name)}</td><td>${escapeHtml(b.envelope)}</td>
            <td style="white-space:nowrap;">${formatDisplayDate(b.due)}${b.overdue ? ' <span class="kind-badge kind-review">Overdue</span>' : ''}</td>
            <td style="text-align:right;">${m$(b.amount)}</td>
            <td>${b.covered === null ? '' : b.covered ? '<span class="tpl-ok">Covered</span>' : `<span class="tpl-short">Short ${m$(b.amount - b.balance)}</span>`}</td></tr>`).join('');
}

// ---------------------------------------------------------------------
// Allocation screen (split a deposit across envelopes)
// ---------------------------------------------------------------------
async function openAllocModal(txnId, presetEnvelopeId, returnToUnalloc) {
    try {
        if (txnId) {
            const d = await api('/api/allocations/draft?txn_id=' + encodeURIComponent(txnId));
            const existing = d.existing;
            allocCtx = {
                mode: 'txn', txn: d.txn, envs: d.envelopes, unallocated: d.unallocated, existing,
                returnTo: !!returnToUnalloc,
                lines: (existing ? existing.lines : d.lines).map(l => Object.assign({}, l)), fromTemplate: d.from_template,
                templateLines: d.lines.map(l => Object.assign({}, l))
            };
        } else {
            await loadEnvelopesTab();
            allocCtx = {
                mode: 'adhoc', txn: null, envs: envState.envelopes, unallocated: envState.unallocated, existing: null,
                lines: presetEnvelopeId ? [{ envelope_id: presetEnvelopeId, amount: 0 }] : [], fromTemplate: false, templateLines: []
            };
        }
        renderAllocModal();
        document.getElementById('allocModalOverlay').classList.add('active');
        document.body.style.overflow = 'hidden';
    } catch (err) {
        showToast(err.message, true);
    }
}

function closeAllocModal() {
    if (!document.getElementById('allocModalOverlay').classList.contains('active')) return;
    document.getElementById('allocModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
    const back = allocCtx && allocCtx.returnTo;
    allocCtx = null;
    if (back) openUnallocModal();
}

// ---------------------------------------------------------------------
// Unallocated: the deposits waiting to be split, each with its own Allocate button
// ---------------------------------------------------------------------
function openUnallocModal() {
    renderUnallocModal();
    document.getElementById('unallocModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeUnallocModal() {
    if (!document.getElementById('unallocModalOverlay').classList.contains('active')) return;
    document.getElementById('unallocModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

function renderUnallocModal() {
    const total = envQueue.reduce((sum, q) => sum + q.remaining, 0);
    document.getElementById('unallocSummary').innerHTML =
        `<strong>${m$(envState.unallocated)}</strong> is in Unallocated. ` +
        (envQueue.length ? `${envQueue.length} deposit${envQueue.length === 1 ? '' : 's'} (${m$(total)}) still need to be split across envelopes.` : 'Every deposit has been split.') +
        (envState.over_allocated ? ' <span class="tpl-short">More has been assigned than you have.</span>' : '');
    document.getElementById('unallocBody').innerHTML = envQueue.length === 0
        ? '<tr><td colspan="4" class="import-more">Nothing waiting. Anything left in Unallocated is spending without an envelope, or money you chose to hold back.</td></tr>'
        : envQueue.map(q => `<tr>
            <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(q.date))}</td>
            <td><div style="font-weight:500;">${escapeHtml(q.person_name || q.name)}${q.source_name ? ' &middot; ' + escapeHtml(q.source_name) : ''}</div>
                <div class="import-file-sub">${q.txn_kind === 'paycheck' ? (q.income_subtype === 'bonus' ? 'Bonus' : 'Paycheck') : 'Other income' + (q.income_label ? ' · ' + escapeHtml(q.income_label) : '')}</div></td>
            <td class="amount-cell amount-income">${queueAmountMarkup(q)}</td>
            <td style="text-align:right;"><button type="button" class="btn-modal-save" onclick="allocateFromUnalloc('${escapeHtml(q.txn_id)}')">${q.partial ? 'Finish' : 'Allocate'}</button></td></tr>`).join('');
}

function allocateFromUnalloc(txnId) {
    closeUnallocModal();
    openAllocModal(txnId, undefined, true);
}

function allocAvailable() {
    return allocCtx.mode === 'txn' ? allocCtx.txn.amount : allocCtx.unallocated;
}

function allocTotal() {
    return allocCtx.lines.reduce((s, l) => s + (parseFloat(l.amount) || 0), 0);
}

function renderAllocModal() {
    const c = allocCtx;
    const t = c.txn;
    document.getElementById('allocTitle').textContent = c.mode === 'txn' ? 'Fill envelopes from this deposit' : 'Fund envelopes from Unallocated';
    document.getElementById('allocSubtitle').textContent = c.mode === 'txn'
        ? `${t.name} · ${formatDisplayDate(t.date)} · ${m$(t.amount)}`
        : `${m$(c.unallocated)} is in Unallocated right now`;
    document.getElementById('allocNote').innerHTML = c.existing
        ? 'This deposit is already split. Saving replaces that split, or you can undo it.'
        : (c.mode === 'txn' ? (c.fromTemplate ? 'Pre-filled from this pay source\'s template. Change anything you like.'
            : (t.kind === 'paycheck' && t.subtype !== 'bonus' ? 'No template for this pay source yet. Use Suggest, or set one up on the Budget tab.' : 'Bonuses and other income start empty. Suggest fills the envelopes that need it most.')) : '');
    document.getElementById('allocDateWrap').hidden = c.mode === 'txn';
    if (c.mode === 'adhoc' && !document.getElementById('allocDate').value) document.getElementById('allocDate').value = todayIso();
    document.getElementById('btnAllocVoid').hidden = !c.existing;
    document.getElementById('btnAllocTemplate').hidden = !(c.templateLines && c.templateLines.length);

    const used = new Set(c.lines.map(l => l.envelope_id));
    const active = c.envs.filter(e => e.active);
    document.getElementById('allocBody').innerHTML = c.lines.length === 0
        ? '<tr><td colspan="6" class="import-more">No lines yet. Use Suggest or add an envelope.</td></tr>'
        : c.lines.map((l, i) => {
            const e = c.envs.find(x => x.id === l.envelope_id) || {};
            const gap = e.gap;
            return `<tr>
                <td><select class="select-input import-mini" onchange="allocSetEnv(${i}, parseInt(this.value, 10))">${active.filter(x => x.id === l.envelope_id || !used.has(x.id)).map(x => `<option value="${x.id}" ${x.id === l.envelope_id ? 'selected' : ''}>${escapeHtml(x.name)}</option>`).join('')}</select></td>
                <td style="text-align:right;">${m$(e.balance)}</td>
                <td style="text-align:right;">${e.marker === null || e.marker === undefined ? '—' : m$(e.marker)}</td>
                <td style="text-align:right;" class="${gap > 0 ? 'tpl-short' : ''}">${gap === null || gap === undefined ? '—' : (gap > 0 ? m$(gap) + ' short' : 'on pace')}</td>
                <td><input type="number" step="0.01" min="0" class="input-number import-mini" value="${l.amount}" oninput="allocSetAmount(${i}, this.value)"></td>
                <td><button type="button" class="btn-env-icon delete" onclick="allocRemove(${i})" title="Remove">&times;</button></td></tr>`;
        }).join('');
    document.getElementById('btnAllocAdd').disabled = active.every(x => used.has(x.id));
    updateAllocTotals();
}

function updateAllocTotals() {
    const avail = allocAvailable(), total = allocTotal();
    const left = Math.round((avail - total) * 100) / 100;
    document.getElementById('allocTotals').innerHTML =
        `${allocCtx.mode === 'txn' ? 'Deposit' : 'Available'} <strong>${m$(avail)}</strong> &middot; Allocated <strong>${m$(total)}</strong> &middot; ` +
        (left >= 0 ? `<span class="tpl-ok">${m$(left)} stays in Unallocated</span>` : `<span class="tpl-short">${m$(-left)} more than this deposit</span>`);
    document.getElementById('allocWarning').textContent = left < 0
        ? (allocCtx.mode === 'txn' ? 'This takes the extra from Unallocated. It will only save if Unallocated can cover it.' : 'More than Unallocated holds. It will not save.') : '';
}

function allocSetAmount(i, v) { allocCtx.lines[i].amount = v === '' ? 0 : parseFloat(v); updateAllocTotals(); }
function allocSetEnv(i, id) { allocCtx.lines[i].envelope_id = id; renderAllocModal(); }
function allocRemove(i) { allocCtx.lines.splice(i, 1); renderAllocModal(); }

function allocAddLine() {
    const used = new Set(allocCtx.lines.map(l => l.envelope_id));
    const free = allocCtx.envs.find(e => e.active && !used.has(e.id));
    if (!free) return;
    allocCtx.lines.push({ envelope_id: free.id, amount: 0 });
    renderAllocModal();
}

function allocApplyTemplate() {
    allocCtx.lines = allocCtx.templateLines.map(l => Object.assign({}, l));
    renderAllocModal();
}

// Spreads whatever is left (or over) evenly across the lines so Allocated lands exactly on the deposit.
// Works in whole cents. Odd cents favor the envelope with the biggest shortfall: when adding, the most short
// get them first; when removing, the least short give them up first. Removing never takes a line below zero.
function allocBalance() {
    const lines = allocCtx.lines;
    if (!lines.length) { showToast('Add an envelope first.'); return; }
    const cents = lines.map(l => Math.round((parseFloat(l.amount) || 0) * 100));
    const shortfall = lines.map(l => {
        const e = allocCtx.envs.find(x => x.id === l.envelope_id);
        return e && typeof e.gap === 'number' ? e.gap : -Infinity;
    });
    const neediest = lines.map((l, i) => i).sort((a, b) => shortfall[b] - shortfall[a]);
    let diff = Math.round(allocAvailable() * 100) - cents.reduce((s, c) => s + c, 0);
    if (diff === 0) { showToast('Already balanced.'); return; }
    if (diff > 0) {
        const q = Math.floor(diff / cents.length), r = diff % cents.length;
        cents.forEach((c, i) => { cents[i] = c + q; });
        neediest.slice(0, r).forEach(i => { cents[i] += 1; });
        diff = 0;
    } else {
        let need = -diff;
        while (need > 0) {
            const live = neediest.slice().reverse().filter(i => cents[i] > 0);
            if (!live.length) break;
            const share = Math.floor(need / live.length);
            if (share === 0) {
                live.slice(0, need).forEach(i => { cents[i] -= 1; });
                need = 0;
            } else {
                live.forEach(i => { const take = Math.min(share, cents[i]); cents[i] -= take; need -= take; });
            }
        }
    }
    lines.forEach((l, i) => { l.amount = cents[i] / 100; });
    renderAllocModal();
}

async function allocSuggest() {
    try {
        const r = await apiPost('/api/allocations/suggest', { amount: Math.max(0, allocAvailable()) });
        allocCtx.lines = r.lines;
        renderAllocModal();
        if (!r.lines.length) showToast('Nothing needs money right now.');
    } catch (err) { showToast(err.message, true); }
}

async function allocConfirm() {
    const c = allocCtx;
    try {
        await apiPost('/api/allocations/confirm', {
            income_txn_id: c.txn ? c.txn.txn_id : null, lines: c.lines.filter(l => parseFloat(l.amount) > 0),
            date: c.mode === 'adhoc' ? document.getElementById('allocDate').value : undefined,
            replace_allocation_id: c.existing ? c.existing.id : undefined
        });
        showToast('Envelopes filled.');
        await refreshTransactions();
        await loadEnvelopesTab();
        closeAllocModal();
    } catch (err) { showToast(err.message, true); }
}

async function allocVoid() {
    if (!allocCtx.existing || !confirm('Undo this fill? The money goes back to Unallocated.')) return;
    try {
        await apiPost('/api/allocations/void', { allocation_id: allocCtx.existing.id });
        showToast('Fill undone.');
        await refreshTransactions();
        await loadEnvelopesTab();
        closeAllocModal();
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Transfers
// ---------------------------------------------------------------------
function openTransferModal(fromId, toId) {
    const options = (sel) => [{ id: envState.unallocated_id, name: 'Unallocated' }].concat(envState.envelopes.filter(e => e.active))
        .map(e => `<option value="${e.id}" ${e.id === sel ? 'selected' : ''}>${escapeHtml(e.name)}</option>`).join('');
    document.getElementById('transferFrom').innerHTML = options(fromId || envState.unallocated_id);
    document.getElementById('transferTo').innerHTML = options(toId || (envState.envelopes.find(e => e.active) || {}).id);
    document.getElementById('transferAmount').value = '';
    document.getElementById('transferDate').value = todayIso();
    document.getElementById('transferNote').value = '';
    document.getElementById('transferModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeTransferModal() {
    document.getElementById('transferModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function saveTransfer() {
    try {
        await apiPost('/api/moves/transfer', {
            from_envelope_id: parseInt(document.getElementById('transferFrom').value, 10),
            to_envelope_id: parseInt(document.getElementById('transferTo').value, 10),
            amount: document.getElementById('transferAmount').value, date: document.getElementById('transferDate').value,
            note: document.getElementById('transferNote').value
        });
        closeTransferModal();
        showToast('Money moved.');
        await loadEnvelopesTab();
    } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Envelope drawer: settings summary, bills, and every move with a running balance
// ---------------------------------------------------------------------
async function openEnvDrawer(id) {
    drawerEnvId = id;
    document.getElementById('envDrawerOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    await renderEnvDrawer();
}

function closeEnvDrawer() {
    if (!document.getElementById('envDrawerOverlay').classList.contains('active')) return;
    document.getElementById('envDrawerOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
    drawerEnvId = null;
}

async function renderEnvDrawer() {
    const e = envState.envelopes.find(x => x.id === drawerEnvId);
    if (!e) { closeEnvDrawer(); return; }
    document.getElementById('drawerTitle').textContent = e.name;
    document.getElementById('drawerSub').innerHTML =
        `${KIND_OPTIONS_LABEL[e.kind]} &middot; budget ${m$(e.budget)} a month`;
    document.getElementById('drawerFacts').innerHTML = [
        ['Balance', m$(e.balance)], ['Target', e.marker === null ? '—' : m$(e.marker)], ['Full at', m$(e.capacity)],
        ['Status', `<span class="env-chip st-${e.status}">${STATUS_LABELS[e.status]}</span>`]
    ].map(([k, v]) => `<div class="drawer-fact"><div class="info-label">${k}</div><div class="drawer-val">${v}</div></div>`).join('');
    const body = document.getElementById('drawerLedger');
    body.innerHTML = '<tr><td colspan="5">Loading…</td></tr>';
    try {
        const l = await api('/api/envelopes/ledger?envelope_id=' + e.id);
        const rows = l.entries.slice().reverse();
        body.innerHTML = rows.length === 0 ? '<tr><td colspan="5" class="import-more">Nothing yet.</td></tr>' : rows.map(r => `<tr>
            <td style="white-space:nowrap;">${formatDisplayDate(r.date)}</td>
            <td>${escapeHtml(r.label)}${r.note && r.kind === 'move' ? `<div class="import-file-sub">${escapeHtml(r.note)}</div>` : ''}</td>
            <td class="amount-cell ${r.amount >= 0 ? 'amount-income' : 'amount-spend'}">${formatSigned(r.amount)}</td>
            <td class="amount-cell">${m$(r.balance)}</td>
            <td style="white-space:nowrap;">${rowAction(r)}</td></tr>`).join('');
    } catch (err) {
        body.innerHTML = `<tr><td colspan="5">${escapeHtml(err.message)}</td></tr>`;
    }
}

// Fills from a paycheck can be edited (reopens the split screen); other fills and transfers can be undone
function rowAction(r) {
    if (r.move_type === 'transfer') return `<button type="button" class="btn-inline-action btn-inline-cancel" onclick="undoTransfer(${r.id})">Undo</button>`;
    if (r.move_type === 'fill' && r.income_txn_id) return `<button type="button" class="btn-inline-action btn-inline-save" onclick="editFillFromDrawer('${escapeHtml(r.income_txn_id)}')">Edit fill</button>`;
    if (r.move_type === 'fill' && r.allocation_id) return `<button type="button" class="btn-inline-action btn-inline-cancel" onclick="undoFillFromDrawer(${r.allocation_id})">Undo</button>`;
    return '';
}

function editFillFromDrawer(txnId) {
    closeEnvDrawer();
    openAllocModal(txnId);
}

async function undoFillFromDrawer(allocationId) {
    if (!confirm('Undo this fill? The money goes back to Unallocated.')) return;
    try {
        await apiPost('/api/allocations/void', { allocation_id: allocationId });
        await loadEnvelopesTab();
    } catch (err) { showToast(err.message, true); }
}

const KIND_OPTIONS_LABEL = { spending: 'Spending', bill: 'Bills', savings: 'Savings' };

async function undoTransfer(id) {
    if (!confirm('Undo this transfer?')) return;
    try {
        await apiPost('/api/moves/void', { id });
        await loadEnvelopesTab();
    } catch (err) { showToast(err.message, true); }
}
