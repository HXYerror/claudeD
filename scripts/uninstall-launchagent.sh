#!/bin/bash
set -euo pipefail
INSTALL_DIR="$HOME/Library/LaunchAgents"
CACHE_DIR="$HOME/Library/Caches/clauded"
UID_GUI="gui/$(id -u)"
for name in com.hxy.clauded com.hxy.clauded.healthcheck; do
    launchctl bootout "$UID_GUI/$name" 2>/dev/null || true
    rm -f "$INSTALL_DIR/${name}.plist"
done
# #168: clean up cache files (heartbeat + per-day restart counters) so a
# stale heartbeat from a previous install doesn't confuse a future
# healthcheck reinstall into thinking a long-dead bot is wedged.
rm -f "$CACHE_DIR/heartbeat"
rm -f "$CACHE_DIR/restart-count."*  # per-day files: restart-count.YYYYMMDD
echo "✅ Uninstalled. Logs preserved at $HOME/Library/Logs/clauded/ (caches cleaned)."