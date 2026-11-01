#!/bin/bash
# Launches the EVBudget dashboard on macOS (double-click this file in Finder).
# Updates come from the Help menu inside the app ("Update available"). First run creates a private Python environment in .venv and installs requirements.txt.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/ or run: brew install python"
    read -r -p "Press Enter to close..."
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Setting up Python environment (first run only)..."
    python3 -m venv .venv || { read -r -p "Could not create .venv. Press Enter to close..."; exit 1; }
fi

# Quietly keep dependencies current; if offline, carry on with what is installed.
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || echo "Could not install requirements (offline?). PDF import may not work."

# Troubleshooting options go after the command, e.g.:  ./launch_dashboard.command --no-app-window   or   --keep-running
# Closing the page or using Quit stops the server. If it stops with an error, keep the window open to read it.
.venv/bin/python budget_dashboard.py "$@" || read -r -p "Stopped with an error. Press Enter to close..."
