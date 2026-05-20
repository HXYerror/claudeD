# PRD — #254 + #255 + #257 cog validation hardening

**Issues**: #254 (duplicate overwrite), #255 (empty identifier), #257 (missing unbound check)
**Branch**: `fix/254-255-257-cog-validation`
**Status**: APPROVED

## #254 — duplicate name overwrite
In `agent_manager.create()` and `project_manager.add_mcp_server()`: if name already exists, return error instead of silently overwriting.

## #255 — empty/whitespace/special char in identifiers
Add validation in:
- `agent_manager.create(name)` — reject empty/whitespace/newline
- `project_manager.set_env(key)` — reject empty/whitespace/newline/`=`
- `project_manager.add_mcp_server(name)` — reject empty/whitespace
- `cogs/agent.py` `/agent create` — validate before calling manager
- `cogs/ops.py` or wherever `/env set` lives — validate before calling

## #257 — 9 cog commands missing reject_if_unbound
Add `reject_if_unbound` (or equivalent bound-check) to these 9 commands:
- `/env list` — needs binding to know which channel's env
- `/budget show` — needs binding
- `/budget clear` — needs binding
- `/mcp list` — needs binding
- `/project dirs` — needs binding
- `/project remove-dir` — needs binding
- `/project set-mode` — needs binding
- (others from issue audit)

## Tests
- Duplicate name → error message (not silent overwrite)
- Empty/whitespace name → error message
- Unbound channel → ephemeral error for each of 9 commands

## AC
- AC1: /agent create with existing name → "already exists" error
- AC2: /mcp add with existing name → same
- AC3: /agent create with empty name → validation error
- AC4: /env set with key containing "=" → validation error
- AC5: 9 listed commands in unbound channel → ephemeral error
