/* Budgets (profiles) inside the dashboard: the header chip, the PIN setting, and the "who is this rule for?" question. */

let budgetHasPin = false;

async function loadBudgetChip() {
    try {
        const info = await api('/api/profiles');
        if (!info.active) return;
        document.getElementById('budgetName').textContent = info.active;
        document.getElementById('btnSwitchBudget').hidden = false;
        budgetHasPin = (info.profiles.find(p => p.name === info.active) || {}).has_pin || false;
    } catch (err) {
        // A fixed single-budget copy has no profiles; the chip simply stays hidden.
    }
}

async function switchBudget() {
    try {
        await apiPost('/api/profiles/close');
        location.href = '/';
    } catch (err) {
        showToast('Could not close this budget: ' + err.message, true);
    }
}

// ---------------------------------------------------------------------
// PIN
// ---------------------------------------------------------------------
async function refreshPinSection() {
    await loadBudgetChip();
    document.getElementById('pinStateText').textContent = budgetHasPin
        ? 'This budget has a PIN.'
        : 'This budget has no PIN. Anyone who opens the app can open it.';
    document.getElementById('pinCurrent').hidden = !budgetHasPin;
    document.getElementById('btnRemovePin').hidden = !budgetHasPin;
    ['pinCurrent', 'pinNew'].forEach(id => { document.getElementById(id).value = ''; });
}

async function saveBudgetPin() {
    const next = document.getElementById('pinNew').value;
    if (!next) { showToast('Enter the new PIN, or use Remove PIN.', true); return; }
    try {
        await apiPost('/api/profiles/pin', { current_pin: document.getElementById('pinCurrent').value, new_pin: next });
        showToast('PIN saved.');
        await refreshPinSection();
    } catch (err) {
        showToast(err.message, true);
    }
}

async function removeBudgetPin() {
    try {
        await apiPost('/api/profiles/pin', { current_pin: document.getElementById('pinCurrent').value, new_pin: '' });
        showToast('PIN removed.');
        await refreshPinSection();
    } catch (err) {
        showToast(err.message, true);
    }
}

document.addEventListener('DOMContentLoaded', loadBudgetChip);
document.addEventListener('settingsopen', refreshPinSection);
