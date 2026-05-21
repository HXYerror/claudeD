# PRD — #273 /model switch preserves context via set_model()

**Issue**: #273 (bug, P1)
**Branch**: `fix/273-model-switch-context`
**Status**: APPROVED

## Problem
`/model switch` destroys session context by calling `_recreate_session`. SDK provides `set_model()` runtime method (symmetric with `set_permission_mode()` used by `/mode set`) that switches model mid-session without recreating the bridge.

## Approach (locked — A from issue)

1. Add `set_model(model)` to `claude_bridge.py` (calls `self._client.set_model(model)`)
2. `/model switch` handler: if active session exists, call `bridge.set_model(name)` instead of `_recreate_session`. No active session → fall through to normal create.
3. Update embed: "✅ Model switched. Context preserved." instead of "⚠️ context was reset"

## Files
- `src/clauded/claude_bridge.py` — add `async def set_model(self, model: str)`
- `src/clauded/cogs/model.py` — change `/model switch` handler
- `tests/test_model_cmd.py` — test context-preserving switch + no-session fallback

## AC
- AC1: switch preserves context (no recreate when session active)
- AC2: /model current shows new model after switch
- AC3: embed says "Context preserved" not "context was reset"
- AC4: no active session → still works (create path)
- AC5: _recreate_session callers (/effort /tools /budget) unaffected
