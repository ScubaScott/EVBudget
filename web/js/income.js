/* Income tab: who gets paid what over time, the monthly figure to budget from, and the people / pay sources behind it. */

let incomeData = null;
let incomeSources = [];
let incomePlanning = null;
let incomePeople = [];
let incomeLoaded = false;
let chartIncomeMonthly = null;
let chartIncomeTrend = null;
let chartIncomeVsSpend = null;
let sourcePreviewTimer = null;

const FREQ_LABELS = { weekly: 'week', monthly: 'month', yearly: 'year', once: 'once' };

// The Income view is part of the Spending & Income tab, so these only matter while it is showing
function incomeViewActive() { return activeTab === 'analytics' && analyticsView === 'income'; }

function showIncomeView() {
    incomeLoaded = true;
    loadIncome();
}

document.addEventListener('tabchange', (e) => { if (e.detail.tab === 'analytics' && analyticsView === 'income') showIncomeView(); });
document.addEventListener('analyticsviewchange', (e) => { if (e.detail.view === 'income' && activeTab === 'analytics') showIncomeView(); });
document.addEventListener('themechange', () => { if (incomeData && incomeViewActive()) renderIncomeCharts(); });
// An edit made anywhere (e.g. in the transaction dialog) refreshes the income numbers
document.addEventListener('txnsrefreshed', () => { if (incomeLoaded && incomeViewActive()) loadIncome(); });

document.addEventListener('DOMContentLoaded', () => {
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { closePersonModal(); closeSourceModal(); }
    });
});

// ---------------------------------------------------------------------
// Filters: the Income view uses the same filter bar as Spending. Its dates pick the range for the charts.
// ---------------------------------------------------------------------
let incomeRangeLoaded = '';

function incomeRange() {
    let from = document.getElementById('filterStartDate').value;
    let to = document.getElementById('filterEndDate').value;
    if (!from) {   // "All time" leaves the dates blank: start at the first transaction
        const dates = allTransactions.map(t => t.date).sort();
        from = dates.length ? dates[0] : `${new Date().getFullYear()}-01-01`;
    }
    return { from, to: to || toInputDateFormat(new Date()) };
}

document.addEventListener('filterschanged', () => {
    if (!incomeLoaded || !incomeViewActive()) return;
    const r = incomeRange();
    if (`${r.from}|${r.to}` !== incomeRangeLoaded) loadIncome();   // new dates: charts need fresh numbers
});

// ---------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------
async function loadIncome() {
    const { from, to } = incomeRange();
    incomeRangeLoaded = `${from}|${to}`;
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
        loadPayDiscovery();
    } catch (err) {
        showToast('Could not load income: ' + err.message, true);
    }
}

// ---------------------------------------------------------------------
// Deposits we noticed: steady deposits that aren't a pay source yet
// ---------------------------------------------------------------------
let payDiscoverList = [];

function payScheduleText(freq, interval) {
    const unit = FREQ_LABELS[freq] || freq;
    return interval > 1 ? `Every ${interval} ${unit}s` : `Every ${unit}`;
}

async function loadPayDiscovery() {
    try {
        payDiscoverList = (await api('/api/income/discover')).candidates;
    } catch (err) {
        payDiscoverList = [];
    }
    document.getElementById('payDiscoverCard').hidden = payDiscoverList.length === 0;
    document.getElementById('payDiscoverBody').innerHTML = payDiscoverList.map((c, i) => `<tr>
        <td><div style="font-weight:500;">${escapeHtml(c.name)}</div><div style="font-size:11px;color:var(--text-dim);">Matches "${escapeHtml(c.match_phrase)}"</div></td>
        <td>${payScheduleText(c.freq, c.interval)}${c.confidence === 'high' ? '' : ' <span class="kind-badge kind-review" title="Less certain: fewer deposits or a less even schedule than a clear paycheck. Check it before making it a pay source.">Check</span>'}</td>
        <td class="amount-cell amount-income">${formatCurrency(c.typical_amount)}</td>
        <td>${c.deposits} (last ${formatDisplayDate(c.last_date)})</td>
        <td>${c.next_date ? formatDisplayDate(c.next_date) : '&mdash;'}</td>
        <td style="white-space:nowrap; text-align:right;">
            <button type="button" class="btn-refresh" onclick="makeSourceFromDiscovered(${i})">Make pay source</button>
            <button type="button" class="btn-refresh" onclick="dismissPayDiscovered(${i})" title="This isn't a paycheck">Dismiss</button>
        </td></tr>`).join('');
}

function makeSourceFromDiscovered(i) {
    const c = payDiscoverList[i];
    if (!c) return;
    openSourceModal(null, {
        name: c.suggested_name, match_phrase: c.match_phrase, cadence_freq: c.freq,
        cadence_interval: c.interval, cadence_anchor_date: c.last_date
    });
}

async function dismissPayDiscovered(i) {
    const c = payDiscoverList[i];
    if (!c) return;
    try {
        await apiPost('/api/income/discover/dismiss', { key: c.key });
        loadPayDiscovery();
    } catch (err) {
        showToast(err.message, true);
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

const NEW_PERSON = '__new';

// prefill: starting values for a new source (from "Deposits we noticed")
function openSourceModal(id, prefill) {
    const s = incomeSources.find(x => x.id === id);
    const isNew = !s;
    const f = s || prefill || {};
    const people = incomePeople.filter(p => p.active || (s && s.person_id === p.id));
    document.getElementById('sourceId').value = s ? s.id : '';
    document.getElementById('sourcePerson').innerHTML = people
        .map(p => `<option value="${p.id}" ${s && s.person_id === p.id ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')
        + `<option value="${NEW_PERSON}" ${people.length === 0 ? 'selected' : ''}>+ New person&hellip;</option>`;
    document.getElementById('sourceNewPerson').value = '';
    onSourcePersonChange();
    document.getElementById('sourceName').value = f.name || '';
    document.getElementById('sourcePhrase').value = f.match_phrase || '';
    document.getElementById('sourceFreq').value = f.cadence_freq || 'weekly';
    document.getElementById('sourceInterval').value = f.cadence_interval || 2;
    document.getElementById('sourceAnchor').value = f.cadence_anchor_date || '';
    onSourceFreqChange();
    document.getElementById('sourceTypical').value = s && s.typical_source === 'manual' ? s.typical : '';
    document.getElementById('sourceThreshold').value = s && s.bonus_threshold_pct ? s.bonus_threshold_pct : '';
    document.getElementById('sourceActive').checked = s ? !!s.active : true;
    document.getElementById('sourceApply').checked = isNew;
    document.getElementById('sourceModalTitle').textContent = isNew ? 'Add pay source' : 'Edit pay source';
    document.getElementById('sourcePreview').textContent = '';
    document.getElementById('sourceModalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
    if (f.match_phrase) previewSourcePhrase();
}

function onSourcePersonChange() {
    const isNew = document.getElementById('sourcePerson').value === NEW_PERSON;
    const input = document.getElementById('sourceNewPerson');
    input.hidden = !isNew;
    if (isNew) setTimeout(() => input.focus(), 50);
}

// The person picked in the dialog. "+ New person" creates them first.
async function sourcePersonId() {
    const sel = document.getElementById('sourcePerson');
    if (sel.value !== NEW_PERSON) return sel.value;
    const name = document.getElementById('sourceNewPerson').value.trim();
    if (!name) throw new Error('Type the name of the person this pay is for.');
    const res = await apiPost('/api/people/save', { name, color: '', active: 1 });
    incomePeople.push({ id: res.id, name, color: '', active: 1 });
    // Keep the new person selected, so a retry after a failed save doesn't add them twice
    sel.insertAdjacentHTML('afterbegin', `<option value="${res.id}">${escapeHtml(name)}</option>`);
    sel.value = String(res.id);
    onSourcePersonChange();
    return String(res.id);
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
        const personId = await sourcePersonId();
        const res = await apiPost('/api/income/sources/save', {
            id: document.getElementById('sourceId').value || null,
            person_id: personId,
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
