/* Shared helpers: theme, tabs, toast, dates, API calls, and the "match to expense" picker. */

// ---------------------------------------------------------------------
// Shared data (filled in by analytics.js when transactions load)
// ---------------------------------------------------------------------
let peopleList = [];          // [{id, name}]
let incomeSourcesList = [];   // [{id, name, person_id}]

const KIND_LABELS = {
    expense: 'Expense',
    refund: 'Refund',
    paycheck: 'Paycheck',
    other_income: 'Other income',
    transfer: 'Transfer',
    cc_payment: 'Card payment'
};
const EXCLUDED_KINDS = ['transfer', 'cc_payment'];
const INCOME_KINDS = ['paycheck', 'other_income'];

function personName(id) {
    const p = peopleList.find(x => String(x.id) === String(id));
    return p ? p.name : '';
}

// ---------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------
let currentTheme = 'dark';
try { currentTheme = localStorage.getItem('budget_dashboard_theme') || 'dark'; } catch (e) { /* storage blocked */ }

function applyTheme(themeName) {
    if (!['dark', 'greenback', 'ledger'].includes(themeName)) {
        themeName = 'dark';
    }
    currentTheme = themeName;
    document.documentElement.setAttribute('data-theme', themeName);
    try { localStorage.setItem('budget_dashboard_theme', themeName); } catch (e) { /* storage blocked */ }

    ['dark', 'greenback', 'ledger'].forEach(t => {
        const card = document.getElementById('themeCard' + t.charAt(0).toUpperCase() + t.slice(1));
        if (card) card.classList.toggle('active', t === themeName);
    });

    document.dispatchEvent(new CustomEvent('themechange'));
}

function getThemeChartColors() {
    const isLedger = currentTheme === 'ledger';
    const isGreenback = currentTheme === 'greenback';
    return {
        tickColor: isLedger ? '#4a5b51' : (isGreenback ? '#86efac' : '#94a3b8'),
        gridColor: isLedger ? 'rgba(74, 91, 82, 0.15)' : (isGreenback ? 'rgba(134, 239, 172, 0.12)' : 'rgba(38, 51, 82, 0.4)'),
        gridSubtleColor: isLedger ? 'rgba(74, 91, 82, 0.1)' : (isGreenback ? 'rgba(134, 239, 172, 0.08)' : 'rgba(38, 51, 82, 0.2)'),
        borderColor: isLedger ? '#fdfcf7' : (isGreenback ? '#12291e' : '#151d30')
    };
}

function openSettingsModal() {
    applyTheme(currentTheme);
    document.getElementById('settingsModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    document.dispatchEvent(new CustomEvent('settingsopen'));
}

function closeSettingsModal() {
    document.getElementById('settingsModalOverlay').classList.remove('active');
    document.body.style.overflow = '';
}

function handleSettingsOverlayClick(event) {
    if (event.target === document.getElementById('settingsModalOverlay')) {
        closeSettingsModal();
    }
}

// ---------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------
function formatCurrency(val) {
    return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    }).format(val);
}

function formatSigned(val) {
    return (val < 0 ? '-' : '+') + formatCurrency(Math.abs(val));
}

// Dates are stored as YYYY-MM-DD. Parse as a local date (not UTC) so the day never shifts.
function parseTxnDate(dateStr) {
    if (!dateStr) return new Date(0);
    const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
    if (iso) return new Date(parseInt(iso[1], 10), parseInt(iso[2], 10) - 1, parseInt(iso[3], 10));
    const parts = dateStr.split('/');
    if (parts.length === 3) {
        return new Date(parseInt(parts[2], 10), parseInt(parts[0], 10) - 1, parseInt(parts[1], 10));
    }
    return new Date(dateStr);
}

// Shown to people as MM/DD/YYYY
function formatDisplayDate(isoStr) {
    const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoStr || '');
    return iso ? `${iso[2]}/${iso[3]}/${iso[1]}` : (isoStr || '');
}

function toInputDateFormat(dateObj) {
    const y = dateObj.getFullYear();
    const m = String(dateObj.getMonth() + 1).padStart(2, '0');
    const d = String(dateObj.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
}

function escapeHtml(text) {
    if (text === null || text === undefined || text === '') return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.toString().replace(/[&<>"']/g, m => map[m]);
}

// ---------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------
let toastTimer = null;
function showToast(message, isError = false) {
    const toast = document.getElementById('toastNotification');
    toast.textContent = message;
    toast.classList.toggle('error', isError);
    toast.classList.add('active');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('active'), isError ? 5000 : 3200);
}

// ---------------------------------------------------------------------
// API helpers (throw on failure so callers can show a message)
// ---------------------------------------------------------------------
async function api(path, options) {
    const res = await fetch(path, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
    if (!res.ok || data.success === false) {
        throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
}

function apiPost(path, body) {
    return api(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
}

// ---------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------
const TABS = ['envelopes', 'analytics', 'income', 'budget', 'import'];
let activeTab = 'envelopes';

function showTab(name) {
    if (!TABS.includes(name)) name = 'envelopes';
    activeTab = name;
    TABS.forEach(t => {
        const panel = document.getElementById('tab-' + t);
        const btn = document.getElementById('tabBtn-' + t);
        if (panel) panel.hidden = (t !== name);
        if (btn) btn.classList.toggle('active', t === name);
    });
    if (location.hash !== '#' + name) {
        history.replaceState(null, '', '#' + name);
    }
    document.dispatchEvent(new CustomEvent('tabchange', { detail: { tab: name } }));
}

function setupTabs() {
    window.addEventListener('hashchange', () => {
        const name = location.hash.replace('#', '');
        if (TABS.includes(name) && name !== activeTab) showTab(name);
    });
    const initial = location.hash.replace('#', '');
    showTab(TABS.includes(initial) ? initial : 'envelopes');
}

// ---------------------------------------------------------------------
// Match a reimbursement (e.g. Venmo) to the expense it pays back
// ---------------------------------------------------------------------
let matchModalCallback = null;

async function openMatchModal(opts, onPick) {
    // opts: { txnId?, date, amount, label }
    matchModalCallback = onPick;
    document.getElementById('matchModalInfo').textContent =
        `${opts.label || 'Reimbursement'} ${formatCurrency(Math.abs(opts.amount))} on ${formatDisplayDate(opts.date)}`;
    const list = document.getElementById('matchModalList');
    list.innerHTML = '<div class="match-empty">Looking for expenses&hellip;</div>';
    document.getElementById('matchModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';

    try {
        const query = opts.txnId
            ? `txn_id=${encodeURIComponent(opts.txnId)}`
            : `date=${encodeURIComponent(opts.date)}&amount=${encodeURIComponent(opts.amount)}`;
        const data = await api('/api/transactions/reimbursement-candidates?' + query);
        window._matchCandidates = data.candidates;
        if (data.candidates.length === 0) {
            list.innerHTML = '<div class="match-empty">No expenses from the 60 days before this date are large enough. Close this and pick an envelope directly.</div>';
            return;
        }
        list.innerHTML = data.candidates.map((c, i) => `
            <button type="button" class="match-item" onclick="pickMatchCandidate(${i})">
                <span class="match-main">
                    <span class="match-name">${escapeHtml(c.name)}</span>
                    <span class="match-sub">${formatDisplayDate(c.date)} &bull; ${escapeHtml(c.envelope || 'Unallocated')} &bull; ${c.days_ago} day${c.days_ago === 1 ? '' : 's'} earlier${c.already_reimbursed ? ` &bull; ${formatCurrency(c.already_reimbursed)} already paid back` : ''}</span>
                </span>
                <span class="match-amt">${formatCurrency(Math.abs(c.amount))}</span>
                ${c.likely ? `<span class="match-tag">Looks like ${escapeHtml(c.split)}</span>` : ''}
            </button>`).join('');
    } catch (err) {
        list.innerHTML = `<div class="match-empty">Could not load expenses: ${escapeHtml(err.message)}</div>`;
    }
}

function pickMatchCandidate(index) {
    const cand = (window._matchCandidates || [])[index];
    const cb = matchModalCallback;
    closeMatchModal();
    if (cand && cb) cb(cand);
}

function closeMatchModal() {
    document.getElementById('matchModalOverlay').classList.remove('active');
    matchModalCallback = null;
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

function handleMatchOverlayClick(event) {
    if (event.target === document.getElementById('matchModalOverlay')) closeMatchModal();
}

// ---------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    applyTheme(currentTheme);
    setupTabs();
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeMatchModal();
    });
});
