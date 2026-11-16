#!/bin/zsh
# Build "Label Printer Bridge.app": a tiny app bundle that runs the Bluetooth
# daemon. macOS only lets a process use Bluetooth if the app responsible for it
# declares NSBluetoothAlwaysUsageDescription, which Claude and plain python do not.
set -euo pipefail
REPO=${0:A:h:h}
APP=${1:-$HOME/Applications/Label Printer Bridge.app}
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>uk.marcuslab.label-printer-bridge</string>
  <key>CFBundleName</key><string>Label Printer Bridge</string>
  <key>CFBundleExecutable</key><string>bridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key><string>Talks to the SUPVAN E11 label printer.</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/bridge" <<SH
#!/bin/zsh
exec "$REPO/.venv/bin/python" -m supvan_e11_mcp.daemon "\$@" >> "\$HOME/Library/Logs/label-printer-bridge.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/bridge"
codesign --force --sign - "$APP" 2>/dev/null || true
echo "$APP"
