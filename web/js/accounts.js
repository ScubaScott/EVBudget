/* Accounts: the list of cards and bank accounts that imports come from. Add, rename, change type,
   merge into another account, delete, and jump to an account's transactions. No balances are tracked. */

let acctData = [];            // [{id, name, account_type, transactions, first_date, last_date, files}]
let acctTypes = [];
let acctEditingId = null;
let acctDeleting = null;

async function loadAccounts() {
    try {
        const d = await api('/api/accounts');
        acctData = d.accounts;
        acctTypes = d.types;
        document.getElementById('acctNewType').innerHTML = typeOptions(acctTypes[0]);
        renderAccounts();
    } catch (err) {
        document.getElementById('acctBody').innerHTML = `<tr><td colspan="6">Could not load accounts: ${escapeHtml(err.message)}</td></tr>`;
    }
}

function typeOptions(selected) {
    return acctTypes.map(t => `<option value="${escapeHtml(t)}" ${t === selected ? 'selected' : ''}>${escapeHtml(t)}</option>`).join('');
}

function renderAccounts() {
    const total = acctData.reduce((n, a) => n + a.transactions, 0);
    document.getElementById('acctSub').textContent = acctData.length
        ? `${acctData.length} account${acctData.length === 1 ? '' : 's'}, ${total.toLocaleString()} transactions`
        : 'No accounts yet';
    if (!acctData.length) {
        document.getElementById('acctBody').innerHTML =
            '<tr><td colspan="6" class="import-more">No accounts yet. Add one above before importing: each import goes to an account from this list.</td></tr>';
        return;
    }
    document.getElementById('acctBody').innerHTML = acctData.map(a => {
        const editing = a.id === acctEditingId;
        const dates = a.transactions ? `${escapeHtml(formatDisplayDate(a.first_date))} &ndash; ${escapeHtml(formatDisplayDate(a.last_date))}` : '<span class="import-muted">&mdash;</span>';
        const files = a.files.length
            ? escapeHtml(a.files.slice(0, 3).join(', ')) + (a.files.length > 3 ? ` <span title="${escapeHtml(a.files.slice(3).join(', '))}">and ${a.files.length - 3} more</span>` : '')
            : '<span class="import-muted">No imports yet</span>';
        if (editing) {
            return `<tr>
                <td><input type="text" id="acctEditName" class="input-text" maxlength="60" value="${escapeHtml(a.name)}" onkeydown="if (event.key === 'Enter') saveAccountEdit(${a.id}); if (event.key === 'Escape') cancelAccountEdit()"></td>
                <td><select id="acctEditType" class="select-input">${typeOptions(a.account_type)}</select></td>
                <td style="text-align: right;">${a.transactions.toLocaleString()}</td>
                <td>${dates}</td>
                <td class="acct-files">${files}</td>
                <td><div class="acct-actions">
                    <button type="button" class="btn-inline-action btn-inline-save" onclick="saveAccountEdit(${a.id})">Save</button>
                    <button type="button" class="btn-inline-action btn-inline-cancel" onclick="cancelAccountEdit()">Cancel</button>
                </div></td></tr>`;
        }
        return `<tr>
            <td><strong>${escapeHtml(a.name)}</strong></td>
            <td>${escapeHtml(a.account_type)}</td>
            <td style="text-align: right;">${a.transactions.toLocaleString()}</td>
            <td>${dates}</td>
            <td class="acct-files">${files}</td>
            <td><div class="acct-actions">
                <button type="button" class="btn-inline-action btn-inline-cancel" ${a.transactions ? '' : 'disabled'} onclick="goToAccount(${a.id})">View</button>
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="editAccount(${a.id})">Edit</button>
                <button type="button" class="btn-inline-action btn-inline-cancel" onclick="openAccountDelete(${a.id})">${a.transactions ? 'Merge / delete' : 'Delete'}</button>
            </div></td></tr>`;
    }).join('');
    if (acctEditingId) { const el = document.getElementById('acctEditName'); if (el) el.focus(); }
}

async function addAccount() {
    const input = document.getElementById('acctNewName');
    const name = input.value.trim();
    if (!name) { showToast('Give the account a name.', true); input.focus(); return; }
    try {
        await apiPost('/api/accounts/save', { name, account_type: document.getElementById('acctNewType').value });
        input.value = '';
        await loadAccounts();
        const after = acctAfterAdd;
        if (after) {
            acctAfterAdd = null;
            await showImportSub('import');
            await after.done(name);
            showToast(`Added "${name}" and chose it for ${after.what}.`);
        } else {
            showToast(`Added "${name}". Choose it in the account box when you import.`);
        }
    } catch (err) {
        showToast(err.message, true);
    }
}

// Imports only go to accounts on this list. When the import screen has nothing to tie a file or bank account to,
// it sends the person here; once the account is added they go back with it chosen.
let acctAfterAdd = null;      // {what: 'Chase3855_Activity.csv', done(name)}

async function acctGoAdd(what, suggestedName, suggestedType, done) {
    acctAfterAdd = { what, done };
    document.getElementById('acctAddingForText').innerHTML =
        `Add the account for <strong>${escapeHtml(what)}</strong>, then you'll go back to Import with it chosen.`;
    const input = document.getElementById('acctNewName');
    input.value = suggestedName || '';
    const loading = showImportSub('accounts');     // shows the page (and the note above) at once; the list follows
    input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    input.focus();
    input.select();
    await loading;
    if (suggestedType && acctTypes.includes(suggestedType)) document.getElementById('acctNewType').value = suggestedType;
}

function acctCancelAddFor() {
    acctAfterAdd = null;
    showImportSub('import');
}

function editAccount(id) { acctEditingId = id; renderAccounts(); }
function cancelAccountEdit() { acctEditingId = null; renderAccounts(); }

async function saveAccountEdit(id) {
    const name = document.getElementById('acctEditName').value.trim();
    const type = document.getElementById('acctEditType').value;
    const old = acctData.find(a => a.id === id);
    try {
        await apiPost('/api/accounts/save', { id, name, account_type: type });
        acctEditingId = null;
        await loadAccounts();
        await refreshTransactions();   // transactions carry the account name
        showToast(old && old.name !== name ? `Renamed to "${name}" on ${old.transactions} transaction${old.transactions === 1 ? '' : 's'}.` : 'Saved.');
    } catch (err) {
        showToast(err.message, true);
    }
}

// Merge into another account, or delete an account that has nothing in it
function openAccountDelete(id) {
    const a = acctData.find(x => x.id === id);
    if (!a) return;
    acctDeleting = a;
    const others = acctData.filter(x => x.id !== id);
    const has = a.transactions > 0;
    document.getElementById('acctDeleteTitle').textContent = has ? `Merge "${a.name}"` : `Delete "${a.name}"`;
    document.getElementById('acctDeleteSub').textContent = has ? `${a.transactions.toLocaleString()} transactions are in this account` : 'Nothing is in this account';
    document.getElementById('acctMergeWrap').hidden = !has;
    document.getElementById('acctMergeInto').innerHTML = others.map(x => `<option value="${x.id}">${escapeHtml(x.name)}</option>`).join('');
    const go = document.getElementById('acctDeleteGo');
    if (has && !others.length) {
        document.getElementById('acctDeleteNote').textContent = 'Add another account first, then merge into it. Transactions always belong to an account you can see.';
        go.disabled = true;
    } else {
        document.getElementById('acctDeleteNote').textContent = has
            ? 'Every transaction moves to the account you choose, and this account is removed. Your transactions are not deleted.'
            : 'This removes the account from the list.';
        go.disabled = false;
    }
    go.textContent = has ? 'Merge and remove' : 'Delete';
    document.getElementById('acctDeleteOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeAccountDelete() {
    document.getElementById('acctDeleteOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function confirmAccountDelete() {
    const a = acctDeleting;
    if (!a) return;
    const body = { id: a.id };
    if (a.transactions) body.merge_into = parseInt(document.getElementById('acctMergeInto').value, 10);
    try {
        const res = await apiPost('/api/accounts/delete', body);
        closeAccountDelete();
        await loadAccounts();
        if (res.moved) await refreshTransactions();
        showToast(res.moved ? `Moved ${res.moved.toLocaleString()} transaction${res.moved === 1 ? '' : 's'}. "${a.name}" is removed.` : `"${a.name}" is removed.`);
    } catch (err) {
        showToast(err.message, true);
    }
}

// All of an account's transactions, whatever dates they fall on
function goToAccount(id) {
    const a = acctData.find(x => x.id === id);
    if (!a) return;
    showTab('analytics', 'spending');
    setInclusionFilter('all');
    document.getElementById('filterSearch').value = '';
    document.getElementById('filterEnvelope').value = '';
    document.getElementById('filterKind').value = '';
    document.getElementById('filterPerson').value = '';
    document.getElementById('filterAccount').value = a.name;
    applyPreset('all_time');
}

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAccountDelete(); });
});
