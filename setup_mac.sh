#!/bin/bash
# One-time setup for EVBudget on a Mac. Open Terminal, go to this folder, and run:
#
#     bash setup_mac.sh
#
# It is safe to run again at any time. It fixes file permissions and line endings, installs the Python
# packages into a private .venv folder, and puts the EVBudget icon on your Desktop.

cd "$(dirname "$0")" || exit 1

step() { printf '\n==> %s\n' "$*"; }
stop() { printf '\nSetup stopped: %s\n\n' "$*"; exit 1; }

step "1/5 Fixing line endings and permissions on the launch scripts"
# Files made on Windows end every line with an invisible extra character that macOS cannot run.
for f in *.command *.sh; do
    [ -f "$f" ] || continue
    LC_ALL=C sed -i '' $'s/\r$//' "$f"
    chmod +x "$f"
done
# Files downloaded or copied from elsewhere can be flagged "quarantined", which makes macOS refuse to run them.
xattr -dr com.apple.quarantine . 2>/dev/null || true
echo "Done."

step "2/5 Checking for Python 3.9 or newer"
# A fresh Mac has a placeholder python3 that only offers to install developer tools, so run it for real.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    stop "Python 3.9 or newer was not found.
  Install it from https://www.python.org/downloads/macos/ (download and run the installer),
  or with Homebrew:  brew install python
  If a window popped up asking to install developer tools, you can click Install and wait for it
  to finish. Then close Terminal, open it again, and re-run:  bash setup_mac.sh"
fi
python3 --version

step "3/5 Creating the private Python environment (.venv)"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv || stop "Could not create the .venv folder."
fi
echo "Done."

step "4/5 Installing the required packages (needs internet)"
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || stop "Could not install the packages. Check your internet connection and run this again."
.venv/bin/python -c "import evbudget, pypdf" 2>/dev/null || stop "EVBudget did not load. Run this to see why:  .venv/bin/python budget_dashboard.py"
echo "Done."

step "5/5 Putting the EVBudget icon on your Desktop"
# The shortcut script ends by waiting for Enter, so feed it one.
echo | bash ./create_desktop_shortcut.command || stop "Could not create the Desktop icon."

cat <<'DONE'

All set. To start EVBudget, double-click the EVBudget icon on your Desktop.
A Terminal window opens and the dashboard appears in your browser. Closing the page stops it.

If the icon ever does nothing, start it from Terminal instead so any error is visible:
    cd into this folder, then run:  ./launch_dashboard.command
If macOS says the icon is from an "unidentified developer", right-click it, choose Open, then Open.
DONE
