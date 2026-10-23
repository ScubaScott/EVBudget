/* Income tab: who gets paid what over time, the monthly figure to budget from, and the people / pay sources behind it. */

let incomeData = null;
let incomeSources = [];
let incomePlanning = null;
let incomePeople = [];
let incomePreset = 'ytd';
let incomeLoaded = false;
let chartIncomeMonthly = null;
let chartIncomeTrend = null;
let chartIncomeVsSpend = null;
let sourcePreviewTimer = null;

const INCOME_PRESETS = {
    ytd: 'Year to date', this_year: 'This year', last_12: 'Last 12 months', last_year: 'Last year', all_time: 'All time'
};
const FREQ_LABELS = { weekly: 'week', monthly: 'month', yearly: 'year', once: 'once' };

document.addEventListener('tabchange', (e) => {
    if (e.detail.tab === 'income') {
        if (!incomeLoaded) {
            incomeLoaded = true;
            renderIncomePresets();
            applyIncomePreset('ytd');
        } else {
            loadIncome();
        }
    }
});
document.addEventListener('themechange', () => { if (incomeData) renderIncomeCharts(); });

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { closePersonModal(); closeSourceModal(); }
    });
});

// ---------------------------------------------------------------------
// Timeframe
// ---------------------------------------------------------------------
function renderIncomePresets() {
    document.getElementById('incomePresets').innerHTML =
        '<span class="control-label">Timeframe:</span>' +
        Object.entries(INCOME_PRESETS).map(([k, label]) =>
            `<button type="button" class="preset-btn ${k === incomePreset ? 'active' : ''}" data-preset="${k}" onclick="applyIncomePreset('${k}')">${label}</button>`).join('');
}

function applyIncomePreset(key) {
    incomePreset = key;
    const today = new Date();
    let start, end = today;
    if (key === 'ytd') start = new Date(today.getFullYear(), 0, 1);
    else if (key === 'this_year') { start = new Date(today.getFullYear(), 0, 1); end = new Date(today.getFullYear(), 11, 31); }
    else if (key === 'last_year') { start = new Date(today.getFullYear() - 1, 0, 1); end = new Date(today.getFullYear() - 1, 11, 31); }
    else if (key === 'last_12') { start = new Date(today.getFullYear(), today.getMonth() - 11, 1); }
    else start = null;
    if (!start) {
        const dates = allTransactions.map(t => t.date).sort();
        start = dates.length ? parseTxnDate(dates[0]) : new Date(today.getFullYear(), 0, 1);
    }
    document.getElementById('incomeStart').value = toInputDateFormat(start);
    document.getElementById('incomeEnd').value = toInputDateFormat(end);
    renderIncomePresets();
    loadIncome();
}

function onIncomeDatesChange() {
    incomePreset = 'custom';
    renderIncomePresets();
    loadIncome();
}

// ---------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------
async function loadIncome() {
    const from = document.getElementById('incomeStart').value;
    const to = document.getElementById('incomeEnd').value;
    try {
        const [summary, sources, people] = await Promise.all([
            api(`/api/income/summary?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`),
            api('/api/income/sources'),
            api('/api/people')
        ]);
        incomeData = summary;
        incomeSources = sources.sources;
        incomePlanning = sources.planning;
        incomePeople = people.people;
        renderIncomeKpis();
        renderIncomeCharts();
        renderIncomeManager();
        renderIncomeTxns();
    } catch (err) {
        showToast('Could not load income: ' + err.message, true);
    }
}

function incomeSeriesColor(name) {
    if (name === 'Bonus') return '#f59e0b';
    if (name === 'Other income') return '#14b8a6';
    const person = name.replace(/ regular$/, '');
    const p = incomePeople.find(x => x.name === person);
    if (p && p.color) return p.color;
    const idx = Math.max(0, incomePeople.findIndex(x => x.name === person));
    return COLOR_PALETTE[idx % COLOR_PALETTE.length];
}

function renderIncomeKpis() {
    const c = incomeData.cards;
    const card = (cls, title, value, sub, badge) => `
        <div class="kpi-card ${cls}"><div class="kpi-header"><span class="kpi-title">${title}</span>${badge ? `<span class="kpi-badge badge-neutral">${badge}</span>` : ''}</div>
        <div class="kpi-value ${cls}">${value}</div><div class="kpi-subtitle">${sub}</div></div>`;
    let html = '';
    Object.entries(c.income_by_person).forEach(([who, total]) => {
        const next = c.next_pay_dates[who];
        html += card('income', escapeHtml(who), formatCurrency(total), next ? `Next paycheck ${formatDisplayDate(next)}` : 'Paychecks in range', 'Income');
    });
    html += card('net', 'Plan around', formatCurrency(c.planning_monthly) + '<span class="kpi-unit"> / month</span>',
        c.observed_regular_monthly === null ? 'Typical regular paychecks, bonuses left out'
            : `Typical regular paychecks. Actual regular pay averaged ${formatCurrency(c.observed_regular_monthly)} a month here.`);
    html += card('spend', 'Bonuses', formatCurrency(c.bonus_total), 'Not counted in the plan figure');
    const labels = Object.entries(c.other_by_label).map(([k, v]) => `${escapeHtml(k)} ${formatCurrency(v)}`).slice(0, 3).join(' &bull; ');
    html += card('count', 'Other income', formatCurrency(c.other_total), labels || 'Refunds of tax, interest, side income');
    document.getElementById('incomeKpis').innerHTML = html;
}

function incomeChartDefaults() {
    const t = getThemeChartColors();
    return {
        t,
        scales: (stacked) => ({
            x: { stacked, grid: { color: t.gridSubtleColor }, ticks: { color: t.tickColor, maxRotation: 45, minRotation: 0 } },
            y: { stacked, beginAtZero: true, grid: { color: t.gridColor }, ticks: { color: t.tickColor, callback: v => '$' + v.toLocaleString() } }
        }),
        legend: { position: 'top', labels: { color: t.tickColor, boxWidth: 11, padding: 8, font: { size: 11 } } }
    };
}

function monthLabel(m, extra) {
    const [y, mo] = m.split('-');
    const name = new Date(+y, +mo - 1, 1).toLocaleString('en-US', { month: 'short' });
    return `${name} ${y.slice(2)}${extra ? ' ★' : ''}`;
}

function renderIncomeCharts() {
    const d = incomeData;
    const def = incomeChartDefaults();
    const labels = d.monthly.map(m => monthLabel(m.month, m.extra_paycheck_sources.length > 0));
    const hasExtra = d.monthly.some(m => m.extra_paycheck_sources.length > 0);
    document.getElementById('incomeMonthlySubtitle').textContent =
        'Regular pay by person, bonuses, and other income' + (hasExtra ? ' • ★ = a month with an extra paycheck' : '');

    // 1. Monthly income by person (stacked)
    if (chartIncomeMonthly) chartIncomeMonthly.destroy();
    chartIncomeMonthly = new Chart(document.getElementById('chartIncomeMonthly').getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: d.series_names.map(name => ({
                label: name, data: d.monthly.map(m => m.series[name] || 0),
                backgroundColor: incomeSeriesColor(name), stack: 'income', borderRadius: 3
            }))
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
                legend: def.legend,
                tooltip: {
                    callbacks: {
                        label: c => ` ${c.dataset.label}: ${formatCurrency(c.raw)}`,
                        afterBody: items => {
                            const m = d.monthly[items[0].dataIndex];
                            return m.extra_paycheck_sources.length ? `Extra paycheck: ${m.extra_paycheck_sources.join(', ')}` : '';
                        }
                    }
                }
            },
            scales: def.scales(true)
        }
    });

    // 2. Paycheck trend per source; bonuses drawn as separate markers
    const dates = Array.from(new Set(d.trend.flatMap(s => s.points.map(p => p.date)))).sort();
    const trendSets = [];
    d.trend.filter(s => s.points.length).forEach((s, i) => {
        const color = incomeSeriesColor(`${s.person || s.name} regular`);
        const reg = new Map(s.points.filter(p => p.subtype !== 'bonus').map(p => [p.date, p.amount]));
        const bon = new Map(s.points.filter(p => p.subtype === 'bonus').map(p => [p.date, p.amount]));
        trendSets.push({
            label: `${s.person || ''} ${s.name}`.trim(), data: dates.map(x => (reg.has(x) ? reg.get(x) : null)),
            borderColor: color, backgroundColor: color, borderWidth: 2.5, tension: 0.25, pointRadius: 3, spanGaps: true
        });
        if (bon.size) {
            trendSets.push({
                label: `${s.person || ''} bonus`.trim(), data: dates.map(x => (bon.has(x) ? bon.get(x) : null)),
                showLine: false, borderColor: '#f59e0b', backgroundColor: '#f59e0b', pointStyle: 'rectRot', pointRadius: 7
            });
        }
    });
    if (chartIncomeTrend) chartIncomeTrend.destroy();
    chartIncomeTrend = new Chart(document.getElementById('chartIncomeTrend').getContext('2d'), {
        type: 'line',
        data: { labels: dates.map(formatDisplayDate), datasets: trendSets },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: def.legend, tooltip: { callbacks: { label: c => ` ${c.dataset.label}: ${formatCurrency(c.raw)}` } } },
            scales: def.scales(false)
        }
    });

    // 3. Income above zero, spending below it, and the month's net as a line through the middle of each pair
    const net = d.monthly.map(m => Math.round((m.income - m.spend) * 100) / 100);
    const netColor = '#3b82f6';
    const t = def.t;
    const axis = def.scales(true);
    axis.y.ticks.callback = v => (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString();
    axis.y.grid = { color: ctx => (ctx.tick.value === 0 ? t.tickColor : t.gridColor), lineWidth: ctx => (ctx.tick.value === 0 ? 2 : 1) };
    if (chartIncomeVsSpend) chartIncomeVsSpend.destroy();
    chartIncomeVsSpend = new Chart(document.getElementById('chartIncomeVsSpend').getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: [
                { label: 'Income', data: d.monthly.map(m => m.income), backgroundColor: '#10b981', stack: 'flow', borderRadius: 3, order: 2 },
                { label: 'Spending', data: d.monthly.map(m => -m.spend), backgroundColor: '#ef4444', stack: 'flow', borderRadius: 3, order: 2 },
                {
                    type: 'line', label: 'Net', data: net, order: 1, borderColor: netColor, backgroundColor: netColor,
                    borderWidth: 3, tension: 0.25, pointRadius: 5, pointHoverRadius: 7, stack: 'net',
                    pointBorderColor: t.borderColor, pointBorderWidth: 2
                }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
                legend: def.legend,
                tooltip: {
                    callbacks: {
                        label: c => c.dataset.label === 'Spending' ? ` Spending: ${formatCurrency(-c.raw)}`
                            : c.dataset.label === 'Net' ? ` Net: ${formatSigned(c.raw)} (${c.raw >= 0 ? 'saved' : 'overspent'})`
                            : ` ${c.dataset.label}: ${formatCurrency(c.raw)}`
                    }
                }
            },
            scales: axis
        }
    });
}

// ---------------------------------------------------------------------
// People and pay sources
// ---------------------------------------------------------------------
function scheduleText(s) {
    if (s.cadence_freq === 'once') return 'One time';
    const every = s.cadence_interval === 1 ? `Every ${FREQ_LABELS[s.cadence_freq]}` : `Every ${s.cadence_interval} ${FREQ_LABELS[s.cadence_freq]}s`;
    return s.cadence_anchor_date ? `${every}, e.g. ${formatDisplayDate(s.cadence_anchor_date)}` : every;
}

function renderIncomeManager() {
    document.getElementById('peopleBody').innerHTML = incomePeople.map(p => `<tr class="${p.active ? '' : 'excluded-row'}">
        <td><span class="person-dot" style="background:${p.color || incomeSeriesColor(p.name + ' regular')}"></span>${escapeHtml(p.name)}</td>
        <td>${p.active ? 'Active' : 'Hidden'}</td>
        <td style="text-align:right;"><button type="button" class="btn-inline-action btn-inline-cancel" onclick="openPersonModal(${p.id})">Edit</button></td></tr>`).join('');

    document.getElementById('sourcesBody').innerHTML = incomeSources.length === 0
        ? '<tr><td colspan="7" class="import-more">No pay sources yet. Add one so paychecks get tagged to a person.</td></tr>'
        : incomeSources.map(s => `<tr class="${s.active ? '' : 'excluded-row'}">
            <td>${escapeHtml(s.person_name || '')}</td>
            <td><div style="font-weight:500;">${escapeHtml(s.name)}</div><div class="import-file-sub"><code>${escapeHtml(s.match_phrase)}</code></div></td>
            <td>${escapeHtml(scheduleText(s))}</td>
            <td style="text-align:right;">${s.typical ? formatCurrency(s.typical) : '—'}<div class="import-file-sub">${s.typical_source === 'manual' ? 'set by you' : s.typical ? `median of ${Math.min(s.regular_count, 6)} paydays` : 'no paychecks yet'}</div></td>
            <td style="text-align:right;">${s.monthly_amount ? formatCurrency(s.monthly_amount) : '—'}</td>
            <td>${s.next_pay_date ? formatDisplayDate(s.next_pay_date) : '—'}</td>
            <td style="text-align:right;"><button type="button" class="btn-inline-action btn-inline-cancel" onclick="openSourceModal(${s.id})">Edit</button></td></tr>`).join('');

    document.getElementById('planningLine').innerHTML =
        `Plan around <strong>${formatCurrency(incomePlanning.monthly_total)}</strong> a month` +
        (Object.keys(incomePlanning.by_person).length > 1
            ? ' (' + Object.entries(incomePlanning.by_person).map(([k, v]) => `${escapeHtml(k)} ${formatCurrency(v)}`).join(', ') + ')' : '') +
        '. That is each active source\'s typical regular paycheck × paychecks per year ÷ 12.';
}

function openPersonModal(id) {
    const p = incomePeople.find(x => x.id === id) || { id: null, name: '', color: '', active: 1 };
    document.getElementById('personId').value = p.id || '';
    document.getElementById('personName').value = p.name;
    document.getElementById('personColor').value = p.color || '#3b82f6';
    document.getElementById('personUseColor').checked = !!p.color;
    document.getElementById('personActive').checked = !!p.active;
    document.getElementById('personModalTitle').textContent = p.id ? 'Edit person' : 'Add person';
    document.getElementById('personModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    document.getElementById('personName').focus();
}

function closePersonModal() {
    document.getElementById('personModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

async function savePerson() {
    try {
        await apiPost('/api/people/save', {
            id: document.getElementById('personId').value || null,
            name: document.getElementById('personName').value,
            color: document.getElementById('personUseColor').checked ? document.getElementById('personColor').value : '',
            active: document.getElementById('personActive').checked ? 1 : 0
        });
        closePersonModal();
        await refreshTransactions();   // person pickers elsewhere
        await loadIncome();
        showToast('Person saved.');
    } catch (err) {
        showToast(err.message, true);
    }
}

function openSourceModal(id) {
    const s = incomeSources.find(x => x.id === id);
    const isNew = !s;
    document.getElementById('sourceId').value = s ? s.id : '';
    document.getElementById('sourcePerson').innerHTML = incomePeople.filter(p => p.active || (s && s.person_id === p.id))
        .map(p => `<option value="${p.id}" ${s && s.person_id === p.id ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('');
    document.getElementById('sourceName').value = s ? s.name : '';
    document.getElementById('sourcePhrase').value = s ? s.match_phrase : '';
    document.getElementById('sourceFreq').value = s ? s.cadence_freq : 'weekly';
    document.getElementById('sourceInterval').value = s ? s.cadence_interval : 2;
    document.getElementById('sourceAnchor').value = s && s.cadence_anchor_date ? s.cadence_anchor_date : '';
    document.getElementById('sourceTypical').value = s && s.typical_source === 'manual' ? s.typical : '';
    document.getElementById('sourceThreshold').value = s && s.bonus_threshold_pct ? s.bonus_threshold_pct : '';
    document.getElementById('sourceActive').checked = s ? !!s.active : true;
    document.getElementById('sourceApply').checked = isNew;
    document.getElementById('sourceModalTitle').textContent = isNew ? 'Add pay source' : 'Edit pay source';
    document.getElementById('sourcePreview').textContent = '';
    document.getElementById('sourceModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    if (s) previewSourcePhrase();
}

function closeSourceModal() {
    document.getElementById('sourceModalOverlay').classList.remove('active');
    if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

// Shows how many saved deposits the match text would pick up, before saving
function previewSourcePhrase() {
    clearTimeout(sourcePreviewTimer);
    sourcePreviewTimer = setTimeout(async () => {
        const phrase = document.getElementById('sourcePhrase').value.trim();
        const box = document.getElementById('sourcePreview');
        if (!phrase) { box.textContent = ''; return; }
        try {
            const r = await api('/api/income/sources/preview?phrase=' + encodeURIComponent(phrase));
            box.innerHTML = r.count === 0
                ? 'No saved deposits match this text yet.'
                : `Matches <strong>${r.count}</strong> saved deposit${r.count === 1 ? '' : 's'} totalling ${formatCurrency(r.total)}. Latest: ` +
                  r.examples.slice(0, 3).map(e => `${formatDisplayDate(e.date)} ${formatCurrency(e.amount)}`).join(', ');
        } catch (err) { box.textContent = ''; }
    }, 300);
}

function onSourceFreqChange() {
    const once = document.getElementById('sourceFreq').value === 'once';
    document.getElementById('sourceIntervalWrap').style.display = once ? 'none' : '';
}

async function saveSource() {
    try {
        const res = await apiPost('/api/income/sources/save', {
            id: document.getElementById('sourceId').value || null,
            person_id: document.getElementById('sourcePerson').value,
            name: document.getElementById('sourceName').value,
            match_phrase: document.getElementById('sourcePhrase').value,
            cadence_freq: document.getElementById('sourceFreq').value,
            cadence_interval: document.getElementById('sourceInterval').value,
            cadence_anchor_date: document.getElementById('sourceAnchor').value,
            typical_amount: document.getElementById('sourceTypical').value,
            bonus_threshold_pct: document.getElementById('sourceThreshold').value,
            active: document.getElementById('sourceActive').checked ? 1 : 0,
            apply_to_existing: document.getElementById('sourceApply').checked
        });
        closeSourceModal();
        await refreshTransactions();
        await loadIncome();
        showToast(res.retagged ? `Saved. ${res.retagged} existing deposit${res.retagged === 1 ? '' : 's'} re-tagged.` : 'Pay source saved.');
    } catch (err) {
        showToast(err.message, true);
    }
}

async function deleteSource() {
    const id = document.getElementById('sourceId').value;
    if (!id || !confirm('Remove this pay source? If it has paychecks it is switched off instead, so history stays.')) return;
    try {
        const res = await apiPost('/api/income/sources/delete', { id });
        closeSourceModal();
        await loadIncome();
        showToast(res.deactivated ? 'Switched off (it has paycheck history).' : 'Pay source removed.');
    } catch (err) {
        showToast(err.message, true);
    }
}


// ---------------------------------------------------------------------
// Income transactions (same timeframe as the rest of the tab)
// ---------------------------------------------------------------------
let incomeTxnFilter = 'all';
let incomeTxnSort = { col: 'date', asc: false };
let incomeTxnLimit = 100;

// An edit made anywhere (e.g. in the dialog opened from this table) refreshes the tab
document.addEventListener('txnsrefreshed', () => { if (incomeLoaded && activeTab === 'income') loadIncome(); });

function setIncomeTxnFilter(f) {
    incomeTxnFilter = f;
    incomeTxnLimit = 100;
    document.getElementById('incomeFilterAll').classList.toggle('active', f === 'all');
    document.getElementById('incomeFilterPaycheck').classList.toggle('active', f === 'paycheck');
    document.getElementById('incomeFilterOther').classList.toggle('active', f === 'other_income');
    renderIncomeTxns();
}

function sortIncomeTxns(col) {
    if (incomeTxnSort.col === col) incomeTxnSort.asc = !incomeTxnSort.asc;
    else incomeTxnSort = { col, asc: col !== 'date' && col !== 'amount' };
    ['date', 'name', 'person', 'type', 'amount'].forEach(c => {
        const el = document.getElementById('isortinc_' + c);
        if (el) el.textContent = c === incomeTxnSort.col ? (incomeTxnSort.asc ? '▲' : '▼') : '';
    });
    renderIncomeTxns();
}

function incomeTxnType(t) {
    if (t.txn_kind === 'paycheck') return t.income_subtype === 'bonus' ? 'Bonus' : 'Paycheck';
    return 'Other income' + (t.income_label ? ' · ' + t.income_label : '');
}

function renderIncomeTxns() {
    const from = document.getElementById('incomeStart').value;
    const to = document.getElementById('incomeEnd').value;
    const rows = allTransactions.filter(t =>
        t.is_included === 1 && INCOME_KINDS.includes(t.txn_kind) &&
        (!from || t.date >= from) && (!to || t.date <= to) &&
        (incomeTxnFilter === 'all' || t.txn_kind === incomeTxnFilter));
    const key = { date: t => t.date, name: t => (t.name || '').toLowerCase(), person: t => personName(t.person_id).toLowerCase(),
        type: t => incomeTxnType(t).toLowerCase(), amount: t => t.amount }[incomeTxnSort.col];
    const dir = incomeTxnSort.asc ? 1 : -1;
    rows.sort((a, b) => (key(a) < key(b) ? -dir : key(a) > key(b) ? dir : 0));

    const total = rows.reduce((sum, t) => sum + t.amount, 0);
    document.getElementById('incomeTxnCount').textContent =
        `${rows.length.toLocaleString()} transaction${rows.length === 1 ? '' : 's'} • ${formatCurrency(total)}`;

    const shown = rows.slice(0, incomeTxnLimit);
    document.getElementById('incomeTxnBody').innerHTML = shown.length === 0
        ? '<tr><td colspan="5" class="import-more">No income in this timeframe.</td></tr>'
        : shown.map(t => `<tr data-id="${escapeHtml(t.txn_id)}" onclick="openEditModal(this.dataset.id)">
            <td style="white-space:nowrap;">${escapeHtml(formatDisplayDate(t.date))}</td>
            <td><div style="font-weight:500;">${escapeHtml(t.name)}</div>${t.raw_name && t.raw_name !== t.name ? `<div style="font-size:11px;color:var(--text-dim);">${escapeHtml(t.raw_name)}</div>` : ''}</td>
            <td>${escapeHtml(personName(t.person_id)) || '<span class="import-muted">Not set</span>'}</td>
            <td>${escapeHtml(incomeTxnType(t))}${t.needs_review ? ' <span class="kind-badge kind-review">Review</span>' : ''} ${fillBadgeMarkup(t)}</td>
            <td class="amount-cell amount-income">${formatSigned(t.amount)}</td></tr>`).join('');

    const more = document.getElementById('incomeTxnMore');
    if (rows.length > shown.length) {
        more.innerHTML = `<button type="button" class="btn-bulk-action" onclick="incomeTxnLimit += 200; renderIncomeTxns();">Show more (${rows.length - shown.length} hidden)</button>`;
    } else {
        more.textContent = rows.length ? 'All matching transactions shown.' : '';
    }
}
