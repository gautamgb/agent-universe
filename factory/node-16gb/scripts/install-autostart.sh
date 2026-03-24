#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART_SCRIPT="$SCRIPT_DIR/autostart-gateway.sh"
PLIST_PATH="$HOME/Library/LaunchAgents/com.agent-universe.gateway-autostart.plist"
LAUNCHD_LABEL="com.agent-universe.gateway-autostart"
LOG_PATH="$HOME/Library/Logs/agent-universe-gateway-launchd.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod +x "$AUTOSTART_SCRIPT"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${AUTOSTART_SCRIPT}</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <false/>

  <key>StandardOutPath</key>
  <string>${LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_PATH}</string>
</dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}"

echo "Installed ${LAUNCHD_LABEL}"
echo "plist: ${PLIST_PATH}"
echo "script: ${AUTOSTART_SCRIPT}"
