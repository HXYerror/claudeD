# PRD — #263 /context + footer 🧠 use Free space supplement

**Issue**: #263 (bug, P1)
**Branch**: `fix/263-context-pct`
**Status**: APPROVED

## Problem
`/context` + footer 🧠 use `totalTokens/maxTokens` which is **last-turn input footprint**, not current buffer occupancy. Result: shows 0%/<1% when real usage is ~45%.

## Approach (locked — Approach A from issue)
Use Free space supplement: `global_used = maxTokens - free_space_tokens`. Categories list unchanged.

## Files to change

### `src/clauded/discord_renderer.py`
In `_fetch_context_pct_settled` (or `_compute` helper around line 333-351):
```python
free_space = next((c["tokens"] for c in cu["categories"] if c["name"] in ("Free space", "Available", "Remaining")), None)
if free_space is not None and cu.get("maxTokens", 0) > 0:
    global_used = cu["maxTokens"] - free_space
    global_pct = global_used / cu["maxTokens"] * 100
else:
    global_used = cu.get("totalTokens", 0)
    global_pct = float(cu.get("percentage", 0))
```
Use `global_pct` for footer 🧠 instead of `totalTokens/maxTokens`.

### `src/clauded/cogs/context.py`
Lines 93-95 + 106 + 108: same Free space supplement. Title shows `📊 Context: {global_pct:.1f}%`. Progress shows `{global_used/1000:.1f}k / {max/1000:.1f}k`.

### Tests
- Construct fixture: `totalTokens=408, maxTokens=1_000_000, categories=[..., {"name":"Free space","tokens":551100}]`
- Assert footer pct ≈ 44.9% (not 0%)
- Assert /context title shows ~45%
- Assert Free space missing → fallback to totalTokens (no crash)

## AC
- AC1: footer 🧠 shows ~45% in same scenario (not <1%)
- AC2: /context title shows ~45%
- AC3: /context progress shows ~449k / 1000k
- AC4: categories list unchanged
- AC5: Free space missing → fallback, no crash
- AC6: unit tests cover divergence case
