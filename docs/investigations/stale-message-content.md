# Truncation Bug — Second Root Cause: Stale `Message.content` After Edit

**Status:** Resolved (fix in PR — see below)
**Reported:** 2026-05-09 by user (hxy) — pushback after PR #108 merge: "did you actually test it? in real conditions?"
**Investigated:** 2026-05-09
**Fix:** `src/clauded/discord_renderer.py` (shadow `_last_msg_text`, sync `msg.content` after edit)
**Issue:** #113

---

## 1. Context

PR #108 (commit `400d76f`) fixed one truncation root cause: `_safe_send`/`_safe_edit` were silently swallowing `discord.HTTPException` and dropping content on retry-able 5xx storms. Verification was offline only — under real Discord conditions a separate truncation pattern remained, with **~33% reproduction rate** on long sessions.

This document is the root cause analysis for that second pattern.

## 2. Symptom

E2E test `scripts/e2e_truncation.py --mode A` against real Discord + real Claude:

| Run | SDK delta | SDK result | Discord channel | render_diff | Verdict |
|-----|-----------|------------|-----------------|-------------|---------|
| 1 | 18161 | 18161 | 17997 | +164 (within tolerance) | PASS |
| **2** | **17553** | **17553** | **16199** | **+1354 LOST** | **FAIL** |
| 3 | 14741 | 14741 | 14517 | +224 (within tolerance) | PASS |

The failing run captured artifacts in `logs/e2e_A_1778326687_bodies/`. Examining the diff between `result.txt` (SDK ground truth) and `channel.txt` (`channel.history` ground truth) showed **the channel matched perfectly through char 1762, then started losing content** — culminating in the entire ` 总结` section (last ~1336 chars) never reaching Discord at all.

There were **zero** ERROR logs (`DROPPED N chars` / `UNDELIVERED N chars`) — meaning none of PR #108's retry-failure paths fired. `saw_retry_log: false, saw_dropped_log: false` in the run summary.

This case also matches user-reported screenshots from 5/8 (471s session, 6.7k tokens, content cut mid-sentence with cursor `▌` still present + cost footer immediately following).

## 3. Methodology — instrumentation, not speculation

Round-1 mistake (PR #108): I merged based on offline harness verification + 7-perspective review only. User pushed back. To find the second root cause I needed to actually observe what `msg.content` was each time the renderer touched it.

### 3.1 Tried offline replay first — couldn't reproduce

`/tmp/replay_failing_run.py` replayed the captured 5589 SDK events through a `SpyTarget`/`SpyMessage` into the real `DiscordRenderer.render_response`. Result: **17591 chars rendered, 0 lost.** Offline cannot reproduce. So the bug is in the real-Discord-HTTP layer that offline mocks bypass.

### 3.2 Added per-call instrumentation, ran real E2E

Added `T-INSTR` log lines (commit-scoped, reverted before final fix):

```python
# In _safe_edit, before _retry_http:
log.info("T-INSTR edit-pre: msg_id=%s pre_content_len=%d new_content_len=%d", ...)
# After _retry_http:
log.info("T-INSTR edit-post: msg_id=%s ok=%s post_content_len=%d (was pre=%d new=%d)", ...)
# Just before cost footer reads _last_msg.content:
log.info("T-INSTR cost-footer-pre: last_msg_id=%s last_msg_content_len=%d", ...)
```

Re-ran `e2e_truncation.py --mode A`. **First run reproduced the bug** and produced this smoking-gun log:

```
edit-pre:  msg_id=...411836 pre_content_len=499 new_content_len=911
edit-post: msg_id=...411836 ok=True post_content_len=499 (was pre=499 new=911)  ← still 499
edit-pre:  msg_id=...411836 pre_content_len=499 new_content_len=1388
edit-post: msg_id=...411836 ok=True post_content_len=499 (was pre=499 new=1388) ← still 499
cost-footer-pre: last_msg_id=...411836 last_msg_content_len=499                  ← reads 499
edit-pre:  msg_id=...411836 pre_content_len=499 new_content_len=544              ← writes back 499 + footer
```

Aggregate: **39/39 successful edits, 0 syncs to local `msg.content`**. Every single edit on the same `msg` object left `msg.content` at its initial-send value forever.

## 4. Root Cause

### 4.1 discord.py 2.0+ behavior change

[`discord/message.py:2868-2997`](https://github.com/Rapptz/discord.py/blob/master/discord/message.py) — `Message.edit(...)`:

```python
async def edit(self, ...) -> Message:
    """Edits the message.

    .. versionchanged:: 2.0
        Edits are no longer in-place, the newly edited message is returned instead.
    """
    ...
    data = await self._state.http.edit_message(self.channel.id, self.id, params=params)
    message = Message(state=self._state, channel=self.channel, data=data)  # NEW object
    ...
    return message
```

After a successful `await msg.edit(content=X)`:
- ✅ Discord server-side content is updated to `X`
- ❌ Local `msg.content` stays at whatever it was when `msg` was first constructed (typically the original `send` payload)

This was a deliberate breaking change in discord.py 2.0. v1.x updated `self` in place; v2.0 explicitly does not.

### 4.2 Discord API confirms the protocol

[Discord docs — Edit Message](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx#L1161-L1168):

> ## Edit Message
> `PATCH /channels/{channel.id}/messages/{message.id}`
> ...
> **Returns a message object.** Fires a Message Update Gateway event.

The API returns the full edited message object. discord.py 2.0 builds a fresh `Message` from that response and returns it instead of mutating the original.

### 4.3 Renderer pre-fix: discards the return value

`discord_renderer.py` pre-fix:

```python
async def _safe_edit(self, msg, content=...):
    async def _op():
        await msg.edit(**kwargs)   # ← return value discarded
        return msg
    result = await self._retry_http(_op, ...)
    return result is not None
```

And the cost footer (line 780):

```python
current = (self._last_msg.content or "").rstrip(CURSOR)  # ← stale!
await self._safe_edit(self._last_msg, content=current + footer)
```

The cost-footer path reads `_last_msg.content` (= stale initial-send value, typically a few hundred chars of cursor-stage text), appends the footer, and **edits that short content + footer back** into the live message — clobbering the long content that all the previous typewriter ticks had successfully written.

### 4.4 Why the bug is intermittent

- Short sessions (<3s): fast-path, no typewriter, single send. `_last_msg.content` matches what was sent. No stale state.
- Long sessions where the final content happens to be short (≤ first send size): stale read returns ~current value, footer overwrite produces no visible loss.
- Long sessions where the final live message has been edited multiple times to grow large: stale read returns initial cursor-stage value (a few hundred chars), footer overwrite causes thousands of chars to vanish. **This is the failing 1/3 case.**

## 5. Fix

`src/clauded/discord_renderer.py`:

1. **`__init__`** — add `self._last_msg_text: str = ""` (shadow of what we last wrote to `_last_msg`).
2. **`_safe_send`** — after a successful content send, set `self._last_msg = msg; self._last_msg_text = content`.
3. **`_safe_edit`** — after a successful edit on `msg`, sync `msg.content = content` and (if `msg is self._last_msg`) update `self._last_msg_text = content`.
4. **Cost-footer site** — read `self._last_msg_text.rstrip(CURSOR)` instead of `self._last_msg.content`.

The `msg.content = content` write is layer 1 of the defense (any future code reading `msg.content` will see the right value); the `_last_msg_text` shadow is layer 2 (the specific cost-footer path is independent of any `msg.content` source).

### Why I'm not switching to "use the return value of `msg.edit()` as the new live_msg"

It would be more idiomatic but invasive: `_safe_edit` is called from many sites with different `msg` lifetimes (live cursor msg, fixed tool-status msgs in dictionaries, sub-agent state). Threading a "new msg" return through every callsite risks regressing other paths. The shadow-on-success pattern is local and minimally invasive.

## 6. Verification

### 6.1 Real bot, real Claude, real Discord — 4 runs

After the fix:

| Run | SDK delta | SDK result | Discord channel | render_diff | Verdict |
|-----|-----------|------------|-----------------|-------------|---------|
| e2e A run 1 | 16065 | 16065 | 16047 | -18 | ✅ PASS |
| e2e A run 2 | 13392 | 13392 | 13378 | -14 | ✅ PASS |
| e2e A run 3 | 17726 | 17726 | 17708 | -18 | ✅ PASS |
| **real bot dogfood** (user-triggered) | **3055** | **3055** | **3090** | **-35** | **✅ PASS** |

The `-N` `render_diff` values are negative-or-small because:
- `_smart_split`'s `lstrip("\n")` strips leading paragraph blanks from each chunk after splitting (~9 × 2 = 18 chars per long-session split; visually identical, prevents double-spacing at chunk boundaries).
- `_format_tables` adds ` ``` `code-fence wrappers around markdown tables (the `+35` in the real-bot run came from a table being wrapped — channel literally has *more* characters than the SDK output, all formatting overhead).

Neither is content loss. Both behaviors predate this fix and are not symptomatic of any bug.

### 6.2 Real-bot dogfood comparison — same prompt, before vs after

Thread `1502169957033574451`, same user repeatedly asking for the same research summary:

| Time | Fix? | bot reply | Last 50 chars |
|------|------|-----------|---------------|
| 13:47 | ❌ | 2 msgs / **2135 chars** | `"...| `top_k` | reque...se64，"` ← truncated mid-string |
| 15:42 | ✅ | 2 msgs / **3090 chars** | `"...| ```"` ← natural code-fence close |

Same prompt, same model, same `~$0.41` cost / `~28s` duration. Pre-fix: truncated. Post-fix: complete. User had asked at 13:47 *"消息没发全，重新给我发一下"* (message wasn't fully sent, send it again) — the exact symptom.

### 6.3 pytest

`PYTHONPATH=src pytest tests/` → 242 passed / 2 failed. Same 2 pre-existing failures from commit `2608502` (tracked separately as P1, unrelated to this fix).

### 6.4 What's NOT verified

- The defensive `msg.content = content` write in `_safe_edit` could in theory be a problem if a `MESSAGE_UPDATE` gateway event arrives shortly after and overwrites it back to the (correct) value — in either case the value seen by next reader is correct, so this is a non-issue.
- Behavior under the discord.py 1.x semantics (in-place edit) is untested — but the project requires `discord.py>=2.7` per `pyproject.toml`, so 1.x is out of scope.

## 7. Process retrospective

This is the second time this project has shipped a "truncation fix" that wasn't actually verified end-to-end.

**5/8 (previous manager):** Closed investigation after 5 short tests, hypothesized `max_tokens` truncation. Wrong.

**5/9 (me, PR #108):** Found genuine bug in retry path, wrote thorough offline harness, ran 3 rounds of 7-perspective review, merged. Did NOT run real E2E before merge. Investigation §6 explicitly deferred real-world verification with rationale "ships with error-level logging so future occurrences become visible". User pushed back: *"did you actually test? in real conditions?"* — and produced the second root cause.

The hard rule going forward, written into `memory/claudeD.md`:

> **For silent-loss class bugs, real-environment E2E verification is a merge gate. Not deferrable. Not "ships with logging".** The first time the user tries it after merge IS the verification — by which point you've spent a round of 7-perspective review and 3 dev iterations on a fix that may not be the fix.

Specifically, this fix was found by:
1. Adding observability (`T-INSTR` logs) at the exact suspect lines.
2. Running E2E once. Bug reproduced first try.
3. Reading the log. Bug visible in log line 1.

That's 5 minutes of work. Done before PR #108 merge it would have caught both root causes simultaneously.

## 8. Artifacts

```
docs/investigations/stale-message-content.md            ← this report
.crew/stale-content-fix/status.json                     ← status tracking
logs/e2e_A_1778326687_bodies/{result,channel,delta}.txt ← failing run capture
logs/e2e_A_1778337230.{log,json}                        ← instrumented run that nailed root cause
.crew/truncation-fix/real-bot-thread/                   ← real-bot dogfood capture
```

`logs/` is gitignored; artifacts are regenerable from `scripts/e2e_truncation.py`.
