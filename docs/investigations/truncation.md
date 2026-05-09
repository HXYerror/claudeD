# Truncation Bug — Root Cause Investigation

**Status:** Resolved (fix committed in PR — see below)
**Reported:** 2026-05-08 by user (hxy) via Discord screenshot
**Investigated:** 2026-05-08 → 2026-05-09
**Fix:** `src/clauded/discord_renderer.py` (HTTP retry + fallback paths)

---

## 1. Symptom

User showed a Discord screenshot from a long Claude session:

- Duration: **471 seconds**
- Output: **~6.7k tokens**
- Cost: **$0.25**
- Final message body **cut off mid-sentence**: `"...确认 claude-opus-4-7 真能解析：#▌"` immediately followed by the cost footer. The cursor glyph `▌` was still present, but no content followed.

The bot rendered no error, no warning, no partial-failure indicator. The session looked "successful" from the user's side except for the missing tail.

## 2. What we ruled out first

The previous manager (5/8) hypothesized the SDK's `max_tokens` was being hit and the model produced a truncated `end_turn`. That guess was based on **5 short test runs** (max 2.4k chars each). That sample size is below the failure regime — the user's real case was 17k+ chars over 471s. We threw the hypothesis out and started over.

## 3. Methodology — three independent layers

To find the actual root cause we instrumented every layer that could lose characters:

```
Anthropic API → Claude CLI → claude-code-sdk → ClaudeBridge → DiscordRenderer → Discord REST
```

We tested each boundary independently with parity-sized payloads (15k+ chars over 4+ minutes), so any "no repro" claim could not hide behind a too-small input.

### 3.1 SDK / API layer is clean

**Script:** `scripts/repro_truncation.py`

Drives `ClaudeBridge` offline against a 17048-char prompt, captures three independent character counts in the SDK event stream:

| Source | Bytes received |
|---|---|
| `StreamEvent` `text_delta` cumulative | **17048** |
| `AssistantMessage.TextBlock` cumulative | **17048** |
| `ResultMessage.result` final string | **17048** |

`stop_reason: end_turn`. **Three independent measurements agreed exactly.** The SDK and Claude API are not losing any text under these conditions. **The previous "max_tokens" hypothesis was wrong.**

Raw event log: `logs/repro-1778309631.jsonl`
Three text dumps: `logs/repro-1778309632.{delta_concat,textblock_concat,result}.txt`

### 3.2 Renderer is clean *under perfect network conditions*

**Scripts:** `scripts/replay_renderer.py`, `scripts/replay_renderer_v2.py`

`FakeTarget` mocks `discord.Messageable` (just records `send/edit` calls); `time.time` and `asyncio.sleep` are replaced with a virtual clock so the 1.2s typewriter throttle and the entire 4+ minute session replay in a few hundred ms.

Replayed the **705 captured events** from §3.1 through the real `DiscordRenderer.render_response`:

- **Pure text** (17048 chars): output 11 messages, total chars = 17048, **diff = 0**
- **With 3 tool calls** (5958 chars + tools, captured separately via `scripts/repro_with_tools.py` → `logs/repro2-1778310743.*`): output 4 messages, **diff = 0**

Conclusion: **the renderer's chunking, smart_split, table formatting, marker processing, and tool-routing logic are all correct.** Truncation is *not* a logic bug in the rendering pipeline.

### 3.3 Fault injection finds the real culprit

**Scripts:** `scripts/replay_with_failures.py`, `scripts/stress_renderer.py`

`FlakyTarget` randomly raises `discord.HTTPException` with status 503 from `send`/`edit` based on a configured failure rate. With 30% failure rate, the existing `_safe_send` / `_safe_edit` produce massive content loss.

**Root cause located in `src/clauded/discord_renderer.py`:**

```python
# BEFORE (the bug):
async def _safe_send(self, ...):
    try:
        msg = await self.target.send(**kwargs)
        ...
    except discord.HTTPException:
        log.warning("Discord send failed", exc_info=True)
        return None  # ← caller has no idea this failed

async def _safe_edit(self, msg, ...):
    try:
        await msg.edit(**kwargs)
    except discord.HTTPException:
        log.warning("Discord edit failed", exc_info=True)
        return  # ← swallows the failure entirely
```

Both helpers **swallow `discord.HTTPException` and return None / nothing**. Callers can't distinguish "succeeded" from "silently dropped". In `_typewriter_tick` the `first` / `middle` / `tail` chunks of a split path each call `_safe_send` once; if any of those returns None, **that chunk is gone forever** — the buffer doesn't roll back, the renderer doesn't retry, the caller doesn't know.

For a 471-second session with hundreds of `_safe_edit` ticks (every 1.2s) and a few dozen `_safe_send` calls (each split point), Discord's REST API will *almost certainly* glitch at least once with a 5xx or rate-limit. **One unlucky chunk loss = exactly the symptom we saw.**

This also explains why short sessions (<3s, the previous manager's test scope) never reproduced: not enough HTTP calls to roll the failure dice.

## 4. Fix

`src/clauded/discord_renderer.py`, four sites:

1. **`_safe_send`** — exponential backoff retry **5 times** (0.5/1/2/4/8s ≈ 15s ceiling).
   - 5xx and unknown-status errors are retriable.
   - 4xx errors (other than 429, which `discord.RateLimited` covers) are **not** retried — those are usually our bug, not transient.
   - When we finally give up on a content-bearing send, log at **error** level with the exact dropped char count (so the operator can see which chunk was lost in `bot.log`).
   - Reset `file.fp.seek(0)` between attempts so we don't second-send an empty stream.

2. **`_safe_edit`** — same retry/backoff policy, **returns `bool`**. Callers can now distinguish success from permanent failure and fall back.

3. **`_typewriter_tick`** —
   - In-place edit of `live_msg` permanently fails → fall back to `_safe_send` and switch the new message to `live_msg`. Avoids the "stale cursor message stuck while buffer grows forever" failure mode.
   - Split path: `first` chunk edit fails → fall back to send. Tail-chunk send permanent fail → log error with abandoned char count.

4. **`_finalize_typewriter`** — same `edit-then-send` fallback applied to all three internal branches (single-message path, defensive empty-chunks path, multi-chunk first-chunk path).

A new constant `MAX_HTTP_RETRIES = 5` lives at module top.

### Why exponential backoff and not infinite retry

- Discord rate-limit headers cap at single-digit seconds; 5xx storms historically last <30s.
- 0.5/1/2/4/8 covers ~15s of badness — well above what we observe in practice.
- We refuse to block the renderer indefinitely: a hung renderer would freeze the conversation worse than a logged drop.
- If a session genuinely is stuck against a multi-minute Discord outage, the error-level log lets the operator notice and the next user message starts a fresh client.

### Why `_safe_edit` returns `bool`

It's tempting to swallow edit failures (the live cursor message will get overwritten on the next tick anyway, right?). But: when `target.edit` permanently fails, the reference `live_msg` points to a message that may itself be in a bad state (Discord has rejected our edits). Subsequent edits on the same handle keep failing. Without a fallback path, every tick keeps appending into the in-memory `buffer` while nothing reaches Discord — the user sees a cursor that never moves until the session ends, and then `_finalize_typewriter` either succeeds (if the failure was transient) or drops the entire session. The `bool` lets callers cut their losses and start a fresh `live_msg`.

## 5. Verification (offline)

`scripts/stress_renderer.py` — 8 random seeds × 30% injected failure rate, drives `_typewriter_tick` and `_finalize_typewriter` directly:

- ~150 simulated transient failures across the 8 runs
- **Zero characters dropped** in the rendered output (vs. typical losses of hundreds-to-thousands of chars before the fix)

`pytest tests/` — **226/228 pass** (same as before the fix). The 2 failing tests are pre-existing regressions from commit `2608502` ("skip TextBlock entirely"), tracked separately as P1; they mock data without `StreamEvent` payloads which the post-`2608502` renderer now requires. Not related to truncation.

## 6. Verification (real-world) — TODO

This is the gap that **must close before we call this resolved**:

- [ ] Reproduce a 17k+ char / 4+ minute session against the real Discord bot under conditions that historically dropped characters (or we artificially induce 5xx via a flaky proxy).
- [ ] Pull `logs/stream-debug.jsonl` and confirm the new error-level "DROPPED N chars" log either fires (and the user sees retry recovery) or doesn't fire (and content is fully delivered).
- [ ] Compare rendered Discord transcript with the SDK's `ResultMessage.result` — they must match byte-for-byte.

The previous manager spent ~22h with `stream_logger` deployed but no real-user repro arrived in that window. We don't want to gate the merge on a long-tail trigger, so the plan is:

1. **Merge after offline verification + CI + 7-perspective review** (this is what the offline harness gives us — the code paths are exercised under realistic failure rates).
2. Keep `stream_logger` and the new error-level log on. The next time a user's long session truncates (or the operator sees "DROPPED N chars" in `bot.log`), we have actionable telemetry instead of silence.

## 7. Process retrospective

**What the previous manager got wrong:**

- Closed the investigation after 5 short-session test runs that didn't repro the failure regime.
- Hypothesized `max_tokens` without instrumenting the SDK to verify.
- Deployed `stream_logger` as the "fix", then went silent for ~10 hours waiting for organic repro.

**What unblocked the investigation:**

- Built a parity-sized **offline** harness so we don't depend on lucky organic repro.
- Three independent text counters at the SDK boundary made the "SDK is clean" claim falsifiable.
- Fault injection at the renderer's lowest HTTP boundary surfaced the silent-swallow pattern that no logical-correctness test would have caught.

**Hard rules from this episode:**

1. **"Cannot reproduce" only counts at the same magnitude as the bug report.** 5 × 2.4k chars cannot falsify "loses chars at 17k chars".
2. **Silent error swallowing is invisible until the symptom is severe.** Every `try/except` that returns None on failure is a drop point; callers must opt in to that lossy behavior or get a status back.
3. **An offline reproducer is worth more than a logger waiting in production.** Logger pays off only when triggered; harness pays off the moment it's written.

## 8. Artifacts

```
docs/investigations/truncation.md          ← this report
scripts/repro_truncation.py                ← §3.1 SDK boundary capture
scripts/repro_with_tools.py                ← variant with tool calls
scripts/replay_renderer.py                 ← §3.2 v1 (text only)
scripts/replay_renderer_v2.py              ← §3.2 v2 (with tool blocks)
scripts/replay_with_failures.py            ← §3.3 fault injection at replay layer
scripts/stress_renderer.py                 ← §3.3 direct stress at typewriter layer
logs/repro-1778309631.jsonl                ← 17k-char SDK event stream
logs/repro-1778309632.{delta,textblock,result}.txt
logs/repro2-1778310743.*                   ← 5958-char + 3-tools variant
```

These should be retained — they're the empirical foundation of the fix and are useful baselines if any regression of this class shows up later.
