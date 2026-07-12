# PRD — #293 /agent, /model, /mcp list use SDK runtime APIs

**Issue**: #293 (bug, P1)
**Branch**: `fix/293-list-runtime-api`
**Status**: APPROVED

## Fix
3 list commands should query SDK runtime instead of local data.

### /agent list
- If active session: `bridge.get_server_info()` → agents list
- Merge with `/agent create` local agents from data/agents.json
- No session: read `.claude/agents/*.md` from project path + `~/.claude/agents/`

### /model list
- If active session: use bridge model info from SDK
- Keep KNOWN_MODELS as fallback for no-session display

### /mcp list
- If active session: call `bridge.get_mcp_status()` (or similar SDK API)
- Show all loaded servers (CLI + user + project)
- No session: fallback to project_manager data

## AC
- AC1-AC6 per issue
