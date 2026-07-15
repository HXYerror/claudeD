# PRD — #311 flush buffer before subagent dispatch

**Issue**: #311 (bug, P1)
**Branch**: `fix/311-flush-before-subagent`
**Status**: APPROVED

## Root cause
When Agent/Task tool_use triggers subagent creation, the main buffer (pending text) is not flushed. User sees the text only after subagent completes (minutes later).

## Fix
In `discord_renderer.py`, at the `if name in ("Task", "Agent"):` block (~L1600), flush the buffer BEFORE creating the sub-thread:

```python
if name in ("Task", "Agent"):
    # #311: flush pending text so user sees it before subagent runs
    if buffer.strip():
        live_msg, buffer = await self._typewriter_tick(live_msg, buffer, force=True)
    # ... existing sub-thread creation ...
```

## AC
- AC1: text before Agent tool call is visible immediately
- AC2: subagent starts after flush (user sees full context)
