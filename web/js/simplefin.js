/* Import tab: bank sync through SimpleFIN Bridge. A sync downloads posted transactions, then they go through the same
   review grid, rules and duplicate checks as bank files (importShowPreview in import.js). Nothing is saved until Save. */

let sfState = null;              // the server's answer: connection, accounts from the last download, problems, preview
let sfBusy = false;

document.addEventListener('tabchange', (e) => {
    if (e.detail.tab === 'import' && sfState === null) sfLoadStatus();
});

async function sfLoadStatus() {
    try {
        sfState = await api('/api/simplefin/status');
    } catch (err) {
        sfState = { connected: false, load_error: err.message };
    }
    sfRender();
}

function sfDownloadsLine(s) {
    return `${s.requests_today} of ${s.daily_limit} downloads used today`;
}

// When SimpleFIN last heard from a bank, in this computer's time: "11/02/2026 4:40 PM"
function sfWhen(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return '';
    return `${formatDisplayDate(toInputDateFormat(d))} ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}

function sfStale(s) {
    return ((s && s.accounts) || []).filter(a => a.stale);
}

function sfRender() {
    const s = sfState || {};
    const sub = document.getElementById('sfSubtitle');
    const actions = document.getElementById('sfActions');
    if (s.load_error) {
        sub.textContent = 'Could not check bank sync: ' + s.load_error;
        actions.innerHTML = '<button class="btn-refresh" onclick="sfLoadStatus()">Try again</button>';
        return;
    }
    if (!s.connected) {
        sub.innerHTML = 'Bring transactions in straight from your banks through SimpleFIN Bridge, instead of downloading files. They are reviewed here before anything is saved, the same as a file.';
        actions.innerHTML = '<button class="btn-modal-save" onclick="sfShowConnect(true)">Connect&hellip;</button>';
        document.getElementById('sfErrors').hidden = true;
        sfHideAccounts();
        return;
    }
    const parts = [`Connected to <strong>${escapeHtml(s.host)}</strong>.`];
    parts.push(s.synced_through
        ? `Saved through ${escapeHtml(formatDisplayDate(s.synced_through))}; the next sync starts a few days before that, to catch late postings.`
        : 'Not synced yet: the first sync brings the last 90 days, and anything already saved from files is recognized.');
    if (s.range) {
        parts.push(`Downloaded ${escapeHtml(formatDisplayDate(s.range.start))} to ${escapeHtml(formatDisplayDate(s.range.end))}.`);
        if (s.cut_short) parts.push('The gap since the last sync was longer than SimpleFIN allows in one go, so the oldest days were not asked for: bring those in from a bank file.');
    }
    if (s.reach_back) parts.push(`<strong>Press Sync now to bring in transactions from ${escapeHtml(formatDisplayDate(s.reach_back))}</strong> (an account's starting day was moved earlier).`);
    parts.push(`<span class="import-hint" title="SimpleFIN asks apps for no more than 24 downloads a day, and turns off access for apps that keep going over.">${sfDownloadsLine(s)}.</span>`);
    sub.innerHTML = parts.join(' ');
    const canMap = Array.isArray(s.accounts) && s.accounts.length > 0;
    actions.innerHTML = `
        <button class="btn-modal-save" id="btnSfSync" onclick="sfSync()" ${sfBusy ? 'disabled' : ''}>${sfBusy ? 'Downloading&hellip;' : 'Sync now'}</button>
        ${canMap ? '<button class="btn-refresh" onclick="sfShowAccounts()" title="Choose which EVBudget account each bank account goes to, and the day each one starts from">Accounts&hellip;</button>' : ''}
        <button class="btn-refresh" onclick="sfOpenBridge()" title="Fix a bank login, add a bank, or manage the subscription">SimpleFIN Bridge</button>
        <button class="btn-refresh" onclick="sfDisconnect()" title="Stop syncing. Saved transactions stay.">Disconnect</button>`;
    sfRenderErrors();
}

// Every problem SimpleFIN reported, as it asks apps to do, with what each means; and banks SimpleFIN hasn't
// checked lately, whose newest transactions can't come in until it does
function sfRenderErrors() {
    const box = document.getElementById('sfErrors');
    const errs = (sfState && sfState.errors) || [];
    const stale = sfStale(sfState);
    const n = errs.filter(e => e.level !== 'note').length;
    box.hidden = errs.length === 0 && stale.length === 0;
    box.classList.toggle('sf-notes-only', n === 0);    // red only for problems SimpleFIN reported
    let html = '';
    if (errs.length) {
        const head = n
            ? `SimpleFIN reported ${n === 1 ? 'a problem' : n + ' problems'}${errs.length > n ? ' and a note' : ''}:`
            : `${errs.length === 1 ? 'A note' : 'Notes'} from SimpleFIN:`;
        html += `<strong>${head}</strong><ul>` +
            errs.map(e => `<li>${e.level === 'note' ? '' : escapeHtml(e.explain) + ' '}${e.message ? `<span class="${e.level === 'note' ? '' : 'import-hint'}">SimpleFIN said: &ldquo;${escapeHtml(e.message)}&rdquo;</span>` : escapeHtml(e.explain)}</li>`).join('') + '</ul>';
    }
    if (stale.length) {
        html += `<strong>SimpleFIN hasn't checked ${stale.length === 1 ? 'this bank account' : 'these bank accounts'} in over a day, so newer transactions there can't come in yet:</strong><ul>` +
            stale.map(a => `<li>${escapeHtml(a.name || a.sf_name)}${a.name && !a.name.includes(a.sf_name) ? ` <span class="import-hint">(${escapeHtml(a.sf_name)})</span>` : ''}: last checked ${escapeHtml(sfWhen(a.updated))}</li>`).join('') +
            '</ul><span class="import-hint">SimpleFIN usually checks each bank about once a day. If one stays behind, open SimpleFIN Bridge and look at that connection: the bank may need you to sign in again there.</span>';
    }
    box.innerHTML = html;
}

// ---------------------------------------------------------------------
// Connect / disconnect
// ---------------------------------------------------------------------
function sfShowConnect(show) {
    document.getElementById('sfConnect').hidden = !show;
    document.getElementById('sfConnectNote').textContent = '';
    if (show) {
        document.getElementById('sfToken').value = '';
        document.getElementById('sfToken').focus();
    }
}

async function sfConnect() {
    const token = document.getElementById('sfToken').value.trim();
    const note = document.getElementById('sfConnectNote');
    if (!token) { note.textContent = 'Paste the setup token first.'; return; }
    const btn = document.getElementById('btnSfConnect');
    btn.disabled = true;
    note.textContent = 'Connecting…';
    try {
        sfState = await apiPost('/api/simplefin/connect', { token });
        document.getElementById('sfToken').value = '';
        sfShowConnect(false);
        sfRender();
        showToast('Bank sync connected. Press Sync now to download transactions.');
    } catch (err) {
        note.textContent = err.message;
    } finally {
        btn.disabled = false;
    }
}

async function sfDisconnect() {
    if (!confirm('Stop bank sync for this budget?\n\nTransactions already saved stay. Your account choices are kept in case you connect again. To cut off access completely, also remove EVBudget under Apps at SimpleFIN Bridge.')) return;
    try {
        sfState = await apiPost('/api/simplefin/disconnect');
        sfRender();
        showToast('Bank sync disconnected.');
    } catch (err) { showToast(err.message, true); }
}

async function sfOpenBridge() {
    const url = (sfState && sfState.bridge_url) || 'https://beta-bridge.simplefin.org';
    try { await apiPost('/api/open-link', { url }); } catch (err) { showToast(err.message, true); }
}

// ---------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------
async function sfSync() {
    if (sfBusy) return;
    sfBusy = true;
    sfRender();
    try {
        sfHandle(await apiPost('/api/simplefin/sync'));
    } catch (err) {
        showToast('Bank sync: ' + err.message, true);
        sfLoadStatus();
    } finally {
        sfBusy = false;
        sfRender();
    }
}

function sfHandle(res) {
    sfState = res;
    sfRender();
    if (res.needs_mapping) {
        sfShowAccounts(true);
    } else if (res.preview) {
        sfHideAccounts();
        importShowPreview(res.preview, 'sync');
        const n = res.preview.summary.new - (res.preview.summary.likely_duplicates || 0);
        const stale = sfStale(res).length;
        showToast(n ? `${n} new transaction${n === 1 ? '' : 's'} to review.`
            : stale ? 'Nothing new yet: SimpleFIN hasn\'t checked some of your banks lately (see Bank sync).'
            : 'Nothing new from your banks.');
    } else {
        // Account choices saved with nothing downloaded in this session
        sfHideAccounts();
        showToast(res.reach_back ? `Saved. Press Sync now to bring in transactions from ${formatDisplayDate(res.reach_back)}.` : 'Saved. The next sync uses these choices.');
    }
}

// After Save: show the same download again, so the grid marks what was just saved, without downloading again
async function sfAfterSave() {
    try {
        importShowPreview(await apiPost('/api/simplefin/preview'), 'sync', true);
    } catch (err) { showToast(err.message, true); }
    sfLoadStatus();
}

// ---------------------------------------------------------------------
// Where each bank account goes
// ---------------------------------------------------------------------
// keep: choices typed into the open screen ({key: {use, name, start}}), kept when it is drawn again
function sfShowAccounts(firstTime, keep) {
    const s = sfState;
    if (!s || !s.accounts) return;
    const unmapped = s.accounts.filter(a => !a.mapped).length;
    document.getElementById('sfAccountsIntro').innerHTML = unmapped
        ? `<strong>${unmapped === s.accounts.length ? 'Tell EVBudget where each bank account goes.' : `${unmapped} new bank account${unmapped === 1 ? '' : 's'} to set up.`}</strong>
           Choose the account from your Accounts list (rows already saved from files are recognized, not saved twice). If it isn't there, choose <em>Add a new account</em> to add it first. Untick loans and investment accounts you don't budget from.
           The starting day leaves out older transactions; for an account you already import files for, it starts a week before the newest saved one.`
        : 'Change where a bank account goes, the day it starts from, or leave one out. Changes apply to this download and every sync after it; moving a starting day earlier makes the next sync reach back to it. Transactions already saved are not moved (use Accounts &rarr; Merge for that).';
    const options = s.account_options || [];
    document.getElementById('sfAccountsBody').innerHTML = s.accounts.map((a, i) => {
        const k = (keep || {})[a.key];
        const use = k ? k.use : !a.ignore;
        const name = k ? k.name : (a.name || '');
        const start = k ? k.start : (a.start_date || '');
        return `
        <tr data-i="${i}" class="${use ? '' : 'import-row-skipped'}">
            <td style="text-align:center;"><input type="checkbox" class="txn-checkbox sf-use" ${use ? 'checked' : ''} onchange="this.closest('tr').classList.toggle('import-row-skipped', !this.checked)"></td>
            <td><div style="font-weight:500;">${escapeHtml(a.sf_name)}</div>
                <div class="import-file-sub">${escapeHtml(a.connection)}${a.balance ? ` &middot; balance ${escapeHtml(a.balance)} ${escapeHtml(a.currency || '')}` : ''}
                ${a.posted != null ? `&middot; ${a.posted} posted${a.pending ? `, ${a.pending} pending` : ''}${a.first_date ? `, ${escapeHtml(formatDisplayDate(a.first_date))} to ${escapeHtml(formatDisplayDate(a.last_date))}` : ''}` : ''}</div>
                ${a.saved_through ? `<div class="import-file-sub">Saved in EVBudget through ${escapeHtml(formatDisplayDate(a.saved_through))}</div>` : ''}
                ${a.updated ? `<div class="import-file-sub${a.stale ? ' sf-stale' : ''}" title="When SimpleFIN last got news from this bank. Newer transactions can't come in until it checks again.">SimpleFIN last checked ${escapeHtml(sfWhen(a.updated))}</div>` : ''}</td>
            <td>${sfAccountSelect(options, name)}<div class="import-file-sub sf-type">${escapeHtml(sfTypeOf(name))}</div></td>
            <td><input type="date" class="input-text import-mini sf-start" value="${escapeHtml(start)}"></td>
        </tr>`;
    }).join('');
    document.getElementById('sfAccountsNote').textContent = '';
    document.getElementById('btnSfAccountsCancel').hidden = !!unmapped;
    document.getElementById('sfAccounts').hidden = false;
    document.getElementById('sfAccounts').scrollIntoView({ behavior: 'smooth', block: firstTime ? 'nearest' : 'center' });
}

function sfHideAccounts() {
    document.getElementById('sfAccounts').hidden = true;
}

// Bank accounts go to accounts on the Accounts list; the last choice sends the person there to add one first
function sfAccountSelect(options, selected) {
    return `<select class="select-input import-mini sf-name" data-prev="${escapeHtml(selected || '')}" onchange="sfAccountChosen(this)">
        <option value="" ${selected ? '' : 'selected'}>Choose an account&hellip;</option>
        ${options.map(o => `<option value="${escapeHtml(o.name)}" ${o.name === selected ? 'selected' : ''}>${escapeHtml(o.name)}</option>`).join('')}
        <option value="__add">+ Add a new account&hellip;</option>
    </select>`;
}

function sfTypeOf(name) {
    const o = ((sfState && sfState.account_options) || []).find(x => x.name === name);
    return o ? o.account_type : '';
}

// What the open screen says now, by bank account
function sfReadAccountForm() {
    const out = {};
    document.querySelectorAll('#sfAccountsBody tr').forEach(tr => {
        const a = sfState.accounts[parseInt(tr.dataset.i, 10)];
        if (!a) return;
        const sel = tr.querySelector('.sf-name');
        out[a.key] = {
            use: tr.querySelector('.sf-use').checked, name: sel.value === '__add' ? (sel.dataset.prev || '') : sel.value,
            start: tr.querySelector('.sf-start').value || ''
        };
    });
    return out;
}

function sfAccountChosen(sel) {
    const tr = sel.closest('tr');
    const a = sfState.accounts[parseInt(tr.dataset.i, 10)];
    if (sel.value !== '__add') {
        sel.dataset.prev = sel.value;
        tr.querySelector('.sf-type').textContent = sfTypeOf(sel.value);
        return;
    }
    sel.value = sel.dataset.prev || '';
    const keep = sfReadAccountForm();
    acctGoAdd(a.sf_name, a.sf_name, '', async (name) => {
        keep[a.key] = Object.assign({}, keep[a.key], { name, use: true });
        await sfRefreshAccountChoices(keep);
    });
}

// The Accounts list may have changed: fetch it again, keeping what was chosen on the open screen
async function sfRefreshAccountChoices(keep) {
    if (!sfState || !sfState.connected) return;
    const open = !document.getElementById('sfAccounts').hidden;
    const form = keep || (open ? sfReadAccountForm() : null);
    try {
        sfState = await api('/api/simplefin/status');
    } catch (err) {
        return;
    }
    sfRender();
    if (open || keep) {
        const names = new Set((sfState.account_options || []).map(o => o.name));
        Object.values(form || {}).forEach(k => { if (k.name && !names.has(k.name)) k.name = ''; });
        sfShowAccounts(false, form);
    }
}

async function sfSaveAccounts() {
    const rows = Array.from(document.querySelectorAll('#sfAccountsBody tr'));
    const accounts = rows.map(tr => {
        const a = sfState.accounts[parseInt(tr.dataset.i, 10)];
        return {
            key: a.key, sf_name: a.sf_name, ignore: !tr.querySelector('.sf-use').checked, name: tr.querySelector('.sf-name').value,
            start_date: tr.querySelector('.sf-start').value || null
        };
    });
    const missing = accounts.find(a => !a.ignore && !a.name);
    if (missing) {
        document.getElementById('sfAccountsNote').textContent = `Choose an account for ${missing.sf_name}, or untick it. If it isn't in the list, choose "Add a new account".`;
        return;
    }
    try {
        sfHandle(await apiPost('/api/simplefin/accounts', { accounts }));
    } catch (err) {
        document.getElementById('sfAccountsNote').textContent = err.message;
    }
}
