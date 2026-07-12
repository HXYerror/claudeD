# PRD — #301 persist session_id immediately after bridge start

**Issue**: #301 (bug, P0)
**Branch**: `fix/301-early-session-persist`
**Status**: APPROVED

## Problem
session_id only written on ResultMessage. Mid-turn kill → resume=None → context lost.

## Fix
After `bridge.start()` succeeds and `bridge.session_id` is available, immediately call `session_manager.save_session_state(thread_id, session_id=bridge.session_id, project_path=...)`.

## Files
- `src/clauded/bot.py` — after each `create_session` call that starts a bridge, add early persist
- `src/clauded/session_manager.py` — verify `save_session_state` can be called with just session_id
- Tests

## AC
- AC1: session_id on disk within seconds of bridge start (before any ResultMessage)
- AC2: mid-turn kill → restart → resume works (session_id was persisted)
- AC3: existing ResultMessage persist path unchanged (updates last_active etc.)
