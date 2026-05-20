# PRD — #250 resolve_session_id + 5 sibling fixes

**Issue**: #250 (epic, P1)
**Branch**: `fix/250-session-resolve`
**Status**: APPROVED

## Problem
5 cog commands use `interaction.channel.id` to look up sessions, but sessions are keyed by thread_id. In channels (non-thread), this always returns None → misleading "no active session" instead of "use in a thread".

## Approach
1. Create `resolve_session_id(interaction) -> int | None` helper in `cogs/_unbound.py` that returns `interaction.channel.id` only if channel is a Thread, else None.
2. Apply to all 5 sibling sites:
   - mode.py: /mode set, /mode cycle, /mode current
   - ops.py: /health, /notify
3. When resolve_session_id returns None → ephemeral "Use this command inside a thread."

## AC (from issue)
- AC1: resolve_session_id returns thread_id for Thread, None for channel
- AC2: 5 sites → "Use in a thread" in channel, correct session in thread
- AC3: grep lint test
- AC4: #247 already fixed (done in PR #268)
- AC5: /notify in channel → "Use in a thread"
