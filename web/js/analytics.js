function updateChartThemeStyles() {
    const themeCols = getThemeChartColors();

    if (chartIncomeSpendInstance) {
        if (chartIncomeSpendInstance.options.scales.x) {
            chartIncomeSpendInstance.options.scales.x.ticks.color = currentTheme === 'ledger' ? '#1b2620' : '#f8fafc';
        }
        if (chartIncomeSpendInstance.options.scales.y) {
            chartIncomeSpendInstance.options.scales.y.ticks.color = themeCols.tickColor;
            chartIncomeSpendInstance.options.scales.y.grid.color = themeCols.gridColor;
        }
        chartIncomeSpendInstance.update();
    }

    if (chartCategorySpendInstance) {
        if (chartCategorySpendInstance.options.plugins && chartCategorySpendInstance.options.plugins.legend) {
            chartCategorySpendInstance.options.plugins.legend.labels.color = themeCols.tickColor;
        }
        if (chartCategorySpendInstance.data.datasets[0]) {
            chartCategorySpendInstance.data.datasets[0].borderColor = themeCols.borderColor;
        }
        chartCategorySpendInstance.update();
    }

    if (chartTimeSpendInstance) {
        if (chartTimeSpendInstance.options.plugins && chartTimeSpendInstance.options.plugins.legend) {
            chartTimeSpendInstance.options.plugins.legend.labels.color = themeCols.tickColor;
        }
        if (chartTimeSpendInstance.options.scales.x) {
            chartTimeSpendInstance.options.scales.x.ticks.color = themeCols.tickColor;
            chartTimeSpendInstance.options.scales.x.grid.color = themeCols.gridSubtleColor;
        }
        if (chartTimeSpendInstance.options.scales.y) {
            chartTimeSpendInstance.options.scales.y.ticks.color = themeCols.tickColor;
            chartTimeSpendInstance.options.scales.y.grid.color = themeCols.gridColor;
        }
        chartTimeSpendInstance.update();
    }
}


// Master state holders
let allTransactions = [];
let goLiveDate = null;
let filteredTransactions = [];
let configuredEnvelopesList = [];
let activePreset = "last_30_days";
let inclusionFilter = "included"; // 'included' | 'all' | 'excluded'
let currentSortColumn = "date";
let currentSortDirection = "desc";
let granularityOverride = null; // null for auto, or 'daily' | 'weekly' | 'monthly'
let activeEditingTxnId = null;

// Chart view toggles and drill-down state
let breakdownMode = "category"; // 'category' | 'payee'
let drilldownCategory = null;   // null for all categories, or specific envelope name
let spendingStackMode = "total"; // 'total' | 'stacked' | 'percent'

// Shared color palette for consistent category colors across all charts
const COLOR_PALETTE = [
    '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
    '#14b8a6', '#f97316', '#06b6d4', '#6366f1', '#84cc16',
    '#d946ef', '#0ea5e9', '#eab308', '#a855f7', '#64748b'
];
const envelopeColorMap = {};

function getEnvelopeColor(name) {
    if (!envelopeColorMap[name]) {
        const colorIdx = Object.keys(envelopeColorMap).length % COLOR_PALETTE.length;
        envelopeColorMap[name] = COLOR_PALETTE[colorIdx];
    }
    return envelopeColorMap[name];
}

// Audit engine state queue
let auditCandidatesQueue = [];
let currentAuditIndex = 0;

// Infinite scroll pagination state
const PAGE_CHUNK_SIZE = 50;
let renderedRecordCount = 0;

// Chart.js references
let chartIncomeSpendInstance = null;
let chartCategorySpendInstance = null;
let chartTimeSpendInstance = null;

// Income and spending are decided by each transaction's kind, not by the sign of the amount.
function isIncomeKind(txn) {
    return INCOME_KINDS.includes(txn.txn_kind);
}

// Expenses add to spending. Refunds and reimbursements take it back out. Transfers and card payments are neither.
function netSpendAmount(txn) {
    if (txn.txn_kind === 'expense' || txn.txn_kind === 'refund') return -txn.amount;
    return 0;
}

document.addEventListener('themechange', updateChartThemeStyles);

document.addEventListener('DOMContentLoaded', () => {
    reloadTransactionData();
    setupScrollListener();
    setupKeyboardShortcuts();
    setupTableRowClickListener();
    setupRegexHelpDismissListener();
});




// Setup Escape key to close open modals and regex help popover
function setupKeyboardShortcuts() {
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeEditModal();
            closeAuditModal();
            closeSettingsModal();
            closeBulkModal();
            closeRegexHelp();
        }
    });
}

// Toggles the regular expression syntax help tooltip popover
function toggleRegexHelp(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const popover = document.getElementById('regexPopover');
    const btn = document.getElementById('btnRegexHelp');
    if (popover) {
        const isOpen = popover.classList.toggle('active');
        if (btn) btn.classList.toggle('active', isOpen);
    }
}

// Explicitly dismisses the regular expression syntax tooltip popover
function closeRegexHelp() {
    const popover = document.getElementById('regexPopover');
    const btn = document.getElementById('btnRegexHelp');
    if (popover) popover.classList.remove('active');
    if (btn) btn.classList.remove('active');
}

// Closes regex help popover when clicking anywhere outside of its container
function setupRegexHelpDismissListener() {
    document.addEventListener('click', (e) => {
        const wrap = document.getElementById('regexTooltipWrap');
        if (wrap && !wrap.contains(e.target)) {
            closeRegexHelp();
        }
    });
}

// Event delegation listener for opening transaction edit modal
function setupTableRowClickListener() {
    const tbody = document.getElementById('transactionTableBody');
    tbody.addEventListener('click', (e) => {
        // Do not trigger modal if clicking inside the checkbox column or on a checkbox
        if (e.target.closest('.row-select-cell') || e.target.type === 'checkbox') {
            return;
        }
        const tr = e.target.closest('tr');
        if (tr && tr.hasAttribute('data-id')) {
            openEditModal(tr.getAttribute('data-id'));
        }
    });
}

// A paycheck that was split across envelopes can't be deleted or excluded until the fill is undone.
async function postTxnWithVoidPrompt(url, payload) {
    const send = async (body) => {
        const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        return res.json();
    };
    let result = await send(payload);
    if (result && result.needs_void && confirm(result.error + '\n\nUndo that fill and continue?')) {
        result = await send(Object.assign({}, payload, { void_allocation: true }));
    }
    return result;
}

// Reloads transactions from the server but keeps the current filters, so edits don't reset the view
async function refreshTransactions() {
    try {
        const res = await fetch('/api/transactions');
        const data = await res.json();
        allTransactions = data.transactions || [];
        goLiveDate = data.go_live_date || null;
        configuredEnvelopesList = data.envelopes || configuredEnvelopesList;
        peopleList = data.people || peopleList;
        incomeSourcesList = data.income_sources || incomeSourcesList;
        populateEnvelopeDropdowns();
        populatePeopleDropdowns();
        onFilterChange();
        document.dispatchEvent(new CustomEvent('txnsrefreshed'));
    } catch (err) {
        console.error("Failed to refresh transactions:", err);
        showToast("Could not refresh data from the server", true);
    }
}

// Fetches all transactions and metadata from the local Python backend API
async function reloadTransactionData() {
    try {
        const res = await fetch('/api/transactions');
        const data = await res.json();
        allTransactions = data.transactions || [];
        goLiveDate = data.go_live_date || null;
        configuredEnvelopesList = data.envelopes || [];
        peopleList = data.people || [];
        incomeSourcesList = data.income_sources || [];
        populateEnvelopeDropdowns();
        populatePeopleDropdowns();
        applyPreset('last_30_days');
        document.dispatchEvent(new CustomEvent('txnsrefreshed'));
    } catch (err) {
        console.error("Failed to load transactions:", err);
        showToast("Failed to connect to backend", true);
    }
}

// Populates both filter, edit modal, and bulk action envelope dropdowns dynamically
function populateEnvelopeDropdowns() {
    const distinctEnvs = new Set(configuredEnvelopesList);
    allTransactions.forEach(t => {
        if (t.envelope && t.envelope.trim().length > 0) {
            distinctEnvs.add(t.envelope.trim());
        }
    });
    const sortedEnvs = Array.from(distinctEnvs).sort((a, b) => a.localeCompare(b));

    // Populate Filter dropdown
    const filterSelect = document.getElementById('filterEnvelope');
    const currentFilterVal = filterSelect.value;
    filterSelect.innerHTML = '<option value="">All Envelopes</option>';
    filterSelect.innerHTML += '<option value="__UNALLOCATED__">Unallocated (Inflows / Blank)</option>';
    sortedEnvs.forEach(env => {
        const opt = document.createElement('option');
        opt.value = env;
        opt.textContent = env;
        filterSelect.appendChild(opt);
    });
    filterSelect.value = currentFilterVal;

    // Populate Modal Edit dropdown
    const editSelect = document.getElementById('editEnvelope');
    editSelect.innerHTML = '<option value="">(Leave Blank for Inflows / Unallocated)</option>';
    sortedEnvs.forEach(env => {
        const opt = document.createElement('option');
        opt.value = env;
        opt.textContent = env;
        editSelect.appendChild(opt);
    });

    // Populate Bulk Action Envelope dropdown
    const bulkSelect = document.getElementById('bulkEnvelopeSelect');
    if (bulkSelect) {
        bulkSelect.innerHTML = '<option value="">(Blank / Unallocated)</option>';
        sortedEnvs.forEach(env => {
            const opt = document.createElement('option');
            opt.value = env;
            opt.textContent = env;
            bulkSelect.appendChild(opt);
        });
    }
}

// Fills the person pickers (filter bar, edit dialog, bulk dialog)
function populatePeopleDropdowns() {
    const options = peopleList.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');

    const filterSelect = document.getElementById('filterPerson');
    const current = filterSelect.value;
    filterSelect.innerHTML = '<option value="">Everyone</option>' + options;
    filterSelect.value = current;

    document.getElementById('editPerson').innerHTML = '<option value="">(Not set)</option>' + options;
    document.getElementById('bulkPersonSelect').innerHTML = '<option value="">(Person not set)</option>' + options;
}

// Jumps to spending and refunds since go-live that have no envelope
function goToUncategorized() {
    showTab('analytics');
    setInclusionFilter('included');
    document.getElementById('filterSearch').value = '';
    document.getElementById('filterPerson').value = '';
    document.getElementById('filterKind').value = '';
    applyPreset('all_time');
    document.getElementById('filterEnvelope').value = '__UNALLOCATED__';
    if (goLiveDate) {
        document.getElementById('filterStartDate').value = goLiveDate;
        onCustomDateChange();
    } else {
        onFilterChange();
    }
}

// Jumps to the transactions that still need a decision (unmatched Venmo, unknown deposits, ...)
function goToNeedsReview() {
    showTab('analytics');
    setInclusionFilter('included');
    document.getElementById('filterSearch').value = '';
    document.getElementById('filterEnvelope').value = '';
    document.getElementById('filterPerson').value = '';
    document.getElementById('filterKind').value = '__NEEDS_REVIEW__';
    applyPreset('all_time');
}

// Preset Timeframe Handlers
function applyPreset(presetKey) {
    activePreset = presetKey;
    granularityOverride = null;

    // Update UI buttons
    document.querySelectorAll('.preset-btn').forEach(btn => {
        if (btn.getAttribute('data-preset') === presetKey) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    const today = new Date();
    const startInput = document.getElementById('filterStartDate');
    const endInput = document.getElementById('filterEndDate');

    let startDate = null;
    let endDate = null;

    if (presetKey === 'this_week') {
        const day = today.getDay();
        const diffToMon = (day === 0 ? -6 : 1) - day;
        startDate = new Date(today);
        startDate.setDate(today.getDate() + diffToMon);
        startDate.setHours(0, 0, 0, 0);

        endDate = new Date(startDate);
        endDate.setDate(startDate.getDate() + 6);
        endDate.setHours(23, 59, 59, 999);
    } else if (presetKey === 'last_week') {
        const day = today.getDay();
        const diffToMon = (day === 0 ? -6 : 1) - day - 7;
        startDate = new Date(today);
        startDate.setDate(today.getDate() + diffToMon);
        startDate.setHours(0, 0, 0, 0);

        endDate = new Date(startDate);
        endDate.setDate(startDate.getDate() + 6);
        endDate.setHours(23, 59, 59, 999);
    } else if (presetKey === 'this_month') {
        startDate = new Date(today.getFullYear(), today.getMonth(), 1);
        endDate = new Date(today.getFullYear(), today.getMonth() + 1, 0, 23, 59, 59, 999);
    } else if (presetKey === 'last_month') {
        startDate = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        endDate = new Date(today.getFullYear(), today.getMonth(), 0, 23, 59, 59, 999);
    } else if (presetKey === 'this_year') {
        startDate = new Date(today.getFullYear(), 0, 1);
        endDate = new Date(today.getFullYear(), 11, 31, 23, 59, 59, 999);
    } else if (presetKey === 'last_year') {
        startDate = new Date(today.getFullYear() - 1, 0, 1);
        endDate = new Date(today.getFullYear() - 1, 11, 31, 23, 59, 59, 999);
    } else if (presetKey === 'last_7_days') {
        endDate = new Date(today);
        endDate.setHours(23, 59, 59, 999);
        startDate = new Date(today);
        startDate.setDate(today.getDate() - 6);
        startDate.setHours(0, 0, 0, 0);
    } else if (presetKey === 'last_30_days') {
        endDate = new Date(today);
        endDate.setHours(23, 59, 59, 999);
        startDate = new Date(today);
        startDate.setDate(today.getDate() - 29);
        startDate.setHours(0, 0, 0, 0);
    } else if (presetKey === 'ytd') {
        startDate = new Date(today.getFullYear(), 0, 1);
        endDate = new Date(today);
        endDate.setHours(23, 59, 59, 999);
    } else if (presetKey === 'last_365_days') {
        endDate = new Date(today);
        endDate.setHours(23, 59, 59, 999);
        startDate = new Date(today);
        startDate.setDate(today.getDate() - 364);
        startDate.setHours(0, 0, 0, 0);
    } else if (presetKey === 'all_time') {
        startDate = null;
        endDate = null;
    }

    if (startDate) {
        startInput.value = toInputDateFormat(startDate);
    } else {
        startInput.value = '';
    }

    if (endDate) {
        endInput.value = toInputDateFormat(endDate);
    } else {
        endInput.value = '';
    }

    onFilterChange();
}

// Handles manual input changes on the Start and End date pickers
function onCustomDateChange() {
    activePreset = "custom";
    document.querySelectorAll('.preset-btn').forEach(btn => btn.classList.remove('active'));
    onFilterChange();
}

// Controls inclusion filter status toggle
function setInclusionFilter(type) {
    inclusionFilter = type;
    document.getElementById('btnIncOnly').classList.toggle('active', type === 'included');
    document.getElementById('btnIncAll').classList.toggle('active', type === 'all');
    document.getElementById('btnIncExcluded').classList.toggle('active', type === 'excluded');
    onFilterChange();
}

// Sets the time granularity override for chart 3
function setGranularity(granularity) {
    granularityOverride = granularity;
    updateGranularityToggleButtons(granularity);
    updateSpendingOverTimeChart();
}

function updateGranularityToggleButtons(activeGran) {
    document.getElementById('btnGranDaily').classList.toggle('active', activeGran === 'daily');
    document.getElementById('btnGranWeekly').classList.toggle('active', activeGran === 'weekly');
    document.getElementById('btnGranMonthly').classList.toggle('active', activeGran === 'monthly');
}

// Clears search payee, envelope filter, and resets inclusion to 'included' while preserving timeframe
function clearFilterCriteria() {
    const searchInput = document.getElementById('filterSearch');
    if (searchInput) {
        searchInput.value = '';
        searchInput.classList.remove('regex-invalid');
        searchInput.title = '';
    }
    const envelopeSelect = document.getElementById('filterEnvelope');
    if (envelopeSelect) {
        envelopeSelect.value = '';
    }
    document.getElementById('filterKind').value = '';
    document.getElementById('filterPerson').value = '';
    closeRegexHelp();
    setInclusionFilter('included');
}

// Backward compatibility alias for clear action
const resetAllFilters = clearFilterCriteria;

// Central filter calculation that updates KPIs, Charts, and Table
function onFilterChange() {
    const searchInput = document.getElementById('filterSearch');
    const searchRaw = searchInput ? searchInput.value.trim() : '';
    const envelopeVal = document.getElementById('filterEnvelope').value;
    const kindVal = document.getElementById('filterKind').value;
    const personVal = document.getElementById('filterPerson').value;
    const startDateStr = document.getElementById('filterStartDate').value;
    const endDateStr = document.getElementById('filterEndDate').value;

    let startDateObj = startDateStr ? new Date(startDateStr + 'T00:00:00') : null;
    let endDateObj = endDateStr ? new Date(endDateStr + 'T23:59:59.999') : null;

    // Prepare regular expression search matching
    let searchRegex = null;
    let isRegexValid = true;
    let searchLower = '';

    if (searchRaw.length > 0) {
        try {
            // Compile case-insensitive regular expression
            searchRegex = new RegExp(searchRaw, 'i');
        } catch (e) {
            // Safe fallback to literal substring search if syntax is incomplete or invalid
            isRegexValid = false;
            searchLower = searchRaw.toLowerCase();
        }
    }

    // Visual feedback on search input when user enters invalid regex
    if (searchInput) {
        if (searchRaw.length > 0 && !isRegexValid) {
            searchInput.classList.add('regex-invalid');
            searchInput.title = 'Incomplete or invalid regular expression. Falling back to plain text search.';
        } else {
            searchInput.classList.remove('regex-invalid');
            searchInput.title = '';
        }
    }

    filteredTransactions = allTransactions.filter(txn => {
        // Inclusion filter check
        if (inclusionFilter === 'included' && txn.is_included !== 1) return false;
        if (inclusionFilter === 'excluded' && txn.is_included === 1) return false;

        // Date range check
        if (startDateObj || endDateObj) {
            const txnDate = parseTxnDate(txn.date);
            if (startDateObj && txnDate < startDateObj) return false;
            if (endDateObj && txnDate > endDateObj) return false;
        }

        // Kind and person checks
        if (kindVal === '__NEEDS_REVIEW__') {
            if (!txn.needs_review) return false;
        } else if (kindVal === '__INCOME__') {
            if (!isIncomeKind(txn)) return false;
        } else if (kindVal && txn.txn_kind !== kindVal) {
            return false;
        }
        if (personVal && String(txn.person_id || '') !== personVal) return false;

        // Envelope check
        if (envelopeVal === '__UNALLOCATED__') {
            if (txn.envelope && txn.envelope.trim().length > 0) return false;
        } else if (envelopeVal && envelopeVal.length > 0) {
            if (txn.envelope !== envelopeVal) return false;
        }

        // Search query check supporting regular expressions and case-insensitive matching
        if (searchRaw.length > 0) {
            const targetFields = [
                txn.name || '',
                txn.raw_name || '',
                txn.envelope || '',
                txn.txn_id || '',
                txn.check_num || '',
                txn.account_type || '',
                txn.exclusion_reason || '',
                txn.income_label || '',
                KIND_LABELS[txn.txn_kind] || ''
            ];

            if (searchRegex) {
                // Test individual fields to support anchors (^, $) or combined string for cross-field phrases
                const combinedText = targetFields.join(' ');
                const isMatch = targetFields.some(field => searchRegex.test(field)) || searchRegex.test(combinedText);
                if (!isMatch) return false;
            } else {
                // Plain text fallback match
                const combinedText = targetFields.join(' ').toLowerCase();
                if (!combinedText.includes(searchLower)) return false;
            }
        }

        return true;
    });

    // Sort filtered transactions
    sortFilteredTransactions();

    // Refresh KPIs, Charts, and reset Table Infinite Scroll
    updateKPIs();
    updateCharts();
    resetAndRenderTable();
}

// Sorts the filteredTransactions list according to currentSortColumn & currentSortDirection
function sortFilteredTransactions() {
    filteredTransactions.sort((a, b) => {
        let valA = a[currentSortColumn];
        let valB = b[currentSortColumn];

        if (currentSortColumn === 'date') {
            valA = parseTxnDate(valA).getTime();
            valB = parseTxnDate(valB).getTime();
        } else if (currentSortColumn === 'amount') {
            valA = parseFloat(valA);
            valB = parseFloat(valB);
        } else {
            valA = (valA || '').toString().toLowerCase();
            valB = (valB || '').toString().toLowerCase();
        }

        if (valA < valB) return currentSortDirection === 'asc' ? -1 : 1;
        if (valA > valB) return currentSortDirection === 'asc' ? 1 : -1;
        return 0;
    });
}

// Handles clicking on a table column header
function sortTable(columnKey) {
    if (currentSortColumn === columnKey) {
        currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        currentSortColumn = columnKey;
        currentSortDirection = 'asc';
    }

    // Update header sort indicator arrows
    document.querySelectorAll('.sort-indicator').forEach(el => el.textContent = '');
    const activeIndicator = document.getElementById(`sort_${columnKey}`);
    if (activeIndicator) {
        activeIndicator.textContent = currentSortDirection === 'asc' ? '▲' : '▼';
    }

    sortFilteredTransactions();
    resetAndRenderTable();
}

// Calculates metrics and updates KPI summary tiles
function updateKPIs() {
    let totalIncome = 0.0;
    let totalSpend = 0.0;
    let incomeCount = 0;
    let spendCount = 0;

    filteredTransactions.forEach(txn => {
        if (isIncomeKind(txn)) {
            totalIncome += txn.amount;
            incomeCount++;
        } else if (txn.txn_kind === 'expense' || txn.txn_kind === 'refund') {
            totalSpend += netSpendAmount(txn);
            if (txn.txn_kind === 'expense') spendCount++;
        }
    });

    const netSavings = totalIncome - totalSpend;
    const savingsRate = totalIncome > 0 ? ((netSavings / totalIncome) * 100).toFixed(1) : 0;

    document.getElementById('kpiTotalIncome').textContent = formatCurrency(totalIncome);
    document.getElementById('kpiIncomeCount').textContent = `${incomeCount} income deposits`;

    document.getElementById('kpiTotalSpend').textContent = formatCurrency(totalSpend);
    document.getElementById('kpiSpendCount').textContent = `${spendCount} expense records`;

    const netEl = document.getElementById('kpiNetSavings');
    netEl.textContent = formatCurrency(netSavings);
    if (netSavings >= 0) {
        netEl.style.color = "var(--accent-teal)";
    } else {
        netEl.style.color = "var(--accent-red)";
    }

    const rateBadge = document.getElementById('kpiSavingsRate');
    rateBadge.textContent = `${savingsRate}% Saved`;
    if (netSavings >= 0) {
        rateBadge.className = "kpi-badge badge-positive";
    } else {
        rateBadge.className = "kpi-badge badge-negative";
    }

    document.getElementById('kpiFilteredCount').textContent = filteredTransactions.length.toLocaleString();
    document.getElementById('kpiTotalDatabaseCount').textContent = `Total in localstore: ${allTransactions.length.toLocaleString()}`;
}

// Updates all three interactive charts
function updateCharts() {
    updateIncomeVsSpendChart();
    updateCategorySpendChart();
    updateSpendingOverTimeChart();
}

// Chart 1: Income vs Spend
function updateIncomeVsSpendChart() {
    let totalIncome = 0.0;
    let totalSpend = 0.0;

    filteredTransactions.forEach(txn => {
        if (isIncomeKind(txn)) totalIncome += txn.amount;
        totalSpend += netSpendAmount(txn);
    });

    const themeCols = getThemeChartColors();
    const ctx = document.getElementById('chartIncomeSpend').getContext('2d');
    if (chartIncomeSpendInstance) chartIncomeSpendInstance.destroy();

    chartIncomeSpendInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['Total Income', 'Total Spend'],
            datasets: [{
                data: [totalIncome, totalSpend],
                backgroundColor: ['#10b981', '#ef4444'],
                borderRadius: 8,
                barPercentage: 0.55
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            onClick: (event, elements) => {
                if (!elements || elements.length === 0) return;
                const elementIndex = elements[0].index;
                const label = chartIncomeSpendInstance.data.labels[elementIndex];
                if (label === 'Total Income') {
                    document.getElementById('filterEnvelope').value = '';
                    document.getElementById('filterKind').value = '__INCOME__';
                    onFilterChange();
                    showToast('Filtered to income (paychecks and other income)');
                } else if (label === 'Total Spend') {
                    document.getElementById('filterEnvelope').value = '';
                    document.getElementById('filterKind').value = '';
                    onFilterChange();
                    showToast('Showing all transactions');
                }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => ` ${formatCurrency(ctx.raw)}`
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: themeCols.gridColor },
                    ticks: {
                        color: themeCols.tickColor,
                        callback: (val) => '$' + val.toLocaleString()
                    }
                },
                x: {
                    grid: { display: false },
                    ticks: {
                        color: currentTheme === 'ledger' ? '#1b2620' : '#f8fafc',
                        font: { weight: '600' }
                    }
                }
            }
        }
    });
}

// Controls Chart 2 Breakdown Mode (Categories vs Top Payees)
function setBreakdownMode(mode) {
    breakdownMode = mode;
    drilldownCategory = null;
    document.getElementById('btnBreakdownCategory').classList.toggle('active', mode === 'category');
    document.getElementById('btnBreakdownPayee').classList.toggle('active', mode === 'payee');
    updateCategorySpendChart();
}

// Exits category-specific payee drill-down back to all categories
function exitCategoryDrilldown() {
    drilldownCategory = null;
    updateCategorySpendChart();
}

// Controls Chart 3 Spending Over Time Stack Mode (Total, Stacked, Percent)
function setSpendingStackMode(mode) {
    spendingStackMode = mode;
    document.getElementById('btnStackTotal').classList.toggle('active', mode === 'total');
    document.getElementById('btnStackCategory').classList.toggle('active', mode === 'stacked');
    document.getElementById('btnStackPercent').classList.toggle('active', mode === 'percent');
    updateSpendingOverTimeChart();
}

// Chart 2: Spend Breakdown (Category vs Payee & Drilldown)
function updateCategorySpendChart() {
    const totals = {};
    const titleEl = document.getElementById('chartBreakdownTitle');
    const subtitleEl = document.getElementById('chartBreakdownSubtitle');
    const backBtn = document.getElementById('btnBackToCategories');

    if (drilldownCategory) {
        // Mode: Specific Category drilled down into its payees
        titleEl.textContent = `${drilldownCategory}: Top Payees`;
        subtitleEl.textContent = `Top payees in ${drilldownCategory} • Click slice to filter`;
        backBtn.style.display = 'inline-flex';

        filteredTransactions.forEach(txn => {
            if (txn.envelope === drilldownCategory) {
                const spend = netSpendAmount(txn);
                if (spend !== 0) {
                    const payee = (txn.name && txn.name.trim().length > 0) ? txn.name.trim() : 'Unknown Payee';
                    totals[payee] = (totals[payee] || 0) + spend;
                }
            }
        });
    } else if (breakdownMode === 'payee') {
        // Mode: All Top Payees across all categories
        titleEl.textContent = 'Spend by Top Payees';
        subtitleEl.textContent = 'Top 10 payees by total spend • Click to filter';
        backBtn.style.display = 'none';

        filteredTransactions.forEach(txn => {
            const spend = netSpendAmount(txn);
            if (spend !== 0) {
                const payee = (txn.name && txn.name.trim().length > 0) ? txn.name.trim() : 'Unknown Payee';
                totals[payee] = (totals[payee] || 0) + spend;
            }
        });
    } else {
        // Mode: Standard Category Breakdown
        titleEl.textContent = 'Spend by Category';
        subtitleEl.textContent = 'Expense distribution • Click slice to drill down into payees';
        backBtn.style.display = 'none';

        filteredTransactions.forEach(txn => {
            const spend = netSpendAmount(txn);
            if (spend !== 0) {
                const envelope = txn.envelope && txn.envelope.trim().length > 0 ? txn.envelope.trim() : 'Unallocated';
                totals[envelope] = (totals[envelope] || 0) + spend;
            }
        });
    }

    // Sort descending and cluster beyond top 10 into 'Other'
    const sortedItems = Object.keys(totals)
        .map(k => ({ name: k, amount: Math.max(0, totals[k]) }))
        .filter(item => item.amount > 0)
        .sort((a, b) => b.amount - a.amount);

    let displayItems = [];
    if (sortedItems.length > 10) {
        const top10 = sortedItems.slice(0, 10);
        const otherSum = sortedItems.slice(10).reduce((acc, curr) => acc + curr.amount, 0);
        displayItems = top10;
        if (otherSum > 0) {
            displayItems.push({ name: 'Other', amount: otherSum });
        }
    } else {
        displayItems = sortedItems;
    }

    const labels = displayItems.map(c => c.name);
    const dataVals = displayItems.map(c => c.amount);

    // Assign colors (using consistent envelope colors if in category mode)
    const colors = displayItems.map((item, idx) => {
        if (!drilldownCategory && breakdownMode === 'category') {
            return getEnvelopeColor(item.name);
        }
        return COLOR_PALETTE[idx % COLOR_PALETTE.length];
    });

    const themeCols = getThemeChartColors();
    const ctx = document.getElementById('chartCategorySpend').getContext('2d');
    if (chartCategorySpendInstance) chartCategorySpendInstance.destroy();

    chartCategorySpendInstance = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: labels.length > 0 ? labels : ['No Spending'],
            datasets: [{
                data: dataVals.length > 0 ? dataVals : [1],
                backgroundColor: dataVals.length > 0 ? colors : ['#334155'],
                borderWidth: 2,
                borderColor: themeCols.borderColor
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            onClick: (event, elements) => {
                if (!elements || elements.length === 0) return;
                const elementIndex = elements[0].index;
                const selectedLabel = chartCategorySpendInstance.data.labels[elementIndex];
                if (!selectedLabel || selectedLabel === 'No Spending') return;

                if (!drilldownCategory && breakdownMode === 'category') {
                    // Category slice clicked: drill down into payees for that category!
                    if (selectedLabel !== 'Unallocated' && selectedLabel !== 'Other') {
                        drilldownCategory = selectedLabel;
                        updateCategorySpendChart();
                        showToast(`Drilled into payees for: ${selectedLabel}`);
                    } else {
                        const selectEl = document.getElementById('filterEnvelope');
                        selectEl.value = '__UNALLOCATED__';
                        onFilterChange();
                        showToast('Filtered to Unallocated');
                    }
                } else {
                    // Payee slice clicked: filter table to this payee
                    if (selectedLabel !== 'Other') {
                        document.getElementById('filterSearch').value = selectedLabel;
                        onFilterChange();
                        showToast(`Filtered to Payee: ${selectedLabel}`);
                    }
                }
            },
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        color: themeCols.tickColor,
                        boxWidth: 12,
                        padding: 8,
                        font: { size: 11 }
                    }
                },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            if (dataVals.length === 0) return ' No expense data';
                            const total = dataVals.reduce((a, b) => a + b, 0);
                            const pct = total > 0 ? ((ctx.raw / total) * 100).toFixed(1) : 0;
                            return ` ${ctx.label}: ${formatCurrency(ctx.raw)} (${pct}%)`;
                        }
                    }
                }
            },
            cutout: '68%'
        }
    });
}

// Chart 3: Spending Over Time (Supports Total, Stacked by Category, and 100% Stacked)
function updateSpendingOverTimeChart() {
    let granularity = granularityOverride;
    if (!granularity) {
        if (activePreset === 'last_7_days' || activePreset === 'this_week' || activePreset === 'last_week' || activePreset === 'last_30_days') {
            granularity = 'daily';
        } else if (activePreset === 'this_month' || activePreset === 'last_month') {
            granularity = 'weekly';
        } else {
            granularity = 'monthly';
        }
    }

    updateGranularityToggleButtons(granularity);

    // Group spend by interval bucket and by envelope
    const timeBuckets = {};
    const activeEnvelopesInTimeframe = new Set();

    filteredTransactions.forEach(txn => {
        const hasEnvelope = txn.envelope && txn.envelope.trim().length > 0;
        const netSpend = netSpendAmount(txn);

        if (netSpend !== 0) {
            const d = parseTxnDate(txn.date);
            let bucketKey = '';

            if (granularity === 'daily') {
                bucketKey = toInputDateFormat(d);
            } else if (granularity === 'weekly') {
                const day = d.getDay();
                const diff = (day === 0 ? -6 : 1) - day;
                const mon = new Date(d);
                mon.setDate(d.getDate() + diff);
                bucketKey = 'Wk ' + toInputDateFormat(mon);
            } else {
                const y = d.getFullYear();
                const m = String(d.getMonth() + 1).padStart(2, '0');
                bucketKey = `${y}-${m}`;
            }

            const env = hasEnvelope ? txn.envelope.trim() : 'Unallocated';
            activeEnvelopesInTimeframe.add(env);

            if (!timeBuckets[bucketKey]) {
                timeBuckets[bucketKey] = { total: 0, byEnv: {} };
            }
            timeBuckets[bucketKey].total += netSpend;
            timeBuckets[bucketKey].byEnv[env] = (timeBuckets[bucketKey].byEnv[env] || 0) + netSpend;
        }
    });

    const sortedKeys = Object.keys(timeBuckets).sort();
    const sortedEnvs = Array.from(activeEnvelopesInTimeframe).sort();

    const subtitleEl = document.getElementById('chartTimeSubtitle');
    let datasets = [];
    let chartScales = {};
    const themeCols = getThemeChartColors();

    if (spendingStackMode === 'total') {
        // Mode 1: Single Bar per period + Trendline
        const spendValues = sortedKeys.map(k => Math.max(0, timeBuckets[k].total));
        const trendValues = [];
        for (let i = 0; i < spendValues.length; i++) {
            const start = Math.max(0, i - 1);
            const end = Math.min(spendValues.length, i + 2);
            const subset = spendValues.slice(start, end);
            const avg = subset.reduce((a, b) => a + b, 0) / subset.length;
            trendValues.push(avg);
        }

        subtitleEl.textContent = `Total Spend (${granularity}) • Click bar to drill down`;
        datasets = [
            {
                type: 'bar',
                label: 'Spend',
                data: spendValues.length > 0 ? spendValues : [0],
                backgroundColor: 'rgba(59, 130, 246, 0.65)',
                borderColor: '#3b82f6',
                borderWidth: 1,
                borderRadius: 6,
                order: 2
            },
            {
                type: 'line',
                label: 'Spend Trend',
                data: trendValues.length > 0 ? trendValues : [0],
                borderColor: '#f59e0b',
                backgroundColor: 'transparent',
                borderWidth: 2.5,
                tension: 0.35,
                pointRadius: 3,
                pointBackgroundColor: '#f59e0b',
                order: 1
            }
        ];

        chartScales = {
            y: {
                beginAtZero: true,
                grid: { color: themeCols.gridColor },
                ticks: {
                    color: themeCols.tickColor,
                    callback: (val) => '$' + val.toLocaleString()
                }
            },
            x: {
                grid: { color: themeCols.gridSubtleColor },
                ticks: { color: themeCols.tickColor, maxRotation: 45, minRotation: 0 }
            }
        };
    } else if (spendingStackMode === 'stacked') {
        // Mode 2: Stacked by Category
        subtitleEl.textContent = `Category Stacked (${granularity}) • Click bar to drill down`;

        sortedEnvs.forEach(env => {
            const dataVals = sortedKeys.map(k => Math.max(0, timeBuckets[k].byEnv[env] || 0));
            datasets.push({
                type: 'bar',
                label: env,
                data: dataVals,
                backgroundColor: getEnvelopeColor(env),
                stack: 'spendStack',
                borderRadius: 3
            });
        });

        chartScales = {
            x: {
                stacked: true,
                grid: { color: themeCols.gridSubtleColor },
                ticks: { color: themeCols.tickColor, maxRotation: 45, minRotation: 0 }
            },
            y: {
                stacked: true,
                beginAtZero: true,
                grid: { color: themeCols.gridColor },
                ticks: {
                    color: themeCols.tickColor,
                    callback: (val) => '$' + val.toLocaleString()
                }
            }
        };
    } else {
        // Mode 3: 100% Normalized Stacked (% Breakdown)
        subtitleEl.textContent = `100% Category Breakdown (${granularity}) • Click bar to drill down`;

        sortedEnvs.forEach(env => {
            const dataVals = sortedKeys.map(k => {
                const total = timeBuckets[k].total;
                const envAmt = timeBuckets[k].byEnv[env] || 0;
                return total > 0 ? parseFloat(((envAmt / total) * 100).toFixed(2)) : 0;
            });

            datasets.push({
                type: 'bar',
                label: env,
                data: dataVals,
                backgroundColor: getEnvelopeColor(env),
                stack: 'spendPercentStack',
                borderRadius: 2
            });
        });

        chartScales = {
            x: {
                stacked: true,
                grid: { color: themeCols.gridSubtleColor },
                ticks: { color: themeCols.tickColor, maxRotation: 45, minRotation: 0 }
            },
            y: {
                stacked: true,
                min: 0,
                max: 100,
                grid: { color: themeCols.gridColor },
                ticks: {
                    color: themeCols.tickColor,
                    callback: (val) => val + '%'
                }
            }
        };
    }

    const ctx = document.getElementById('chartTimeSpend').getContext('2d');
    if (chartTimeSpendInstance) chartTimeSpendInstance.destroy();

    chartTimeSpendInstance = new Chart(ctx, {
        data: {
            labels: sortedKeys.length > 0 ? sortedKeys : ['No Activity'],
            datasets: datasets.length > 0 ? datasets : [{ label: 'Spend', data: [0], backgroundColor: '#3b82f6' }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            onClick: (event, elements) => {
                if (!elements || elements.length === 0) return;
                const elementIndex = elements[0].index;
                const datasetIndex = elements[0].datasetIndex;
                const label = chartTimeSpendInstance.data.labels[elementIndex];
                if (!label || label === 'No Activity') return;

                // If in stacked mode and clicked an envelope segment, also filter by that envelope
                if (spendingStackMode !== 'total' && chartTimeSpendInstance.data.datasets[datasetIndex]) {
                    const clickedEnv = chartTimeSpendInstance.data.datasets[datasetIndex].label;
                    if (clickedEnv && clickedEnv !== 'Spend' && clickedEnv !== 'Spend Trend') {
                        const selectEl = document.getElementById('filterEnvelope');
                        selectEl.value = clickedEnv === 'Unallocated' ? '__UNALLOCATED__' : clickedEnv;
                    }
                }

                applyTimeBarFilter(label, granularity);
            },
            plugins: {
                legend: {
                    display: spendingStackMode !== 'total',
                    position: 'top',
                    labels: {
                        color: themeCols.tickColor,
                        boxWidth: 11,
                        padding: 8,
                        font: { size: 11 }
                    }
                },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            if (spendingStackMode === 'percent') {
                                return ` ${ctx.dataset.label}: ${ctx.raw}%`;
                            }
                            return ` ${ctx.dataset.label}: ${formatCurrency(ctx.raw)}`;
                        }
                    }
                }
            },
            scales: chartScales
        }
    });
}

// Applies a clicked bar timeframe filter to zoom into a specific month, week, or day
function applyTimeBarFilter(label, currentGranularity) {
    const startInput = document.getElementById('filterStartDate');
    const endInput = document.getElementById('filterEndDate');

    // Deactivate all preset buttons
    document.querySelectorAll('.preset-btn').forEach(btn => btn.classList.remove('active'));
    activePreset = "custom";

    if (currentGranularity === 'monthly') {
        // Label format: YYYY-MM (e.g. "2026-07")
        const parts = label.split('-');
        if (parts.length === 2) {
            const year = parseInt(parts[0], 10);
            const month = parseInt(parts[1], 10);
            const startDate = new Date(year, month - 1, 1);
            const endDate = new Date(year, month, 0, 23, 59, 59, 999);

            startInput.value = toInputDateFormat(startDate);
            endInput.value = toInputDateFormat(endDate);

            // Auto-switch granularity to weekly for a deeper view inside the selected month
            granularityOverride = 'weekly';
            showToast(`Filtered to Month: ${label}`);
        }
    } else if (currentGranularity === 'weekly') {
        // Label format: "Wk YYYY-MM-DD" (e.g. "Wk 2026-07-06")
        const dateStr = label.replace(/^Wk\s+/, '').trim();
        const monday = new Date(dateStr + 'T00:00:00');
        if (!isNaN(monday.getTime())) {
            const sunday = new Date(monday);
            sunday.setDate(monday.getDate() + 6);
            sunday.setHours(23, 59, 59, 999);

            startInput.value = toInputDateFormat(monday);
            endInput.value = toInputDateFormat(sunday);

            // Auto-switch granularity to daily for day-by-day inspection
            granularityOverride = 'daily';
            showToast(`Filtered to Week: ${toInputDateFormat(monday)} to ${toInputDateFormat(sunday)}`);
        }
    } else if (currentGranularity === 'daily') {
        // Label format: YYYY-MM-DD (e.g. "2026-07-06")
        startInput.value = label;
        endInput.value = label;
        showToast(`Filtered to Day: ${label}`);
    }

    onFilterChange();
}

// Infinite Scroll Table Rendering Logic
function resetAndRenderTable() {
    const tbody = document.getElementById('transactionTableBody');
    tbody.innerHTML = '';
    renderedRecordCount = 0;
    appendNextTableChunk();
    updateBulkActionBar();
}

function appendNextTableChunk() {
    const tbody = document.getElementById('transactionTableBody');
    const total = filteredTransactions.length;
    if (renderedRecordCount >= total) {
        document.getElementById('scrollSentinel').textContent =
            total === 0 ? "No transactions match your current filter." : "All matching records loaded.";
        return;
    }

    const nextChunk = filteredTransactions.slice(renderedRecordCount, renderedRecordCount + PAGE_CHUNK_SIZE);
    const fragment = document.createDocumentFragment();

    nextChunk.forEach(txn => {
        const tr = document.createElement('tr');
        tr.setAttribute('data-id', txn.txn_id);

        const isSelected = selectedTxnIds.has(txn.txn_id);
        if (isSelected) {
            tr.classList.add('selected-row');
        }

        if (txn.is_included === 0) {
            tr.classList.add('excluded-row');
        }

        let amountClass = 'amount-spend';
        let formattedAmount = formatSigned(txn.amount);
        const hasEnvelope = txn.envelope && txn.envelope.trim().length > 0;

        if (isIncomeKind(txn)) {
            amountClass = 'amount-income';
        } else if (txn.txn_kind === 'refund') {
            amountClass = 'amount-refund';
            formattedAmount += ' (Refund)';
        } else if (EXCLUDED_KINDS.includes(txn.txn_kind)) {
            amountClass = 'amount-muted';
        }

        let envelopeMarkup = hasEnvelope
            ? `<span class="envelope-badge">${escapeHtml(txn.envelope)}</span>`
            : `<span class="envelope-badge unallocated">Unallocated</span>`;
        envelopeMarkup += kindBadgeMarkup(txn);

        const statusMarkup = txn.is_included === 1
            ? `<span class="status-pill status-included">Included</span>`
            : `<span class="status-pill status-excluded" title="${escapeHtml(txn.exclusion_reason)}">Excluded</span>`;

        tr.innerHTML = `
            <td class="row-select-cell" style="text-align: center;" onclick="event.stopPropagation()">
                <input type="checkbox" class="txn-checkbox row-select-cb" value="${escapeHtml(txn.txn_id)}" ${isSelected ? 'checked' : ''} onchange="onRowSelectChange(this, '${escapeHtml(txn.txn_id)}')">
            </td>
            <td style="white-space: nowrap;">${escapeHtml(formatDisplayDate(txn.date))}</td>
            <td>
                <div style="font-weight: 500;">${escapeHtml(txn.name)}</div>
                ${txn.raw_name && txn.raw_name !== txn.name ? `<div style="font-size: 11px; color: var(--text-dim);">${escapeHtml(txn.raw_name)}</div>` : ''}
            </td>
            <td>${envelopeMarkup}</td>
            <td style="color: var(--text-muted); font-size: 12.5px;">${escapeHtml(txn.account_type || '—')}</td>
            <td class="amount-cell ${amountClass}">${formattedAmount}</td>
            <td>${statusMarkup}</td>
            <td style="font-family: monospace; font-size: 11.5px; color: var(--text-dim);">${escapeHtml(txn.txn_id)}</td>
        `;

        fragment.appendChild(tr);
    });

    tbody.appendChild(fragment);
    renderedRecordCount += nextChunk.length;

    document.getElementById('tableRecordCountDisplay').textContent =
        `Showing ${renderedRecordCount} of ${total.toLocaleString()} records`;

    if (renderedRecordCount >= total) {
        document.getElementById('scrollSentinel').textContent = "All matching records loaded.";
    } else {
        document.getElementById('scrollSentinel').textContent = "Scroll down to load next 50 records...";
    }
}

// Does this deposit count toward envelope fills (income, included, on or after go-live)?
function isFillable(txn) {
    return !!goLiveDate && INCOME_KINDS.includes(txn.txn_kind) && txn.is_included === 1 && txn.date >= goLiveDate;
}

// A clickable badge that opens the deposit's fill: view or edit it if funded, create it if not
function fillBadgeMarkup(txn) {
    if (!isFillable(txn)) return '';
    const id = escapeHtml(txn.txn_id);
    return txn.funded
        ? `<button type="button" class="kind-badge kind-refund kind-link" title="View or edit how this deposit was split across envelopes" onclick="event.stopPropagation(); openAllocModal('${id}')">Funded &rsaquo;</button>`
        : `<button type="button" class="kind-badge kind-review kind-link" title="Split this deposit across envelopes" onclick="event.stopPropagation(); openAllocModal('${id}')">To fill &rsaquo;</button>`;
}

// Shows the fill link inside the transaction editor
function refreshEditFill(txn) {
    const wrap = document.getElementById('editFillWrap');
    const show = isFillable(txn);
    wrap.style.display = show ? '' : 'none';
    if (!show) return;
    document.getElementById('editFillInfo').textContent = txn.funded
        ? 'This deposit has been split across envelopes.' : 'This deposit has not been split across envelopes yet.';
    document.getElementById('editFillBtn').textContent = txn.funded ? 'View or edit fill\u2026' : 'Fill envelopes\u2026';
}

function openFillFromEditModal() {
    const id = activeEditingTxnId;
    closeEditModal();
    if (id) openAllocModal(id);
}

// Small labels under the envelope: who got paid, what kind of money this is, and whether it needs a look
function kindBadgeMarkup(txn) {
    const bits = [];
    if (txn.txn_kind === 'paycheck') {
        const who = personName(txn.person_id);
        bits.push(`<span class="kind-badge kind-paycheck">${txn.income_subtype === 'bonus' ? 'Bonus' : 'Paycheck'}${who ? ' &middot; ' + escapeHtml(who) : ''}</span>`);
    } else if (txn.txn_kind === 'other_income') {
        bits.push(`<span class="kind-badge kind-income">Other income${txn.income_label ? ' &middot; ' + escapeHtml(txn.income_label) : ''}</span>`);
    } else if (txn.txn_kind === 'refund' && txn.reimburses_txn_id) {
        bits.push(`<span class="kind-badge kind-refund">Reimbursement</span>`);
    } else if (EXCLUDED_KINDS.includes(txn.txn_kind)) {
        bits.push(`<span class="kind-badge kind-muted">${escapeHtml(KIND_LABELS[txn.txn_kind])}</span>`);
    }
    const fill = fillBadgeMarkup(txn);
    if (fill) bits.push(fill);
    if (txn.needs_review) {
        bits.push(`<span class="kind-badge kind-review" title="Needs a decision">Review</span>`);
    }
    return bits.length ? `<div class="kind-badges">${bits.join('')}</div>` : '';
}

// Window scroll listener for infinite scroll loading & back-to-top visibility
function setupScrollListener() {
    window.addEventListener('scroll', () => {
        const scrollPos = window.scrollY;
        const windowHeight = window.innerHeight;
        const docHeight = document.documentElement.scrollHeight;

        const btnTop = document.getElementById('btnBackToTop');
        if (scrollPos > 400) {
            btnTop.classList.add('visible');
        } else {
            btnTop.classList.remove('visible');
        }

        if ((windowHeight + scrollPos) >= (docHeight - 250)) {
            if (renderedRecordCount < filteredTransactions.length) {
                appendNextTableChunk();
            }
        }
    });
}

function scrollToTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// =====================================================================
// Modal Dialog Logic: View & Edit Transaction Details
// =====================================================================

function openEditModal(txnId) {
    const txn = allTransactions.find(t => t.txn_id === txnId);
    if (!txn) return;

    activeEditingTxnId = txnId;

    document.getElementById('modalTxnId').textContent = txn.txn_id;
    document.getElementById('modalSourceFile').textContent = txn.source_file || '—';
    document.getElementById('modalRawName').textContent = txn.raw_name || '—';
    document.getElementById('modalMatchSource').textContent = txn.match_source || 'Manual Edit';
    document.getElementById('modalUpdatedAt').textContent = txn.updated_at ? new Date(txn.updated_at).toLocaleString() : '—';

    document.getElementById('editName').value = txn.name || '';
    document.getElementById('editEnvelope').value = txn.envelope || '';
    document.getElementById('editAccountType').value = txn.account_type || 'Bank / Checking';
    document.getElementById('editAmount').value = txn.amount !== undefined ? txn.amount : 0.0;
    document.getElementById('editCheckNum').value = txn.check_num || '';
    document.getElementById('editExclusionReason').value = txn.exclusion_reason || '';

    document.getElementById('editKind').value = txn.txn_kind || 'expense';
    document.getElementById('editPerson').value = txn.person_id ? String(txn.person_id) : '';
    document.getElementById('editSubtype').value = txn.income_subtype || 'regular';
    document.getElementById('editIncomeLabel').value = txn.income_label || '';
    document.getElementById('editNeedsReview').checked = !!txn.needs_review;
    editReimbursesTxnId = txn.reimburses_txn_id || '';
    refreshEditKindFields();
    refreshEditFill(txn);

    const d = parseTxnDate(txn.date);
    document.getElementById('editDate').value = toInputDateFormat(d);

    const incCheckbox = document.getElementById('editIsIncluded');
    incCheckbox.checked = txn.is_included === 1;

    const modal = document.getElementById('editModalOverlay');
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
}

// Which expense the transaction being edited pays back (set with "Match to expense")
let editReimbursesTxnId = '';

// Shows only the fields that make sense for the chosen kind
function refreshEditKindFields() {
    const kind = document.getElementById('editKind').value;
    const isIncome = INCOME_KINDS.includes(kind);
    document.getElementById('editPersonWrap').style.display = isIncome ? '' : 'none';
    document.getElementById('editSubtypeWrap').style.display = kind === 'paycheck' ? '' : 'none';
    document.getElementById('editIncomeLabelWrap').style.display = kind === 'other_income' ? '' : 'none';
    document.getElementById('editReimburseWrap').style.display = kind === 'refund' ? '' : 'none';

    const info = document.getElementById('editReimburseInfo');
    const linked = editReimbursesTxnId ? allTransactions.find(t => t.txn_id === editReimbursesTxnId) : null;
    info.textContent = linked
        ? `${linked.name} (${formatDisplayDate(linked.date)}, ${formatCurrency(Math.abs(linked.amount))})`
        : (editReimbursesTxnId ? editReimbursesTxnId : 'Not linked');
}

// Changing the kind keeps the "included" checkbox in step: transfers and card payments are always excluded.
function onEditKindChange() {
    const kind = document.getElementById('editKind').value;
    const include = document.getElementById('editIsIncluded');
    if (EXCLUDED_KINDS.includes(kind)) {
        include.checked = false;
    } else {
        include.checked = true;
    }
    onInclusionCheckboxToggle();
    refreshEditKindFields();
}

function openMatchForEditModal() {
    const txn = allTransactions.find(t => t.txn_id === activeEditingTxnId);
    if (!txn) return;
    openMatchModal({ txnId: txn.txn_id, date: txn.date, amount: txn.amount, label: txn.name }, (cand) => {
        editReimbursesTxnId = cand.txn_id;
        document.getElementById('editEnvelope').value = cand.envelope || '';
        document.getElementById('editNeedsReview').checked = false;
        refreshEditKindFields();
    });
}

function clearEditReimbursement() {
    editReimbursesTxnId = '';
    refreshEditKindFields();
}

function closeEditModal() {
    const modal = document.getElementById('editModalOverlay');
    const wasOpen = modal.classList.contains('active');
    modal.classList.remove('active');
    document.body.style.overflow = '';
    activeEditingTxnId = null;
    if (wasOpen) document.dispatchEvent(new CustomEvent('editclosed'));
}

function handleModalOverlayClick(event) {
    if (event.target === document.getElementById('editModalOverlay')) {
        closeEditModal();
    }
}

function onInclusionCheckboxToggle() {
    const isIncluded = document.getElementById('editIsIncluded').checked;
    const reasonInput = document.getElementById('editExclusionReason');
    if (isIncluded) {
        reasonInput.value = '';
    } else if (!reasonInput.value) {
        reasonInput.value = 'User Excluded';
    }
}

function submitModalForm() {
    const form = document.getElementById('editTxnForm');
    if (!form.checkValidity()) {
        form.reportValidity();
        return;
    }
    saveTransactionEdits();
}

async function saveTransactionEdits(event) {
    if (event) event.preventDefault();
    if (!activeEditingTxnId) return;

    const saveBtn = document.getElementById('btnSaveModal');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    const formattedDate = document.getElementById('editDate').value;  // already YYYY-MM-DD

    const kind = document.getElementById('editKind').value;
    const updatePayload = {
        txn_id: activeEditingTxnId,
        name: document.getElementById('editName').value.trim(),
        envelope: document.getElementById('editEnvelope').value.trim(),
        account_type: document.getElementById('editAccountType').value,
        date: formattedDate,
        amount: parseFloat(document.getElementById('editAmount').value) || 0.0,
        check_num: document.getElementById('editCheckNum').value.trim(),
        is_included: document.getElementById('editIsIncluded').checked ? 1 : 0,
        exclusion_reason: document.getElementById('editExclusionReason').value.trim(),
        txn_kind: kind,
        person_id: INCOME_KINDS.includes(kind) ? (parseInt(document.getElementById('editPerson').value, 10) || null) : null,
        income_subtype: kind === 'paycheck' ? document.getElementById('editSubtype').value : null,
        income_label: kind === 'other_income' ? document.getElementById('editIncomeLabel').value.trim() : '',
        reimburses_txn_id: kind === 'refund' ? (editReimbursesTxnId || null) : null,
        needs_review: document.getElementById('editNeedsReview').checked ? 1 : 0
    };

    try {
        const result = await postTxnWithVoidPrompt('/api/transactions/update', updatePayload);
        if (result.success) {
            const idx = allTransactions.findIndex(t => t.txn_id === activeEditingTxnId);
            if (idx !== -1) {
                allTransactions[idx].name = updatePayload.name;
                allTransactions[idx].envelope = updatePayload.envelope;
                allTransactions[idx].account_type = updatePayload.account_type;
                allTransactions[idx].date = updatePayload.date;
                allTransactions[idx].amount = updatePayload.amount;
                allTransactions[idx].check_num = updatePayload.check_num;
                allTransactions[idx].is_included = updatePayload.is_included;
                allTransactions[idx].exclusion_reason = updatePayload.exclusion_reason;
                allTransactions[idx].updated_at = result.updated_at;
            }

            closeEditModal();
            showToast("Transaction updated successfully!");
            await refreshTransactions();
        } else {
            showToast("Save failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Save transaction error:", err);
        showToast("Save failed: Network error", true);
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Changes';
    }
}

// =====================================================================
// Audit Tool Engine: Duplicates & Verification Modal
// =====================================================================

function startAuditProcess() {
    auditCandidatesQueue = [];
    currentAuditIndex = 0;

    // 1. Find Exact Duplicates (Identical Date & Exact Amount)
    const mapByDateAndAmount = {};
    filteredTransactions.forEach(t => {
        const key = `${t.date}|${t.amount.toFixed(2)}`;
        if (!mapByDateAndAmount[key]) {
            mapByDateAndAmount[key] = [];
        }
        mapByDateAndAmount[key].push(t);
    });

    // Create pairwise duplicate candidates
    Object.keys(mapByDateAndAmount).forEach(key => {
        const group = mapByDateAndAmount[key];
        if (group.length > 1) {
            for (let i = 0; i < group.length - 1; i++) {
                for (let j = i + 1; j < group.length; j++) {
                    auditCandidatesQueue.push({
                        type: 'duplicate_pair',
                        itemA: group[i],
                        itemB: group[j]
                    });
                }
            }
        }
    });

    // 2. Find Uncategorized Expenses (Negative amount, is_included = 1, blank envelope)
    filteredTransactions.forEach(t => {
        const isExpense = t.amount < 0 && t.txn_kind === 'expense';
        const isBlankEnv = !t.envelope || t.envelope.trim().length === 0;
        if (isExpense && isBlankEnv && t.is_included === 1) {
            auditCandidatesQueue.push({
                type: 'uncategorized_expense',
                item: t
            });
        }
    });

    if (auditCandidatesQueue.length === 0) {
        showToast("Audit Complete: No duplicates or uncategorized transactions found in current view!");
        return;
    }

    openAuditModal();
}

function openAuditModal() {
    const modal = document.getElementById('auditModalOverlay');
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    renderCurrentAuditCandidate();
}

function closeAuditModal() {
    const modal = document.getElementById('auditModalOverlay');
    const wasOpen = modal.classList.contains('active');
    modal.classList.remove('active');
    document.body.style.overflow = '';
    auditCandidatesQueue = [];
    currentAuditIndex = 0;
    if (wasOpen) document.dispatchEvent(new CustomEvent('comparedclosed'));
}

function handleAuditOverlayClick(event) {
    if (event.target === document.getElementById('auditModalOverlay')) {
        closeAuditModal();
    }
}

function renderCurrentAuditCandidate() {
    const body = document.getElementById('auditModalBody');
    const footer = document.getElementById('auditModalFooter');

    if (currentAuditIndex >= auditCandidatesQueue.length) {
        body.innerHTML = `
            <div style="text-align: center; padding: 40px 20px; display: flex; flex-direction: column; align-items: center; gap: 14px;">
                <div style="font-size: 40px;">✅</div>
                <div style="font-size: 18px; font-weight: 700; color: var(--text-main);">Audit Review Complete</div>
                <div style="font-size: 13.5px; color: var(--text-muted); max-width: 440px;">
                    You have reviewed all flagged candidates for this filtered list.
                </div>
            </div>
        `;
        footer.innerHTML = `
            <div class="modal-footer-msg">Audit queue cleared.</div>
            <button class="btn-modal-save" onclick="closeAuditModal()">Done</button>
        `;
        return;
    }

    const current = auditCandidatesQueue[currentAuditIndex];
    const candidateNum = currentAuditIndex + 1;
    const totalCandidates = auditCandidatesQueue.length;

    if (current.type === 'duplicate_pair') {
        const a = current.itemA;
        const b = current.itemB;

        const diffName = (a.name || '').trim().toLowerCase() !== (b.name || '').trim().toLowerCase();
        const diffEnv = (a.envelope || '').trim().toLowerCase() !== (b.envelope || '').trim().toLowerCase();
        const diffAccount = (a.account_type || '').trim().toLowerCase() !== (b.account_type || '').trim().toLowerCase();
        const diffSource = (a.source_file || '').trim().toLowerCase() !== (b.source_file || '').trim().toLowerCase();

        body.innerHTML = `
            <div class="audit-progress-bar">
                <span class="audit-counter">Candidate ${candidateNum} of ${totalCandidates}</span>
                <span class="audit-type-tag">Exact Date & Amount Match</span>
            </div>

            <div class="audit-comparison-grid">
                <!-- Entry A Card -->
                <div class="audit-card">
                    <div class="audit-card-header">
                        <span class="audit-card-title">Entry A</span>
                        <span class="audit-card-badge mono">${escapeHtml(a.txn_id)}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Date & Amount</span>
                        <span class="audit-row-value" style="font-weight: 700; color: ${a.amount < 0 ? 'var(--text-main)' : 'var(--accent-green)'};">
                            ${escapeHtml(formatDisplayDate(a.date))} &bull; ${formatCurrency(a.amount)}
                        </span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Payee Name</span>
                        <span class="audit-row-value ${diffName ? 'diff-highlight' : ''}">${escapeHtml(a.name)}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Envelope</span>
                        <span class="audit-row-value ${diffEnv ? 'diff-highlight' : ''}">${escapeHtml(a.envelope || '(Unallocated)')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Account</span>
                        <span class="audit-row-value ${diffAccount ? 'diff-highlight' : ''}">${escapeHtml(a.account_type || '—')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Source File</span>
                        <span class="audit-row-value ${diffSource ? 'diff-highlight' : ''}">${escapeHtml(a.source_file || '—')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Raw Description</span>
                        <span class="audit-row-value" style="font-size: 11.5px; color: var(--text-dim);">${escapeHtml(a.raw_name || '—')}</span>
                    </div>

                    <button class="btn-keep-choice" onclick="resolveDuplicatePair('${a.txn_id}', '${b.txn_id}')">
                        Keep Entry A (Delete B)
                    </button>
                </div>

                <!-- Entry B Card -->
                <div class="audit-card">
                    <div class="audit-card-header">
                        <span class="audit-card-title">Entry B</span>
                        <span class="audit-card-badge mono">${escapeHtml(b.txn_id)}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Date & Amount</span>
                        <span class="audit-row-value" style="font-weight: 700; color: ${b.amount < 0 ? 'var(--text-main)' : 'var(--accent-green)'};">
                            ${escapeHtml(formatDisplayDate(b.date))} &bull; ${formatCurrency(b.amount)}
                        </span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Payee Name</span>
                        <span class="audit-row-value ${diffName ? 'diff-highlight' : ''}">${escapeHtml(b.name)}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Envelope</span>
                        <span class="audit-row-value ${diffEnv ? 'diff-highlight' : ''}">${escapeHtml(b.envelope || '(Unallocated)')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Account</span>
                        <span class="audit-row-value ${diffAccount ? 'diff-highlight' : ''}">${escapeHtml(b.account_type || '—')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Source File</span>
                        <span class="audit-row-value ${diffSource ? 'diff-highlight' : ''}">${escapeHtml(b.source_file || '—')}</span>
                    </div>

                    <div class="audit-row-item">
                        <span class="audit-row-label">Raw Description</span>
                        <span class="audit-row-value" style="font-size: 11.5px; color: var(--text-dim);">${escapeHtml(b.raw_name || '—')}</span>
                    </div>

                    <button class="btn-keep-choice" onclick="resolveDuplicatePair('${b.txn_id}', '${a.txn_id}')">
                        Keep Entry B (Delete A)
                    </button>
                </div>
            </div>
        `;

        footer.innerHTML = `
            <div style="display: flex; gap: 8px; align-items: center;">
                <button class="btn-modal-cancel" onclick="closeAuditModal()">Cancel & Exit</button>
                <button class="audit-nav-btn" ${currentAuditIndex === 0 ? 'disabled style="opacity:0.4;"' : ''} onclick="prevAuditCandidate()">← Prev</button>
                <button class="audit-nav-btn" onclick="nextAuditCandidate()">Skip →</button>
            </div>
            <div class="modal-actions">
                <button class="btn-keep-both" onclick="nextAuditCandidate()">Leave As Is (Keep Both)</button>
            </div>
        `;
    } else if (current.type === 'uncategorized_expense') {
        const item = current.item;
        body.innerHTML = `
            <div class="audit-progress-bar">
                <span class="audit-counter">Candidate ${candidateNum} of ${totalCandidates}</span>
                <span class="audit-type-tag" style="background: rgba(239, 68, 68, 0.18); color: var(--accent-red); border-color: rgba(239, 68, 68, 0.35);">
                    Uncategorized Expense
                </span>
            </div>

            <div class="audit-card" style="max-width: 600px; margin: 0 auto; width: 100%;">
                <div class="audit-card-header">
                    <span class="audit-card-title">Expense Missing Envelope</span>
                    <span class="audit-card-badge mono">${escapeHtml(item.txn_id)}</span>
                </div>

                <div class="audit-row-item">
                    <span class="audit-row-label">Date & Amount</span>
                    <span class="audit-row-value" style="font-weight: 700; color: var(--accent-red);">
                        ${escapeHtml(formatDisplayDate(item.date))} &bull; ${formatCurrency(item.amount)}
                    </span>
                </div>

                <div class="audit-row-item">
                    <span class="audit-row-label">Payee Name</span>
                    <span class="audit-row-value">${escapeHtml(item.name)}</span>
                </div>

                <div class="audit-row-item">
                    <span class="audit-row-label">Raw Description</span>
                    <span class="audit-row-value" style="font-size: 11.5px; color: var(--text-dim);">${escapeHtml(item.raw_name || '—')}</span>
                </div>

                <div class="field-group" style="margin-top: 6px;">
                    <label for="auditAssignEnv">Assign Active Envelope</label>
                    <select id="auditAssignEnv" class="select-input">
                        <option value="">-- Choose Envelope --</option>
                        ${configuredEnvelopesList.map(e => `<option value="${escapeHtml(e)}">${escapeHtml(e)}</option>`).join('')}
                    </select>
                </div>
            </div>
        `;

        footer.innerHTML = `
            <div style="display: flex; gap: 8px; align-items: center;">
                <button class="btn-modal-cancel" onclick="closeAuditModal()">Cancel & Exit</button>
                <button class="audit-nav-btn" ${currentAuditIndex === 0 ? 'disabled style="opacity:0.4;"' : ''} onclick="prevAuditCandidate()">← Prev</button>
                <button class="audit-nav-btn" onclick="nextAuditCandidate()">Skip →</button>
            </div>
            <div class="modal-actions">
                <button class="btn-keep-both" onclick="deleteSingleAuditTransaction('${item.txn_id}')">Delete Expense</button>
                <button class="btn-modal-save" onclick="saveAuditEnvelopeAssignment('${item.txn_id}')">Assign & Continue</button>
            </div>
        `;
    }
}

function nextAuditCandidate() {
    currentAuditIndex++;
    renderCurrentAuditCandidate();
}

function prevAuditCandidate() {
    if (currentAuditIndex > 0) {
        currentAuditIndex--;
        renderCurrentAuditCandidate();
    }
}

// Resolves duplicate pair by permanently deleting the discarded transaction
async function resolveDuplicatePair(keepTxnId, deleteTxnId) {
    try {
        const result = await postTxnWithVoidPrompt('/api/transactions/delete', { txn_id: deleteTxnId });

        if (result.success) {
            allTransactions = allTransactions.filter(t => t.txn_id !== deleteTxnId);
            filteredTransactions = filteredTransactions.filter(t => t.txn_id !== deleteTxnId);

            auditCandidatesQueue = auditCandidatesQueue.filter(cand => {
                if (cand.type === 'duplicate_pair') {
                    return cand.itemA.txn_id !== deleteTxnId && cand.itemB.txn_id !== deleteTxnId;
                }
                if (cand.type === 'uncategorized_expense') {
                    return cand.item.txn_id !== deleteTxnId;
                }
                return true;
            });

            showToast(`Kept ${keepTxnId}, deleted duplicate ${deleteTxnId}`);
            onFilterChange();
            renderCurrentAuditCandidate();
        } else {
            showToast("Delete failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Duplicate resolution error:", err);
        showToast("Failed to delete duplicate: Network error", true);
    }
}

// Deletes single uncategorized item during audit
async function deleteSingleAuditTransaction(txnId) {
    try {
        const result = await postTxnWithVoidPrompt('/api/transactions/delete', { txn_id: txnId });

        if (result.success) {
            allTransactions = allTransactions.filter(t => t.txn_id !== txnId);
            filteredTransactions = filteredTransactions.filter(t => t.txn_id !== txnId);
            auditCandidatesQueue = auditCandidatesQueue.filter(cand => {
                if (cand.type === 'duplicate_pair') {
                    return cand.itemA.txn_id !== txnId && cand.itemB.txn_id !== txnId;
                }
                if (cand.type === 'uncategorized_expense') {
                    return cand.item.txn_id !== txnId;
                }
                return true;
            });

            showToast(`Deleted transaction ${txnId}`);
            onFilterChange();
            renderCurrentAuditCandidate();
        } else {
            showToast("Delete failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Delete error:", err);
        showToast("Failed to delete transaction", true);
    }
}

// Saves assigned envelope for an uncategorized item
async function saveAuditEnvelopeAssignment(txnId) {
    const selectEl = document.getElementById('auditAssignEnv');
    const chosenEnv = selectEl ? selectEl.value : '';
    if (!chosenEnv) {
        showToast("Please choose an envelope to assign", true);
        return;
    }

    const txn = allTransactions.find(t => t.txn_id === txnId);
    if (!txn) return;

    const updatePayload = {
        txn_id: txn.txn_id,
        name: txn.name,
        envelope: chosenEnv,
        account_type: txn.account_type,
        date: txn.date,
        amount: txn.amount,
        check_num: txn.check_num,
        is_included: txn.is_included,
        exclusion_reason: txn.exclusion_reason
    };

    try {
        const res = await fetch('/api/transactions/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(updatePayload)
        });
        const result = await res.json();
        if (result.success) {
            txn.envelope = chosenEnv;
            txn.updated_at = result.updated_at;
            showToast(`Assigned ${chosenEnv} to ${txnId}`);
            currentAuditIndex++;
            onFilterChange();
            renderCurrentAuditCandidate();
        } else {
            showToast("Update failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Assign error:", err);
        showToast("Network error saving envelope assignment", true);
    }
}


// =====================================================================
// Bulk Actions & Rule Automation Logic
// =====================================================================

// Set tracking selected transaction IDs
const selectedTxnIds = new Set();

// Row checkbox toggle handler
function onRowSelectChange(checkbox, txnId) {
    if (checkbox.checked) {
        selectedTxnIds.add(txnId);
    } else {
        selectedTxnIds.delete(txnId);
    }

    const tr = checkbox.closest('tr');
    if (tr) {
        tr.classList.toggle('selected-row', checkbox.checked);
    }

    updateBulkActionBar();
}

// Header Select-All toggle handler
function toggleSelectAll(isChecked) {
    const visibleCheckboxes = document.querySelectorAll('#transactionTableBody .row-select-cb');
    visibleCheckboxes.forEach(cb => {
        cb.checked = isChecked;
        const txnId = cb.value;
        if (isChecked) {
            selectedTxnIds.add(txnId);
        } else {
            selectedTxnIds.delete(txnId);
        }
        const tr = cb.closest('tr');
        if (tr) {
            tr.classList.toggle('selected-row', isChecked);
        }
    });
    updateBulkActionBar();
}

// Clears all currently selected transactions
function clearAllSelections() {
    selectedTxnIds.clear();
    const selectAll = document.getElementById('selectAllCheckbox');
    if (selectAll) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
    }

    document.querySelectorAll('#transactionTableBody .row-select-cb').forEach(cb => {
        cb.checked = false;
        const tr = cb.closest('tr');
        if (tr) tr.classList.remove('selected-row');
    });

    updateBulkActionBar();
}

// Synchronizes the bulk action toolbar display and header select-all state
function updateBulkActionBar() {
    const bar = document.getElementById('bulkActionBar');
    const countDisplay = document.getElementById('bulkSelectedCount');
    const selectAll = document.getElementById('selectAllCheckbox');
    const size = selectedTxnIds.size;

    if (size > 0) {
        bar.classList.add('active');
        bar.style.display = 'flex';
        countDisplay.textContent = `${size} selected`;
    } else {
        bar.classList.remove('active');
        bar.style.display = 'none';
        countDisplay.textContent = '0 selected';
    }

    const visibleCheckboxes = document.querySelectorAll('#transactionTableBody .row-select-cb');
    if (visibleCheckboxes.length > 0) {
        const allChecked = Array.from(visibleCheckboxes).every(cb => cb.checked);
        const someChecked = Array.from(visibleCheckboxes).some(cb => cb.checked);
        if (selectAll) {
            selectAll.checked = allChecked;
            selectAll.indeterminate = !allChecked && someChecked;
        }
    } else if (selectAll) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
    }
}

// Toggles interactive availability of each bulk edit field in the modal
function toggleBulkField(field) {
    if (field === 'payee') {
        const checked = document.getElementById('bulkApplyPayee').checked;
        const wrap = document.getElementById('bulkPayeeFieldWrap');
        wrap.style.opacity = checked ? '1' : '0.5';
        wrap.style.pointerEvents = checked ? 'auto' : 'none';
        if (checked) document.getElementById('bulkPayeeInput').focus();
    } else if (field === 'envelope') {
        const checked = document.getElementById('bulkApplyEnvelope').checked;
        const wrap = document.getElementById('bulkEnvelopeFieldWrap');
        wrap.style.opacity = checked ? '1' : '0.5';
        wrap.style.pointerEvents = checked ? 'auto' : 'none';
    } else if (field === 'status') {
        const checked = document.getElementById('bulkApplyStatus').checked;
        const wrap = document.getElementById('bulkStatusFieldWrap');
        wrap.style.opacity = checked ? '1' : '0.5';
        wrap.style.pointerEvents = checked ? 'auto' : 'none';
    } else if (field === 'kind') {
        const checked = document.getElementById('bulkApplyKind').checked;
        const wrap = document.getElementById('bulkKindFieldWrap');
        wrap.style.opacity = checked ? '1' : '0.5';
        wrap.style.pointerEvents = checked ? 'auto' : 'none';
    }
    validateBulkForm();
}

// The person picker only matters for income kinds
function onBulkKindChange() {
    const kind = document.getElementById('bulkKindSelect').value;
    document.getElementById('bulkPersonSelect').style.display = INCOME_KINDS.includes(kind) ? '' : 'none';
}

// Adjusts exclusion reason visibility when status dropdown changes
function onBulkStatusChange() {
    const statusVal = document.getElementById('bulkStatusSelect').value;
    const reasonInput = document.getElementById('bulkExclusionReason');
    if (statusVal === '0') {
        reasonInput.style.display = 'block';
        if (!reasonInput.value) reasonInput.value = 'Bulk Excluded';
    } else {
        reasonInput.style.display = 'none';
    }
}

// Toggles display of the persistent rule keyword input
function onToggleBulkSaveRule() {
    const checked = document.getElementById('bulkAlwaysDoThis').checked;
    const wrap = document.getElementById('bulkRuleInputsWrap');
    wrap.style.display = checked ? 'block' : 'none';
    if (checked) {
        const ruleInput = document.getElementById('bulkRulePhrase');
        if (!ruleInput.value) {
            const payeeVal = document.getElementById('bulkPayeeInput').value.trim();
            if (payeeVal) ruleInput.value = payeeVal;
        }
        ruleInput.focus();
    }
}

// Opens the bulk action modal, pre-focusing the selected action category
function openBulkModal(targetField) {
    if (selectedTxnIds.size === 0) {
        showToast("Please select at least one transaction first.", true);
        return;
    }

    document.getElementById('bulkModalCount').textContent = `${selectedTxnIds.size} transactions`;

    document.getElementById('bulkApplyPayee').checked = (targetField === 'payee');
    document.getElementById('bulkApplyEnvelope').checked = (targetField === 'envelope');
    document.getElementById('bulkApplyStatus').checked = (targetField === 'status');
    document.getElementById('bulkApplyKind').checked = (targetField === 'kind');
    document.getElementById('bulkAlwaysDoThis').checked = false;
    document.getElementById('bulkRuleInputsWrap').style.display = 'none';

    toggleBulkField('payee');
    toggleBulkField('envelope');
    toggleBulkField('status');
    toggleBulkField('kind');

    // Pre-fill initial defaults from the first selected record
    const firstId = Array.from(selectedTxnIds)[0];
    const firstTxn = allTransactions.find(t => t.txn_id === firstId);
    if (firstTxn) {
        document.getElementById('bulkPayeeInput').value = firstTxn.name || '';
        document.getElementById('bulkEnvelopeSelect').value = firstTxn.envelope || '';
        document.getElementById('bulkStatusSelect').value = String(firstTxn.is_included !== undefined ? firstTxn.is_included : 1);
        document.getElementById('bulkExclusionReason').value = firstTxn.exclusion_reason || '';
        document.getElementById('bulkRulePhrase').value = firstTxn.name || '';
        document.getElementById('bulkKindSelect').value = firstTxn.txn_kind || 'expense';
        document.getElementById('bulkPersonSelect').value = firstTxn.person_id ? String(firstTxn.person_id) : '';
        onBulkStatusChange();
        onBulkKindChange();
    }

    const modal = document.getElementById('bulkModalOverlay');
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    validateBulkForm();
}

// Closes the bulk action modal
function closeBulkModal() {
    const modal = document.getElementById('bulkModalOverlay');
    modal.classList.remove('active');
    document.body.style.overflow = '';
}

// Handles clicks outside the modal dialog to dismiss
function handleBulkOverlayClick(event) {
    if (event.target === document.getElementById('bulkModalOverlay')) {
        closeBulkModal();
    }
}

// Verifies at least one modification category is active before allowing submit
function validateBulkForm() {
    const applyPayee = document.getElementById('bulkApplyPayee').checked;
    const applyEnvelope = document.getElementById('bulkApplyEnvelope').checked;
    const applyStatus = document.getElementById('bulkApplyStatus').checked;
    const applyKind = document.getElementById('bulkApplyKind').checked;
    const saveBtn = document.getElementById('btnSaveBulkModal');
    const msg = document.getElementById('bulkModalStatusMsg');

    const hasAny = applyPayee || applyEnvelope || applyStatus || applyKind;
    saveBtn.disabled = !hasAny;
    msg.textContent = hasAny
        ? `Ready to apply updates to ${selectedTxnIds.size} transactions.`
        : 'Select at least one change to apply.';
}

// Dispatches bulk transaction modifications and optional rule persistence
async function submitBulkModalForm() {
    const applyPayee = document.getElementById('bulkApplyPayee').checked;
    const applyEnvelope = document.getElementById('bulkApplyEnvelope').checked;
    const applyStatus = document.getElementById('bulkApplyStatus').checked;
    const applyKind = document.getElementById('bulkApplyKind').checked;
    const saveRule = document.getElementById('bulkAlwaysDoThis').checked;
    const rulePhrase = document.getElementById('bulkRulePhrase').value.trim();

    if (!applyPayee && !applyEnvelope && !applyStatus && !applyKind) {
        showToast("Please choose at least one field to update.", true);
        return;
    }

    if (saveRule && !rulePhrase) {
        showToast("Please enter a match keyword or phrase for the rule.", true);
        document.getElementById('bulkRulePhrase').focus();
        return;
    }

    const updates = {};
    if (applyPayee) {
        updates.name = document.getElementById('bulkPayeeInput').value.trim();
    }
    if (applyEnvelope) {
        updates.envelope = document.getElementById('bulkEnvelopeSelect').value.trim();
    }
    if (applyStatus) {
        updates.is_included = parseInt(document.getElementById('bulkStatusSelect').value, 10);
        updates.exclusion_reason = updates.is_included === 0 ? document.getElementById('bulkExclusionReason').value.trim() : '';
    }
    if (applyKind) {
        updates.txn_kind = document.getElementById('bulkKindSelect').value;
        if (INCOME_KINDS.includes(updates.txn_kind)) {
            updates.person_id = parseInt(document.getElementById('bulkPersonSelect').value, 10) || 0;
        }
        if (EXCLUDED_KINDS.includes(updates.txn_kind) && !applyStatus) {
            updates.exclusion_reason = '';
        }
    }

    const payload = {
        txn_ids: Array.from(selectedTxnIds),
        updates: updates,
        save_rule: saveRule,
        rule_phrase: rulePhrase
    };

    const saveBtn = document.getElementById('btnSaveBulkModal');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Applying...';

    try {
        const res = await fetch('/api/transactions/bulk-update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();

        if (result.success) {
            closeBulkModal();
            clearAllSelections();
            await refreshTransactions();

            let msg = `Updated ${result.updated_count} transactions.`;
            if (result.rule_saved) {
                msg += ` Rule saved for "${rulePhrase}".`;
            }
            showToast(msg);
        } else {
            showToast("Bulk update failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Bulk update error:", err);
        showToast("Network error applying bulk updates", true);
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Apply Bulk Updates';
    }
}

// =====================================================================
// Envelope Manager Engine: Add, Rename, Delete & Reassignment
// =====================================================================

let editingEnvelopeName = null;
let envelopeKinds = {};   // name -> { id, kind }, from the server
let deleteTargetEnvelope = null;
let deleteTargetTxnCount = 0;

async function loadEnvelopeKinds() {
    try { envelopeKinds = (await api('/api/envelopes')).kinds || {}; } catch (err) { /* the list still works without it */ }
    renderEnvelopeManagerList();
}

async function setEnvelopeKind(name, kind) {
    const info = envelopeKinds[name];
    if (!info) return;
    try {
        await apiPost('/api/envelopes/save', { id: info.id, kind });
        info.kind = kind;
        showToast(`"${name}" is now a ${{ spending: 'spending', bill: 'bills', savings: 'savings' }[kind]} envelope.`);
    } catch (err) {
        showToast(err.message, true);
        renderEnvelopeManagerList();
    }
}

function openEnvelopeManagerModal() {
    editingEnvelopeName = null;
    loadEnvelopeKinds();
    document.getElementById('newEnvelopeInput').value = '';
    document.getElementById('envSearchFilter').value = '';
    renderEnvelopeManagerList();
    const modal = document.getElementById('envelopeModalOverlay');
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
    setTimeout(() => document.getElementById('newEnvelopeInput').focus(), 100);
}

function closeEnvelopeManagerModal() {
    editingEnvelopeName = null;
    const modal = document.getElementById('envelopeModalOverlay');
    modal.classList.remove('active');
    document.body.style.overflow = '';
    document.dispatchEvent(new CustomEvent('txnsrefreshed'));  // other tabs reload their envelope lists
}

function handleEnvelopeModalOverlayClick(event) {
    if (event.target === document.getElementById('envelopeModalOverlay')) {
        closeEnvelopeManagerModal();
    }
}

function getEnvelopeTxnCounts() {
    const counts = {};
    configuredEnvelopesList.forEach(e => {
        counts[e.trim().toLowerCase()] = 0;
    });
    allTransactions.forEach(t => {
        const env = (t.envelope || '').trim().toLowerCase();
        if (env) {
            counts[env] = (counts[env] || 0) + 1;
        }
    });
    return counts;
}

function renderEnvelopeManagerList() {
    const container = document.getElementById('envelopeListTbody');
    const searchVal = (document.getElementById('envSearchFilter').value || '').trim().toLowerCase();
    const counts = getEnvelopeTxnCounts();

    let envList = [...configuredEnvelopesList];
    if (searchVal) {
        envList = envList.filter(e => e.toLowerCase().includes(searchVal));
    }
    // Sort alphabetically
    envList.sort((a, b) => a.localeCompare(b));

    document.getElementById('envSummaryCount').textContent = `${envList.length} envelope${envList.length === 1 ? '' : 's'}`;

    if (envList.length === 0) {
        container.innerHTML = `
            <tr>
                <td colspan="4" style="text-align: center; padding: 30px; color: var(--text-muted);">
                    ${searchVal ? 'No envelopes match your search.' : 'No envelopes configured. Add one above!'}
                </td>
            </tr>
        `;
        return;
    }

    container.innerHTML = envList.map(env => {
        const txnCount = counts[env.trim().toLowerCase()] || 0;
        const isEditing = editingEnvelopeName === env;

        if (isEditing) {
            return `
                <tr class="envelope-row">
                    <td colspan="3">
                        <div class="env-inline-edit">
                            <input type="text" id="inlineRenameInput" class="env-inline-input" value="${escapeHtml(env)}" onkeydown="if(event.key==='Enter') submitInlineRename('${escapeHtml(env)}'); if(event.key==='Escape') cancelInlineRename();">
                            <button class="btn-inline-action btn-inline-save" onclick="submitInlineRename('${escapeHtml(env)}')">Save</button>
                            <button class="btn-inline-action btn-inline-cancel" onclick="cancelInlineRename()">Cancel</button>
                        </div>
                    </td>
                    <td class="envelope-actions-cell">
                        <span class="env-count-badge ${txnCount > 0 ? 'has-txns' : 'zero-txns'}">${txnCount} txn${txnCount === 1 ? '' : 's'}</span>
                    </td>
                </tr>
            `;
        }

        return `
            <tr class="envelope-row">
                <td class="env-name-cell">
                    <span>${escapeHtml(env)}</span>
                </td>
                <td>
                    <select class="select-input env-kind-select" onchange="setEnvelopeKind(this.dataset.env, this.value)" data-env="${escapeHtml(env)}" ${envelopeKinds[env] ? '' : 'disabled'} aria-label="Type of ${escapeHtml(env)}">
                        ${[['spending', 'Spending'], ['bill', 'Bills'], ['savings', 'Savings']].map(([v, l]) =>
                            `<option value="${v}" ${(envelopeKinds[env] || {}).kind === v ? 'selected' : ''}>${l}</option>`).join('')}
                    </select>
                </td>
                <td>
                    <span class="env-count-badge ${txnCount > 0 ? 'has-txns' : 'zero-txns'}">
                        ${txnCount} txn${txnCount === 1 ? '' : 's'}
                    </span>
                </td>
                <td class="envelope-actions-cell">
                    <button class="btn-env-icon" onclick="filterDashboardToEnvelope('${escapeHtml(env)}')" title="Filter Dashboard to this Envelope">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="11" cy="11" r="8"></circle>
                            <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                    </button>
                    <button class="btn-env-icon" onclick="startInlineRename('${escapeHtml(env)}')" title="Rename Envelope">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                        </svg>
                    </button>
                    <button class="btn-env-icon delete" onclick="handleEnvelopeDeleteClick('${escapeHtml(env)}', ${txnCount})" title="Delete Envelope">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </td>
            </tr>
        `;
    }).join('');
}

function filterDashboardToEnvelope(envName) {
    closeEnvelopeManagerModal();
    const filterSelect = document.getElementById('filterEnvelope');
    filterSelect.value = envName;
    onFilterChange();
    showToast(`Filtering by envelope: ${envName}`);
}

async function submitAddEnvelope() {
    const input = document.getElementById('newEnvelopeInput');
    const cleanName = input.value.trim();
    if (!cleanName) {
        showToast("Please enter an envelope name", true);
        input.focus();
        return;
    }

    const exists = configuredEnvelopesList.some(e => e.trim().toLowerCase() === cleanName.toLowerCase());
    if (exists) {
        showToast(`Envelope "${cleanName}" already exists`, true);
        input.focus();
        return;
    }

    try {
        const res = await fetch('/api/envelopes/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ envelope: cleanName, kind: document.getElementById('newEnvelopeKind').value })
        });
        const result = await res.json();
        if (result.success) {
            configuredEnvelopesList = result.envelopes || [...configuredEnvelopesList, cleanName];
            envelopeKinds = result.kinds || envelopeKinds;
            input.value = '';
            populateEnvelopeDropdowns();
            renderEnvelopeManagerList();
            showToast(`Envelope "${cleanName}" added successfully`);
        } else {
            showToast("Error adding envelope: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Add envelope error:", err);
        showToast("Network error adding envelope", true);
    }
}

function startInlineRename(envName) {
    editingEnvelopeName = envName;
    renderEnvelopeManagerList();
    setTimeout(() => {
        const input = document.getElementById('inlineRenameInput');
        if (input) {
            input.focus();
            input.select();
        }
    }, 50);
}

function cancelInlineRename() {
    editingEnvelopeName = null;
    renderEnvelopeManagerList();
}

async function submitInlineRename(oldName) {
    const input = document.getElementById('inlineRenameInput');
    if (!input) return;
    const newName = input.value.trim();

    if (!newName) {
        showToast("Envelope name cannot be empty", true);
        input.focus();
        return;
    }

    if (newName.toLowerCase() === oldName.toLowerCase()) {
        cancelInlineRename();
        return;
    }

    const exists = configuredEnvelopesList.some(e => e.toLowerCase() === newName.toLowerCase() && e.toLowerCase() !== oldName.toLowerCase());
    if (exists) {
        showToast(`Envelope "${newName}" already exists`, true);
        input.focus();
        return;
    }

    try {
        const res = await fetch('/api/envelopes/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        const result = await res.json();
        if (result.success) {
            configuredEnvelopesList = result.envelopes;
            envelopeKinds = result.kinds || envelopeKinds;
            // Update all matching in-memory transactions
            allTransactions.forEach(t => {
                if ((t.envelope || '').trim().toLowerCase() === oldName.trim().toLowerCase()) {
                    t.envelope = newName;
                }
            });

            // Update active filter if it matched oldName
            const filterSelect = document.getElementById('filterEnvelope');
            if (filterSelect.value.trim().toLowerCase() === oldName.trim().toLowerCase()) {
                filterSelect.value = newName;
            }

            populateEnvelopeDropdowns();
            editingEnvelopeName = null;
            renderEnvelopeManagerList();
            onFilterChange();
            showToast(`Renamed "${oldName}" to "${newName}" (${result.renamed_count} transactions updated)`);
        } else {
            showToast("Rename failed: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Rename error:", err);
        showToast("Network error renaming envelope", true);
    }
}

function handleEnvelopeDeleteClick(envName, txnCount) {
    deleteTargetEnvelope = envName;
    deleteTargetTxnCount = txnCount;

    if (txnCount === 0) {
        if (!confirm(`Are you sure you want to remove envelope "${envName}"? No transactions are currently tagged to it.`)) {
            return;
        }
        executeDeleteEnvelopeDirect(envName, null);
        return;
    }

    // Envelope has transactions: show warning & reassignment modal
    document.getElementById('deleteEnvTargetName').textContent = envName;
    document.getElementById('deleteEnvTxnCount').textContent = txnCount;

    const select = document.getElementById('deleteReassignTargetSelect');
    const otherEnvelopes = configuredEnvelopesList.filter(e => e.trim().toLowerCase() !== envName.trim().toLowerCase());
    otherEnvelopes.sort((a, b) => a.localeCompare(b));

    select.innerHTML = otherEnvelopes.map(e => `<option value="${escapeHtml(e)}">${escapeHtml(e)}</option>`).join('');

    document.getElementById('optReassign').checked = true;
    toggleDeleteHandlingMode();

    const modal = document.getElementById('deleteEnvelopeModalOverlay');
    modal.classList.add('active');
}

function closeDeleteEnvelopeModal() {
    deleteTargetEnvelope = null;
    deleteTargetTxnCount = 0;
    const modal = document.getElementById('deleteEnvelopeModalOverlay');
    modal.classList.remove('active');
}

function handleDeleteModalOverlayClick(event) {
    if (event.target === document.getElementById('deleteEnvelopeModalOverlay')) {
        closeDeleteEnvelopeModal();
    }
}

function toggleDeleteHandlingMode() {
    const isReassign = document.getElementById('optReassign').checked;
    const wrap = document.getElementById('reassignSelectWrap');
    if (wrap) {
        wrap.style.display = isReassign ? 'block' : 'none';
    }
}

async function confirmDeleteEnvelopeWithReassign() {
    if (!deleteTargetEnvelope) return;
    const isReassign = document.getElementById('optReassign').checked;
    let reassignTo = null;

    if (isReassign) {
        const select = document.getElementById('deleteReassignTargetSelect');
        reassignTo = select.value;
        if (!reassignTo) {
            showToast("Please choose an envelope to reassign to", true);
            return;
        }
    } else {
        reassignTo = ""; // Set unallocated
    }

    const targetName = deleteTargetEnvelope;
    closeDeleteEnvelopeModal();
    await executeDeleteEnvelopeDirect(targetName, reassignTo);
}

async function executeDeleteEnvelopeDirect(envName, reassignTo) {
    try {
        const res = await fetch('/api/envelopes/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ envelope: envName, reassign_to: reassignTo })
        });
        const result = await res.json();
        if (result.success) {
            configuredEnvelopesList = result.envelopes;
            envelopeKinds = result.kinds || envelopeKinds;

            // Update in-memory transactions
            allTransactions.forEach(t => {
                if ((t.envelope || '').trim().toLowerCase() === envName.trim().toLowerCase()) {
                    t.envelope = reassignTo || "";
                }
            });

            // If active filter was the deleted envelope, reset filter to all
            const filterSelect = document.getElementById('filterEnvelope');
            if (filterSelect.value.trim().toLowerCase() === envName.trim().toLowerCase()) {
                filterSelect.value = '';
            }

            populateEnvelopeDropdowns();
            renderEnvelopeManagerList();
            onFilterChange();

            if (result.reassigned_count > 0) {
                const destText = reassignTo ? `reassigned to "${reassignTo}"` : `marked Unallocated`;
                showToast(`Envelope "${envName}" removed; ${result.reassigned_count} transactions ${destText}`);
            } else {
                showToast(`Envelope "${envName}" removed successfully`);
            }
        } else {
            showToast("Error deleting envelope: " + (result.error || "Unknown error"), true);
        }
    } catch (err) {
        console.error("Delete envelope error:", err);
        showToast("Network error deleting envelope", true);
    }
}