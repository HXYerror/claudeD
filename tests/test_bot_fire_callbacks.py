"""Tests for the bot-side scheduled-fire callbacks (issue #241, Subtask 6 / B5).

R1 Tester BLOCKER #1: the three callbacks on :class:`ClaudedBot` —
``_fire_schedule_message`` / ``_fire_schedule_new_task`` /
``_notify_schedule_expired`` — shipped with zero unit coverage. The
"only thing standing between merge and works" was a not-yet-run real
Discord e2e, which the 5/19 directive explicitly forbids.

These 6 tests pin the actual call arguments (not just call counts) on
the production methods — extracting via :meth:`ClaudedBot._format_fire_quoted_what`
helper (B6) and the :meth:`ClaudedBot._send_fire_prefix_and_quote` wrapper
(B6/M3) so each test stays scoped to one observable behavior:

  1. ``_format_fire_quoted_what`` wraps each line in ``> 「<line>」``
     and survives multi-line + empty input cleanly.
  2. ``_send_fire_prefix_and_quote`` posts BOTH the AC13 prefix line
     AND the bracketed-quote line, in order, with the prefix matching
     the schedule's ``name``.
  3. ``_send_fire_prefix_and_quote`` truncates the quoted content at
     1900 chars (Discord 2000 ceiling minus headroom).
  4. ``_send_fire_prefix_and_quote`` swallows + logs send errors so a
     transient Discord failure on the prefix line never aborts the
     actual fire.
  5. ``_safe_fire_render`` dispatches the #224 auto-crash bundle on a
     non-transient renderer crash AND re-raises so the scheduler can
     classify the failure.
  6. ``_safe_fire_render`` does NOT dispatch the crash bundle on
     transient discord errors — those should bubble through the normal
     retry path without polluting the audit trail.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.bot import ClaudedBot


# ----------------------------------------------------------------------
# Helpers — we instantiate just the methods of interest off a MagicMock-
# spec'd ClaudedBot. The constructor pulls in too much (config, intents,
# session_manager construction, the SchedulerManager). The methods we
# test are functionally pure (apart from awaiting thread.send / renderer
# methods), so binding them onto a MagicMock gives strict per-test
# isolation without dragging in the full bot wiring.
# ----------------------------------------------------------------------


def _bare_bot() -> MagicMock:
    """A MagicMock that exposes the bot fire-callback helpers via real impls.

    We deliberately bind the *unbound* methods from ``ClaudedBot`` onto
    a MagicMock so the staticmethod / regular-method semantics stay
    correct and we can call them with the same self contract production
    uses, but we avoid the constructor cost / side effects.
    """
    bot = MagicMock()
    # Bind real implementations of the helpers under test.
    bot._format_fire_quoted_what = ClaudedBot._format_fire_quoted_what
    # _send_fire_prefix_and_quote and _safe_fire_render are regular
    # methods — bind them with the MagicMock as ``self``.
    bot._send_fire_prefix_and_quote = (
        ClaudedBot._send_fire_prefix_and_quote.__get__(bot, MagicMock)
    )
    bot._safe_fire_render = (
        ClaudedBot._safe_fire_render.__get__(bot, MagicMock)
    )
    bot._maybe_dispatch_auto_crash_bundle = AsyncMock()
    return bot


# ----------------------------------------------------------------------
# Test 1 — _format_fire_quoted_what per-line 「<line>」 wrapping
# ----------------------------------------------------------------------


def test_format_fire_quoted_what_wraps_each_line_in_corner_brackets():
    """B6 / PRD AC13: every visible line of the injected ``what`` is
    independently wrapped in 「…」 and prefixed with Discord's quote ``> ``.

    Strict: the multi-line case must produce one ``> 「line」`` per source
    line — NOT one big ``> 「multi\\nline」`` blob (which would render in
    Discord as one quoted line with a literal newline inside the brackets).
    """
    out = ClaudedBot._format_fire_quoted_what("line1\nline2\nline3")
    assert out == "> 「line1」\n> 「line2」\n> 「line3」"

    # Single-line input: still one bracketed quote line.
    assert ClaudedBot._format_fire_quoted_what("hello") == "> 「hello」"

    # Empty input: an empty bracketed quote — preserves the AC13 marker
    # without leaking bogus content (the old behaviour was "> " literal).
    assert ClaudedBot._format_fire_quoted_what("") == "> 「」"


# ----------------------------------------------------------------------
# Test 2 — _send_fire_prefix_and_quote sends prefix + quote, in order
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_fire_prefix_and_quote_emits_prefix_then_quoted_what():
    """PRD AC13: humans see (1) the ``-# ⏰ Scheduled fire: <label>``
    prefix line, then (2) the bracketed quote of ``what``.

    Strict: pin the actual content of both ``send`` calls and the call
    order — the prefix must precede the quote. A regression that swaps
    the order, or that drops the prefix, would still satisfy
    ``call_count == 2`` but break the AC.
    """
    bot = _bare_bot()
    thread = MagicMock()
    thread.send = AsyncMock()

    await bot._send_fire_prefix_and_quote(
        thread, "weekly_report", "first\nsecond",
        sched_id_for_log="abc",
    )

    assert thread.send.await_count == 2
    first_call, second_call = thread.send.await_args_list
    assert first_call.kwargs == {
        "content": "-# ⏰ Scheduled fire: weekly_report",
    }
    assert second_call.kwargs == {
        "content": "> 「first」\n> 「second」",
    }


# ----------------------------------------------------------------------
# Test 3 — _send_fire_prefix_and_quote truncates the quoted body to 1900
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_fire_prefix_and_quote_truncates_long_what_to_1900_chars():
    """Discord's hard message ceiling is 2000 chars; we cap the quoted
    line at 1900 to leave headroom for the ``> 「`` + ``」`` framing
    characters and to defend against future-Discord ceiling tweaks.

    Strict: the second ``send`` call's ``content`` MUST be exactly
    1900 chars long when ``what`` exceeds that. A regression that
    forgets the truncation slice would push >2000 and Discord would
    reject the message — silently for the user.
    """
    bot = _bare_bot()
    thread = MagicMock()
    thread.send = AsyncMock()

    long_what = "x" * 3000
    await bot._send_fire_prefix_and_quote(
        thread, "label", long_what, sched_id_for_log="abc",
    )

    second_call = thread.send.await_args_list[1]
    assert len(second_call.kwargs["content"]) == 1900


# ----------------------------------------------------------------------
# Test 4 — _send_fire_prefix_and_quote swallows send failures
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_fire_prefix_and_quote_swallows_send_failure(caplog):
    """A transient Discord failure on the prefix/quote sends must NOT
    abort the broader fire callback — the actual claude turn (the
    ``render_response`` after this helper) is the load-bearing work.

    Strict: ``_send_fire_prefix_and_quote`` returns normally (no
    exception) even when ``thread.send`` raises, AND a WARNING-level
    log line lands so the audit trail records the dropped prefix.
    """
    bot = _bare_bot()
    thread = MagicMock()
    thread.send = AsyncMock(side_effect=RuntimeError("discord 503"))

    with caplog.at_level("WARNING"):
        # No exception propagates.
        await bot._send_fire_prefix_and_quote(
            thread, "lbl", "what", sched_id_for_log="sched-xyz",
        )

    # The log line includes the schedule id so /log dump can correlate.
    assert any(
        "sched-xyz" in r.message and "prefix/quoted send failed" in r.message
        for r in caplog.records
    )


# ----------------------------------------------------------------------
# Test 5 — _safe_fire_render dispatches the auto-crash bundle on a
# non-transient renderer crash AND re-raises
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_fire_render_dispatches_crash_bundle_and_reraises():
    """M3: a scheduled-fire renderer crash MUST (a) dispatch the #224
    auto-crash bundle so the audit trail captures the failure exactly
    like human-driven turns get via ``_render_with_retry``, AND (b)
    re-raise so :meth:`SchedulerManager._fire_with_retry` can apply its
    1s/4s/16s backoff + terminal-disable policy.

    Strict: both behaviours are pinned. Pre-M3 code skipped the bundle
    entirely; a partial fix that dispatches the bundle but swallows the
    exception would break the scheduler's retry contract.
    """
    bot = _bare_bot()
    renderer = MagicMock()
    boom = RuntimeError("renderer template KeyError: missing token")
    renderer.render_response = AsyncMock(side_effect=boom)
    bridge = MagicMock()
    thread = MagicMock()

    with pytest.raises(RuntimeError, match="renderer template"):
        await bot._safe_fire_render(
            renderer=renderer, bridge=bridge, what="x", thread=thread,
        )

    bot._maybe_dispatch_auto_crash_bundle.assert_awaited_once()
    call_kwargs = bot._maybe_dispatch_auto_crash_bundle.await_args.kwargs
    assert call_kwargs["thread"] is thread
    assert call_kwargs["bridge"] is bridge
    assert call_kwargs["exc"] is boom


# ----------------------------------------------------------------------
# Test 6 — _safe_fire_render does NOT dispatch the crash bundle on
# transient discord errors (those bubble for the scheduler retry path)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_fire_render_no_bundle_on_transient_discord_error(
    monkeypatch,
):
    """M3 boundary: a transient discord error (HTTPException 5xx, etc.)
    must NOT dispatch the crash bundle — those are normal "retry me"
    failures, not renderer crashes. Dispatching a bundle for every
    transient blip would pollute the audit trail and trip the 5min
    cooldown so a *real* crash later gets silenced.

    Strict: the function still re-raises (so the scheduler retries),
    but ``_maybe_dispatch_auto_crash_bundle`` is NEVER awaited.
    """
    bot = _bare_bot()

    # Force ``is_transient_discord_error`` to True regardless of the
    # actual exception class so this test is isolated from discord.py's
    # internal classification.
    monkeypatch.setattr(
        "clauded.bot.is_transient_discord_error", lambda exc: True,
    )

    renderer = MagicMock()
    blip = RuntimeError("simulated transient")
    renderer.render_response = AsyncMock(side_effect=blip)
    bridge = MagicMock()
    thread = MagicMock()

    with pytest.raises(RuntimeError, match="simulated transient"):
        await bot._safe_fire_render(
            renderer=renderer, bridge=bridge, what="x", thread=thread,
        )

    bot._maybe_dispatch_auto_crash_bundle.assert_not_awaited()
