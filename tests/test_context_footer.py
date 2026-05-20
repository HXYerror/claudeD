"""#182 — context-window usage `🧠 N%` segment in cost footer."""
import pytest
import sys
sys.path.insert(0, "src")

from clauded.discord_renderer import _format_context_segment

from tests.conftest import FakeBridge, FakeTarget  # noqa: E402


# ---------------------------------------------------------------------------
# Unit tests for the helper
# ---------------------------------------------------------------------------


def test_format_context_returns_none_when_missing():
    assert _format_context_segment(None) is None
    assert _format_context_segment({}) is None
    assert _format_context_segment({"context_percentage": None}) is None


def test_format_context_returns_none_on_invalid():
    assert _format_context_segment({"context_percentage": "abc"}) is None
    assert _format_context_segment({"context_percentage": -1}) is None
    assert _format_context_segment({"context_percentage": 101}) is None


@pytest.mark.parametrize("pct,emoji", [
    (0, "🧠"),
    (1, "🧠"),
    (50, "🧠"),
    (74, "🧠"),
    (74.9, "🧠"),
    (75, "⚠️"),
    (75.0, "⚠️"),
    (80, "⚠️"),
    (89, "⚠️"),
    (89.9, "⚠️"),
    (90, "🔥"),
    (90.0, "🔥"),
    (95, "🔥"),
    (99, "🔥"),
    (100, "🔥"),
])
def test_format_context_emoji_thresholds(pct, emoji):
    """Boundary check: 89.9 → ⚠️, 90.0 → 🔥, 74.9 → 🧠, 75.0 → ⚠️."""
    out = _format_context_segment({"context_percentage": pct})
    assert out is not None
    assert emoji in out, f"pct={pct} expected {emoji!r} in {out!r}"


def test_format_context_sub_1_percent_floor():
    """0 < pct < 1 → display `<1%` instead of `0%`."""
    assert _format_context_segment({"context_percentage": 0.4}) == " │ 🧠 <1%"
    assert _format_context_segment({"context_percentage": 0.99}) == " │ 🧠 <1%"
    # 0% exact stays as 0% (haven't started the session)
    assert _format_context_segment({"context_percentage": 0}) == " │ 🧠 0%"
    # #v1.18 precision tier: 1–10% range uses 1-decimal precision so
    # users see movement (was `🧠 1%` flat regardless of 1.0 vs 9.9).
    assert _format_context_segment({"context_percentage": 1.0}) == " │ 🧠 1.0%"
    assert _format_context_segment({"context_percentage": 2.8}) == " │ 🧠 2.8%"
    assert _format_context_segment({"context_percentage": 9.9}) == " │ 🧠 9.9%"
    # >= 10% reverts to int (precision adds no value at scale).
    assert _format_context_segment({"context_percentage": 10.0}) == " │ 🧠 10%"
    assert _format_context_segment({"context_percentage": 73.4}) == " │ 🧠 73%"


def test_format_context_shape():
    """The segment must START with `\\u2502 ` (space separator) so it can
    be concatenated to the existing footer without breaking layout."""
    out = _format_context_segment({"context_percentage": 50})
    assert out.startswith(" │ "), f"separator missing: {out!r}"
    assert out.endswith("%")


# ---------------------------------------------------------------------------
# Integration test using real SDK shape (per #160 / #172 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_includes_context_segment_e2e():
    """Drive render_response end-to-end with a mock bridge whose
    get_context_usage returns a realistic dict shape; assert the final
    footer contains `🧠 73%`."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="response text")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-ctx",
            total_cost_usd=0.012,
            usage={"input_tokens": 1200, "output_tokens": 340},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    # Realistic ContextUsageResponse shape from SDK 0.1.80.
    # #v1.18: footer now computes pct from totalTokens/maxTokens for
    # precision (SDK's ``percentage`` is int-rounded; 0.0-0.5% shows as
    # 0 even when real ratio is non-zero). Numbers chosen so the
    # self-computed ratio is exactly 73.0% (146k/200k).
    ctx_response = {
        "percentage": 73,  # SDK-rounded int (now ignored by helper)
        "totalTokens": 146000,
        "maxTokens": 200000,
        "rawMaxTokens": 200000,
        "model": "claude-sonnet-4-5",
        # #263: categories with Free space so the new supplement logic works
        "categories": [
            {"name": "Messages", "tokens": 136000},
            {"name": "System", "tokens": 10000},
            {"name": "Free space", "tokens": 54000},  # 200k - 146k = 54k
        ],
    }
    bridge = FakeBridge(events, get_context_usage_returns=ctx_response)
    await renderer.render_response(bridge, "hello")

    # Collect all message content
    all_content = " ".join(m.content for m in target._sent if m.content)
    # The footer should contain 🧠 73% (146000/200000 = 73.0%, int-tier)
    assert "🧠 73%" in all_content, (
        f"Expected '🧠 73%' in footer; got: {all_content!r}"
    )


@pytest.mark.asyncio
async def test_footer_omits_context_segment_on_get_context_usage_failure():
    """If bridge.get_context_usage raises, footer renders without 🧠
    segment and no exception escapes."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="result")],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=50, duration_api_ms=30,
            is_error=False, num_turns=1, session_id="sess-fail",
            total_cost_usd=0.005,
            usage={"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(
        events, get_context_usage_raises=RuntimeError("synthetic CLI failure")
    )
    # MUST NOT raise
    await renderer.render_response(bridge, "hello")
    all_content = " ".join(m.content for m in target._sent if m.content)
    # Footer present (other segments)
    assert "💰" in all_content
    assert "⏱️" in all_content
    # But no 🧠 / ⚠️ / 🔥 segment
    assert "🧠" not in all_content
    # Note: ⚠️ may appear from stop_reason — verify it's not the context one
    # by checking absence of `%` near a context emoji
    for emoji in ("🧠", "🔥"):
        assert emoji not in all_content, f"{emoji} leaked into footer despite get_context_usage failure"


@pytest.mark.asyncio
async def test_footer_omits_context_when_get_context_usage_returns_none():
    """If bridge.get_context_usage returns None (force-dropped bridge per
    #146), footer renders without segment."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="result")],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=50, duration_api_ms=30,
            is_error=False, num_turns=1, session_id="sess-none",
            total_cost_usd=0.001,
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    # Default: get_context_usage_returns=None (matches the old NoneBridge)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "hi")
    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🧠" not in all_content
    assert "🔥" not in all_content


@pytest.mark.asyncio
async def test_footer_context_uses_free_space_not_totalTokens():
    """#263: when totalTokens diverges from real usage (cache hit scenario),
    footer must show the real usage (from Free space supplement), not the
    misleading totalTokens.

    Scenario from the issue: totalTokens=408 (last-turn input), but real
    buffer usage is ~449k (maxTokens=1M - Free space=551100).
    Old code would show <1%; correct answer is ~44.9%.
    """
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="result")],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=50, duration_api_ms=30,
            is_error=False, num_turns=1, session_id="sess-263",
            total_cost_usd=0.01,
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    # Divergence fixture: totalTokens=408 (tiny, from cache hit)
    # but real usage shown by categories is ~449k
    ctx_response = {
        "percentage": 0,          # SDK says 0% (misleading)
        "totalTokens": 408,       # last-turn input only (misleading)
        "maxTokens": 1_000_000,
        "model": "claude-sonnet-4-5",
        "categories": [
            {"name": "Messages", "tokens": 409_000},
            {"name": "Autocompact summary", "tokens": 33_000},
            {"name": "System", "tokens": 6_100},
            {"name": "Skills", "tokens": 858},
            {"name": "Free space", "tokens": 551_100},
        ],
    }
    bridge = FakeBridge(events, get_context_usage_returns=ctx_response)
    await renderer.render_response(bridge, "hello")

    all_content = " ".join(m.content for m in target._sent if m.content)
    # Must show ~45% (from Free space supplement), NOT <1% or 0%
    # 1M - 551100 = 448900 → 44.89%
    assert "🧠 45%" in all_content or "🧠 44%" in all_content, (
        f"Expected '🧠 44%' or '🧠 45%' in footer (Free space supplement); "
        f"got: {all_content!r}"
    )
    # Must NOT show the misleading <1% or 0%
    assert "🧠 <1%" not in all_content, "Should not show <1% with 45% real usage"
    assert "🧠 0%" not in all_content, "Should not show 0% with 45% real usage"


@pytest.mark.asyncio
async def test_footer_context_fallback_when_no_free_space():
    """#263 AC5: if categories don't contain Free space, fallback to
    totalTokens (no crash)."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="result")],
            model="claude-sonnet-4-5", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=50, duration_api_ms=30,
            is_error=False, num_turns=1, session_id="sess-263fb",
            total_cost_usd=0.01,
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    # No categories at all — should fallback to totalTokens
    ctx_response = {
        "totalTokens": 50000,
        "maxTokens": 200000,
        "model": "claude-sonnet-4-5",
    }
    bridge = FakeBridge(events, get_context_usage_returns=ctx_response)
    await renderer.render_response(bridge, "hello")

    all_content = " ".join(m.content for m in target._sent if m.content)
    # Fallback: 50000/200000 = 25%
    assert "🧠 25%" in all_content, (
        f"Expected '🧠 25%' in footer (fallback to totalTokens); got: {all_content!r}"
    )
