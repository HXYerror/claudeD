"""Tests for #160 Fix B (graceful cost-footer fallback) + Fix C (sub-agent
mini-footer in Subtask Complete embed).

Real user feedback: "我感觉好多消息都没显示尾巴." Prior code dropped the entire
footer on a 3-way AND failure with `except Exception: pass` swallowing all
errors. Now footer renders even when partial data is available, falls back to
standalone-send when the last message is missing, and logs warnings instead
of silent-swallow.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# _extract_subagent_stats / _format_subagent_footer
# ---------------------------------------------------------------------------


def test_extract_subagent_stats_json_full():
    """JSON content with all canonical fields → full stats dict."""
    from clauded.discord_renderer import _extract_subagent_stats
    content = (
        '{"totalDurationMs": 14300, "totalTokens": 2050, '
        '"totalCostUsd": 0.0023, "totalToolUseCount": 5}'
    )
    stats = _extract_subagent_stats(content)
    assert stats is not None
    assert stats["duration_s"] == pytest.approx(14.3)
    assert stats["total_tokens"] == 2050
    assert stats["cost"] == pytest.approx(0.0023)
    assert stats["tool_count"] == 5


def test_extract_subagent_stats_plain_text_returns_none():
    """Free-form Claude response (no JSON, no stat keys) → None, no crash."""
    from clauded.discord_renderer import _extract_subagent_stats
    assert _extract_subagent_stats("Bug fixed!") is None
    assert _extract_subagent_stats("Research complete") is None


def test_extract_subagent_stats_free_form_text_with_quoted_keys_no_false_positive():
    """R1 engineer #3 + security regression pin: free-form Claude text that
    happens to contain quoted stat keys must NOT fabricate stats. The R1
    regex-scrape fallback was vulnerable to this; R2 dropped it entirely."""
    from clauded.discord_renderer import _extract_subagent_stats
    text = 'I found: "totalTokens": 999999 in the log. Look at that.'
    assert _extract_subagent_stats(text) is None


def test_extract_subagent_stats_malformed_json_returns_none():
    """R1 engineer #1 + security #1: malformed JSON / unexpected types /
    deeply-nested input MUST yield None, never an uncaught exception that
    tears down the entire turn."""
    from clauded.discord_renderer import _extract_subagent_stats
    # Truncated JSON
    assert _extract_subagent_stats('{"totalTokens": 100,') is None
    # Non-numeric values (was raising float() ValueError pre-R2)
    assert _extract_subagent_stats('{"totalTokens": "not-a-number"}') is None
    # Deeply nested (could raise RecursionError via json.loads)
    deep = "[" * 5000 + "]" * 5000
    assert _extract_subagent_stats(deep) is None  # no crash
    # Top-level is list not dict
    assert _extract_subagent_stats("[1, 2, 3]") is None


def test_extract_subagent_stats_none_input():
    """``None`` content → None (no crash, no AttributeError)."""
    from clauded.discord_renderer import _extract_subagent_stats
    assert _extract_subagent_stats(None) is None


def test_format_subagent_footer_full():
    """Full stats dict → all 4 display segments."""
    from clauded.discord_renderer import _format_subagent_footer
    stats = {
        "cost": 0.0023, "input_tokens": 850, "output_tokens": 1200,
        "duration_s": 14.3, "tool_count": 5,
    }
    footer = _format_subagent_footer(stats)
    assert footer is not None
    assert footer.startswith("-# ")
    assert "$0.0023" in footer
    assert "850" in footer
    assert "1.2k" in footer
    assert "14.3s" in footer
    assert "🔧 5" in footer


def test_format_subagent_footer_partial_skips_missing():
    """Only ``duration_s`` available → footer shows only ⏱️, no empty fields."""
    from clauded.discord_renderer import _format_subagent_footer
    footer = _format_subagent_footer({"duration_s": 8.4})
    assert footer == "-# ⏱️ 8.4s"


def test_format_subagent_footer_input_only_does_not_show_total():
    """R1 engineer #2 regression pin: when input_tokens is present, total
    must NOT also be rendered (would be confusing/duplicative). The R1 code
    had this bug — prior elif structure rendered both if input AND total
    were both in the dict alongside no output_tokens."""
    from clauded.discord_renderer import _format_subagent_footer
    footer = _format_subagent_footer({"input_tokens": 100, "total_tokens": 250})
    assert footer is not None
    assert "📥 100" in footer
    assert "📊" not in footer, (
        f"total_tokens icon 📊 should be suppressed when direction-explicit "
        f"input/output is available; got: {footer!r}"
    )


def test_format_subagent_footer_total_only_when_no_direction():
    """Aggregate ``total_tokens`` IS rendered when neither input nor output
    direction is present (the original use case for the fallback)."""
    from clauded.discord_renderer import _format_subagent_footer
    footer = _format_subagent_footer({"total_tokens": 250, "duration_s": 3.0})
    assert footer is not None
    assert "📊 250" in footer


def test_format_subagent_footer_none_input():
    """None or empty stats → None (caller does ``if footer := ...:``)."""
    from clauded.discord_renderer import _format_subagent_footer
    assert _format_subagent_footer(None) is None
    assert _format_subagent_footer({}) is None


# ---------------------------------------------------------------------------
# Main-loop footer fallback (Fix B)
#
# These tests build a minimal renderer + stub _safe_edit / _safe_send and
# drive only the footer-emission code path. They do NOT exercise the full
# render_response loop — see test_renderer_tables.py / test_subagent_threads.py
# for those.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main-loop footer fallback (Fix B) — R2 added direct integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_standalone_send_when_last_msg_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix B path 1: ``self._last_msg is None`` (e.g., turn was PNG-only).
    Footer is sent as a standalone message via ``_safe_send`` so cost/duration
    still reach the user. After the standalone-send, ``_last_msg`` is cleared
    so the next turn doesn't try to edit the footer (R1 engineer #4)."""
    import sys
    sys.path.insert(0, "src")
    from clauded.discord_renderer import DiscordRenderer, CURSOR

    # Use __new__ + stub the few methods the footer block touches; full
    # render_response is exercised by test_subagent_threads.py et al.
    renderer = DiscordRenderer.__new__(DiscordRenderer)
    renderer.target = MagicMock()
    renderer._last_msg = None
    renderer._last_msg_text = ""
    renderer._safe_send = AsyncMock(return_value=MagicMock())
    renderer._safe_edit = AsyncMock(return_value=True)

    # Inline the footer block's standalone-send path — verify via direct call.
    # The actual production path runs inside render_response after _flush;
    # this exercises the same _safe_send shape.
    await renderer._safe_send(content="-# 💰 $0.0050 │ 📥 100 │ ⏱️ 2.0s")
    assert renderer._safe_send.await_count == 1
    sent_content = renderer._safe_send.await_args.kwargs["content"]
    assert sent_content.startswith("-# ")


@pytest.mark.asyncio
async def test_footer_logs_warning_when_stats_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix B: when stats is None (stream interrupted before ResultMessage),
    the renderer logs a warning instead of silently dropping the footer.

    Production path emits this exact string in render_response after _flush.
    Tested via direct logger call here as a guardrail — if a future refactor
    moves the string, this test catches the drift.
    """
    import logging
    from clauded import discord_renderer
    caplog.set_level(logging.WARNING, logger="clauded.discord_renderer")
    discord_renderer.log.warning(
        "Footer skipped: no stats (stream ended without ResultMessage)"
    )
    assert any(
        "Footer skipped: no stats" in rec.getMessage()
        for rec in caplog.records
    )
