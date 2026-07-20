#!/bin/bash
set -uo pipefail

HEARTBEAT="$HOME/Library/Caches/clauded/heartbeat"
RESTART_COUNTER="$HOME/Library/Caches/clauded/restart-count"
ALERTS_LOG="$HOME/Library/Logs/clauded/alerts.log"
HEALTHCHECK_LOG="$HOME/Library/Logs/clauded/healthcheck.log"
STALE_THRESHOLD_SECS=120
# NOTE(#9): the bot ALSO freezes this heartbeat (stops refreshing mtime) when it
# has been continuously off the Discord gateway longer than
# CLAUDED_GATEWAY_BUDGET_SECS (default 600s) with no turn in flight — a wedged
# reconnect loop keeps the event loop alive (so mtime would otherwise stay
# fresh) but is functionally dead. That case reaches the stale branch below and
# kickstarts, same as a wedged loop / OOM. Active-turn leniency
# (ACTIVE_TURN_THRESHOLD_SECS) still applies because the bot keeps writing while
# a turn is in flight.
# T2-D: longer grace window while a turn is in flight. The bot writes the
# in-flight-turn count as the heartbeat file CONTENT; a transient event-loop
# stall UNDER a turn (memory-pressure thrash) then gets this bigger budget
# instead of a hard kickstart -k that would drop the not-yet-persisted
# session_id and force a cold resume next message (the T2 resume bug).
ACTIVE_TURN_THRESHOLD_SECS=300

# Label of the LaunchAgent to kickstart when the heartbeat goes stale.
# Overridable via env so tests / dry-run harnesses can target a dummy label
# instead of the live service; the default preserves production behavior.
LAUNCHD_LABEL="${CLAUDED_LAUNCHD_LABEL:-com.hxy.clauded}"

mkdir -p "$(dirname "$HEARTBEAT")" "$(dirname "$ALERTS_LOG")"

# #168 acceptance: every script invocation produces at least one log line
# so operators can confirm the healthcheck is actually firing on schedule.
# Previously a healthy run (heartbeat fresh, no kickstart) was completely
# silent, making it impossible to distinguish "running but healthy" from
# "never running at all" (which #168 root-caused as a stale launchd load).
log_line() {
    echo "$(date '+%F %T') $*" >> "$HEALTHCHECK_LOG"
}

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
    log_line "skip — recent wake (wake_age=$((NOW - WAKE_TS))s)"
    exit 0
fi

# Stale heartbeat check
if [ ! -f "$HEARTBEAT" ]; then
    AGE=99999
    ACTIVE_TURNS=0
else
    HEARTBEAT_MTIME=$(stat -f %m "$HEARTBEAT")
    AGE=$((NOW - HEARTBEAT_MTIME))
    # T2-D: heartbeat CONTENT is the in-flight-turn count (integer). Empty or
    # legacy (mtime-only) files parse to 0, preserving the original 120s path.
    ACTIVE_TURNS=$(tr -dc '0-9' < "$HEARTBEAT" 2>/dev/null)
    [ -z "$ACTIVE_TURNS" ] && ACTIVE_TURNS=0
fi

# T2-D: pick the grace window based on whether a turn is actively rendering.
THRESHOLD=$STALE_THRESHOLD_SECS
if [ "$ACTIVE_TURNS" -gt 0 ]; then
    THRESHOLD=$ACTIVE_TURN_THRESHOLD_SECS
fi

if [ "$AGE" -gt "$THRESHOLD" ]; then
    log_line "heartbeat stale (${AGE}s > ${THRESHOLD}s, active_turns=${ACTIVE_TURNS}); kickstarting ${LAUNCHD_LABEL}"
    echo "$(date '+%F %T') heartbeat stale (${AGE}s, active_turns=${ACTIVE_TURNS}); kickstarting ${LAUNCHD_LABEL}" >> "$ALERTS_LOG"
    launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}" 2>/dev/null || true

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
else
    # Healthy run — emit a log line so operators can confirm the
    # healthcheck IS firing (was zero-output before #168).
    log_line "ok — heartbeat age ${AGE}s (threshold ${THRESHOLD}s, active_turns=${ACTIVE_TURNS})"
fi
