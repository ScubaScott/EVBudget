# EVBudget: Envelope Budgeting Build Spec

**Status:** Approved design, ready to build
**Date:** 2026-10-22
**Applies to:** `budget_dashboard.py` v2.1, `dashboard.html`, `dashboard.css`, `Import_transactions.py` v2.7
**Supersedes:** The Goodbudget workflow in `Goodbudget_Rules.md`. Its categorization table becomes seed data for the rules engine (see §5).

---

## 1. Goals

1. Make the dashboard the **system of record** for envelope budgeting. Retire Goodbudget.
2. **Tag income** by person and source, and report on income over time.
3. **Suggest a budget** from spending history, and show average regular monthly income as the amount available to budget.
4. **Fund envelopes from income.** Each paycheck opens a pre-filled allocation, based on a template, that is linked to that income transaction.
5. **Show envelope fullness** with a "should have" marker based on the budget and the current date.
6. Give each envelope **bills with due dates and recurrence** (weekly, monthly, every N months, yearly, one-time).
7. **Move the import tool into the dashboard** as a tab. One server, one launcher.

### Non-goals (out of scope)
- Bank sync or API connections. Import stays file-based.
- Splitting one transaction across several envelopes. A later phase could add this; the schema doesn't block it.
- Tracking balances for multiple accounts (checking, savings, card). Savings is handled with savings envelopes only (§6.6).
- Multiple users, authentication, or remote access. The server stays bound to `127.0.0.1`.
- Mobile layout beyond what the existing responsive CSS already gives.

---

## 2. Decision Log

| # | Topic | Decision |
|---|---|---|
| D1 | System of record | The dashboard's SQLite database holds all balances. The Goodbudget export is **removed**. |
| D2 | Positive transactions | **Paychecks** are tagged to a person through Income Source rules. **Refunds** return to the envelope they came from. **Reimbursements** (Venmo cashouts and payments received, usually paying you back for a shared expense or a bill you covered) are also refunds: they go back to a spending envelope that the user picks, or by linking the original expense. **Other income** (tax refund, Vrbo, Robinhood) is tagged `other_income` with a source label, and can be allocated like a paycheck. |
| D3 | Bonuses | Paychecks have a subtype of `regular` or `bonus`. Bonus is auto-flagged when the check is more than 50% above the source's typical amount, and the user can override. Planning income uses **regular only**. |
| D4 | Fill model | Each income source has a **fixed-dollar fill template**. A paycheck opens a pre-filled allocation screen, and the user confirms it. The remainder goes to Unallocated. Bonus and other income start empty, with a **Suggest** button. |
| D5 | Rollover | Set **per envelope**: `rollover` or `reset`. Reset sweeps a positive leftover to Unallocated at period end. **Negative balances always carry forward.** |
| D6 | Target marker | **Two modes.** Spending envelopes pace *down* through the period. Bill and sinking envelopes accrue *up* toward the due date. |
| D7 | Due dates | **Bill items inside envelopes.** Each has its own amount, due date, and recurrence. An envelope's bill target is the sum of its bills. |
| D8 | Suggestions | Spending envelopes use the **last 6 complete months**. Bills use the **last 12 months**, annualized to a monthly amount. Show the average, median, min/max, and a confidence level. The user accepts or edits each one. |
| D9 | Cutover | Pick a **go-live date** and enter an opening balance per envelope. Only activity on or after go-live affects balances. All history is still used for suggestions and reports. |
| D10 | Layout | **Top-level tabs:** Import · Analytics · Envelopes · Budget · Income. |
| D11 | Templates | **Fixed dollars.** A shortfall shows a warning. The remainder goes to Unallocated. |
| D12 | Income reports | All four: monthly income by person, paycheck trend per person, income vs budget vs spend, YTD KPI cards. |
| D13 | Savings | Savings is a **savings envelope kind**, filled through templates. The bank transfer to savings stays **excluded**, so it isn't counted twice. |
| D14 | Goodbudget export | **Removed.** The converter feeds only the local database. |
| D15 | Import tool | Becomes the **Import tab** in Phase 1. `Import_transactions.py` and `launch_converter.bat` are retired. |
| D16 | Envelope transfers | Supported: envelope ↔ envelope and Unallocated ↔ envelope. Every move is logged. |
| D17 | Spending period | Spending envelopes use the **calendar month**. Bills follow their own recurrence. |
| D18 | Bill matching | Auto-match by payee phrase, an amount tolerance, and a date window. A match moves `next_due_date` forward. Variable bills use a rolling average of the last 3 matched payments. |

---

## 3. Current State and Known Issues

Findings from reviewing the code. Phase 1 must fix all of them.

| # | Issue | Location | Impact |
|---|---|---|---|
| K1 | Dates are stored as `MM/DD/YYYY` text, so SQL `ORDER BY`, `MIN`/`MAX`, and range filters are wrong. For example, `MIN(date)` returns `01/01/2026` and `MAX(date)` returns `12/31/2025`. | `Import_transactions.py` `parse_date_string()` | Every period-based calculation in this spec depends on correct date ranges. |
| K2 | **Re-imports overwrite user edits.** The upsert's `ON CONFLICT DO UPDATE` rewrites `name`, `envelope`, `is_included`, `exclusion_reason`, and `match_source`. | `save_to_local_store()` | Loading an overlapping bank file wipes dashboard edits. Once income tags and bill links exist, it would also break the links that fills depend on. |
| K3 | Every positive amount is treated as income. | `determine_envelope_for_record()` Rule 1, and dashboard `amount > 0` checks for KPIs and the Income vs Spend chart | Refunds inflate income and leave spending envelopes overstated. |
| K4 | Exclusions, the kid-transfer exception, and envelope heuristics are hardcoded. | `evaluate_exclusion()`, Rule 5 | Can't be edited without changing code. |
| K5 | Envelopes are a plain name list in `envelopes.json`, with no budget, kind, or bill data. | `DataFiles/envelopes.json` | No place to store envelope settings. |
| K6 | The HTTP handler repeats the same parse-and-respond code for every endpoint in a long `if` chain. | `BudgetDashboardHandler` | This spec adds about 30 endpoints, and the current pattern won't scale to that. |
| K7 | `dashboard.html` is about 3,300 lines with all its JS inline. | `dashboard.html` | Five tabs of inline JS will be hard to maintain. |
| K8 | Chart.js is loaded from the CDN without a version (`cdn.jsdelivr.net/npm/chart.js`). | `dashboard.html:12` | An upstream release could break the charts without warning. |

### Observed data (as of 2026-10-21)
- About 1,500 transactions from 2025-12-20 to 2026-10-19. Sources are Chase credit (1,250 rows) and checking (250 rows).
- **Scott:** `CARGILL INCORPOR PAYROLL … REILING,SCOTT L`, biweekly on Fridays, about $3,500 net. A separate bonus deposit of $10,474.36 came on 2026-07-31.
- **Kendra:** `HealthPartners I PAYROLL … Reiling Kendra`, biweekly on the alternate Fridays, about $1,720 net.
- Positive non-paycheck items that are refunds today: Amazon, Target, Boot Barn, HomeGoods, Dick's, Scheels, Michaels, ALDI, and a barbershop. Reimbursements: Venmo cashouts and Venmo payments received (about $2.2K across 6+ items). Other income: an IRS refund, a Vrbo payout, Robinhood credits, and a tour refund. `STATEMENT CREDIT` items are ambiguous and need review.

---

## 4. Architecture

### 4.1 Principles
- **Keep using only the Python standard library** (`http.server`, `sqlite3`) and plain JS with Chart.js. No build step and no pip installs, same as today.
- **Store only what can't be derived.** Envelope balances are computed from transactions plus the move ledger. Spending is never copied into a ledger, so editing a transaction's envelope updates balances automatically.
- **Use integer cents in all new tables** (`*_cents` columns). `transactions.amount` stays `REAL` for compatibility. The engine converts with `round(amount * 100)`.
- **Make migrations automatic and safe.** They run at startup, back up the database first, are versioned, and can run more than once without harm.

### 4.2 Module layout (target)

```
budget_dashboard.py          Entry point: server start, route table, static files
evbudget/
  __init__.py
  http_util.py               JSON body parsing, JSON/err responses, route dispatch table
  store.py                   Connection factory, schema, migrations, backups, settings get/set
  rules.py                   Rules engine: exclusion → income source → refund → envelope → bill match
  import_engine.py           Ported from Import_transactions.py: file scan, header inspect,
                             column mapping/presets, parsing, txn_id generation, commit
  income.py                  Typical paycheck, bonus detection, planning income, income reports
  recurrence.py              Due-date math (next occurrence, cycle start, occurrences/year)
  envelopes.py               Balances, targets/marker, period sweeps, moves, allocations
  budget.py                  Suggestions, template generation, budget-vs-income summary
web/
  dashboard.html             Shell: header, tab bar, settings modal, shared modals, one <section> per tab
  dashboard.css              Existing themes (Dark/Greenback/Ledger) + new components
  js/core.js                 api() helper, formatting, theme, tab router (hash-based: #import, #envelopes…)
  js/analytics.js            Existing dashboard logic, extracted from dashboard.html
  js/import.js               Ported converter SPA
  js/envelopes.js            Fullness chart, fill queue, upcoming bills, envelope drawer, transfers
  js/budget.js               Envelope setup, suggestions, bill editor, template editor, go-live wizard
  js/income.js               Income KPIs and charts, people/source manager
tests/
  test_recurrence.py, test_rules.py, test_envelopes.py, test_income.py, test_migration.py
```

- Tests use the standard library `unittest` with a temporary SQLite fixture.
- Static files are served from `/web/...`. Replace the per-file `get_css_content()` with a generic static handler that restricts paths to the `web/` folder (no `..` traversal) and sets content types.
- Pin Chart.js to an exact version, for example `chart.js@4.4.1` (fixes K8).
- Keep `DataFiles/` as the data root. `DataFiles/` stays git-ignored.

### 4.3 Request routing
Replace the `if path == …` chain with a dispatch table such as `{("GET", "/api/envelopes"): handler_fn, …}`. Each handler takes `(query: dict, body: dict)` and returns `(status, payload)`. `http_util` handles JSON parsing, error wrapping (400 with `{"success": false, "error": …}`), and response headers (fixes K6). Existing endpoint paths and response shapes stay the same so the Analytics tab keeps working.

---

## 5. Data Model

All tables live in `DataFiles/LocalStore/budget_store.db`.

### 5.1 `schema_meta`
| Column | Type | Notes |
|---|---|---|
| key | TEXT PK | `schema_version` |
| value | TEXT | Integer as text. The version before migration is 1, and this spec brings it to 2+. |

### 5.2 `settings` (key/value)
| Key | Default | Purpose |
|---|---|---|
| `go_live_date` | *(unset)* | ISO date. Envelope math is turned off until this is set. |
| `bonus_threshold_pct` | `50` | D3 |
| `suggest_window_spending_months` | `6` | D8 |
| `suggest_window_bill_months` | `12` | D8 |
| `bill_match_window_days` | `10` | ±days around the due date (weekly bills use ±3) |
| `bill_amount_tolerance_pct` | `10` | Default; can be overridden per bill |
| `refund_lookback_days` | `120` | Refund detection (§6.2) |
| `theme` | existing | Moved here from client-side storage (optional) |

### 5.3 `transactions` (existing table, extended)
Existing columns stay. **Changed:** `date` is stored as ISO `YYYY-MM-DD`.

New columns:

| Column | Type | Notes |
|---|---|---|
| txn_kind | TEXT NOT NULL | `expense` · `refund` · `paycheck` · `other_income` · `transfer` · `cc_payment`. Excluded rows keep a kind for audit purposes. |
| income_source_id | INTEGER NULL | FK → `income_sources`. Set when kind is `paycheck`. |
| person_id | INTEGER NULL | FK → `people`. Set for paychecks, and optionally for other income. |
| income_subtype | TEXT NULL | `regular` · `bonus`. Paychecks only. |
| income_label | TEXT NULL | Source label for other income (e.g. "IRS Refund", "Vrbo"). |
| reimburses_txn_id | TEXT NULL | FK → `transactions.txn_id`. Optional link from a reimbursement (refund) to the expense it pays back. Used to set the envelope and to show the net cost of a shared expense. |
| bill_id | INTEGER NULL | FK → `bills`. Set when the transaction pays a bill. |
| needs_review | INTEGER DEFAULT 0 | 1 when classification is unsure (unmatched positive amounts, statement credits). |
| user_modified | INTEGER DEFAULT 0 | Set to 1 by any dashboard edit. Rules re-runs skip these rows unless forced. |
| first_imported_at / last_seen_at | TEXT | Import audit trail. |

Indexes: `(date)`, `(envelope)`, `(txn_kind, date)`, `(income_source_id, date)`, `(bill_id)`.

**Re-import behavior (fixes K2):** When an imported `txn_id` already exists, update **only** `last_seen_at` and append to `source_file`. Never overwrite classification or user-editable fields. The import preview marks these rows as **"Already in database"** and shows the stored values. To reclassify on purpose, use the **Re-run rules** action (§7). It skips `user_modified` rows unless the user checks "include edited rows."

### 5.4 `people`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT UNIQUE | Seed: `Scott`, `Kendra` |
| color | TEXT NULL | Optional series color for charts. Defaults to the theme palette. |
| active | INTEGER DEFAULT 1 | |

### 5.5 `income_sources`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| person_id | INTEGER FK | |
| name | TEXT | e.g. "Cargill", "HealthPartners" |
| match_phrase | TEXT | Case-insensitive substring of `raw_name`, e.g. `CARGILL INCORPOR PAYROLL` |
| cadence_freq / cadence_interval | TEXT / INTEGER | Uses the recurrence model in §6.4. Seed values: `weekly` with interval 2. |
| cadence_anchor_date | TEXT | A known pay date (Cargill: 2026-10-09, HealthPartners: 2026-10-16) |
| typical_amount_cents | INTEGER NULL | Optional manual override. Otherwise computed (§6.3). |
| bonus_threshold_pct | INTEGER NULL | Optional override of the global setting |
| active | INTEGER DEFAULT 1 | |

### 5.6 `envelopes` (replaces `envelopes.json`, fixes K5)
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT UNIQUE | The display name. The dashboard keeps matching `transactions.envelope` by name, and rename updates both (same as today). |
| kind | TEXT | `spending` · `bill` · `savings` · `system` |
| rollover_mode | TEXT | `rollover` · `reset`. Defaults: spending → reset, bill and savings → rollover. |
| monthly_budget_cents | INTEGER | **Spending:** the budget. **Bill:** derived from bills (read-only in the UI unless the envelope has no bills). **Savings:** the monthly contribution. |
| goal_cents / goal_date | INTEGER / TEXT NULL | Savings only, optional (§6.5) |
| sort_order | INTEGER | |
| active | INTEGER DEFAULT 1 | Archived envelopes are hidden but keep their history. |

The `system` kind has exactly one row, **Unallocated**. It can't be renamed, deleted, or budgeted.

**Delete rule:** An envelope with a non-zero balance can't be deleted. The user must transfer the balance out first. Transactions are reassigned as they are today. After go-live, archiving is preferred over deleting.

### 5.7 `bills`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| envelope_id | INTEGER FK | |
| name | TEXT | e.g. "Quantum Fiber", "Home Insurance" |
| match_phrase | TEXT NULL | Payee substring used for auto-matching |
| amount_mode | TEXT | `fixed` · `estimated` (estimated = rolling average of the last 3 matched payments) |
| amount_cents | INTEGER | The fixed amount, or the seed value for an estimate |
| freq | TEXT | `once` · `weekly` · `monthly` · `yearly` |
| interval | INTEGER | Weekly: N weeks. Monthly: N months (6 = semi-annual). Yearly: N years. |
| anchor_date | TEXT | Any known due date. It sets the day of week or month. |
| next_due_date | TEXT | Kept up to date by the matching logic |
| last_paid_date / last_paid_txn_id | TEXT NULL | |
| amount_tolerance_pct | INTEGER NULL | Overrides the setting |
| active | INTEGER DEFAULT 1 | |

### 5.8 `envelope_moves` (the money-movement ledger)
Each row moves money from one place to another. `NULL` means outside the system (used only for opening balances).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| date | TEXT | ISO date the move takes effect |
| from_envelope_id | INTEGER NULL | |
| to_envelope_id | INTEGER NULL | |
| amount_cents | INTEGER > 0 | |
| move_type | TEXT | `opening` · `fill` · `transfer` · `sweep` · `adjustment` |
| allocation_id | INTEGER NULL | Set for `fill` moves |
| period_key | TEXT NULL | `sweep` only, e.g. `2026-11`. Unique together with the envelope, so sweeps can be re-run safely. |
| note | TEXT NULL | |
| created_at / voided_at | TEXT | Voided rows are ignored in all math. Rows are voided, never deleted. |

### 5.9 `allocations` (one per funded income transaction)
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| income_txn_id | TEXT NULL UNIQUE | FK → `transactions.txn_id`. `NULL` for an ad-hoc allocation from Unallocated. |
| template_id | INTEGER NULL | The template it was pre-filled from |
| total_cents | INTEGER | |
| status | TEXT | `confirmed` · `voided` |
| created_at / voided_at | TEXT | |

Confirming creates one `fill` move per line (Unallocated → envelope), dated with the income transaction's date. Voiding marks the allocation and all its moves voided.

### 5.10 `fill_templates` and `fill_template_lines`
| Table | Columns |
|---|---|
| fill_templates | id, income_source_id (UNIQUE, one template per source), name, updated_at |
| fill_template_lines | template_id, envelope_id, amount_cents, sort_order. PK is (template_id, envelope_id). |

### 5.11 `rules` (replaces `user_rules.json`, `known_vendors.json`, and the hardcoded logic, fixes K4)
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| phrase | TEXT | Case-insensitive substring match on `raw_name` |
| stage | TEXT | `exclude` · `include_override` · `income` · `refund` · `envelope` (see §6.1 for order) |
| set_kind | TEXT NULL | e.g. `transfer`, `cc_payment`, `other_income`, `refund` |
| set_envelope | TEXT NULL | |
| set_payee | TEXT NULL | Display-name rewrite |
| set_income_label | TEXT NULL | |
| exclusion_reason | TEXT NULL | |
| priority | INTEGER | Lower runs first within a stage. User rules come before vendor rules, which come before core rules. |
| origin | TEXT | `core` (seeded from the old code) · `vendor` (from known_vendors) · `user` (learned from the dashboard) |
| active | INTEGER DEFAULT 1 | |

`column_presets` from `user_rules.json` (the import column mappings) move to `settings` as JSON under `import_column_presets`.

### 5.12 `import_batches`
id, created_at, files (JSON), rows_total, rows_new, rows_existing, rows_excluded, rows_needs_review. Shown as history on the Import tab.

### 5.13 `master_transactions.csv`
Keep it as a plain export copy of the database, with the new columns added. It's never read back in.

---

## 6. Business Rules and Calculations

### 6.1 Classification pipeline (`rules.py`)
This runs for every imported row, and for "Re-run rules." **The first stage that matches decides the row's kind.** The envelope and bill stages then fill in the remaining fields.

1. **Include override** (`include_override` rules). Example: `ONLINE TRANSFER TO REILING J` → kind `expense`, envelope `Shopping`. This keeps the kid-transfer exception.
2. **Exclusion** (`exclude` rules). Credit card payments and autopays → `cc_payment`. Internal transfers (Way2Save, transfers to/from REILING S/J) → `transfer`. Both set `is_included = 0` with a reason.
3. **Income source** (amount > 0 and an `income_sources.match_phrase` matches) → `paycheck`, plus `income_source_id`, `person_id`, and `income_subtype` (§6.3).
4. **Positive, not a paycheck.** Check in this order:
   1. A `refund` or `income` stage rule matches → use what it sets. Seed rules: `IRS TREAS` → `other_income` with label "Tax Refund". `VENMO CASHOUT` and `VENMO PAYMENT` → `refund` with **no envelope** and `needs_review = 1` (a reimbursement; see below).
   2. An `envelope` rule (user or vendor) matches the payee → `refund` to that envelope.
   3. The normalized payee matches an included expense within the last `refund_lookback_days` → `refund` to that expense's envelope.
   4. Otherwise → `other_income`, with the label taken from the cleaned payee and `needs_review = 1`.
   `STATEMENT CREDIT` is seeded with `needs_review` set, with no automatic kind.

**Reimbursements (Venmo and similar).** The payee ("VENMO CASHOUT") says nothing about which envelope the money belongs to, so the user picks it in one of two ways:
- **Match to expense.** A picker lists included expenses from the last 60 days whose amount is at least the reimbursement. They're ranked by how close the reimbursement is to the full amount, ½, ⅓, or ¼ of the expense (common splits), then by recency. Picking one sets `reimburses_txn_id` and copies that expense's envelope.
- **Pick an envelope** directly, for a bill you covered for someone without a matching expense in the data.

Until an envelope is chosen, the row stays `needs_review` and its amount sits in Unallocated (§6.2), so the money is never lost. Reimbursements are never counted as income.
5. **Negative amount** → `expense`. The envelope comes from the existing order: user rules → known vendors → core rules → check-number heuristic → online lookup → default `Shopping`. This is kept as-is apart from moving storage into the `rules` table.
6. **Bill match.** The envelope's kind is `bill` (or any envelope that has bills), and a bill matches on phrase, amount within tolerance, and date within the window of `next_due_date`. Set `bill_id` and advance the bill (§6.4).

**Learned rules.** The existing bulk-edit "save rule" action writes `origin = user` rows to `rules`. Bulk edits can now also set `txn_kind`, person, and income label, so a rule like "always treat ZELLE FROM … as a refund" can be learned too.

### 6.2 Kinds and their effect on money
| Kind | Counts as income | Counts as spend | Envelope effect (on or after go-live) |
|---|---|---|---|
| paycheck | ✔ | | Raises Unallocated until allocated |
| other_income | ✔ | | Raises Unallocated until allocated |
| expense | | ✔ | Lowers its envelope. **Blank envelope lowers Unallocated** and is listed in the audit. |
| refund | | ✔ (negative spend) | Raises its envelope. **Blank envelope** (a reimbursement awaiting review) raises Unallocated. |
| transfer, cc_payment | | | None (excluded) |

The **Analytics tab** must switch to these kinds. Total Income = paycheck + other_income. Total Spend = −Σ(expense + refund). Category breakdowns are net of refunds. This fixes K3 in the existing UI.

### 6.3 Income calculations (`income.py`)
- **Typical paycheck** for a source: the median of its last 6 `regular` paychecks, or `typical_amount_cents` if set.
- **Bonus detection:** `amount > typical × (1 + bonus_threshold_pct/100)` → `bonus`. Computed at classification time using paychecks that came before it. The user can override in the transaction modal, which sets `user_modified`.
- **Periods per year** come from cadence: weekly with interval N → 52/N, monthly with interval N → 12/N.
- **Planning income (monthly):** Σ over active sources of `typical × periods_per_year / 12`. This is the number shown as "available to budget." It's about $11.3K/month at today's run rate. A biweekly cadence gives two months a year with three paychecks. The run-rate figure already averages those in, and the Income tab marks the three-paycheck months.
- **Observed monthly income:** the actual Σ by month, split by person and subtype, plus other income. Used in the reports.
- **Next expected pay date** per source: the next occurrence of the cadence after its last paycheck.

### 6.4 Recurrence (`recurrence.py`)
- **Model:** `{freq: once|weekly|monthly|yearly, interval: N, anchor_date}`.
- **Monthly:** keep the anchor's day of month, and clamp to the end of short months. If the anchor is the 31st, the due date is the last day of each month.
- **Functions needed:** `next_occurrence(rule, after_date)`, `previous_occurrence(rule, on_or_before)`, `occurrences_per_year(rule)`, `occurrences_between(rule, start, end)`.
- **Advancing a bill on match:** set `last_paid_date` and `last_paid_txn_id`. Set `next_due_date = next_occurrence(rule, after=current next_due_date)`. A one-time bill becomes inactive after it's paid.
- **Overdue:** `next_due_date < today` and not paid → shown as overdue on the Envelopes tab.
- **Monthly normalized amount:** `amount × occurrences_per_year / 12`. A bill envelope's `monthly_budget_cents` is the sum of this across its active bills.

### 6.5 Envelope balances and targets (`envelopes.py`)
Let *G* be the go-live date and *D* the "as of" date (default: today).

**Balance(E, D)** = Σ moves into E − Σ moves out of E (not voided, date ≤ D)
  + Σ `amount` of included transactions with `envelope = E.name`, kind in {expense, refund}, and G ≤ date ≤ D.

**Unallocated(D)** = Σ moves in − moves out
  + Σ included paycheck and other_income transactions with G ≤ date ≤ D
  + Σ included expense and refund transactions with a blank envelope and G ≤ date ≤ D.

**Invariant (must be tested):** the sum of all envelope balances, including Unallocated, equals Σ opening moves + Σ income − Σ net spend over [G, D]. A negative Unallocated balance shows an "Over-allocated" warning.

**Period sweeps (reset envelopes, D5).** These run when the app starts and when a balance is requested. For each `reset` envelope and each full calendar month since G with no sweep yet, if the balance at the end of that month is positive, create a `sweep` move to Unallocated dated the 1st of the next month, with `period_key = YYYY-MM`. A negative balance carries forward. Changing an envelope's mode does not rewrite past sweeps.

**Capacity (100% of the bar):**
- **Spending:** carry-in + the monthly budget. Carry-in is the balance at the start of the month, after the sweep.
- **Bill:** Σ over active bills of the amount for their next occurrence.
- **Savings:** `goal_cents` if set. Otherwise the balance plus the monthly contribution.

**"Should have" marker (D6):**
- **Spending (pacing down):** `marker = carry_in + budget × (1 − f)`, where `f = (day_of_month − 1) / days_in_month`. On day 15 of 30 with a $600 budget and $0 carry-in, the marker is about $320.
- **Bill (accruing up):** Σ over bills of `amount × f_bill`, where `f_bill = (D − cycle_start) / (next_due − cycle_start)` clamped to [0, 1], and `cycle_start = previous_occurrence(rule, before next_due)`. If the bill is paid early, `next_due` moves forward and its share resets to nearly zero. For a $1,800 semi-annual bill halfway through its cycle, the marker is $900.
- **Savings:** with a goal and goal date, the marker is a straight line from the go-live (or creation) balance to the goal by the goal date. With no goal, there is no marker.

**Status (for color and sorting):**
- `overdrawn`: balance < 0
- `behind`: balance < marker × 0.9
- `on_track`: balance ≥ marker × 0.9
- `funded`: balance ≥ capacity

### 6.6 Savings (D13)
Savings envelopes (e.g. "Savings: Emergency") are funded through templates like any other envelope. The real bank transfer to savings stays excluded as `transfer`. A "spend" from a savings envelope happens when the user moves money from savings back to Unallocated (a `transfer` move). Real withdrawals from the savings account stay invisible to the system.

### 6.7 Fill workflow
- **Fill queue:** included paycheck and other_income transactions with date ≥ G and no `confirmed` allocation. The count shows as a badge on the Envelopes tab.
- **Draft for a regular paycheck:** use the source template's lines. Each line shows envelope, template amount, current balance, marker, and gap (marker − balance).
- **Draft for a bonus or other income:** start with no lines and offer **Suggest** (below).
- **Totals:**
  - Remainder = deposit − Σ lines, and it stays in Unallocated.
  - If Σ lines > deposit, show a shortfall warning. Confirming is allowed only if the difference ≤ Unallocated's current balance, and the extra is taken from Unallocated with a note. Otherwise the user must reduce lines.
- **Suggest algorithm:**
  1. Fund `overdrawn` envelopes back to $0, most negative first.
  2. Fund bill envelopes up to their marker, soonest due first.
  3. Fund spending envelopes up to their marker, biggest gap first.
  4. Fund savings envelopes up to their marker.
  5. Leave the rest in Unallocated.
  The suggestion only fills the editable lines. Nothing is committed until the user confirms.
- **Confirm** creates the allocation and its fill moves in one database transaction.
- **Edit** a confirmed allocation by voiding it and confirming a new one in the same request, done in one database transaction.
- **Changing the income transaction later:**
  - **Delete** is blocked while a confirmed allocation exists. The user must void it first, or confirm "void and delete."
  - **Exclude** shows the same prompt.
  - **Amount or date change** keeps the allocation but recalculates the remainder. If the date moves before G, the user is warned.
- **Ad-hoc allocation:** "Fund from Unallocated" uses the same screen with no income transaction.
- **Transfer:** any envelope (or Unallocated) to any other, with an amount, date, and note. This is how overspending gets covered.

### 6.8 Budget suggestions (`budget.py`)
For each envelope, use only *complete* calendar months before the current month and only included transactions. Net spend = −Σ(expense + refund).

- **Spending:** use the last `suggest_window_spending_months` (6) months.
  - Show the average, median, min, max, and the number of months that have data.
  - The suggestion is the average rounded up to the nearest $5.
  - Confidence: `high` with 6 or more months of data, `medium` with 3–5, `low` with fewer than 3.
- **Bill envelopes with bill items:** the suggestion is the sum of each bill's monthly normalized amount (§6.4). Show the 12-month actual average next to it as a check.
- **Bill envelopes without bill items:** the 12-month total ÷ 12. For example, a $9,779.90 home insurance payment becomes $815/month. Also show a "Create bill from history" shortcut that pre-fills a bill using the last payment's amount, date, and an inferred interval.
- **Recurring-payee discovery** (Phase 5): a payee with 3 or more payments at a steady interval (±3 days weekly, ±5 days monthly) and an amount within ±15% is offered as a candidate bill.
- **Budget summary:** planning income vs Σ monthly budgets (spending + bill + savings) → surplus or deficit. Also show the actual average spend.

### 6.9 Fill template generation
**Generate from budget:** for each income source, the source's share = its planning income ÷ total planning income. Each line = envelope monthly budget × share ÷ (the source's periods per year ÷ 12), rounded to whole dollars. The user then edits it. A live summary shows template total vs typical paycheck, then lists the remainder or shortfall.

---

## 7. API

All endpoints return JSON. The existing endpoints stay and keep their response shapes, except where noted.

### Import (Phase 1)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/import/scan` | List files in `DataFiles/input` |
| POST | `/api/import/inspect` | Headers, a sample of rows, and a suggested column mapping, using presets |
| POST | `/api/import/preview` | Parse and classify the selected files. Returns rows with kind, person, envelope, bill, `needs_review`, and `already_in_db`. **Writes nothing.** |
| POST | `/api/import/commit` | Insert the new rows with any review-grid edits applied, update existing rows following §5.3, and record an `import_batches` row |
| GET | `/api/import/history` | Recent batches |
| GET/POST | `/api/rules`, `/api/rules/save`, `/api/rules/delete` | Rules management |
| GET | `/api/transactions/reimbursement-candidates?txn_id=` | Ranked expenses for the Match to expense picker (§6.1) |
| POST | `/api/transactions/reclassify` | Re-run rules on a list of IDs, with an option to include edited rows. Returns a before/after diff, and applies only with `apply: true`. |

### Transactions (existing, extended)
- `GET /api/transactions` adds the new fields to each row.
- `POST /api/transactions/update` and `/bulk-update` accept the new fields and set `user_modified = 1`. Bulk-update with `save_rule` writes to the `rules` table.
- `/api/transactions/delete` and `update` (when excluding) return `409` with `{allocation_id}` if a confirmed allocation exists, unless `void_allocation: true` is sent.

### Income (Phase 2)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/people` · POST `/api/people/save` | |
| GET | `/api/income/sources` · POST `/api/income/sources/save` · `/delete` | Includes computed typical amount and next pay date |
| GET | `/api/income/summary?from=&to=` | Monthly totals by person and subtype, trend points per source, YTD cards, planning income, and three-paycheck months |

### Envelopes and budget (Phases 3–4)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/envelopes?as_of=` | **Extended:** keeps `envelopes` (names) and `counts`, and adds `details[]` with kind, mode, budget, balance, capacity, marker, status, and carry-in, plus `unallocated` and `go_live_date` |
| POST | `/api/envelopes/save` | Create or update settings. `add`, `rename`, and `delete` stay. Delete enforces the zero-balance rule. |
| GET | `/api/envelopes/ledger?envelope_id=&from=&to=` | Moves and transactions with a running balance |
| GET | `/api/bills` · POST `/api/bills/save` · `/delete` · `/link` · `/unlink` | `link` manually ties a transaction to a bill |
| GET | `/api/bills/upcoming?days=30` | Includes overdue bills |
| GET | `/api/budget/suggestions` | Per envelope, per §6.8, plus the budget summary |
| POST | `/api/budget/accept` | Apply chosen suggestions as `monthly_budget_cents` |
| GET | `/api/templates` · POST `/api/templates/save` · `/generate` | |
| GET | `/api/allocations/queue` | |
| GET | `/api/allocations/draft?txn_id=` | Pre-filled lines with balances, markers, and gaps |
| POST | `/api/allocations/suggest` | `{amount_cents}` → suggested lines (§6.7) |
| POST | `/api/allocations/confirm` · `/void` | Confirm may include `replace_allocation_id` |
| POST | `/api/moves/transfer` | |
| GET/POST | `/api/settings` | |
| POST | `/api/setup/go-live` | `{date, opening: {envelope_id: cents, unallocated: cents}}`. Creates `opening` moves. Running it again requires `reset: true`, which voids the earlier opening moves. |

---

## 8. UI

### 8.1 Shell
- A tab bar sits in the existing header: **Import · Analytics · Envelopes · Budget · Income**.
- Hash routing (`#envelopes`) keeps the last tab after a refresh. Each tab loads its data when first opened.
- The Settings modal keeps the theme picker and adds: go-live date (read-only after setup, with a "Re-run setup" button), bonus threshold, suggestion windows, bill match window and tolerance, and refund lookback.
- New components use the existing CSS custom properties, so all three themes (Dark, Greenback, Ledger) work. Status colors are theme tokens: `--status-ok`, `--status-behind`, `--status-over`, `--status-funded`.

### 8.2 Import tab (Phase 1)
Port the converter flow and remove everything specific to Goodbudget:
1. **Files.** Scan the `input` folder or upload files, then pick the account type per file. This is the existing step.
2. **Mapping.** Column mapping with saved presets. This is the existing step.
3. **Review grid.** Existing grid plus columns for **Kind**, **Person**, **Income label**, **Bill**, and **Status**. Status is `New`, `Already in DB`, `Excluded`, or `Needs review`. Filter chips are provided per status. Inline edits and "save as rule" carry over. Reimbursement rows (§6.1) show a **Match to expense** button next to the envelope picker.
4. **Save to database.** This replaces "Export to Goodbudget CSV." It shows a summary: new, existing (skipped), excluded, and needs review. After saving, offer links to "Review needs-review items" and "Open fill queue (N new paychecks)."
- An import history panel lists recent batches.

**Removed:** the Goodbudget CSV export, the HTML audit summary file, the 45-character name limit and comma stripping (these were Goodbudget limits), and the `DataFiles/Output` writes.

**Kept:** sanitizing whitespace, check-number formatting, the online vendor lookup, and dedup by `txn_id`.

### 8.3 Analytics tab
The existing page, with JS moved to `js/analytics.js`. Changes:
- KPIs and the Income vs Spend chart use `txn_kind` (§6.2).
- Category charts are net of refunds.
- Filters for Kind and Person are added.
- The transaction detail modal adds Kind, Person, Subtype, Income label, and Bill. Allocation status shows for income rows. Refund rows show **Match to expense** and the linked expense, and expenses show any reimbursements linked to them with the net cost.

### 8.4 Envelopes tab (Phase 4)
- **Summary strip:** Unallocated, Total in envelopes, Fill queue (N), Overdrawn (N), Bills due in 7 days (N).
- **Fullness chart (the main view):**
  - One horizontal bar per active envelope, grouped as Bills, Spending, Savings.
  - Bar length is the balance as a percent of capacity. It overflows past 100% when funded beyond capacity, and extends left of zero when negative.
  - A vertical **tick** marks where the marker falls.
  - Bars are colored by status.
  - Labels show `name · $balance / $capacity`. The tooltip adds the marker, gap, carry-in, the next bill due, and the mode.
  - Clicking a bar opens the **envelope drawer**.
  - Built with Chart.js horizontal bars, with the marker drawn as a floating-bar dataset or annotation, or as HTML/CSS bars. The implementer can choose; theme tokens are required either way.
- **Fill queue panel:** unallocated income rows showing date, person or source, amount, and subtype. Each has an **Allocate** button that opens the allocation modal (§6.7): an editable lines table, a Suggest button, remainder or shortfall shown live, and a Confirm button.
- **Upcoming bills (30 days):** bill, envelope, due date, amount, and whether the envelope covers it. Overdue bills are listed first.
- **Envelope drawer:** settings summary, bills, and the combined ledger (moves and transactions with running balance). Actions: Transfer, Edit settings, Fund from Unallocated.
- **Before go-live:** the tab shows a call to action that opens the go-live wizard on the Budget tab.

### 8.5 Budget tab (Phase 3)
- **Planning panel:** planning income (by person), Σ budgets, surplus or deficit, and actual average spend.
- **Envelope setup table:** name, kind, rollover mode, monthly budget (editable), suggestion (average, median, min–max, confidence), and an **Accept** button. Also **Accept all high-confidence**. Bill envelopes show their budget as derived from bills.
- **Bill editor** (per envelope): add or edit bills with name, match phrase, amount mode and amount, recurrence (frequency, interval, anchor date), and next due (computed and editable). Includes a "Create from history" helper. Shows a preview of the next 3 due dates.
- **Fill templates:** one card per income source, with editable lines, **Generate from budget**, and a live total vs typical paycheck.
- **Go-live wizard:**
  1. Pick a date. 2026-11-01 is suggested.
  2. Enter an opening balance per envelope plus Unallocated. Each field shows a hint with the envelope's suggested budget. A "Copy from Goodbudget" note reminds the user to read current balances from Goodbudget by hand. No integration is built.
  3. Review the totals and confirm.

### 8.6 Income tab (Phase 2)
- **YTD cards:** YTD income per person, YTD bonus total, YTD other income, average regular monthly income (planning), and the next expected pay date per person.
- **Monthly income by person:** stacked bars for Scott regular, Kendra regular, Bonus, and Other income. Three-paycheck months are marked.
- **Paycheck trend:** one line per source showing net paycheck amount over time. Bonus points are shown as separate markers, not connected to the line.
- **Income vs budget vs spend:** monthly grouped bars for income received, Σ budgets (from go-live on), and net spend.
- **People and sources manager:** add or edit people and income sources (match phrase, cadence, anchor date, typical override, bonus threshold override). Shows a "matched N transactions" preview before saving.
- Uses the same timeframe presets as Analytics (YTD by default).

---

## 9. Migration (schema v1 → v2)

Runs automatically when the server starts. It must be safe to re-run.

1. **Back up** `budget_store.db` to `DataFiles/LocalStore/backups/budget_store_<YYYYMMDD_HHMMSS>.db`. Keep the last 10 backups.
2. Create the new tables and add the new columns.
3. **Convert dates:** rewrite `MM/DD/YYYY` to `YYYY-MM-DD`. Rows that already match `^\d{4}-\d{2}-\d{2}$` are left alone. Log any value that can't be parsed and leave it unchanged. `txn_id` values don't change, because the hash already uses ISO dates.
4. **Seed rules:** every hardcoded exclusion, the kid-transfer exception, and the Rule 5 heuristics from `Import_transactions.py` → `origin = core`. `known_vendors.json` → `origin = vendor`. `user_rules.json` phrase rules → `origin = user`. Seed `IRS TREAS` → other income "Tax Refund", `VENMO CASHOUT` and `VENMO PAYMENT` → refund with no envelope and needs review, and `STATEMENT CREDIT` → needs review.
5. **Seed people** (Scott, Kendra) and **income sources** (Cargill and HealthPartners, biweekly, with anchors as in §5.5).
6. **Seed envelopes** from `envelopes.json`, plus the system Unallocated row.
   - Kind is guessed: names starting with `Insurance:`, plus `Property Taxes` and `Bills: Mortgage`, become `bill` with `rollover`. All others become `spending` with `reset`. `Bills: Every Month` and `Bills: Subscriptions` become `bill` with `rollover`, with no bill items yet.
   - The user reviews these on the Budget tab.
7. **Backfill `txn_kind`** on existing rows:
   - Excluded rows → `transfer` or `cc_payment`, based on `exclusion_reason`.
   - Negative amounts → `expense`.
   - Positive amounts run pipeline steps 3–4. Positive rows that already have an envelope become `refund` to that envelope.
   - Bonus flags are computed in date order per source.
   - Rows the backfill can't decide get `needs_review = 1`.
8. Rename `envelopes.json`, `user_rules.json`, and `known_vendors.json` to `*.migrated.json`. They're kept as backups and never read again.
9. Set `schema_version = 2`.

**Go-live is not part of the migration.** Envelope math stays off until the wizard is completed.

---

## 10. Build Phases

Each phase ships on its own and leaves the app working. Version numbers go up per phase (v3.0 → v3.4).

### Phase 1: Foundation and Import tab (v3.0)
- Module layout (§4.2), route table (§4.3), static serving, pinned Chart.js, tab shell, and Analytics JS extracted to its own file.
- Schema v2 migration (§9), including ISO dates, `txn_kind` backfill, and the rules table.
- Rules engine (§6.1) and the import engine ported into the Import tab (§8.2). Goodbudget export removed.
- Re-import protection (§5.3) and "Re-run rules."
- Analytics switched to kind-based income and spend (§6.2, §8.3).
- Retire `Import_transactions.py` and `launch_converter.bat` (move them to `Old Scripts/`). Archive `Goodbudget_Rules.md`.

**Acceptance criteria:**
- Re-importing a file that was already imported creates 0 new rows and changes 0 user-edited fields. (As built: true for every file already in the database. Rows the user deleted earlier in the audit tool come back as "New" the first time, because nothing remembered the deletion yet; from 3.0 on deletions are remembered. See §14.)
- `SELECT MIN(date), MAX(date)` returns `2025-12-20` and `2026-10-19` for the current data.
- All 22 Cargill and 21 HealthPartners deposits are `paycheck` with the right person, and the 2026-07-31 $10,474.36 deposit is `bonus`.
- The Amazon, Target, and Boot Barn positive amounts are `refund` to Shopping.
- All positive Venmo rows are `refund` with `needs_review = 1`, and none count as income.
- The Analytics Total Income KPI no longer includes refunds.
- No remaining code refers to Goodbudget, other than the archived files.

### Phase 2: Income (v3.1)
- People and income sources CRUD, typical and bonus logic, planning income (§6.3).
- Income tab (§8.6).
- Transaction modal fields for Kind, Person, Subtype, and Income label, plus bulk-edit and learned rules for kinds.

**Acceptance criteria:**
- Planning income ≈ (median Cargill regular × 26/12) + (median HealthPartners regular × 26/12), to the cent.
- The monthly chart sums match the SQL totals per month.
- Changing a transaction's subtype immediately changes planning income.

### Phase 3: Envelope model and Budget tab (v3.2)
- Envelopes table UI, bills and recurrence (§6.4), suggestions (§6.8), templates and generation (§6.9), go-live wizard.

**Acceptance criteria:**
- The recurrence unit tests pass: month-end clamping, weekly with interval 2, semi-annual, yearly, one-time.
- Home insurance's suggestion is $9,779.90 ÷ 12, rounded up to the nearest $5.
- A generated template's total for each source is within $5 of (Σ budgets × share ÷ paychecks per month).
- Go-live creates opening moves whose total equals the amounts entered.

### Phase 4: Fills, balances, and Envelopes tab (v3.3)
- Balance engine, sweeps, marker and status (§6.5), allocations, fill queue, Suggest, transfers (§6.7), bill matching and advancing, Envelopes tab (§8.4), and the guards on income transactions.

**Acceptance criteria:**
- The §6.5 invariant holds after a scripted run of opening → 2 paychecks allocated → 20 expenses → 1 refund → 1 transfer → month rollover with sweeps.
- Voiding an allocation returns each envelope to its exact earlier balance.
- Paying a monthly bill moves `next_due_date` forward one month, and its marker share drops to about $0.
- The marker for a $600 spending envelope on the 16th of a 30-day month with $0 carry-in is $300.

### Phase 5: Polish (v3.4)
- Recurring-payee bill discovery (§6.8).
- "Create bill from history."
- Envelope drawer ledger export to CSV.
- Over-allocated and uncategorized-after-go-live warnings shown on the Envelopes tab.

---

## 11. Edge Cases

| Case | Handling |
|---|---|
| Two paychecks on the same day from one source (e.g. regular check + bonus on 07-31) | Each is classified on its own. Each gets its own allocation. |
| A three-paycheck month | Planning income already averages these in. The extra check shows in the fill queue like any other, and its template pre-fills normally. The user may choose to direct it to savings. |
| A refund larger than the envelope's spending | Allowed. The envelope balance goes up. |
| A reimbursement that arrives before the expense is imported (e.g. a friend pays you back before the card statement posts) | Leave it in review with a blank envelope (it sits in Unallocated). Match it once the expense shows up, or pick the envelope directly. |
| One Venmo cashout covering several shared expenses | Pick the envelope directly. Linking to more than one expense isn't supported, since splits are out of scope. |
| An expense dated before go-live but imported after | Ignored for balances (date < G), and still counted in Analytics and suggestions. |
| A transaction edited to move between envelopes | The balance moves automatically because it's derived (§4.1). |
| Envelope renamed | Same as today: transactions are updated by name. Moves, bills, and templates use `envelope_id` and are unaffected. |
| An expense with a blank envelope after go-live | Lowers Unallocated. Flagged in the existing audit tool and in the Envelopes summary. |
| Over-allocation (negative Unallocated) | A warning banner. Not blocked. |
| A bill paid twice in one cycle (duplicate) | The second payment won't auto-match, because the window is already past the advanced due date. It stays as a normal expense, and the duplicate audit flags it. |
| A pending vs posted transaction with a different amount | Different `txn_id`, and the existing duplicate audit handles it. A confirmed allocation on the deleted row triggers the void prompt. |
| Kid transfers | Stay `expense` → Shopping (an include-override rule). A dedicated "Kids" envelope can be set up later with a rule edit. |

---

## 12. Testing

- **Unit tests** (`tests/`, `unittest`):
  - recurrence math
  - rules pipeline, using one fixture row per §3 observed type
  - typical, bonus, and planning income
  - balance, marker, and status math
  - sweeps being safe to re-run
  - the money invariant
  - allocation confirm, void, and replace
  - migration on a copy of a v1 database, run twice to check it's safe to repeat
- **Manual checks:** each phase's acceptance criteria, run against a **copy** of the real `budget_store.db`.
- **Before each phase:** confirm the startup backup was created.

---

## 13. Assumptions to Confirm During Build

1. Paycheck amounts are **net** (the deposit amount). Gross pay isn't tracked.
2. Every positive Venmo transaction is a reimbursement. If one is ever real income (e.g. selling something), the user changes its kind to `other_income` in the review grid.
3. The spending-period length is the calendar month for every spending envelope. A weekly spending envelope isn't needed right now.
4. Opening balances are typed in by hand from Goodbudget's current values on go-live day.
5. One fill template per income source is enough. Separate templates for first vs second paycheck of the month aren't needed. The schema allows adding a `variant` column later.


---

## 14. Phase 1 Build Notes (as built, v3.0)

Phase 1 is built and working. Where the build differs from or adds to the spec above:

**Added to the design**
- **Deleted-row memory.** New `deleted_transactions` table. Deleting a transaction (e.g. resolving a duplicate in the audit tool) records its ID, so loading an overlapping bank file doesn't bring it back. The import grid shows these as "Deleted earlier" with a Restore button.
- **Early-ID aliasing.** An early build of the importer (Oct 21, morning) saved 87 transactions under a different ID formula. The importer now also computes that older ID (`textutil.legacy_txn_id`) and treats a match as "already saved", so re-importing that file doesn't duplicate those rows.
- **Rules table extras.** `rules` gained `match_mode` (`contains`, `word`, `regex`, `vendor`), `label`, and `set_needs_review`. A `payee` stage holds rename-only rules, so they can't hide an envelope rule behind them.
- **Refund rules use built-in rules too.** §6.1 step 4.2 matches user, vendor, **and core** envelope rules (so a Costco refund goes to Groceries), not just user and vendor rules.
- **Newest user rule wins.** A newly saved user rule is tried before older ones.
- **Split deposits.** The typical paycheck uses the largest deposit per pay date (median of the last 6 pay dates), so a small side deposit on the same date doesn't pull it down and cause normal checks to be flagged as bonuses.
- **Excluded rows have no envelope.** Rows excluded by built-in rules are stored with a blank envelope (the old importer filled in a meaningless default).
- **Re-run rules ignores cosmetic differences.** Payee names and the "matched by" label don't count as changes; hand-edited rows are skipped unless asked for.
- **Import grid extras.** A "Save" checkbox per new row (untick to leave one out), a files table with "Not yet imported" selection, remembered column mappings, and an import history dialog.
- **Safer server.** No CORS header (other websites can no longer read the API), requests with a foreign Host header are refused (DNS rebinding), static files can't escape `web/`, uploads never overwrite a different file.

**Module layout changes** (§4.2): added `paths.py`, `textutil.py`, `core_rules.py`, `reclassify.py`, and `api.py` (route handlers). `import_engine.py` and `rules.py` are as planned. No `income.py` reports yet (Phase 2).

**Migration notes** (§9)
- Rows are flagged `user_modified = 1` when their stored envelope or inclusion differs from what the rules would produce today, so "Re-run rules" can't undo manual work. On the real data this flagged 9 of 1,504 rows.
- Date range of current data: 2025-12-20 to 2026-10-19.
- Of 28 positive non-paycheck rows, 14 land in "Needs review" (6 Venmo, 3 statement credits, 5 deposits with no matching purchase).

**Known data notes (not changed by the build)**
- 13 transactions (all transfers or card payments, so they don't affect spending) exist twice because two overlapping checking files were saved under different ID formulas. They can be cleared with the audit tool.
- `GBhistory.csv` in the input folder is a Goodbudget export, not a bank file. Don't import it.

**Not built yet (later phases):** people/income-source editing UI, income reports (Phase 2); budgets, bills, templates (Phase 3); balances, fills, the Envelopes tab (Phase 4). The database already contains the empty tables for them.

**Tests:** `python -m unittest discover -s tests` (77 tests: rules, migration, import, API, and a live-server check).

## 15. Phase 2 Build Notes (as built, v3.1)

Phase 2 (income) is built: a new **Income** tab, people and pay-source management, and planning income.

- **Backend:** `recurrence.py` (weekly/monthly/yearly date math, month-end clamping), plus income statistics and reports in `income.py`. New endpoints: `/api/people`, `/api/people/save`, `/api/income/sources` (+ `/save`, `/delete`, `/preview`), `/api/income/summary`.
- **Income tab:** timeframe presets and dates (default year to date); per-person income cards, plan-around figure, bonuses, other income; monthly income by person (stacked, ★ marks extra-paycheck months); paycheck trend per source with bonuses as separate markers; income vs. spending by month; pay-source and people managers.
- **Pay-source editor:** shows how many saved deposits the match text would pick up before saving, and can re-tag matching deposits already saved (skipping hand-edited ones). A source with paycheck history is switched off rather than deleted.
- **Planning income** = typical regular paycheck × paychecks per year ÷ 12, per active source. On the current data: $11,317.53 a month (Scott $7,583.59, Kendra $3,733.94). Bonuses are excluded, and changing a paycheck's type changes the figure.
- **Extra-paycheck months in 2026:** Cargill Jan and Jul; HealthPartners May and Oct.
- **Small additions:** marking a deposit as a Paycheck by hand links it to the matching pay source; every pay source must belong to a person.
- **Deferred to Phase 3:** the "budget" bar in income vs. spending (budgets don't exist yet).

Tests: 92, run with `python -m unittest discover -s tests`.

## 16. Phase 3 Build Notes (as built, v3.2)

Phase 3 is built: a **Budget** tab with envelope settings, suggestions, bills, fill templates, and the go-live setup.

- **Backend:** `envelopes.py` (envelope settings, bills, go-live and opening balances) and `budget.py` (suggestions, budget-vs-income summary, templates). New endpoints: `/api/budget/envelopes`, `/api/budget/accept`, `/api/envelopes/save`, `/api/bills` (+ `/save`, `/delete`, `/preview`, `/history`), `/api/templates` (+ `/save`, `/generate`), `/api/settings`, `/api/setup/status`, `/api/setup/go-live`.
- **Suggestions:** spending uses the last 6 complete months (average, median, range, confidence); bill envelopes use the sum of their bills, or the last 12 months ÷ 12 when they have no bills yet. Insurance: Home suggests $815 a month ($9,779.90 ÷ 12, rounded up to $5). A first month that starts after the 7th is treated as partial and skipped.
- **Bills:** weekly, monthly, every N months, yearly, or one time. The monthly equivalent is amount × times per year ÷ 12. "Start from a payee" fills a bill from payments already made, using the typical (median) amount and a guessed schedule. This pulls the spec's "create bill from history" helper forward from Phase 5.
- **Templates:** "Generate from budget" spreads each envelope's monthly budget across pay sources by share of income, in whole dollars. Nothing is saved until you press Save.
- **Go-live:** opening balances are stored as moves. Negative balances (overspent envelopes) are allowed. Re-running requires confirmation and replaces the earlier opening balances.
- **Guards:** an envelope that holds money can't be deleted (switch it off instead); deleting an envelope also removes its bills and template lines.
- **Settings dialog:** now includes the bonus threshold, suggestion windows, bill matching window and tolerance, and refund lookback. Bill matching itself arrives in Phase 4.
- **Income tab:** income vs. spending now shows a dashed monthly-budget line from the go-live month on.

Tests: 118, run with `python -m unittest discover -s tests`.

## 17. Phase 4 Build Notes (as built, v3.3)

Phase 4 is built: live envelope balances, paycheck fills, transfers, bill matching, and the **Envelopes** tab.

- **Backend:** `ledger.py` (balances, month-end sweeps, markers and status, fill queue, allocations, transfers, envelope history) and `billmatch.py`. New endpoints: `/api/envelopes/balances`, `/api/envelopes/ledger`, `/api/bills/upcoming`, `/api/bills/link`, `/api/bills/unlink`, `/api/allocations/queue|draft|suggest|confirm|void`, `/api/moves/transfer|void`.
- **Balances are never stored.** They are worked out from money moves, spending tagged to the envelope, and income, counting only from go-live. A built-in check confirms every dollar is accounted for (balances = opening money + income − net spending); the tab shows a warning if it ever fails.
- **Targets:** a spending envelope's "should have" marker counts down through the month (a $600 budget on the 16th of a 30-day month is $300). A bill's marker builds up toward its due date (a $1,800 six-month bill is about $900 at the halfway point). Savings follow a straight line to the goal date.
- **Carry-in** is what an envelope held at the start of the month: earlier months, month-end sweeps, and opening balances. Fills and transfers made during the month are not counted, so they can't inflate the month's target.
- **Month-end:** "start fresh" envelopes hand any positive leftover to Unallocated on the 1st; "carry over" envelopes keep it; negative balances always carry forward. Each month is swept once, and later edits don't rewrite the past.
- **Fills:** each paycheck gets a split screen pre-filled from its pay source's template (bonuses and other income start empty). Suggest funds overdrawn envelopes first, then bills (soonest due), spending (biggest gap), then savings. A fill can't take more than Unallocated holds. Undoing a fill returns every envelope to exactly its earlier balance; the deposit can then be filled again. Added a `voided_income_txn_id` column to keep the record.
- **Guards:** a paycheck that has been filled can't be deleted, excluded, or changed to another kind until its fill is undone (the app asks and does it for you). Amount edits are allowed.
- **Bill matching:** when imported expenses land in an envelope, a bill is matched on bank text, amount (within tolerance), and date (within the window; ±3 days for weekly). The bill's next due date moves forward one cycle. Estimated bills follow the average of the last 3 payments; one-time bills switch off once paid. Payments can be linked or unlinked by hand.
- **Envelopes tab:** a fullness chart (bars with a "should have" line, grouped Bills / Spending / Savings, overdrawn shown to the left of zero), a deposits-to-fill list, bills due in 30 days (overdue first, with whether the envelope covers each), an envelope history drawer with running balance, plus Move money and Fund from Unallocated.
- **Analytics:** income rows show a "Funded" or "To fill" badge.

Tests: 151, run with `python -m unittest discover -s tests`.

**Backdated go-live (added after Phase 4).** Go-live can be set to any past date. Every paycheck on or after it appears under "Deposits to fill", and each fill is dated on its paycheck. Because month-end leftovers are worked out as soon as balances are first viewed (before the old fills exist), a fill, transfer, or undo dated in an already-swept month now rewinds the sweeps from that month on, and they are worked out again from the corrected balances.

## 18. Phase 5 Build Notes (as built, v3.4) — build complete

Phase 5 (polish) is built. All five phases are now done.

- **Bills we noticed (Budget tab):** payees paid at a steady interval and a steady amount, with no bill yet, are offered as bills. A payee needs 3 or more payments on different days; most gaps must sit near weekly, every 2 weeks, monthly, every 2/3/6 months, or yearly, and most amounts within 15% of the usual one. Payees that stopped paying are left out. "Add bill" creates the bill (due dates worked out from the last payment); "×" dismisses a payee for good. On the current data it finds the monthly ortho payment, the quarterly trash service, Peacock, and Netflix.
- **Envelope history export:** an "Export CSV" button in the envelope drawer downloads that envelope's history (date, type, description, note, amount, running balance).
- **Warnings on the Envelopes tab:** a red banner when Unallocated is negative (over-allocated), and an amber banner when spending or refunds since go-live have no envelope, with an "Assign envelopes" button that opens the Spending tab filtered to exactly those transactions.
- **Fix:** setting go-live now refreshes the Spending tab's copy of the date straight away.
- **Already delivered earlier, ahead of this phase:** "create a bill from history" (Phase 3).

Tests: 167, run with `python -m unittest discover -s tests`.

## 19. Cleanup: pattern (regex) rename rules

Rename rules can now match with a regular expression and reuse captured groups in the new name.

- **Syntax:** tick "Match with a pattern" in the Rules dialog and choose "Rename payee". The replacement can use `\1`, `$1`, `${1}`, or a named group `\g<name>`. A pattern rule replaces only the part it matches, so use `(.*)` or `^...$` to rewrite the whole name.
- **Examples:** `SQ\s?\*\s?(.*)` → `\1` turns *SQ \*Vendor* into *Vendor*; `(.*?)\s*#\d+$` → `\1` turns *Store #3456* into *Store*.
- **Chaining:** pattern renames apply in order, each to the previous result, so *SQ \*Store #3456* becomes *Store*. A plain (non-pattern) rename is final for that payee.
- **Categorizing is unaffected:** envelope and kind rules still look at the original bank text, so a rule for `BABES PLACE` still matches *SQ \*BABES PLACE*.
- **Safety:** patterns and group references are checked when you type (a replacement that uses a group the pattern doesn't have is refused), and a live preview shows how many saved transactions would change and a few before/after examples. When you add the rule, you're asked whether to rename those saved transactions too; ones you edited by hand are skipped.

Tests: 181.

**Rule descriptions.** Each rule can have an optional one-line description (up to 120 characters), such as "Remove Square POS prefix". It is typed in a box above the match text when adding a rule, and shown in bold above the match text in the rules list. The rules search also looks at descriptions.

**Editing rules.** Every rule in the Rules dialog has an Edit button. It loads the rule into the form at the top (description, match text, pattern on or off, and what it does) and the button becomes "Save changes". A rule keeps its place in the order, and switching what it does clears settings that no longer apply. Renames offer to update saved transactions again. A few special built-in rules (for example the statement-credit review flag) have options the form can't show; their Edit button is disabled, and they can still be turned off or replaced with your own.

**Links from deposits to their fill.** Income deposits dated on or after go-live show a clickable "Funded ›" badge (or "To fill ›" if not yet split) on the Spending and Income tabs. Clicking it opens the deposit's fill screen, where the split can be viewed, edited, replaced, or undone. The transaction editor has the same link ("View or edit fill…" / "Fill envelopes…").

## 20. Audit (v3.4+)

The header **Audit** button opens a dialog (web/js/audit.js, engine in evbudget/audit.py) with checks grouped as Duplicates, Unusual charges and Housekeeping. Each check has "Run now" and "Every import" switches; choices, the "large" amount (default $500), lookback window (default 120 days) and "open after import" are stored in settings.

- Manual runs scan full history (findings limited to the lookback window); import runs scan only the rows just saved (`audit_last_import_ids`). A red badge on the button shows the pending count from the last import.
- "Mark OK" is remembered in `audit_reviews` (check id + sorted txn ids). Duplicate findings can be opened in the existing side-by-side compare window; the dialog returns afterwards.
- Endpoints: GET /api/audit/config, POST /api/audit/settings, /run, /dismiss, GET /api/audit/pending. `import_commit` returns `audit` and `audit_auto_open`.
- The near-day duplicate check ignores P2P transfers and amounts under $15 to avoid allowance/copay noise.
