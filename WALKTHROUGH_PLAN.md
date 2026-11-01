# EVBudget: Setup Walkthrough

**Status:** Built (phases 1 to 5)
**Date:** 2026-11-01
**Applies to:** `web/dashboard.html`, `web/dashboard.css`, `web/js/walkthrough.js`, `web/js/income.js`, `web/js/budget.js`, `evbudget/walkthrough.py`, `evbudget/income_discovery.py`, `evbudget/api.py`
**Related:** `ENVELOPE_BUILD_SPEC.md` (the features this walkthrough teaches)

---

## 1. Goals

1. Get someone who has never used EVBudget, or envelope budgeting, from an empty budget to a working one, with envelopes funded and balances tracking.
2. Explain the idea in one or two lines, then teach by doing: each step opens the right tab and points at what to click.
3. Let the user go **Back**, **Next**, or **Cancel** at any point, and resume later where they left off.
4. Allow re-running at any time from **Help** in the header and from Settings.

### Non-goals
- Tours of every feature (Audit, Card bills, Rules editor, Re-run rules). The engine makes these easy to add later, but they're not in this build.
- Sample or demo data. The walkthrough works on the user's own data.
- A separate browser window. See D1.

---

## 2. Decision Log

| # | Topic | Decision |
|---|---|---|
| D1 | Where directions appear | A **floating panel inside the app**, not a `window.open` popup. Popups get blocked, hide behind the main window, and are awkward to keep in sync. The panel can be dragged and minimized, and a ring highlights the control to use. |
| D2 | Step checks | Each step **checks whether it's done** and shows ✓ with a short result (for example "1,703 transactions, Dec 2025 to Oct 2026"). **Next always works.** If the step isn't done, Next asks "Skip this for now?" in the panel and says what will be missing. There's no separate Skip button. Skipped steps are listed on the last step, each with a *Go do it* link. |
| D3 | Auto-start | Starts by itself the first time a budget opens with **no transactions and no go-live date**, and the walkthrough has never been started or dismissed for it. Existing budgets never auto-start. |
| D4 | Progress | Saved **per budget** in the database (`settings` key `walkthrough`). Closing the app and coming back resumes at the same step. The panel's position and minimized state are per-browser conveniences in `localStorage`. |
| D5 | Re-run | **Help** in the header opens a menu: *Resume setup (step N of 9)* and *Start setup from the beginning*. Settings has a *Restart walkthrough* button. On a budget that's already set up, most steps show ✓ right away. |
| D6 | Step order | Import → review → pay sources → envelopes → monthly amounts → bills → paycheck split → go-live → routine. See §3. |
| D7 | Envelope editing before go-live | The **Manage envelopes** button only existed inside the live Envelopes view. The Budget tab's Envelopes card now has one too, and the Budget tab refreshes when the envelope manager closes. |
| D8 | Step definitions | Steps are **data** (`WT_STEPS` in `walkthrough.js`). Changing wording or order, or adding a tour later, means editing a list. |
| D9 | People and pay sources | **One step.** The pay source dialog's Person list has *+ New person…*, which creates the person when the source is saved. It's selected automatically when nobody exists yet. People can still be managed on the Income tab. |
| D10 | Go-live date | **Defaults to today** (it used to be the 1st of next month), so balances show up straight away. |
| D11 | Finding paychecks | A new **Deposits we noticed** card on the Income tab, which works like "Bills we noticed". *Make pay source* opens the pay source dialog with the name, bank text, schedule, and a known pay date filled in. |
| D12 | Dialogs | When a dialog opens, the panel moves beside it if there's room. If there isn't (wide dialogs like go-live, or phone width), it shrinks to its pill ("Setup 8/9 · tip", with the tip as a tooltip) so it doesn't cover the dialog's buttons. Clicking the pill opens the panel anyway. Steps can show a tip that only appears while a given dialog is open. |

---

## 3. The Steps

### Why this order
- **Import comes first.** Budget suggestions, Deposits we noticed, and Bills we noticed all need transaction history.
- **Pay sources come before budgets.** The Budget tab's "Income to plan around" and "Left over" numbers come from pay sources.
- **The paycheck split comes after budgets.** *Generate* builds it from the monthly amounts.
- **Go-live comes last.** Opening balances need the final list of envelopes.

| # | Step | Tab | Ring around | Done when |
|---|---|---|---|---|
| — | **Welcome** (centered dialog) | — | — | — |
| 1 | Bring in your bank history | Import | Drop box, then *Process & review* | any transactions, or the review list is showing |
| 2 | Check the envelopes and save | Import | *Save to database* | any transactions |
| 3 | Where your paychecks come from | Spending & Income (Income view) | Deposits we noticed, or *+ Add pay source* | at least 1 active pay source |
| 4 | Your envelopes | Budget | *Manage envelopes* | always (shows the count of each type) |
| 5 | Give each envelope a monthly amount | Budget | *Accept all high-confidence* | no active spending envelope at $0 |
| 6 | Add your bills | Budget | Bills we noticed, or the envelope table | every active bill envelope has a bill |
| 7 | Decide how each paycheck is split | Budget | Fill templates | every pay source has a split with amounts |
| 8 | Start tracking | Budget | *Set up go-live* | go-live date set |
| 9 | You're set up: the routine | Envelopes | Deposits to fill | always; *Finish* |

**Welcome:** "Every dollar gets a job. When money comes in, you split it into envelopes like Groceries, Gas, and Mortgage. When you spend, it comes out of that envelope, so you always know what's left." Buttons: *Start setup* · *Not now* (minimizes to the pill at step 1 and stays on the current tab) · *Don't show this again*.

**Go-live tip, for people new to envelopes:** start with your checking balance **minus what you owe on credit cards today** (that money is already spent). Fill envelopes with bills due soon first, and leave the rest in Unallocated. This tip is in both the walkthrough and the go-live dialog.

---

## 4. Server

| Method | Path | Does |
|---|---|---|
| GET | `/api/walkthrough` | Returns `{state, status, auto_start}`. `status` holds the counts behind each step's checkmark. |
| POST | `/api/walkthrough` | Saves `{status, step, skipped}`. `status` must be `not_started`, `active`, `paused`, `dismissed`, or `done`; step names must be short lowercase identifiers. |
| GET | `/api/income/discover` | Repeating deposits that look like paychecks |
| POST | `/api/income/discover/dismiss` | Hides one candidate for good (`dismissed_income_candidates` setting) |

**How paychecks are detected** (`income_discovery.py`):
- Deposits are grouped by the first three meaningful words of the bank description. Words with digits and bank filler (PPD, ID, ACH…) are ignored, so changing trace numbers still group together.
- A group needs at least 3 pay dates. Most gaps must sit near 7, 14, or 30 days, and the usual amount must be at least $50. Bonuses can stand out from the typical amount.
- Groups whose last deposit is long before the newest imported transaction are left out.
- The suggested bank text is the longest start all descriptions share, cut back to whole words with numbers and filler removed.
- Deposits already matched by a pay source are skipped.
- Checked against the real Reilings data with its pay sources removed: it found exactly Cargill and HealthPartners, every 2 weeks, with the same match text that had been entered by hand.
- **Known gap:** twice-a-month pay (the 1st and 15th) isn't detected, because the schedule model has no "semi-monthly" option.

---

## 5. Files

| File | Change |
|---|---|
| `web/js/walkthrough.js` | **New.** Steps, engine, panel, pill, ring, welcome dialog, Help menu. Builds its own DOM. |
| `web/dashboard.css` | Walkthrough styles at the end. Uses theme tokens only, and respects reduced motion. |
| `web/dashboard.html` | Help button and menu, Settings row, Manage envelopes on the Budget tab, Deposits we noticed card, New person field, go-live tip, script tag. |
| `web/js/income.js` | Deposits we noticed; the pay source dialog takes prefilled values and can create a new person. |
| `web/js/budget.js` | Go-live defaults to today; the tab refreshes after the envelope manager closes. |
| `evbudget/walkthrough.py` | **New.** State, status counts, auto-start rule. |
| `evbudget/income_discovery.py` | **New.** Paycheck detection. |
| `evbudget/api.py` | Four routes. |
| `tests/test_walkthrough.py` | **New.** 13 tests: state, validation, auto-start, status counts, detection (every 2 weeks, monthly, already-matched, irregular/small/stale, dismiss, phrase trimming). |

---

## 6. Testing done
- Full suite: 346 tests pass.
- Driven in headless Chrome against a throwaway budget:
  - The welcome appears on an empty budget. Start, Skip confirm, Stop, and the Help menu resume all work.
  - After loading a copy of real transactions, steps 1 and 2 show ✓. Step 3 lists both paychecks. *Make pay source* fills the dialog, and the panel sits beside it with the tip showing. *+ New person* created "Scott", and the step turned ✓.
  - Steps 4 to 8 work. The go-live date defaults to today, and the panel shrinks to its pill over the wide go-live dialog.
  - Reloading resumes at the same step. Finish lists the skipped steps.
  - Checked in the Dark and Ledger themes and at phone width. There were no console errors.

## 7. Possible next steps
- Semi-monthly pay schedules, so twice-a-month paychecks can be detected and planned.
- Short tours for other areas (Audit, Rules, Card bills), using the same step list format.
