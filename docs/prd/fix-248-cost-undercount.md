# PRD — #248 /cost show undercounts API calls

**Issue**: #248 (bug, P0)
**Branch**: `fix/248-cost-undercount`
**Status**: APPROVED by user 2026-05-20

---

## 1. Problem

`CostTracker.record()` only called when `cost > 0`. Turns with cost=0 (cache hit, error, free model, interrupt) are silently dropped. `/cost show` says "20 API calls" but user sent 100+ messages.

## 2. Approach (locked — Approach A from issue)

Split `calls` into `billable_calls` + `total_turns`. Always call `record()` on every turn. Migration: old `calls` → `billable_calls`, `total_turns` starts from same value.

## 3. Display format (DDD-approved)

```
$1.4923 │ 💬 100 turns │ 💰 20 billable
```

## 4. Files to change

### `src/clauded/cost_tracker.py`
- Schema: `{channel_id: {"total_usd": float, "billable_calls": int, "total_turns": int}}`
- `record(channel_id, cost_usd)`: always `total_turns += 1`; only `billable_calls += 1` when `cost_usd > 0`
- `_load()`: migrate old `calls` field → `billable_calls` + `total_turns = calls` (one-time, on read)
- `get_channel_stats(channel_id)` / `get_total()`: return both counts

### `src/clauded/bot.py`
- Lines ~725-729 (`_handle_channel_message`): remove `if response_cost > 0:` guard; always call `record(parent_id, max(0.0, response_cost))`
- Lines ~916-920 (`_handle_thread_message`): same

### `src/clauded/cogs/ops.py` (or wherever `/cost show` lives)
- Update display: `$X │ 💬 Y turns │ 💰 Z billable`
- `/cost total` same format

### Tests
- `test_cost_tracker.py`: record with cost=0 → total_turns +1, billable_calls +0
- `test_cost_tracker.py`: record with cost>0 → both +1
- `test_cost_tracker.py`: old schema migration (read JSON with only `calls`) → billable_calls = calls, total_turns = calls
- `test_cost_tracker.py`: `/cost show` output contains "💬" and "💰"

## 5. AC

- AC1: 5 messages, 1 with cost=None → shows 5 turns / 4 billable
- AC2: interrupt (no cost) → +1 total_turns
- AC3: old costs.json with `calls` field → migrates correctly
- AC4: `/cost total` / `/cost reset` / JSON schema all consistent

## 6. Out of scope

- Cost breakdown by model
- Daily/monthly reports
- Historical data backfill (impossible — no raw turn count in logs)
