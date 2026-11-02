# EVBudget: notes for every conversation

EVBudget is a local envelope-budgeting app for non-technical household users. Python standard library plus `pypdf`, a SQLite database per budget, and a plain HTML/CSS/JS front end. No build step.

## Every change that ships: keep the version and the change log current

Do this before you finish any task that changes behavior, UI, or data handling (skip it for pure refactors, tests, or docs):

1. **Version:** `VERSION` in `evbudget/__init__.py`. Bump it for a new release; add to the current one only while it is still unreleased.
2. **One-line entry in `CHANGELOG.md`:** newest first, format `- **3.5** (YYYY-MM-DD): short summary.` Keep it to one line. Update the existing line if the version is still the current one.
3. **In-app notes in `evbudget/releases.json`:** newest first, one entry per version with `version`, `date`, `title` (the same short summary as the change log line), and `notes`. Each note is `{"type": "new" | "improved" | "fixed", "text": "..."}`: plain sentences a household user understands (what changed and where to find it; no jargon or file names). Tests fail if the current `VERSION` lacks notes, or if any version in `releases.json` lacks its `CHANGELOG.md` line.
4. How users see it: after an update, the popup lists every version since the one that budget last saw (3.4 to 3.6 shows 3.5 and 3.6). The update dialog previews those notes before installing, and Help > What's new lists every version. A budget sees each version once, so changes added to an already-seen version will not pop again: use a new version when users should be told.

## Project layout

- `evbudget/api.py`: all HTTP routes (`App.build_router`), plain `http.server`, single-threaded. State-changing POSTs must send JSON (`_need_json`).
- `evbudget/`: `store.py` (SQLite), `rule_engine.py`/`rules.py`/`reclassify.py` (rules), `updater.py` (in-app git update), `releases.py`, `walkthrough.py`, `income_discovery.py`, `folder_picker.py`.
- `web/dashboard.html`, `web/dashboard.css`, `web/js/*.js`: vanilla JS, theme colors are CSS variables, Chart.js from a CDN.
- `tests/`: `python -m unittest discover -s tests` (all must pass).
- Launched with `launch_dashboard.bat` / `.command`. These must not run `git pull`; updates come from Help > Check for updates (fast-forward only on `main`).

## How we work

- **Rules and bulk edits always preview first:** any action that rewrites saved transactions shows a list with a tick box per row before it applies. Rows the user edited by hand start unticked.
- **Test in a browser, never on the real budget.** Use a throwaway budget or a copy of the database, drive it with headless Chrome, and stop the test server afterward. Run the full unit suite before saying a change is done.
- **Do not commit unless asked.** When committing, end the message with the attribution line the session gives.
- **Keep it simple for users:** fewer buttons and one dialog per task over many near-identical ones; help text on anything that is not obvious (for example the High/Medium/Low confidence pills).
- **Don't hide failures:** error messages must say what actually failed, not a generic "failed to connect". A chart or render error must never leave the page broken until restart.
- **Cross-platform:** Windows, macOS and Linux. Open files, folders and email through the OS from the server, not by navigating the app window (it runs tabless, with no Back button).
- Specs and plans: `ENVELOPE_BUILD_SPEC.md`, `WALKTHROUGH_PLAN.md`.
