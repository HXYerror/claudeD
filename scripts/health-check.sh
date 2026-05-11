#!/bin/bash
set -uo pipefail

HEARTBEAT="$HOME/Library/Caches/clauded/heartbeat"
RESTART_COUNTER="$HOME/Library/Caches/clauded/restart-count"
ALERTS_LOG="$HOME/Library/Logs/clauded/alerts.log"
STALE_THRESHOLD_SECS=120

mkdir -p "$(dirname "$HEARTBEAT")" "$(dirname "$ALERTS_LOG")"

# Skip if system woke <60s ago (avoid false positive from sleep/wake)
WAKE_AGE=$(pmset -g log 2>/dev/null | awk '/DarkWake|Wake from/ { ts=$1" "$2 } END { print ts }' | xargs -I {} date -j -f "%Y-%m-%d %H:%M:%S" "{}" "+%s" 2>/dev/null || echo 0)
NOW=$(date +%s)
if [ "$WAKE_AGE" -gt 0 ] && [ $((NOW - WAKE_AGE)) -lt 60 ]; then
    echo "$(date '+%F %T') skip — recent wake" >> "$(dirname "$ALERTS_LOG")/healthcheck.log"
    exit 0
fi

# Stale heartbeat check
if [ ! -f "$HEARTBEAT" ]; then
    AGE=99999
else
    HEARTBEAT_MTIME=$(stat -f %m "$HEARTBEAT")
    AGE=$((NOW - HEARTBEAT_MTIME))
fi

if [ "$AGE" -gt "$STALE_THRESHOLD_SECS" ]; then
    echo "$(date '+%F %T') heartbeat stale ($AGE s); kickstarting com.hxy.clauded" >> "$ALERTS_LOG"
    launchctl kickstart -k "gui/$(id -u)/com.hxy.clauded" 2>/dev/null || true

    # Track restart count in rolling 5-min window via /tmp file (per-day rotating)
    DAY=$(date +%Y%m%d)
    COUNTER_TODAY="${RESTART_COUNTER}.${DAY}"
    NOW=$(date +%s)
    if [ -f "$COUNTER_TODAY" ]; then
        # Keep only entries within last 300 s
        awk -v cutoff=$((NOW - 300)) '$1 > cutoff' "$COUNTER_TODAY" > "$COUNTER_TODAY.tmp"
        mv "$COUNTER_TODAY.tmp" "$COUNTER_TODAY"
    fi
    echo "$NOW restart" >> "$COUNTER_TODAY"
    COUNT=$(wc -l < "$COUNTER_TODAY")
    if [ "$COUNT" -ge 3 ]; then
        MSG="claudeD restarted $COUNT times in last 5 min"
        echo "$(date '+%F %T') ALERT $MSG" >> "$ALERTS_LOG"
        osascript -e "display notification \"$MSG\" with title \"claudeD\"" 2>/dev/null || true
    fi
fi
