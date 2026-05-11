#!/bin/bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="$HOME"
TEMPLATE_DIR="$REPO/scripts"
INSTALL_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/clauded"
CACHE_DIR="$HOME/Library/Caches/clauded"

# Sanity
if [ ! -x "$REPO/.venv/bin/clauded" ]; then
    echo "❌ $REPO/.venv/bin/clauded not executable. Run: cd $REPO && python -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi
if [ ! -f "$REPO/.env" ]; then
    echo "❌ $REPO/.env missing. Copy .env.example and set DISCORD_BOT_TOKEN."
    exit 1
fi

mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$CACHE_DIR"
chmod 700 "$LOG_DIR" "$CACHE_DIR"

for name in com.hxy.clauded com.hxy.clauded.healthcheck; do
    sed -e "s|{{REPO}}|$REPO|g" -e "s|{{HOME}}|$HOME_DIR|g" \
        "$TEMPLATE_DIR/${name}.plist.template" > "$INSTALL_DIR/${name}.plist"
done

# Modern bootstrap syntax (macOS 10.10+)
UID_GUI="gui/$(id -u)"
launchctl bootout "$UID_GUI/com.hxy.clauded" 2>/dev/null || true
launchctl bootout "$UID_GUI/com.hxy.clauded.healthcheck" 2>/dev/null || true
launchctl bootstrap "$UID_GUI" "$INSTALL_DIR/com.hxy.clauded.plist"
launchctl bootstrap "$UID_GUI" "$INSTALL_DIR/com.hxy.clauded.healthcheck.plist"
launchctl enable "$UID_GUI/com.hxy.clauded"
launchctl enable "$UID_GUI/com.hxy.clauded.healthcheck"
launchctl kickstart -k "$UID_GUI/com.hxy.clauded"

cat <<EOF
✅ Installed claudeD as a macOS LaunchAgent.

Status:        launchctl print $UID_GUI/com.hxy.clauded
App log:       tail -f $LOG_DIR/clauded.log
launchd out:   tail -f $LOG_DIR/out.log $LOG_DIR/err.log
Alerts:        tail -f $LOG_DIR/alerts.log
Uninstall:     ./scripts/uninstall-launchagent.sh

Bot should be online in Discord within 30 s.
EOF
