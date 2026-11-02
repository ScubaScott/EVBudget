# EVBudget: notes for every conversation

EVBudget is a local envelope-budgeting app for non-technical household users. Python standard library plus `pypdf`, a SQLite database per budget, and a plain HTML/CSS/JS front end. No build step.

## Versions and releases

A **release is a push to `main`**: that is when users can get it (Help > Check for updates pulls `main`). Every push to `main` that changes the app needs a new version. The version is a string, `VERSION = "3.6"` in `evbudget/__init__.py`.

**Which number**
- Features and fixes: the next 0.1 (3.5 to 3.6, 3.9 to 3.10).
- A push that is only bug fixes right after a feature release may use 3.6.1, 3.6.2.
- A new major (4.0) only for a database migration or a redesign of how people use the app. Confirm with the user first.
- Tests, docs and notes alone need no version.

**Records to write for each version** (all three, before pushing):
1. `VERSION` in `evbudget/__init__.py`.
2. A one-line entry in `CHANGELOG.md`, newest first: `- **3.6** (YYYY-MM-DD): short summary.`
3. An entry in `evbudget/releases.json`, newest first, with `version`, `date`, `title` (the same short summary), and `notes`. Each note is `{"type": "new" | "improved" | "fixed", "text": "..."}`: plain sentences a household user understands (what changed and where to find it; no jargon or file names).

While a version is not yet pushed, keep adding to its entries. Once it is on `origin/main` it is released: the next change gets the next version. Check with `python tools/check_release.py --status`, or `git log origin/main`.

**Enforcement**
- `.githooks/pre-push` (runs `tools/check_release.py`) refuses a push to `main` that changes the app without a bigger version, a releases.json entry with notes, and a CHANGELOG line. Turn it on once on the development computer: `git config core.hooksPath .githooks`. Skip once on purpose with `git push --no-verify`; do not do that without being told to.
- Tests fail if the current `VERSION` has no release notes, or any version in releases.json lacks its CHANGELOG line.
- **`/release`** (in `.claude/commands/release.md`) does the whole job: picks the version, writes the notes, runs tests, asks, commits and pushes. When the user asks you to commit or push app changes, prepare a release this way.

**Where work happens:** all development, commits and pushes happen on the Windows computer. The Mac only runs the app and updates itself through Help > Check for updates (fast-forward only), so nothing is ever pushed from there and it needs no hook.

**What users see:** after an update, the popup lists every version since the one that budget last saw (3.4 to 3.6 shows 3.5 and 3.6). The update dialog previews those notes before installing, and Help > What's new lists every version. A budget sees each version once, so changes added to an already-seen version will not pop again.

## Project layout

- `evbudget/api.py`: all HTTP routes (`App.build_router`), plain `http.server`, single-threaded. State-changing POSTs must send JSON (`_need_json`).
- `tools/check_release.py` and `.githooks/`: the release check. `.claude/commands/`: project slash commands.
- `evbudget/`: `store.py` (SQLite), `rule_engine.py`/`rules.py`/`reclassify.py` (rules), `updater.py` (in-app git update), `releases.py`, `walkthrough.py`, `income_discovery.py`, `folder_picker.py`.
- `web/dashboard.html`, `web/dashboard.css`, `web/js/*.js`: vanilla JS, theme colors are CSS variables, Chart.js from a CDN.
- `tests/`: `python -m unittest discover -s tests` (all must pass).
- Launched with `launch_dashboard.bat` / `.command`. These must not run `git pull`; updates come from Help > Check for updates (fast-forward only on `main`).

## How we work

- **Rules and bulk edits always preview first:** any action that rewrites saved transactions shows a list with a tick box per row before it applies. Rows the user edited by hand start unticked.
- **Test in a browser, never on the real budget.** Use a throwaway budget or a copy of the database, drive it with headless Chrome, and stop the test server afterward. Run the full unit suite before saying a change is done.
- **Do not commit or push unless asked.** When committing, end the message with the attribution line the session gives. Release steps are in the section above.
- **Keep it simple for users:** fewer buttons and one dialog per task over many near-identical ones; help text on anything that is not obvious (for example the High/Medium/Low confidence pills).
- **Don't hide failures:** error messages must say what actually failed, not a generic "failed to connect". A chart or render error must never leave the page broken until restart.
- **Cross-platform:** Windows, macOS and Linux. Open files, folders and email through the OS from the server, not by navigating the app window (it runs tabless, with no Back button).
- Specs and plans: `ENVELOPE_BUILD_SPEC.md`, `WALKTHROUGH_PLAN.md`.
