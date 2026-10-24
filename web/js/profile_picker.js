/* The budget picker: shown whenever no budget is open. Opening one reloads into the dashboard. */

let pickerState = null;
let pinFor = null;   // name of the locked budget whose PIN is being entered

try { document.documentElement.setAttribute('data-theme', localStorage.getItem('budget_dashboard_theme') || 'dark'); } catch (e) { /* storage blocked */ }

const pickerEl = id => document.getElementById(id);

function pickerEscape(text) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(text == null ? '' : text).replace(/[&<>"']/g, m => map[m]);
}

async function pickerCall(path, body) {
    const res = await fetch(path, body === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
    if (!res.ok || data.success === false) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
}

function pickerShowError(message) {
    const box = pickerEl('pickerError');
    box.textContent = message || '';
    box.hidden = !message;
}

async function pickerLoad() {
    try {
        pickerState = await pickerCall('/api/profiles');
        if (pickerState.active) { location.href = '/'; return; }
        pickerRender();
    } catch (err) {
        pickerShowError('Could not reach the app: ' + err.message);
    }
}

function pickerRender() {
    const s = pickerState;
    pickerEl('legacyBox').hidden = !s.legacy_found;
    pickerEl('listBox').hidden = s.profiles.length === 0;
    pickerEl('pickerList').innerHTML = s.profiles.map(p => {
        const asking = pinFor === p.name;
        return `<div class="picker-item${asking ? ' asking' : ''}">
            <button type="button" class="picker-open" data-name="${pickerEscape(p.name)}">
                <span class="picker-name">${pickerEscape(p.name)}</span>
                ${p.has_pin ? '<span class="picker-badge">PIN</span>' : ''}
                ${p.last_used ? '<span class="picker-badge subtle">Last opened</span>' : ''}
            </button>
            ${asking ? `<div class="picker-row">
                <input type="password" class="input-text picker-pin" id="pinEntry" autocomplete="off" placeholder="PIN">
                <button type="button" class="btn-modal-save" data-submit-pin="${pickerEscape(p.name)}">Open</button>
                <button type="button" class="btn-modal-cancel" data-cancel-pin>Cancel</button>
            </div>` : ''}
        </div>`;
    }).join('');
    if (pinFor) {
        const entry = pickerEl('pinEntry');
        if (entry) entry.focus();
    }
}

async function openBudget(name, pin) {
    pickerShowError('');
    try {
        await pickerCall('/api/profiles/open', { name, pin: pin || '' });
        location.href = '/';
    } catch (err) {
        pickerShowError(err.message);
        const entry = pickerEl('pinEntry');
        if (entry) { entry.value = ''; entry.focus(); }
    }
}

async function createBudget() {
    const name = pickerEl('newName').value.trim();
    const pin = pickerEl('newPin').value;
    if (!name) { pickerShowError('Enter a name for the new budget.'); pickerEl('newName').focus(); return; }
    pickerShowError('');
    pickerEl('btnCreate').disabled = true;
    try {
        await pickerCall('/api/profiles/create', { name, pin });
        location.href = '/';
    } catch (err) {
        pickerShowError(err.message);
        pickerEl('btnCreate').disabled = false;
    }
}

async function moveLegacy() {
    const name = pickerEl('legacyName').value.trim();
    if (!name) { pickerShowError('Enter a name for your existing budget.'); pickerEl('legacyName').focus(); return; }
    pickerShowError('');
    pickerEl('btnLegacy').disabled = true;
    pickerEl('btnLegacy').textContent = 'Moving files…';
    try {
        await pickerCall('/api/profiles/migrate', { name });
        location.href = '/';
    } catch (err) {
        pickerShowError(err.message);
        pickerEl('btnLegacy').disabled = false;
        pickerEl('btnLegacy').textContent = 'Use this budget';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    pickerEl('pickerList').addEventListener('click', e => {
        const open = e.target.closest('.picker-open');
        const submit = e.target.closest('[data-submit-pin]');
        if (open) {
            const profile = pickerState.profiles.find(p => p.name === open.dataset.name);
            if (profile && profile.has_pin) { pinFor = profile.name; pickerShowError(''); pickerRender(); }
            else openBudget(open.dataset.name);
        } else if (submit) {
            openBudget(submit.dataset.submitPin, pickerEl('pinEntry').value);
        } else if (e.target.closest('[data-cancel-pin]')) {
            pinFor = null;
            pickerShowError('');
            pickerRender();
        }
    });
    pickerEl('pickerList').addEventListener('keydown', e => {
        if (e.key === 'Enter' && e.target.id === 'pinEntry') openBudget(pinFor, e.target.value);
    });
    pickerLoad();
});
