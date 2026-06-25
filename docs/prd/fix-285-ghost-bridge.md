# PRD — #285 scheduled fire ghost bridge

**Issue**: #285 (bug, P0)
**Branch**: `fix/285-ghost-bridge`
**Status**: APPROVED

## Problem
Schedule message fire reuses a "ghost" bridge (is_active=True but SDK client dead). render_response produces zero events → silent empty fire.

## Approach (D — validate + fallback)

1. **Pre-fire validation**: before using existing bridge, call `bridge.get_context_usage()` as a lightweight probe. If it raises/returns None → bridge is dead, discard it and create new one with resume.

2. **Post-render fallback**: after `render_response` returns, check if any events were actually rendered. If zero → log WARNING, mark as failed (not success).

## Files
- `src/clauded/bot.py` — `_fire_schedule_message`: add probe before reuse + event count check after render
- Tests

## AC
- AC1: message fire produces visible output in thread
- AC2: ghost bridge detected → auto-rebuild + resume
- AC3: zero-event render → WARNING logged (not "fire success")
- AC4: new_task unaffected
