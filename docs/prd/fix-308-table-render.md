# PRD — #308 tables render as code blocks

**Issue**: #308 (bug, P1)
**Branch**: `fix/308-table-render`
**Status**: APPROVED

## Root cause
typewriter tick splits buffer at 2000 chars without checking if a markdown table is in progress. Table gets split → finalize can't extract it → code block fallback.

## Fix (Approach A)
In `_typewriter_tick`, before splitting: detect if buffer ends with incomplete table lines (consecutive `|` lines). If so, split only the pre-table portion and keep the table in buffer for `_finalize_typewriter` to handle.

## Helper needed
```python
def _find_table_start_in_tail(self, buffer: str) -> int:
    """Return index where an in-progress table starts at buffer tail, or -1."""
    lines = buffer.split("\n")
    # Walk backwards from end finding consecutive | lines
    table_start_idx = len(buffer)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("|"):
            table_start_idx = sum(len(l) + 1 for l in lines[:i])
        else:
            break
    return table_start_idx if table_start_idx < len(buffer) else -1
```

## AC
- AC1: long response with table → PNG (not code block)
- AC2: short response table still works
- AC3: typewriter UX not degraded (table portion held in buffer briefly)
