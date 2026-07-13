# PRD — #304 /log dump includes CLI session transcript

**Issue**: #304 (feat, P1)
**Branch**: `feat/304-log-dump-transcript`
**Status**: APPROVED

## Fix
Add CLI session transcript (tail 1MB) to /log dump bundle. Find transcript at `~/.claude/projects/<slug>/<session_id>.jsonl`.

## Files
- `src/clauded/diagnostics/bundle.py` — add transcript collection
- Tests

## AC
- AC1: bundle contains transcripts/<session_id>.tail.jsonl
- AC2: multiple active sessions → multiple transcripts included
