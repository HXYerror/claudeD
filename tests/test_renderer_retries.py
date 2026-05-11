"""Tests for the retry / fallback behaviour added to fix the truncation bug.

Scenarios covered (per round-1 review must-have list):

1. ``_safe_send`` exhausts retries on persistent 503 → returns ``None``,
   ``ERROR`` log fires once with the right char count, exactly
   ``MAX_HTTP_RETRIES + 1`` send attempts.
2. ``_safe_send`` 503-then-503-then-succeed → returns the message, no
   ``ERROR`` log, three attempts, sleeps with the ``_BACKOFF`` schedule.
3. ``_safe_send`` on persistent 400 → returns ``None`` after one attempt,
   ``ERROR`` log fires (no retry).
4. ``_safe_send`` always ``RateLimited`` → returns ``None``, ``ERROR``
   log fires (covers the C1 bug).
5. ``_safe_send`` ``RateLimited`` with ``retry_after=None`` → does not
   crash on ``float(None)``.
6. ``_safe_edit`` returns ``True`` on success; ``False`` with ``ERROR``
   log on permanent 503.
7. ``_safe_edit`` with no kwargs returns ``True`` (early-return).
8. ``_typewriter_tick`` single-message branch with permanent-503 edit →
   falls back to a fresh send and returns the new message.
9. ``_typewriter_tick`` split path with permanent-503 edit on first
   chunk → falls back to send for the first chunk.
10. ``_finalize_typewriter`` all three branches each fall back to send
    when edit fails permanently (split into three explicit tests).
11. ``_typewriter_apply`` returns the original ``live_msg`` when both
    edit and send fail permanently (pins the docstring invariant).
12. Sub-agent typewriter path falls back to fresh send on persistent
    edit failure (round-1 architect A1 regression).

Plus a regression test for C2: ``HTTPException(MagicMock(), ...)`` flowing
through ``_safe_send`` does not raise ``TypeError``.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import discord
import pytest

from clauded.discord_renderer import (
    CURSOR,
    DISCORD_MAX_LEN,
    MAX_HTTP_RETRIES,
    DiscordRenderer,
    _BACKOFF,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


def _http(status: int) -> discord.HTTPException:
    return discord.HTTPException(_Resp(status), "boom")


def _ratelimited(retry_after: float | None = 0.0) -> discord.RateLimited:
    """Construct ``discord.RateLimited`` allowing ``retry_after=None``.

    The real constructor formats ``retry_after`` into the message and
    crashes on ``None``; we side-step that by bypassing ``__init__``.
    """
    if retry_after is None:
        exc = discord.RateLimited.__new__(discord.RateLimited)
        Exception.__init__(exc, "rate limited (no retry_after)")
        exc.retry_after = None
        return exc
    return discord.RateLimited(retry_after)


class FakeMessage:
    """Stand-in for ``discord.Message``. Records edits."""

    _next_id = 0

    def __init__(self, content: str = "") -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.content = content
        self.edits: list[str] = []
        # Each entry in ``edit_raises`` is consumed in order; an exception
        # value is raised, ``None`` lets the edit succeed.
        self.edit_raises: list[BaseException | None] = []

    async def edit(self, *, content=None, embed=None, **kw):
        if self.edit_raises:
            exc = self.edit_raises.pop(0)
            if exc is not None:
                raise exc
        if content is not None:
            self.content = content
            self.edits.append(content)
        return self


class FakeTarget:
    """Programmable target — pre-load ``send_raises`` with exceptions to
    simulate transient/permanent failures on consecutive sends.
    ``None`` entries (or running off the end) make the send succeed."""

    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.send_calls: int = 0
        self.send_raises: list[BaseException | None] = []
        self.guild = None
        self.parent = None
        self.id = 1
        self.name = "fake"

    async def send(self, content=None, **kw) -> FakeMessage:
        self.send_calls += 1
        if self.send_raises:
            exc = self.send_raises.pop(0)
            if exc is not None:
                raise exc
        msg = FakeMessage(content or "")
        self.messages.append(msg)
        return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Patch ``asyncio.sleep`` to a no-op so tests stay <1s total."""
    import asyncio

    sleeps: list[float] = []

    async def _instant(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _instant)
    # Hand the recorded sleep durations back to tests via a side channel.
    yield sleeps


# ---------------------------------------------------------------------------
# _safe_send tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_send_exhausts_on_persistent_503(_no_sleep, caplog):
    target = FakeTarget()
    target.send_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)
    renderer = DiscordRenderer(target)

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        result = await renderer._safe_send(content="hello world")

    assert result is None
    assert target.send_calls == MAX_HTTP_RETRIES + 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "DROPPED" in errors[0].getMessage()
    assert "11" in errors[0].getMessage()  # len("hello world")


@pytest.mark.asyncio
async def test_safe_send_503_then_succeed(_no_sleep, caplog):
    target = FakeTarget()
    target.send_raises = [_http(503), _http(503), None]
    renderer = DiscordRenderer(target)

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        result = await renderer._safe_send(content="ok")

    assert result is not None
    assert target.send_calls == 3
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    # Two backoff sleeps observed; first two entries match _BACKOFF[0], [1].
    assert _no_sleep[0] == _BACKOFF[0]
    assert _no_sleep[1] == _BACKOFF[1]


@pytest.mark.asyncio
async def test_safe_send_400_no_retry(_no_sleep, caplog):
    target = FakeTarget()
    target.send_raises = [_http(400)] * 10
    renderer = DiscordRenderer(target)

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        result = await renderer._safe_send(content="boom")

    assert result is None
    assert target.send_calls == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_safe_send_persistent_ratelimit_logs_drop(_no_sleep, caplog):
    """C1 regression: rate-limit-only exhaustion must surface a DROPPED log."""
    target = FakeTarget()
    target.send_raises = [_ratelimited(0.0)] * (MAX_HTTP_RETRIES + 5)
    renderer = DiscordRenderer(target)

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        result = await renderer._safe_send(content="ratelimited content")

    assert result is None
    assert target.send_calls == MAX_HTTP_RETRIES + 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "rate-limited" in msg
    assert "DROPPED" in msg
    assert str(len("ratelimited content")) in msg


@pytest.mark.asyncio
async def test_safe_send_ratelimited_none_retry_after(_no_sleep):
    """``RateLimited`` with ``retry_after=None`` must not crash on float(None)."""
    target = FakeTarget()
    target.send_raises = [_ratelimited(None), None]
    renderer = DiscordRenderer(target)

    result = await renderer._safe_send(content="x")
    assert result is not None  # second attempt succeeds


# ---------------------------------------------------------------------------
# C2 — status coercion regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_send_handles_magicmock_status(_no_sleep):
    """C2 regression: ``HTTPException(MagicMock(), ...)`` must not crash with
    ``TypeError`` when ``status`` flows through the int comparison."""
    target = FakeTarget()
    target.send_raises = [discord.HTTPException(MagicMock(), "msg")]
    renderer = DiscordRenderer(target)

    # The key assertion: no TypeError. The send may correctly fail-fast
    # because ``int(MagicMock())`` resolves to 1 (truthy non-retriable);
    # what matters is that the renderer returns ``None`` cleanly instead
    # of crashing the rendering task.
    result = await renderer._safe_send(content="x")
    assert result is None


# ---------------------------------------------------------------------------
# _safe_edit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_edit_success_returns_true(_no_sleep):
    target = FakeTarget()
    msg = FakeMessage("old")
    renderer = DiscordRenderer(target)

    ok = await renderer._safe_edit(msg, content="new")
    assert ok is True
    assert msg.content == "new"


@pytest.mark.asyncio
async def test_safe_edit_permanent_503_returns_false(_no_sleep, caplog):
    target = FakeTarget()
    msg = FakeMessage("old")
    msg.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)
    renderer = DiscordRenderer(target)

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        ok = await renderer._safe_edit(msg, content="will-not-deliver")

    assert ok is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "UNDELIVERED" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_safe_edit_no_kwargs_early_return(_no_sleep):
    target = FakeTarget()
    msg = FakeMessage("kept")
    renderer = DiscordRenderer(target)
    ok = await renderer._safe_edit(msg)  # nothing to edit
    assert ok is True
    assert msg.content == "kept"


# ---------------------------------------------------------------------------
# _typewriter_tick tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typewriter_tick_single_msg_falls_back_to_send(_no_sleep):
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    live = FakeMessage("stale")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    buffer = "hello"
    new_live, new_buf = await renderer._typewriter_tick(live, buffer)

    assert new_live is not None
    assert new_live is not live  # fallback created a fresh message
    assert new_live.content == buffer + CURSOR
    assert new_buf == buffer


@pytest.mark.asyncio
async def test_typewriter_tick_split_first_chunk_edit_fails(_no_sleep):
    """Split path: first-chunk edit fails permanently → fallback send for first."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    live = FakeMessage("stale")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    # Buffer big enough to require splitting.
    buffer = "x" * (DISCORD_MAX_LEN * 2 + 100)
    new_live, new_buf = await renderer._typewriter_tick(live, buffer)

    # The first chunk must have been re-sent (live.content stays "stale").
    assert live.content == "stale"
    # We produced multiple new messages; total chars >= original buffer.
    total = sum(len(m.content.rstrip(CURSOR)) for m in target.messages)
    assert total >= len(buffer)
    # new_live carries the cursor for the tail.
    assert new_live is not None
    assert new_live.content.endswith(CURSOR)
    assert new_buf == new_live.content.rstrip(CURSOR)


# ---------------------------------------------------------------------------
# _finalize_typewriter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_typewriter_single_message_branch(_no_sleep):
    """Branch 1: buffer ≤ DISCORD_MAX_LEN — edit fails, falls back to send."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    live = FakeMessage("stale")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    await renderer._finalize_typewriter(live, "short content")

    # Edit failed permanently → fresh send took over.
    assert renderer._last_msg is not live
    assert renderer._last_msg is not None
    assert renderer._last_msg.content == "short content"
    # Original live_msg untouched.
    assert live.content == "stale"


@pytest.mark.asyncio
async def test_finalize_typewriter_defensive_empty_chunks_branch(_no_sleep, monkeypatch):
    """Branch 2: ``_smart_split`` returns ``[]`` — defensive ``or [...]``
    fallback kicks in. Edit on the defensive chunk fails, fallback send."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    live = FakeMessage("stale")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    # Force the defensive `or [buffer[:DISCORD_MAX_LEN]]` branch.
    monkeypatch.setattr(DiscordRenderer, "_smart_split", staticmethod(lambda *a, **kw: []))

    big_buffer = "z" * (DISCORD_MAX_LEN + 200)
    await renderer._finalize_typewriter(live, big_buffer)

    # The defensive chunk = buffer[:DISCORD_MAX_LEN] was sent fresh after
    # edit failed permanently.
    assert renderer._last_msg is not live
    assert renderer._last_msg is not None
    assert renderer._last_msg.content == big_buffer[:DISCORD_MAX_LEN]
    assert live.content == "stale"


@pytest.mark.asyncio
async def test_finalize_typewriter_multi_chunk_first_chunk_branch(_no_sleep):
    """Branch 3: buffer > DISCORD_MAX_LEN splits into multiple chunks —
    edit on chunks[0] fails, falls back to send for the first chunk."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    live = FakeMessage("stale")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    # Buffer big enough that smart_split produces ≥ 2 chunks.
    big_buffer = "y" * (DISCORD_MAX_LEN * 2 + 50)
    await renderer._finalize_typewriter(live, big_buffer)

    # First chunk: edit fails → fresh send. Remaining chunks: sent normally.
    # Total characters delivered ≥ original buffer length.
    total = sum(len(m.content) for m in target.messages)
    assert total >= len(big_buffer)
    assert renderer._last_msg is not None
    # At least 2 messages produced (one fallback send + ≥1 follow-up).
    assert len(target.messages) >= 2
    assert live.content == "stale"


# ---------------------------------------------------------------------------
# _typewriter_apply contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typewriter_apply_both_paths_fail_keeps_live_msg(_no_sleep):
    """Documented invariant: when both edit and send fail permanently,
    return the original ``live_msg`` so the next tick has something to
    edit against. Regressing to ``return None`` would lose this."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    msg = FakeMessage("previous")
    msg.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 1)
    target.send_raises = [_http(503)] * (MAX_HTTP_RETRIES + 1)

    result = await renderer._typewriter_apply(msg, "hello")

    assert result is msg                # original live_msg preserved
    assert len(target.messages) == 0    # no new message created


@pytest.mark.asyncio
async def test_subagent_typewriter_falls_back_on_edit_failure(_no_sleep):
    """Sub-agent regression test for round-1 architect A1.

    The sub-agent path calls ``_typewriter_apply`` (the same method used
    by the main path). If a future refactor inlined ``_safe_edit`` /
    ``_safe_send`` into the sub-agent call sites, the silent-loss bug
    would return. Pin the contract on a sub-renderer instance: when its
    ``live_msg`` is permanently-503ing, ``_typewriter_apply`` falls back
    to a fresh send."""
    sub_target = FakeTarget()
    sub_renderer = DiscordRenderer(sub_target)

    live = FakeMessage("old sub content")
    live.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)

    result = await sub_renderer._typewriter_apply(live, "new sub text")

    assert result is not None
    assert result is not live              # fallback created a fresh message
    assert result.content == "new sub text"
    # Original live unchanged — edit attempts all failed.
    assert live.content == "old sub content"


# ---------------------------------------------------------------------------
# Stale-content regression tests (issue #113)
#
# discord.py 2.0+'s ``Message.edit(...)`` is NOT in-place: per its docstring
# "Edits are no longer in-place, the newly edited message is returned
# instead." Pre-fix code read ``self._last_msg.content`` to compute the
# cost-footer overwrite, so it picked up the STALE initial-send value and
# overwrote whatever the typewriter had subsequently edited into Discord.
#
# The default ``FakeMessage`` in this file mimics 1.x semantics (writes
# self.content on edit), which masked the bug in offline tests. Below we
# use ``StaleEditFakeMessage`` which matches the real 2.0+ behavior, and
# assert that the renderer keeps an accurate shadow.
# ---------------------------------------------------------------------------


class StaleEditFakeMessage(FakeMessage):
    """Mimics discord.py 2.0+ Message.edit(): does NOT update self.content.

    The edit succeeds at the API layer (records the call), but the local
    object's ``content`` attribute remains at whatever it was when the
    message was constructed. This matches the real-Discord behavior we
    observed in `T-INSTR` instrumentation: 39/39 successful edits, 0
    syncs to local content.
    """

    async def edit(self, *, content=None, embed=None, **kw):
        if self.edit_raises:
            exc = self.edit_raises.pop(0)
            if exc is not None:
                raise exc
        if content is not None:
            self.edits.append(content)
        # Note: deliberately do NOT touch self.content.
        return self


class StaleEditFakeTarget(FakeTarget):
    """FakeTarget that hands out StaleEditFakeMessage instances."""

    async def send(self, content=None, **kw):
        self.send_calls += 1
        if self.send_raises:
            exc = self.send_raises.pop(0)
            if exc is not None:
                raise exc
        msg = StaleEditFakeMessage(content or "")
        self.messages.append(msg)
        return msg


@pytest.mark.asyncio
async def test_last_msg_text_tracks_edits_through_long_session(_no_sleep):
    """Simulate the long-session pattern: send-then-many-edits.

    The renderer's ``_last_msg_text`` shadow MUST reflect the most
    recent successfully-written content, not the initial-send value.
    Pre-fix, reading ``_last_msg.content`` gave the initial value and
    the cost-footer logic clobbered the long content with that stale
    value + footer.
    """
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    msg = await renderer._safe_send(content="cursor▌")
    assert renderer._last_msg is msg
    assert renderer._last_msg_text == "cursor▌"

    # Simulate the typewriter editing the same message many times.
    growing = ["chunk 1▌", "chunk 1\nchunk 2▌", "chunk 1\nchunk 2\nfinal content"]
    for content in growing:
        ok = await renderer._safe_edit(msg, content=content)
        assert ok is True

    assert renderer._last_msg_text == "chunk 1\nchunk 2\nfinal content"


@pytest.mark.asyncio
async def test_cost_footer_path_does_not_clobber_long_content(_no_sleep):
    """Regression: the cost-footer-style read+rewrite pattern must NOT
    truncate long content back to the initial-send value.

    This reproduces the truncation root cause described in
    docs/investigations/stale-message-content.md. Without the fix, the
    final edit's content is the initial cursor-stage payload + footer,
    losing all the content that intermediate edits had written.
    """
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    # Cursor-stage initial send.
    msg = await renderer._safe_send(content="initial cursor content▌")
    # Typewriter grows the content.
    long_content = "A" * 1500 + " final paragraph closing the response."
    ok = await renderer._safe_edit(msg, content=long_content)
    assert ok is True

    # Cost-footer path (mirrors the production logic in render_response).
    current = renderer._last_msg_text.rstrip(CURSOR)
    footer = "\n\n-# 💰 $0.10 │ ⏱️ 5.0s"
    ok = await renderer._safe_edit(renderer._last_msg, content=current + footer)
    assert ok is True

    final = msg.edits[-1]
    # Exact pin: post-fix content is the long content + footer.
    assert final == long_content + footer
    # Negative pin: the stale failure mode is "initial + footer".
    assert not final.startswith("initial cursor content")


@pytest.mark.asyncio
async def test_safe_send_resets_shadow_on_new_message(_no_sleep):
    """When a fresh message is sent (e.g., split overflow path), the
    shadow must reset to the new content rather than carry forward
    the prior message's text."""
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    msg1 = await renderer._safe_send(content="first message")
    await renderer._safe_edit(msg1, content="first message edited and grown")

    msg2 = await renderer._safe_send(content="second message")
    assert renderer._last_msg is msg2
    assert renderer._last_msg_text == "second message"
    # And the shadow now tracks msg2 — editing msg1 must NOT clobber it.
    await renderer._safe_edit(msg1, content="msg1 grew again")
    assert renderer._last_msg_text == "second message"


@pytest.mark.asyncio
async def test_safe_edit_permanent_failure_does_not_update_shadow(_no_sleep):
    """A permanent edit failure must NOT advance _last_msg_text — Discord
    never accepted the content, so the shadow shouldn't either."""
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    msg = await renderer._safe_send(content="good")
    assert renderer._last_msg_text == "good"

    msg.edit_raises = [_http(503)] * (MAX_HTTP_RETRIES + 5)
    ok = await renderer._safe_edit(msg, content="never-delivered")

    assert ok is False
    assert renderer._last_msg_text == "good"  # NOT "never-delivered"


@pytest.mark.asyncio
async def test_finalize_typewriter_keeps_shadow_synced_with_last_msg(_no_sleep):
    """The shadow invariant must survive _finalize_typewriter's split path
    where self._last_msg is reassigned outside _safe_send/_safe_edit."""
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    # Establish initial _last_msg via a cursor-style send.
    live = await renderer._safe_send(content="cursor▌")
    assert renderer._last_msg is live
    assert renderer._last_msg_text == "cursor▌"

    # Buffer that splits into multiple chunks — exercises the multi-chunk
    # branch (live edited with chunks[0], rest sent fresh).
    big_buffer = ("paragraph " * 250).strip() + " end."
    assert len(big_buffer) > 1900  # safety check

    await renderer._finalize_typewriter(live, big_buffer)

    # _last_msg is now whichever message ended up last (likely the final
    # _safe_send for the last chunk). The shadow MUST equal whatever
    # was last written to _last_msg, not an earlier chunk leaked through.
    assert renderer._last_msg is not None
    expected = (
        renderer._last_msg.edits[-1] if renderer._last_msg.edits
        else renderer._last_msg.content
    )
    assert renderer._last_msg_text == expected


@pytest.mark.asyncio
async def test_cost_footer_after_file_only_send_resets_shadow(_no_sleep):
    """File-only / embed-only sends must reset _last_msg_text so the
    cost footer doesn't splice prior text onto an unrelated message."""
    target = StaleEditFakeTarget()
    renderer = DiscordRenderer(target)

    # Text chunk first (establishes _last_msg + shadow).
    await renderer._safe_send(content="visible text chunk")
    assert renderer._last_msg_text == "visible text chunk"

    # Now a file-only send (no content kwarg).
    file_msg = await renderer._safe_send(content=None, file=object())
    # Either: _last_msg advanced AND shadow reset to "".
    if renderer._last_msg is file_msg:
        assert renderer._last_msg_text == ""
    # Or: _last_msg did NOT advance, shadow still tracks the text msg.
    else:
        assert renderer._last_msg_text == "visible text chunk"
    # In either case, the cost footer's `current + footer` cannot
    # produce "visible text chunk" + footer on the file_msg.


@pytest.mark.asyncio
async def test_shadow_survives_embed_send_between_text_edits(_no_sleep):
    """Tool embeds (sent via _safe_send(embed=...) interleaved with text
    edits) must NOT clobber the typewriter's text shadow. Otherwise the
    cost-footer rewrites long content with '' + footer — a recurrence of
    bug #113 via a different trigger path (architect round-2 finding C1)."""
    target = StaleEditFakeTarget()
    r = DiscordRenderer(target)
    live = await r._safe_send(content="hi▌")
    assert r._last_msg is live
    assert r._last_msg_text == "hi▌"

    # Embed-only send (mimics tool-call embed during streaming).
    # Pre-fix (round-2 widening): this would advance _last_msg to embed_msg
    # and reset shadow to "", silently breaking the typewriter contract.
    embed_msg = await r._safe_send(content=None, embed=object())
    # Post-fix: _last_msg unchanged, shadow unchanged.
    assert r._last_msg is live
    assert r._last_msg_text == "hi▌"

    # Typewriter resumes editing the live cursor msg.
    long_text = "hi there, here is a long response…▌"
    ok = await r._safe_edit(live, content=long_text)
    assert ok is True
    assert r._last_msg_text == long_text  # shadow tracked because is-guard holds

    # Finalize.
    final_text = "hi there, here is a long response."
    await r._finalize_typewriter(live, final_text)

    # Cost-footer-equivalent read MUST see the long content, not "".
    current = r._last_msg_text.rstrip(CURSOR)
    assert current.startswith("hi there, here is a long response")


# ---------------------------------------------------------------------------
# Review I1 — files= retry must seek(0) every stream before re-attempting.
# Without this, a transient HTTP error mid-send would leave the BytesIO at
# EOF on retry and Discord would receive a 0-byte attachment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_send_files_list_resets_streams_on_retry(_no_sleep):
    """``_safe_send(files=[f1, f2])`` with one retryable 503 → both file
    streams are rewound to position 0 before the successful attempt, and
    a read after retry returns the original sentinel bytes."""
    import io as _io
    import discord

    f1_bytes = b"alpha\n"
    f2_bytes = b"png-sentinel-0123456789"

    f1 = discord.File(_io.BytesIO(f1_bytes), filename="table_0.md")
    f2 = discord.File(_io.BytesIO(f2_bytes), filename="table_0.png")

    # Snapshot tell() positions when send is invoked. The first attempt
    # will be at (0, 0) — fresh BytesIO. We then advance both streams to
    # EOF inside the first call (mimicking discord.py reading the buffer)
    # and raise. The renderer's between_attempts hook MUST seek(0) before
    # the second attempt — that's the I1 contract.
    snapshots: list[tuple[int, int]] = []

    target = FakeTarget()

    async def _send_with_snapshot(content=None, **kw):
        target.send_calls += 1
        fs = kw.get("files") or ([kw["file"]] if kw.get("file") else [])
        snapshots.append(tuple(f.fp.tell() for f in fs))  # type: ignore[arg-type]
        if target.send_raises:
            exc = target.send_raises.pop(0)
            if exc is not None:
                # Simulate discord.py reading the buffer to EOF before
                # the HTTP failure surfaces.
                for f in fs:
                    f.fp.read()
                raise exc
        msg = FakeMessage(content or "")
        target.messages.append(msg)
        return msg

    target.send = _send_with_snapshot  # type: ignore[method-assign]
    target.send_raises = [_http(503), None]

    renderer = DiscordRenderer(target)
    result = await renderer._safe_send(files=[f1, f2])

    assert result is not None
    assert target.send_calls == 2
    # First attempt entered at (0, 0) — fresh streams.
    assert snapshots[0] == (0, 0), f"first attempt positions: {snapshots[0]}"
    # CRITICAL pin (I1): the second attempt also saw (0, 0) — i.e.
    # _reset_file rewound both streams that the first attempt drained.
    # Without the I1 fix, snapshots[1] would equal (len(f1_bytes),
    # len(f2_bytes)).
    assert snapshots[1] == (0, 0), (
        f"second (retry) attempt positions: {snapshots[1]} — "
        "_reset_file did not rewind both streams"
    )
    # Sentinel bytes still readable after retry succeeds.
    f1.fp.seek(0)
    f2.fp.seek(0)
    assert f1.fp.read() == f1_bytes
    assert f2.fp.read() == f2_bytes

