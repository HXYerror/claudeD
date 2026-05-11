#!/bin/bash
set -uo pipefail

HEARTBEAT="$HOME/Library/Caches/clauded/heartbeat"
RESTART_COUNTER="$HOME/Library/Caches/clauded/restart-count"
ALERTS_LOG="$HOME/Library/Logs/clauded/alerts.log"
STALE_THRESHOLD_SECS=120

mkdir -p "$(dirname "$HEARTBEAT")" "$(dirname "$ALERTS_LOG")"

# Skip if system woke <60s ago (avoid false positive from sleep/wake).
#
# pmset -g log emits lines like "2026-05-05 01:49:46 +0800 Assertions ..." —
# fields are: $1=date, $2=time, $3=tz-offset. Older code piped all three into
# `date -j -f "%Y-%m-%d %H:%M:%S"`, which can't parse the tz suffix → parse
# always failed → `|| echo 0` → WAKE_TS=0 → skip branch never fired (zombie
# wake-suppression). Take only $1+$2 so the format string matches, and rely
# on `awk '/DarkWake|Wake from/'` to pick the *last* matching line.
#
# Verify manually:
#   pmset -g log | awk '/DarkWake|Wake from/ {ts=$1" "$2} END {print ts}'
# Output should be "YYYY-MM-DD HH:MM:SS" with no trailing tz.
WAKE_LINE=$(pmset -g log 2>/dev/null | awk '/DarkWake|Wake from/ {ts=$1" "$2} END {print ts}')
if [ -n "$WAKE_LINE" ]; then
    WAKE_TS=$(date -j -f "%Y-%m-%d %H:%M:%S" "$WAKE_LINE" "+%s" 2>/dev/null || echo 0)
else
    WAKE_TS=0
fi
NOW=$(date +%s)
if [ "$WAKE_TS" -gt 0 ] && [ $((NOW - WAKE_TS)) -lt 60 ]; then
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
