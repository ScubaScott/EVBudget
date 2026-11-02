---
description: Prepare and push a release - version bump, release notes, change log, tests, commit, push
---

Prepare a release of EVBudget. Follow AGENTS.md ("Versions and releases"). Work through these steps in order and stop if one fails.

1. **See where things stand.** Run `python tools/check_release.py --status` (it fetches `origin/main`), then `git status --short` and `git log origin/main..HEAD --oneline`. If nothing changed in the app since `origin/main` (only tests or docs), say so and stop: no release is needed.
2. **Understand what changed.** Read the diff since `origin/main` (committed and uncommitted, `git diff origin/main`). Work out what a household user would notice: new things, improvements, fixes.
3. **Pick the version.** The next 0.1 above what is on `origin/main` (3.5 -> 3.6). Use 3.6.1-style for a push that is only bug fixes after a feature release. Use the next major (4.0) only for a database migration or a redesign of how people use the app, and confirm that with the user first. If `VERSION` in `evbudget/__init__.py` is already above what is on `origin/main`, the release is in progress: keep that version and just finish its notes.
4. **Write the three records,** updating existing entries for this version rather than adding duplicates:
   - `VERSION = "x.y"` in `evbudget/__init__.py` (a string).
   - `evbudget/releases.json`: a new entry at the top with `version`, today's `date`, a short `title`, and `notes` as `{"type": "new" | "improved" | "fixed", "text": "..."}` in plain language for non-technical users.
   - `CHANGELOG.md`: one line, `- **x.y** (YYYY-MM-DD): short summary.`, newest first.
5. **Run the full tests:** `python -m unittest discover -s tests`. If anything fails, fix it or report it and stop.
6. **Show the user** the version, the notes and the changelog line, and ask for approval to commit and push.
7. **On approval,** stage everything that belongs to the release, commit with a clear message (end it with the attribution line the session gives), and `git push origin main`. The pre-push hook re-checks the version, notes and change log line. If the push is refused, read its message and fix what it names; never use `--no-verify` unless the user says to.
8. Report the version that went out.
