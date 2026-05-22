# PRD — #277 wire resume_session_id through 12 session-config commands

**Issue**: #277 (bug, P1)
**Branch**: `fix/277-resume-session-id`
**Status**: APPROVED

## Problem
12 commands call `_recreate_session` without passing `resume_session_id`. Context is lost on every settings change. User: "所有 session 内的设置都不要 reset，全部整改。"

## Approach
For each of the 12 call sites: get current session's session_id, pass as `resume_session_id` to `_recreate_session`. Update embed from "⚠️ context was reset" to "✅ Context preserved."

## Files
- `src/clauded/cogs/model.py` — /effort, /max-turns, /fallback-model, /bare
- `src/clauded/cogs/tools.py` — /tools allow, /tools deny, /tools reset, /budget set
- `src/clauded/cogs/agent.py` — /agent use
- `src/clauded/cogs/ops.py` — /plugin add
- `src/clauded/cogs/session.py` — /session worktree, /session name, /session settings
- Tests for each

## Pattern (same for all 12)
```python
# Before _recreate_session call:
thread_id = getattr(interaction.channel, "id", None)
current = bot.session_manager.get_session(thread_id) if thread_id else None
sid = getattr(current, "session_id", None) if current and getattr(current, "is_active", False) else None
bridge = await bot._recreate_session(interaction, ..., resume_session_id=sid)
```

## AC
- AC1-AC2: all 12 commands preserve context after settings change
- AC3: /session fork unchanged
- AC4: embed text "Context preserved" not "context was reset"
- AC5: no active session → still works (sid=None)
