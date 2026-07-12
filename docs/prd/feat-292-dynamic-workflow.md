# PRD — #292 Dynamic Workflow events rendering

**Issue**: #292 (feat, P1)
**Branch**: `feat/292-dynamic-workflow-render`
**Status**: APPROVED

## Summary
Add 4 new message type handlers to discord_renderer.py for Dynamic Workflow events: TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, TaskUpdatedMessage.

## AC (from issue)
- AC1: dynamic workflow sub-tasks visible in Discord
- AC2: progress updates edit in-place (not spam new messages)
- AC3: terminal states (completed/failed/stopped/killed) shown with appropriate color
- AC4: no crash on unknown task types
