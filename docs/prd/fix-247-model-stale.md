# PRD — #247 /model list/current stale + resolve + consistency

**Issue**: #247 (bug, P1)
**Branch**: `fix/247-model-stale`
**Status**: APPROVED

## Problem (3 bugs)
A. KNOWN_MODELS hardcoded table is stale (claude-sonnet-4-5 vs actual 4-6)
B. _current_model_for_thread doesn't resolve thread→parent (shows "no session" inside threads)
C. /model list vs /model current inconsistency on session detection

## Approach
**Bug A**: Update KNOWN_MODELS to current model names. The table serves as alias→full-id mapping for `/model switch <alias>`. Update ids to latest. If SDK provides a way to query available models at runtime, use that; otherwise just update the table.

**Bug B**: In `_current_model_for_thread`, if `thread_id` doesn't have a session, try resolving it as a thread and checking parent channel sessions. Use the existing `resolve_binding_id` pattern.

**Bug C**: Make `/model list` and `/model current` use the same session-detection logic.

## Files
- `src/clauded/cogs/model.py` — KNOWN_MODELS table + _current_model_for_thread + list/current commands
- `tests/test_model_cmd.py` — existing tests to update

## AC
- AC1: /model list shows current model ids (claude-sonnet-4-6 etc.)
- AC2: /model list marks current active model with 🟢
- AC3: /model current in thread shows thread's active model
- AC4: /model current in channel shows "Run inside a thread"
- AC5: list + current consistent in same thread
- AC6: pre-first-turn shows "(unset)" consistently
