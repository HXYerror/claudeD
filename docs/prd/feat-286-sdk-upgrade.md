# PRD — #286 SDK upgrade 0.1.80 → 0.2.107+

**Issue**: #286 (epic, P1)
**Branch**: `feat/286-sdk-upgrade`
**Status**: APPROVED

## Approach
5 subtasks, one PR. Subtasks 1-4 in scope, Subtask 5 (new hooks) deferred to Phase 2.

## Subtask 1 — pip upgrade + regression test
- `pyproject.toml`: change `claude-agent-sdk>=0.1.70,<0.2` → `>=0.2.100,<0.3`
- `pip install "claude-agent-sdk==0.2.107"`
- Run full pytest — must pass with 0 regressions

## Subtask 2 — /effort add xhigh + max
- `cogs/model.py`: add xhigh and max to effort choices
- Test

## Subtask 3 — renderer subagent pressure (deferred to manual test)
## Subtask 4 — new Message types fallback (verify renderer duck-typing handles them)

## AC
- AC1: pyproject.toml updated, pip install succeeds
- AC2: full pytest passes (0 regressions)
- AC3: /effort supports xhigh + max
