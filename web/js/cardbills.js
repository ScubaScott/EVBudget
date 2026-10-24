/* "Card bills": find the automatic bills charged to a card, so each can be moved when the card number changes.
   A checklist per card; ticks are saved on the server. */

let cardBillsCards = [];      // [{name, transactions, first_date, last_date, account_type}]
let cardBillsUnnamed = [];    // earlier imports that have no card name yet
let cardBillsItems = [];      // the checklist for the chosen card
let cardBillsLoadedFor = '';

async function openCardBillsModal() {
    document.getElementById('cardBillsModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    await loadCardBillsCards();
}

function closeCardBillsModal() {
    document.getElementById('cardBillsModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function loadCardBillsCards(keepChoice) {
    try {
        const data = await api('/api/cards');
        cardBillsCards = data.cards;
        cardBillsUnnamed = data.unnamed;
    } catch (err) {
        showToast('Could not load cards: ' + err.message, true);
        return;
    }
    renderCardBillsUnnamed();
    const select = document.getElementById('cardBillsCard');
    let remembered = '';
    try { remembered = localStorage.getItem('card_bills_card') || ''; } catch (e) { /* storage blocked */ }
    const previous = keepChoice ? select.value : remembered;
    select.innerHTML = cardBillsCards.map(c =>
        `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)} (${c.transactions.toLocaleString()} transactions)</option>`).join('');
    const names = cardBillsCards.map(c => c.name);
    const credit = cardBillsCards.find(c => c.account_type === 'Credit / Debit');
    select.value = names.includes(previous) ? previous : (credit ? credit.name : (names[0] || ''));
    document.getElementById('cardBillsNone').hidden = cardBillsCards.length > 0;
    document.getElementById('cardBillsMain').hidden = cardBillsCards.length === 0;
    if (cardBillsCards.length > 0) await loadCardBills();
}

// Earlier imports have no card name; name them once, in groups
function renderCardBillsUnnamed() {
    const box = document.getElementById('cardBillsUnnamed');
    box.hidden = cardBillsUnnamed.length === 0;
    if (cardBillsUnnamed.length === 0) return;
    document.getElementById('cardBillsUnnamedBody').innerHTML = cardBillsUnnamed.map((g, i) => `<tr>
        <td><div style="font-weight:500;">${escapeHtml(g.example_file)}</div>
            <div class="import-file-sub">${g.files} file${g.files === 1 ? '' : 's'}, ${g.transactions.toLocaleString()} transactions, ${escapeHtml(formatDisplayDate(g.first_date))} to ${escapeHtml(formatDisplayDate(g.last_date))}</div></td>
        <td><input type="text" class="input-text import-mini" id="cardName${i}" maxlength="60" value="${escapeHtml(g.suggested)}"></td>
    </tr>`).join('');
}

async function saveCardNames() {
    const assignments = cardBillsUnnamed.map((g, i) => ({ key: g.key, name: document.getElementById('cardName' + i).value.trim() }))
        .filter(a => a.name);
    if (assignments.length === 0) { showToast('Enter a name for at least one group.', true); return; }
    try {
        const res = await apiPost('/api/cards/name', { assignments });
        showToast(`Named ${res.named.toLocaleString()} transaction${res.named === 1 ? '' : 's'}.`);
        await refreshTransactions();
        await loadCardBillsCards(true);
    } catch (err) {
        showToast('Could not save the names: ' + err.message, true);
    }
}

async function loadCardBills() {
    const card = document.getElementById('cardBillsCard').value;
    if (!card) return;
    try { localStorage.setItem('card_bills_card', card); } catch (e) { /* storage blocked */ }
    try {
        const data = await api('/api/cards/bills?card=' + encodeURIComponent(card));
        cardBillsItems = data.items;
        cardBillsLoadedFor = card;
        document.getElementById('cardBillsThrough').textContent = data.through ? `Based on charges through ${formatDisplayDate(data.through)}.` : '';
        renderCardBillsProgress(data.progress);
        renderCardBills();
    } catch (err) {
        showToast('Could not look for bills: ' + err.message, true);
    }
}

function renderCardBillsProgress(p) {
    const pct = p.total ? Math.round(100 * p.updated / p.total) : 0;
    document.getElementById('cardBillsProgressText').textContent = p.total
        ? `${p.updated} of ${p.total} moved to the new card`
        : 'No automatic bills found on this card.';
    document.getElementById('cardBillsProgressBar').style.width = pct + '%';
}

function cardBillsProgressNow() {
    const active = cardBillsItems.filter(i => !i.stopped && i.status !== 'not_a_bill');
    return { updated: active.filter(i => i.status === 'updated').length, total: active.length };
}

function renderCardBills() {
    const showMaybe = document.getElementById('cardBillsMaybe').checked;
    const showStopped = document.getElementById('cardBillsStopped').checked;
    const showHidden = document.getElementById('cardBillsHidden').checked;
    const rows = cardBillsItems.map((item, i) => ({ item, i })).filter(({ item }) =>
        (showMaybe || item.confidence === 'likely') && (showStopped || !item.stopped) && (showHidden || item.status !== 'not_a_bill'));
    document.getElementById('cardBillsBody').innerHTML = rows.length === 0
        ? '<tr><td colspan="7" class="import-more">Nothing to show with these options.</td></tr>'
        : rows.map(({ item, i }) => {
            const done = item.status === 'updated';
            const hidden = item.status === 'not_a_bill';
            const range = item.steady_amount ? '' : `<div class="import-file-sub">varies ${formatCurrency(item.min_amount)} to ${formatCurrency(item.max_amount)}</div>`;
            const when = item.stopped
                ? '<div class="import-file-sub">Seems to have stopped</div>'
                : (item.next_expected ? `<div class="import-file-sub">Next about ${escapeHtml(formatDisplayDate(item.next_expected))}</div>` : '');
            return `<tr class="${done || hidden ? 'excluded-row' : ''}">
                <td style="text-align:center;"><input type="checkbox" class="txn-checkbox" ${done ? 'checked' : ''} ${hidden ? 'disabled' : ''} title="Tick when this bill is moved to the new card" onchange="toggleCardBill(${i}, this.checked)"></td>
                <td><div style="font-weight:500;">${escapeHtml(item.merchant)}${item.confidence === 'maybe' ? ' <span class="status-pill status-excluded">Possible</span>' : ''}</div>
                    <div class="import-file-sub">${item.charges} charge${item.charges === 1 ? '' : 's'} since ${escapeHtml(formatDisplayDate(item.first_date))}</div></td>
                <td>${escapeHtml(item.cadence)}</td>
                <td style="text-align:right;">${formatCurrency(item.typical_amount)}${range}</td>
                <td>${escapeHtml(formatDisplayDate(item.last_date))}${when}</td>
                <td>${item.bill ? escapeHtml(item.bill.name) : '<span class="import-not-yet">Not in your bills</span>'}</td>
                <td style="text-align:right; white-space:nowrap;">
                    <button type="button" class="btn-inline-action btn-inline-cancel" onclick="markCardBill(${i}, '${hidden ? 'pending' : 'not_a_bill'}')">${hidden ? 'Restore' : 'Not a bill'}</button></td>
            </tr>`;
        }).join('');
}

async function markCardBill(i, status) {
    const item = cardBillsItems[i];
    const before = item.status;
    item.status = status;
    renderCardBillsProgress(cardBillsProgressNow());
    renderCardBills();
    try {
        await apiPost('/api/cards/bills/mark', { card: cardBillsLoadedFor, key: item.key, status });
    } catch (err) {
        item.status = before;
        renderCardBillsProgress(cardBillsProgressNow());
        renderCardBills();
        showToast('Could not save that: ' + err.message, true);
    }
}

function toggleCardBill(i, checked) {
    markCardBill(i, checked ? 'updated' : 'pending');
}
