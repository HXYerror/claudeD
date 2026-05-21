# PRD — #274 smart split markdown awareness

**Issue**: #274 (bug, P1)
**Branch**: `fix/274-smart-split-markdown`
**Status**: APPROVED

## Problem
`_smart_split` in `discord_renderer.py` splits long messages at 2000 chars without awareness of markdown block structures. Code blocks (` ``` ```), blockquotes (`> `), and tables (`| |`) get cut mid-block, breaking Discord rendering.

## Approach (C — block-aware split + oversized fallback)

### Phase 1: Block-aware splitting
When `_smart_split` finds a split point:
1. Track whether we're inside a ` ``` ``` ` fence (toggle on odd occurrences)
2. If inside a fence, don't split — continue to the closing fence
3. If current + next line both start with `> `, prefer splitting before the blockquote block
4. If current + next line both start with `| `, prefer splitting before the table block

### Phase 2: Oversized block fallback
If a single markdown block exceeds 2000 chars (can't fit in one message):
- Convert to `.md` file attachment (reuse existing long-reply fallback from #161)

## Files
- `src/clauded/discord_renderer.py` — modify `_smart_split` function
- `tests/` — add tests for code block / blockquote / table split scenarios

## AC
- AC1: code block not split mid-fence
- AC2: blockquote not split mid-sequence
- AC3: table header+separator+body stay together
- AC4: oversized single block → .md attachment
- AC5: existing smart split tests still pass + 3 new fixtures
