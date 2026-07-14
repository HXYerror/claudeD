# PRD — #306 subagent render conflict with Task* handlers

**Issue**: #306 (bug, P0)
**Branch**: `fix/306-subagent-task-conflict`
**Status**: APPROVED

## Root cause
#292 Task* isinstance checks (L1328-1337) fire `continue` on TaskStartedMessage/TaskProgressMessage/TaskNotificationMessage. When SDK sends these for normal Agent tool_use (not just Dynamic Workflow), the v1.9 sub-thread creation path (L1600 `name in ("Task", "Agent")`) never executes.

## Fix
In the 4 Task* handler dispatch (L1328-1337), skip (don't handle) events whose `task_type` indicates a normal subagent (not a dynamic workflow). Let them fall through to the existing sub-thread path.

```python
# Before handling Task* messages, check if this is a Dynamic Workflow task
# vs a normal subagent. Normal subagents should fall through to sub-thread path.
if _SdkTaskStartedMessage is not None and isinstance(event, _SdkTaskStartedMessage):
    if getattr(event, "task_type", "") in ("local_workflow", "remote_workflow", "workflow"):
        await self._handle_task_started(event)
        continue
    # else: fall through to sub-thread path for normal Agent
```

Same filter for TaskProgress/TaskNotification/TaskUpdated.

## AC
- AC1: normal subagent content renders to sub-thread
- AC2: subagent completion shows ✅ embed
- AC3: Dynamic Workflow Task* still renders banner/progress
- AC4: two paths coexist
