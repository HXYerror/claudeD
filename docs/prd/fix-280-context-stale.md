# PRD — #280 /context stale maxTokens after model switch

**Issue**: #280 (bug, P1)
**Branch**: `fix/280-context-stale-maxtokens`
**Status**: APPROVED

## Problem
After `/model switch opus`, `/context` shows maxTokens=200k (sonnet's) instead of 1M (opus's). SDK `get_context_usage()` returns stale maxTokens until the next API turn.

## Approach (B hybrid)
After `set_model()` succeeds in the cog handler, look up the new model's context window from KNOWN_MODELS and cache it on the bridge as `_context_window_override`. `compute_global_context_pct` checks this override before using SDK's maxTokens.

## Files
- `src/clauded/claude_bridge.py` — add `_context_window_override: int | None` field, set in `set_model()`
- `src/clauded/_context_usage.py` — accept optional `max_tokens_override` param
- `src/clauded/discord_renderer.py` — pass bridge's override to compute
- `src/clauded/cogs/context.py` — same
- Tests

## AC
- AC1: /context shows 1M maxTokens after switching to opus
- AC2: percentage computed with correct denominator
- AC3: override cleared on next real turn (SDK catches up)
