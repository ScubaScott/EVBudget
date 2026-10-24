#!/bin/bash
# Puts an EVBudget app (with the app icon) on your Desktop. Double-click it to open the dashboard.
# Run this once; rerun it if you move this folder.

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
APP="$HOME/Desktop/EVBudget.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" || exit 1
cp assets/evbudget.icns "$APP/Contents/Resources/evbudget.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>EVBudget</string>
    <key>CFBundleDisplayName</key><string>EVBudget</string>
    <key>CFBundleIdentifier</key><string>local.evbudget.launcher</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>evbudget</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1.0</string>
</dict>
</plist>
PLIST

# The app only opens the launch script in Terminal, so the server's output and Ctrl+C work as usual.
cat > "$APP/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
open -a Terminal "$ROOT/launch_dashboard.command"
LAUNCHER
chmod +x "$APP/Contents/MacOS/launcher" "$ROOT/launch_dashboard.command"
touch "$APP"

echo "Created $APP"
read -r -p "Press Enter to close..."
