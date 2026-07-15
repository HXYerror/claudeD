# PRD — #309 workflow progress per-agent status

**Issue**: #309 (bug, P1)
**Branch**: `fix/309-workflow-progress`
**Status**: APPROVED

## Fix
Parse `event.data.workflowProgress` in `_handle_task_progress` to show per-agent status + phase info.

## Changes
- `discord_renderer.py` `_handle_task_progress`: parse `workflowProgress` array from event data
- Render phases (current/total) + per-agent status lines
- Fold agent list if >10 entries (show first 5 + last 2 + "... N more")
- Cap embed description at 4000 chars

## AC
- AC1-AC5 per issue
