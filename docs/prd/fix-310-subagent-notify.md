# PRD — #310 subagent completion notification after session ends

**Issue**: #310 (bug, P0)
**Branch**: `fix/310-subagent-completion-notify`
**Status**: APPROVED

## Problem
After main session ends, subagent completes in background but user gets no notification. The `Subagent stopped` hook only logs, doesn't send to Discord.

## Subtask 1: Subagent completion notification

In `claude_bridge.py` or `bot.py`, when the "Subagent stopped" event fires:
- Look up the original thread (from the session that spawned the subagent)
- Send `✅ Subagent completed` embed to the thread with summary if available
- Must work even if renderer/main session no longer active

Key: store the thread_id + sub-thread mapping at subagent launch time (in `_task_states` or a dedicated bot-level registry), so it survives main session end.

## Subtask 2: Session stop warning for in-flight subagents

When main session stops and there are still running subagents:
- Send `⚠️ Session ended — N subagent(s) still running, will notify when complete`

## Files
- `src/clauded/bot.py` — wire subagent completion to Discord notification
- `src/clauded/claude_bridge.py` — expose subagent stopped callback/hook
- `src/clauded/discord_renderer.py` — optional: register subagent→thread mapping

## AC
- AC1: subagent completion → embed in thread
- AC2: session stop with in-flight subagents → warning embed
- AC3: notification goes to sub-thread if one was created
- AC4: works even after main renderer returns
