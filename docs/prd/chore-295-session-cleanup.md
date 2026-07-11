# PRD — #295 sessions.json cleanup + permission_mode alignment

**Issue**: #295 (chore, P2)
**Branch**: `chore/295-session-cleanup`
**Status**: APPROVED

## Subtask 1: sessions.json shadow fields
- `session_store.save_session_state()`: stop writing `system_prompt`, `project_path`
- Read sites: use `project_manager` instead of stored values
- Startup migration: strip `model`, `system_prompt`, `project_path` from existing entries

## Subtask 2: permission_mode default
- `config.py`: `claude_permission_mode` default `None` instead of `"default"`
- `claude_bridge.py`: don't pass permission_mode when None

## AC
- AC1-AC5 per issue
