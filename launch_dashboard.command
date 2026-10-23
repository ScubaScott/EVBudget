#!/bin/bash
# Launches the EVBudget dashboard on macOS (double-click this file in Finder).
# Pulls the latest main from Git first. First run creates a private Python environment in .venv and installs requirements.txt.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/ or run: brew install python"
    read -r -p "Press Enter to close..."
    exit 1
fi

# Get the latest code from main. Fast-forward only, so it never overwrites or merges over local edits;
# if it can't (offline, local changes in the way, not on main) it says so and launches what is here.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ]; then
        echo "Checking for updates..."
        git pull --ff-only origin main || echo "Could not update (offline or local changes). Launching the current version."
    else
        echo "Not on the main branch, skipping update."
    fi
fi

if [ ! -x .venv/bin/python ]; then
    echo "Setting up Python environment (first run only)..."
    python3 -m venv .venv || { read -r -p "Could not create .venv. Press Enter to close..."; exit 1; }
fi

# Quietly keep dependencies current; if offline, carry on with what is installed.
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || echo "Could not install requirements (offline?). PDF import may not work."

.venv/bin/python budget_dashboard.py
read -r -p "Press Enter to close..."
