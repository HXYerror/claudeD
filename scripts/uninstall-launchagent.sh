#!/bin/bash
set -euo pipefail
INSTALL_DIR="$HOME/Library/LaunchAgents"
UID_GUI="gui/$(id -u)"
for name in com.hxy.clauded com.hxy.clauded.healthcheck; do
    launchctl bootout "$UID_GUI/$name" 2>/dev/null || true
    rm -f "$INSTALL_DIR/${name}.plist"
done
echo "✅ Uninstalled. Logs preserved at $HOME/Library/Logs/clauded/."
