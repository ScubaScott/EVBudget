/* Updates: an "Update available" button at the top when the git repository has a newer version,
   a dialog that applies it, and an in-place restart (this window reloads itself into the new server).
   The server checks git in the background; the page just reads the result. */

let updState = null;
let updPollTimer = null;
const UPD_RECHECK_MS = 10 * 60 * 1000;

async function updRefresh() {
    try { updState = await api('/api/update/status'); } catch (err) { return null; }
    updShowChip();
    return updState;
}

function updShowChip() {
    const chip = document.getElementById('btnUpdate');
    if (!chip || !updState) return;
    const s = updState.state;
    chip.hidden = !(s === 'available' || s === 'restart');
    chip.textContent = s === 'restart' ? 'Restart to finish update' : 'Update available';
    chip.title = s === 'available' && updState.latest_version ? `Version ${updState.latest_version} is ready` : '';
    chip.classList.toggle('update-chip-restart', s === 'restart');
}

// ---------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------
function updSet(title, bodyHtml, buttonsHtml) {
    document.getElementById('updTitle').textContent = title;
    document.getElementById('updBody').innerHTML = bodyHtml;
    document.getElementById('updActions').innerHTML = buttonsHtml;
    document.getElementById('updateOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeUpdateDialog() {
    if (updBusy) return;
    document.getElementById('updateOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

let updBusy = false;

function openUpdateDialog() {
    const st = updState || {};
    if (st.state === 'restart') { updShowRestart(); return; }
    const version = st.latest_version ? `Version <strong>${escapeHtml(st.latest_version)}</strong>` : 'A new version';
    const changes = (st.changes && st.changes.length)
        ? `<p>Here is what's in it:</p><div class="rel-scroll">${relEntriesHtml(st.changes, false)}</div>`
        : (st.latest_title ? `<p><em>${escapeHtml(st.latest_title)}</em></p>` : '');
    updSet('Update available',
        `<p>${version} is ready to install.</p>${changes}
         <p class="import-hint">Your budgets and data aren't touched. EVBudget will restart to finish, and this window reloads by itself.</p>`,
        `<button type="button" class="btn-modal-cancel" onclick="closeUpdateDialog()">Later</button>
         <button type="button" class="btn-modal-save" onclick="applyUpdate()">Update now</button>`);
}

async function applyUpdate() {
    updBusy = true;
    updSet('Updating…', '<p>Getting the new version. This usually takes a few seconds.</p>', '');
    try {
        const res = await apiPost('/api/update/apply');
        updBusy = false;
        await updRefresh();
        if (!res.updated) { updSet('Already up to date', '<p>You have the latest version.</p>', '<button type="button" class="btn-modal-save" onclick="closeUpdateDialog()">OK</button>'); return; }
        const pkg = res.packages === 'failed'
            ? '<p class="import-hint">Some add-on packages couldn\'t be installed. Everything else will work; PDF import may need a restart through the launch shortcut.</p>' : '';
        updShowRestart(res.version, pkg);
    } catch (err) {
        updBusy = false;
        updSet('Couldn\'t update', `<p>${escapeHtml(err.message)}</p>`, '<button type="button" class="btn-modal-save" onclick="closeUpdateDialog()">OK</button>');
    }
}

function updShowRestart(version, extraHtml) {
    updSet('Update installed',
        `<p>${version ? `Version <strong>${escapeHtml(version)}</strong> is installed. ` : ''}Restart EVBudget to start using it. This window will reload by itself.</p>${extraHtml || ''}`,
        `<button type="button" class="btn-modal-cancel" onclick="closeUpdateDialog()">Restart later</button>
         <button type="button" class="btn-modal-save" onclick="restartApp()">Restart now</button>`);
}

// ---------------------------------------------------------------------
// Restart: the new server takes over the same address, then this window reloads into it
// ---------------------------------------------------------------------
async function restartApp() {
    updBusy = true;
    let before = null;
    try { before = (await api('/api/health')).boot; } catch (err) { /* compare against nothing */ }
    clearInterval(lifecycleTimer);   // the old server is about to go away; don't let the heartbeat show an error
    updSet('Restarting…', '<p>EVBudget is restarting. This window reloads when it\'s back.</p>', '');
    try { await apiPost('/api/update/restart'); } catch (err) {
        updBusy = false;
        lifecycleTimer = setInterval(lifecycleBeat, 10000);
        updSet('Couldn\'t restart', `<p>${escapeHtml(err.message)}</p><p class="import-hint">Close EVBudget and open it again from its shortcut to finish the update.</p>`,
            '<button type="button" class="btn-modal-save" onclick="closeUpdateDialog()">OK</button>');
        return;
    }
    const started = Date.now();
    const wait = async () => {
        try {
            const h = await (await fetch('/api/health', { cache: 'no-store' })).json();
            if (h.boot && h.boot !== before) { location.href = '/'; return; }
        } catch (err) { /* the server is between copies */ }
        if (Date.now() - started > 60000) {
            updBusy = false;
            updSet('Restart didn\'t finish', '<p>EVBudget didn\'t come back on its own.</p><p class="import-hint">Close this window and open EVBudget again from its shortcut.</p>',
                '<button type="button" class="btn-modal-save" onclick="closeUpdateDialog()">OK</button>');
            return;
        }
        setTimeout(wait, 700);
    };
    setTimeout(wait, 1500);
}

// ---------------------------------------------------------------------
// Help > Check for updates
// ---------------------------------------------------------------------
async function checkForUpdates() {
    showToast('Checking for updates…');
    try { await apiPost('/api/update/check'); } catch (err) { showToast(err.message, true); return; }
    const started = Date.now();
    clearInterval(updPollTimer);
    updPollTimer = setInterval(async () => {
        const st = await updRefresh();
        if (st && st.checking && Date.now() - started < 30000) return;
        clearInterval(updPollTimer);
        if (!st) { showToast('Could not check for updates.', true); return; }
        if (st.state === 'available' || st.state === 'restart') openUpdateDialog();
        else if (st.state === 'current') showToast('You\'re up to date.');
        else showToast(st.message || 'Could not check for updates.', true);
    }, 700);
}

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeUpdateDialog(); });
    setTimeout(updRefresh, 1500);
    setInterval(updRefresh, UPD_RECHECK_MS);
});
