/* Setup walkthrough: a floating guide that takes a new user from an empty budget to a working one, one step at a time.
   Each step opens the right tab, rings the control to use, and shows a checkmark once the step is done.
   Progress is saved per budget on the server, so closing the app and coming back resumes at the same step. */

// ---------------------------------------------------------------------
// Steps. Text is our own HTML; anything that comes from data goes through escapeHtml.
// ---------------------------------------------------------------------
const wtVisible = (id) => { const el = document.getElementById(id); return !!el && !el.hidden; };
const wtPlural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const wtMonth = (iso) => {
    const m = /^(\d{4})-(\d{2})/.exec(iso || '');
    return m ? new Date(+m[1], +m[2] - 1, 1).toLocaleString('en-US', { month: 'short', year: 'numeric' }) : '';
};

const WT_STEPS = [
    {
        id: 'import',
        tab: 'import',
        title: 'Bring in your bank history',
        body: `On your bank's website, download the last <strong>6 to 12 months</strong> from your checking account
               <strong>and every credit card</strong> as CSV files (look for "Download" or "Export"). Drop them in the box,
               then click <strong>Process &amp; review</strong>.`,
        target: () => wtVisible('importFileListWrap') ? '#btnImportProcess' : '#importDropzone',
        done: s => s.transactions > 0 || wtVisible('importReviewSection'),
        doneText: s => s.transactions > 0 ? `${wtPlural(s.transactions, 'transaction')} saved` : 'Files read. Next, check them over.',
        skipText: 'Without history, EVBudget can\'t suggest budgets or find your bills and paychecks. You\'ll type everything by hand.',
        hints: { mappingModalOverlay: 'Tell EVBudget which column holds the date, the description, and the amount. It remembers this for the next file from the same bank.' },
    },
    {
        id: 'review',
        tab: 'import',
        title: 'Check the envelopes and save',
        body: `Each row shows the envelope EVBudget guessed. Fix any that look wrong; your fixes teach it for next time.
               Transfers and card payments are left out so they aren't counted twice. When it looks right, click
               <strong>Save to database</strong>.`,
        target: () => wtVisible('importReviewSection') ? '#btnImportCommit' : '#importDropzone',
        done: s => s.transactions > 0,
        doneText: s => `${wtPlural(s.transactions, 'transaction')}, ${wtMonth(s.first_date)} to ${wtMonth(s.last_date)}`,
        skipText: 'Nothing is saved until you click Save to database.',
    },
    {
        id: 'pay_sources',
        tab: 'income',
        title: 'Where your paychecks come from',
        body: () => wtVisible('payDiscoverCard')
            ? `We found deposits that look like paychecks under <strong>Deposits we noticed</strong>. Click
               <strong>Make pay source</strong> on each real paycheck, pick who it's for (or <em>+ New person</em>), and save.
               Dismiss anything that isn't pay.`
            : `Click <strong>+ Add pay source</strong>. Copy a few words from a paycheck's bank description, like
               "ACME PAYROLL", into <em>Text in the bank description</em>. The preview lists the deposits that match.`,
        target: () => wtVisible('payDiscoverCard') ? '#payDiscoverCard' : '#btnAddSource',
        done: s => s.income_sources > 0,
        doneText: s => `${wtPlural(s.income_sources, 'pay source')} for ${wtPlural(s.people, 'person').replace('persons', 'people')}`,
        skipText: 'Paychecks won\'t be recognized, and the Budget tab won\'t know how much you have to plan around.',
        hints: { sourceModalOverlay: 'Pick who gets this pay, or choose <em>+ New person</em>. Leave <em>Also tag matching deposits</em> ticked so past paychecks count too.' },
    },
    {
        id: 'envelopes',
        tab: 'budget',
        title: 'Your envelopes',
        body: `These are starter envelopes. Use <strong>Manage envelopes</strong> to rename, add, or remove them, and
               the <strong>Type</strong> column to say what each one is for:<br>
               <strong>Spending</strong>: groceries, gas, eating out. A fresh amount each month.<br>
               <strong>Bills</strong>: things with a due date, like insurance. Money builds up until it's due.<br>
               <strong>Goal</strong>: something to save toward, like a vacation.`,
        target: '#btnBudgetManageEnvelopes',
        done: () => true,
        doneText: s => `${s.envelopes.spending} spending, ${s.envelopes.bill} bills, ${wtPlural(s.envelopes.goal, 'goal')}`,
        hints: { envelopeModalOverlay: 'Add the envelopes you want and delete the ones you don\'t. Close this when you\'re done, then set each type in the table.' },
    },
    {
        id: 'budgets',
        tab: 'budget',
        title: 'Give each envelope a monthly amount',
        body: `Suggestions come from what you actually spent recently. Click <strong>Accept all high-confidence</strong>,
               then type an amount for the rest. Watch <strong>Left over</strong> at the top: it should stay at zero or more.`,
        target: '#btnAcceptAll',
        done: s => s.spending_without_budget === 0,
        doneText: () => 'Every spending envelope has a monthly amount',
        notDoneText: s => `${wtPlural(s.spending_without_budget, 'spending envelope')} still at $0`,
        skipText: 'Envelopes with no amount won\'t get money when you split a paycheck.',
    },
    {
        id: 'bills',
        tab: 'budget',
        title: 'Add your bills',
        body: () => (wtVisible('discoverCard')
            ? 'Start with <strong>Bills we noticed</strong>: add the ones that are real bills. '
            : '')
            + 'For each Bills envelope, click its <strong>Bills</strong> button and add what\'s due, how much, and how often. '
            + 'EVBudget then knows how much to set aside each month.',
        target: () => wtVisible('discoverCard') ? '#discoverCard' : '#budgetBody',
        done: s => s.bill_envelopes_without_bills === 0,
        doneText: s => s.envelopes.bill ? 'Every bill envelope has a bill' : 'No bill envelopes',
        notDoneText: s => `${wtPlural(s.bill_envelopes_without_bills, 'bill envelope')} without a bill yet`,
        skipText: 'Bill envelopes won\'t know how much to save or when it\'s due.',
        hints: { billsModalOverlay: 'Pick a past payment under the history to fill this in, or type the amount, the next due date, and how often it repeats.' },
    },
    {
        id: 'templates',
        tab: 'budget',
        title: 'Decide how each paycheck is split',
        body: `Under <strong>Fill templates</strong>, click <strong>Generate</strong> for each pay source. It splits
               a typical paycheck across your envelopes based on their monthly amounts. Adjust it, then <strong>Save</strong>.
               Each new paycheck starts with this split.`,
        target: () => { const w = document.getElementById('templatesWrap'); return w ? w.closest('section') : null; },
        done: s => s.income_sources > 0 && s.sources_without_template === 0,
        doneText: () => 'Every pay source has a split',
        notDoneText: s => s.income_sources === 0 ? 'Add a pay source first (step 3)' : `${wtPlural(s.sources_without_template, 'pay source')} without a split`,
        skipText: 'Each paycheck will start with an empty split that you fill by hand.',
    },
    {
        id: 'go_live',
        tab: 'budget',
        title: 'Start tracking',
        body: s => s.go_live_date
            ? 'Envelope balances are already tracking. You only need <strong>Re-run setup</strong> if you want to start over.'
            : `Click <strong>Set up go-live</strong>. Today is filled in. Decide how much to put in envelopes: your
               <strong>checking balance, minus what you owe on credit cards</strong> (that money is already spent).
               Fill envelopes with bills due soon first; leave the rest in Unallocated.`,
        target: '#btnGoLive',
        done: s => !!s.go_live_date,
        doneText: s => `Balances start ${formatDisplayDate(s.go_live_date)}`,
        skipText: 'Envelope balances stay off until this is done.',
        hints: { goLiveModalOverlay: 'Type what each envelope holds today. The total at the bottom should match the money you have to budget.' },
    },
    {
        id: 'routine',
        tab: 'envelopes',
        title: () => wtState.skipped.length ? 'Almost there. Here\'s the routine' : 'You\'re set up. Here\'s the routine',
        body: `<ol class="wt-list">
                 <li><strong>Import</strong> new bank files every week or two.</li>
                 <li>When a paycheck shows up under <strong>Deposits to fill</strong>, click it. Your split is filled in; confirm it.</li>
                 <li>If an envelope goes below zero, use <strong>Move money</strong> to cover it from another one.</li>
               </ol>
               Run this again any time from <strong>Help</strong> in the top bar.`,
        target: () => {
            if (wtVisible('envNotLive')) return '#envNotLive';
            const q = document.getElementById('envQueueCount');
            return q ? q.closest('section') : null;
        },
        done: () => true,
        doneText: () => 'Setup complete',
        last: true,
    },
];

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
let wtState = { status: 'not_started', step: null, skipped: [] };
let wtStatus = null;
let wtIndex = -1;            // -1 is the welcome dialog
let wtOpen = false;          // the panel (or its pill) is showing
let wtMinimized = false;
let wtConfirm = null;        // 'skip' | 'stop' | null
let wtTimer = null;
let wtScrolledFor = null;    // the step whose target was already scrolled into view
let wtDrag = null;
let wtOverDialog = false;    // the user opened the panel on top of a dialog it would otherwise step aside for

const WT_POS_KEY = 'evbudget_walkthrough_pos';
const WT_MIN_KEY = 'evbudget_walkthrough_min';

function wtStore(key, value) {
    try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
}
function wtLoad(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
}

async function wtFetch() {
    const r = await api('/api/walkthrough');
    wtStatus = r.status;
    return r;
}

function wtSave(status) {
    wtState.status = status || wtState.status;
    wtState.step = wtIndex >= 0 ? WT_STEPS[wtIndex].id : null;
    apiPost('/api/walkthrough', { status: wtState.status, step: wtState.step, skipped: wtState.skipped })
        .catch(err => showToast('Could not save setup progress: ' + err.message, true));
}

function wtStepIndex(id) {
    const i = WT_STEPS.findIndex(s => s.id === id);
    return i < 0 ? 0 : i;
}

const wtVal = (v, s) => (typeof v === 'function' ? v(s) : v);

// ---------------------------------------------------------------------
// Starting, moving, stopping
// ---------------------------------------------------------------------
async function wtStart(fromBeginning) {
    try {
        await wtFetch();
    } catch (err) {
        showToast('Could not start setup: ' + err.message, true);
        return;
    }
    if (fromBeginning || !wtState.step) {
        wtState.skipped = [];
        wtShowWelcome();
    } else {
        wtGo(wtStepIndex(wtState.step));
    }
}

function wtShowWelcome() {
    wtHidePanel();
    wtIndex = -1;
    document.getElementById('wtWelcome').hidden = false;
    document.getElementById('wtWelcomeStart').focus();
}

function wtWelcomeChoice(choice) {
    document.getElementById('wtWelcome').hidden = true;
    if (choice === 'dismiss') {
        wtIndex = -1;
        wtSave('dismissed');
        showToast('Setup hidden. You can start it any time from Help.');
        return;
    }
    wtMinimized = choice === 'later';
    wtGo(0, wtMinimized);
}

// stayOnTab: show the step without switching tabs (resuming minimized, or "Not now")
function wtGo(index, stayOnTab) {
    wtIndex = Math.max(0, Math.min(WT_STEPS.length - 1, index));
    wtConfirm = null;
    wtScrolledFor = null;
    const step = WT_STEPS[wtIndex];
    if (!stayOnTab && step.tab && typeof showTab === 'function' && activeTab !== step.tab) showTab(step.tab);
    wtOpen = true;
    wtSave('active');
    wtRender();
    wtStartTimer();
    // Tabs load their data on open, so the target may appear a moment later
    setTimeout(wtRefresh, 400);
}

function wtNext() {
    const step = WT_STEPS[wtIndex];
    if (!wtIsDone(step) && wtConfirm !== 'skip') {
        wtConfirm = 'skip';
        wtRender();
        return;
    }
    if (wtConfirm === 'skip' && !wtState.skipped.includes(step.id)) wtState.skipped.push(step.id);
    if (wtIsDone(step)) wtState.skipped = wtState.skipped.filter(id => id !== step.id);
    if (step.last) { wtFinish(); return; }
    wtGo(wtIndex + 1);
}

function wtBack() {
    if (wtIndex === 0) { wtShowWelcome(); return; }
    wtGo(wtIndex - 1);
}

function wtAskStop() {
    wtConfirm = 'stop';
    wtMinimized = false;
    wtRender();
}

function wtStop() {
    wtSave('paused');
    wtHidePanel();
    showToast('Setup paused. Pick up where you left off from Help.');
}

function wtFinish() {
    wtSave('done');
    wtHidePanel();
    showToast('Setup complete. Nice work!');
}

function wtHidePanel() {
    wtOpen = false;
    wtConfirm = null;
    clearInterval(wtTimer);
    document.getElementById('wtPanel').hidden = true;
    document.getElementById('wtPill').hidden = true;
    document.getElementById('wtRing').hidden = true;
}

function wtPillClick() {
    if (wtActiveModal() && !wtMinimized) { wtOverDialog = true; wtRender(); return; }
    wtSetMinimized(false);
}

function wtSetMinimized(min) {
    wtMinimized = min;
    wtStore(WT_MIN_KEY, min ? '1' : null);
    // Opening the panel again goes back to the step's tab
    const step = WT_STEPS[wtIndex];
    if (!min && step && step.tab && activeTab !== step.tab) { showTab(step.tab); wtScrolledFor = null; }
    wtRender();
}

// ---------------------------------------------------------------------
// Checking progress
// ---------------------------------------------------------------------
function wtIsDone(step) {
    if (!wtStatus) return false;
    try { return !!step.done(wtStatus); } catch (e) { return false; }
}

async function wtRefresh() {
    if (!wtOpen) return;
    const before = wtIndex >= 0 && wtIsDone(WT_STEPS[wtIndex]);
    try { await wtFetch(); } catch (err) { /* keep the last status */ }
    if (!wtOpen) return;
    const after = wtIndex >= 0 && wtIsDone(WT_STEPS[wtIndex]);
    if (after && wtConfirm === 'skip') wtConfirm = null;
    wtRender(!before && after);
}

function wtStartTimer() {
    clearInterval(wtTimer);
    wtTimer = setInterval(() => {
        if (wtOpen && !document.hidden) wtRefresh();
    }, 4000);
}

// ---------------------------------------------------------------------
// Drawing the panel
// ---------------------------------------------------------------------
function wtActiveModal() {
    const all = document.querySelectorAll('.modal-overlay.active');
    return all.length ? all[all.length - 1] : null;
}

function wtRender(justDone) {
    if (!wtOpen || wtIndex < 0) return;
    const step = WT_STEPS[wtIndex];
    const s = wtStatus || {};
    const done = wtIsDone(step);
    const panel = document.getElementById('wtPanel');
    const pill = document.getElementById('wtPill');
    const total = WT_STEPS.length;

    // A dialog with no room beside it: shrink to the pill so the dialog's buttons stay reachable
    const modal = wtActiveModal();
    if (!modal) wtOverDialog = false;
    const crowded = !!modal && !wtBesideSpot(modal) && !wtOverDialog;
    const hint = modal && step.hints ? step.hints[modal.id] : null;
    const min = wtMinimized || crowded;
    pill.textContent = `Setup ${wtIndex + 1}/${total}${done ? ' ✓' : ''}${crowded && hint ? ' · tip' : ''}`;
    pill.title = crowded && hint ? hint.replace(/<[^>]+>/g, '') : 'Show setup steps';
    pill.hidden = !min;
    panel.hidden = min;

    document.getElementById('wtProgress').textContent = `Setup · step ${wtIndex + 1} of ${total}`;
    document.getElementById('wtBar').style.width = `${Math.round(((wtIndex + (done ? 1 : 0)) / total) * 100)}%`;
    document.getElementById('wtTitle').textContent = wtVal(step.title, s);

    let body = wtVal(step.body, s);
    if (step.last && wtState.skipped.length) {
        const links = wtState.skipped.map(id => {
            const i = wtStepIndex(id);
            return `<li>${escapeHtml(wtVal(WT_STEPS[i].title, s))} <button type="button" class="wt-link" onclick="wtGo(${i})">Go do it</button></li>`;
        }).join('');
        body += `<div class="wt-skipped"><strong>You skipped:</strong><ul class="wt-list">${links}</ul></div>`;
    }
    document.getElementById('wtText').innerHTML = body;

    const hintEl = document.getElementById('wtHint');
    hintEl.hidden = !hint;
    hintEl.innerHTML = hint || '';

    const statusEl = document.getElementById('wtStatus');
    if (!wtStatus) {
        statusEl.className = 'wt-status';
        statusEl.textContent = 'Checking…';
    } else if (done) {
        statusEl.className = 'wt-status done';
        statusEl.textContent = '✓ ' + wtVal(step.doneText, s);
    } else {
        statusEl.className = 'wt-status';
        statusEl.textContent = step.notDoneText ? wtVal(step.notDoneText, s) : 'Not done yet';
    }

    const confirm = document.getElementById('wtConfirm');
    confirm.hidden = !wtConfirm;
    if (wtConfirm === 'skip') {
        confirm.innerHTML = `<div><strong>Skip this for now?</strong> ${escapeHtml(step.skipText || '')}</div>
            <div class="wt-confirm-actions"><button type="button" class="btn-modal-cancel" onclick="wtConfirm = null; wtRender();">Stay here</button>
            <button type="button" class="btn-modal-save" onclick="wtNext()">Skip</button></div>`;
    } else if (wtConfirm === 'stop') {
        confirm.innerHTML = `<div><strong>Stop setup?</strong> You can pick up where you left off from Help in the top bar.</div>
            <div class="wt-confirm-actions"><button type="button" class="btn-modal-cancel" onclick="wtConfirm = null; wtRender();">Keep going</button>
            <button type="button" class="btn-modal-save" onclick="wtStop()">Stop</button></div>`;
    }

    const next = document.getElementById('wtNext');
    next.textContent = step.last ? 'Finish' : 'Next';
    next.hidden = !!wtConfirm;
    document.getElementById('wtBack').hidden = !!wtConfirm;
    if (justDone) {
        next.classList.remove('wt-pulse');
        void next.offsetWidth;   // restart the animation
        next.classList.add('wt-pulse');
    }

    wtPlacePanel();
    wtPlaceRing();
}

// ---------------------------------------------------------------------
// Ring around the control to use
// ---------------------------------------------------------------------
function wtTarget() {
    if (wtIndex < 0) return null;
    let t = wtVal(WT_STEPS[wtIndex].target, wtStatus || {});
    if (typeof t === 'string') t = document.querySelector(t);
    if (!t || !t.getClientRects().length) return null;
    return t;
}

function wtPlaceRing() {
    const ring = document.getElementById('wtRing');
    const t = wtOpen ? wtTarget() : null;
    const modal = wtActiveModal();
    if (!t || (modal && !modal.contains(t))) { ring.hidden = true; return; }
    if (wtScrolledFor !== wtIndex) {
        wtScrolledFor = wtIndex;
        t.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    const r = t.getBoundingClientRect();
    const pad = 4;
    ring.style.left = `${r.left - pad}px`;
    ring.style.top = `${r.top - pad}px`;
    ring.style.width = `${r.width + pad * 2}px`;
    ring.style.height = `${r.height + pad * 2}px`;
    ring.hidden = false;
}

let wtRingFrame = 0;
function wtQueueRing() {
    if (!wtOpen || wtRingFrame) return;
    wtRingFrame = requestAnimationFrame(() => { wtRingFrame = 0; wtPlaceRing(); });
}

// ---------------------------------------------------------------------
// Where the panel sits: the user's spot, or beside an open dialog so it doesn't cover the dialog's buttons
// ---------------------------------------------------------------------
const WT_NARROW = 700;
const WT_PANEL_ROOM = 340 + 24;   // panel width plus a gap on each side

// Where the panel can sit beside an open dialog without covering it, or null when there's no room
function wtBesideSpot(modal) {
    const box = modal.querySelector('.modal-container');
    if (!box || window.innerWidth <= WT_NARROW) return null;
    const r = box.getBoundingClientRect();
    const top = Math.max(12, r.top);
    if (window.innerWidth - r.right >= WT_PANEL_ROOM) return { left: r.right + 12, top };
    if (r.left >= WT_PANEL_ROOM) return { left: r.left - WT_PANEL_ROOM + 12, top };
    return null;
}

function wtPlacePanel() {
    const panel = document.getElementById('wtPanel');
    if (panel.hidden) return;
    panel.classList.remove('wt-beside');
    if (window.innerWidth <= WT_NARROW) {
        panel.style.left = panel.style.top = '';
        return;
    }
    const modal = wtActiveModal();
    if (modal) {
        const spot = wtBesideSpot(modal);
        if (spot) {
            panel.style.left = `${spot.left}px`;
            panel.style.top = `${spot.top}px`;
            panel.classList.add('wt-beside');
        } else {
            panel.style.left = panel.style.top = '';   // opened over the dialog on purpose: bottom corner
        }
        return;
    }
    const saved = wtLoad(WT_POS_KEY);
    if (saved) {
        try {
            const p = JSON.parse(saved);
            const x = Math.min(Math.max(8, p.left), window.innerWidth - panel.offsetWidth - 8);
            const y = Math.min(Math.max(8, p.top), window.innerHeight - panel.offsetHeight - 8);
            panel.style.left = `${x}px`;
            panel.style.top = `${y}px`;
            return;
        } catch (e) { /* ignore a bad saved position */ }
    }
    panel.style.left = panel.style.top = '';
}

function wtDragStart(e) {
    if (e.button !== 0 || e.target.closest('button') || window.innerWidth <= WT_NARROW) return;
    const panel = document.getElementById('wtPanel');
    const r = panel.getBoundingClientRect();
    wtDrag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    panel.setPointerCapture(e.pointerId);
    e.preventDefault();
}

function wtDragMove(e) {
    if (!wtDrag) return;
    const panel = document.getElementById('wtPanel');
    const x = Math.min(Math.max(8, e.clientX - wtDrag.dx), window.innerWidth - panel.offsetWidth - 8);
    const y = Math.min(Math.max(8, e.clientY - wtDrag.dy), window.innerHeight - panel.offsetHeight - 8);
    panel.style.left = `${x}px`;
    panel.style.top = `${y}px`;
}

function wtDragEnd() {
    if (!wtDrag) return;
    wtDrag = null;
    const panel = document.getElementById('wtPanel');
    if (!wtActiveModal()) {
        wtStore(WT_POS_KEY, JSON.stringify({ left: parseFloat(panel.style.left), top: parseFloat(panel.style.top) }));
    }
}

// ---------------------------------------------------------------------
// Help menu in the header
// ---------------------------------------------------------------------
function toggleWalkthroughMenu(e) {
    e.stopPropagation();
    const menu = document.getElementById('wtHelpMenu');
    const open = menu.hidden;
    const resume = document.getElementById('wtMenuResume');
    const canResume = wtState.step && wtState.status !== 'done';
    resume.hidden = !canResume;
    if (canResume) resume.textContent = `Resume setup (step ${wtStepIndex(wtState.step) + 1} of ${WT_STEPS.length})`;
    menu.hidden = !open;
    document.getElementById('btnHelp').setAttribute('aria-expanded', String(open));
}

function wtCloseMenu() {
    const menu = document.getElementById('wtHelpMenu');
    if (menu && !menu.hidden) {
        menu.hidden = true;
        document.getElementById('btnHelp').setAttribute('aria-expanded', 'false');
    }
}

function wtMenuPick(choice) {
    wtCloseMenu();
    wtStart(choice === 'restart');
}

// ---------------------------------------------------------------------
// Building the DOM and wiring events
// ---------------------------------------------------------------------
function wtBuild() {
    document.body.insertAdjacentHTML('beforeend', `
        <div class="wt-ring" id="wtRing" hidden></div>
        <button type="button" class="wt-pill" id="wtPill" hidden onclick="wtPillClick()" title="Show setup steps"></button>
        <div class="wt-panel" id="wtPanel" role="dialog" aria-modal="false" aria-labelledby="wtTitle" hidden>
            <div class="wt-head" id="wtHead">
                <span class="wt-progress" id="wtProgress"></span>
                <button type="button" class="wt-icon-btn" onclick="wtSetMinimized(true)" title="Minimize" aria-label="Minimize">&ndash;</button>
                <button type="button" class="wt-icon-btn" onclick="wtAskStop()" title="Stop setup" aria-label="Stop setup">&times;</button>
            </div>
            <div class="wt-track"><div class="wt-bar" id="wtBar"></div></div>
            <div class="wt-body" aria-live="polite">
                <div class="wt-title" id="wtTitle"></div>
                <div class="wt-text" id="wtText"></div>
                <div class="wt-hint" id="wtHint" hidden></div>
                <div class="wt-status" id="wtStatus"></div>
                <div class="wt-confirm" id="wtConfirm" hidden></div>
            </div>
            <div class="wt-foot">
                <button type="button" class="btn-modal-cancel" id="wtBack" onclick="wtBack()">Back</button>
                <button type="button" class="btn-modal-save" id="wtNext" onclick="wtNext()">Next</button>
            </div>
        </div>
        <div class="wt-welcome" id="wtWelcome" hidden>
            <div class="wt-welcome-box" role="dialog" aria-modal="true" aria-labelledby="wtWelcomeTitle">
                <div class="wt-welcome-title" id="wtWelcomeTitle">Every dollar gets a job</div>
                <p>When money comes in, you split it into envelopes like Groceries, Gas, and Mortgage.
                   When you spend, it comes out of that envelope, so you always know what's left.</p>
                <p class="wt-welcome-sub">Setup takes about 15 minutes. You'll need CSV exports from your checking account and
                   credit cards covering the last 6 to 12 months.</p>
                <div class="wt-welcome-actions">
                    <button type="button" class="btn-modal-cancel" onclick="wtWelcomeChoice('later')">Not now</button>
                    <button type="button" class="btn-modal-save" id="wtWelcomeStart" onclick="wtWelcomeChoice('start')">Start setup</button>
                </div>
                <button type="button" class="wt-link wt-welcome-hide" onclick="wtWelcomeChoice('dismiss')">Don't show this again</button>
            </div>
        </div>`);

    const head = document.getElementById('wtHead');
    head.addEventListener('pointerdown', wtDragStart);
    head.addEventListener('pointermove', wtDragMove);
    head.addEventListener('pointerup', wtDragEnd);
    head.addEventListener('pointercancel', wtDragEnd);

    // Dialogs opening and closing: show the matching hint, move beside the dialog, and re-check progress on close
    const watcher = new MutationObserver(() => { if (wtOpen) { wtRender(); wtRefresh(); } });
    document.querySelectorAll('.modal-overlay').forEach(o => watcher.observe(o, { attributes: true, attributeFilter: ['class'] }));
    // Cards that appear and disappear (import review, "noticed" lists) change which control to point at
    ['importReviewSection', 'importFileListWrap', 'payDiscoverCard', 'discoverCard', 'envNotLive'].forEach(id => {
        const el = document.getElementById(id);
        if (el) watcher.observe(el, { attributes: true, attributeFilter: ['hidden'] });
    });

    window.addEventListener('scroll', wtQueueRing, true);
    window.addEventListener('resize', () => { if (wtOpen) wtRender(); });
    window.addEventListener('focus', () => { if (wtOpen) wtRefresh(); });
    document.addEventListener('tabchange', () => { if (wtOpen) setTimeout(wtRefresh, 300); });
    document.addEventListener('click', (e) => { if (!e.target.closest('.wt-help-wrap')) wtCloseMenu(); });
    // Capture phase, so this sees whether a dialog was open before the dialogs' own Escape handlers close it
    window.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        wtCloseMenu();
        if (!document.getElementById('wtWelcome').hidden) { wtWelcomeChoice('later'); return; }
        if (wtOpen && !wtMinimized && !wtActiveModal()) wtSetMinimized(true);
    }, true);
}

async function wtInit() {
    wtBuild();
    let r;
    try {
        r = await wtFetch();
    } catch (err) {
        return;   // no budget open yet, or the server is gone: the Help button still works later
    }
    wtState = r.state;
    if (r.auto_start) {
        wtShowWelcome();
    } else if (wtState.status === 'active' && wtState.step) {
        wtMinimized = wtLoad(WT_MIN_KEY) === '1';
        wtGo(wtStepIndex(wtState.step), wtMinimized);
    }
}

document.addEventListener('DOMContentLoaded', wtInit);
