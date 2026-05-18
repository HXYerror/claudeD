"""#220 — footer 🧠 0% race: SDK control plane lags `ResultMessage` emit
by ~hundreds of ms, so `get_context_usage()` called immediately returns
percentage=0. Fix: settle delay + retry-on-0 via `_fetch_context_pct_settled`.

Tests pass `settle_delay=0.0` to skip actual sleep.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock

from clauded.discord_renderer import _fetch_context_pct_settled


# ---------------------------------------------------------------------------
# Unit tests on the helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_first_call_value_when_above_zero():
    """percentage > 0 on first call → no retry, return immediately."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(return_value={"percentage": 42.5})
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct == 42.5
    assert bridge.get_context_usage.await_count == 1


@pytest.mark.asyncio
async def test_retries_once_when_first_call_returns_zero():
    """percentage == 0 first → retry; if second call >0, use that."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(
        side_effect=[{"percentage": 0.0}, {"percentage": 12.3}]
    )
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct == 12.3
    assert bridge.get_context_usage.await_count == 2


@pytest.mark.asyncio
async def test_returns_zero_when_both_calls_zero_fresh_session():
    """Legit fresh-session 0% case: both calls return 0 → return 0
    (not None). _format_context_segment renders `🧠 0%` correctly for
    a brand-new conversation with no context yet."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(
        side_effect=[{"percentage": 0.0}, {"percentage": 0.0}]
    )
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct == 0.0
    assert bridge.get_context_usage.await_count == 2


@pytest.mark.asyncio
async def test_returns_none_when_first_call_returns_none():
    """get_context_usage → None → no retry, return None (fail-soft)."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(return_value=None)
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct is None
    assert bridge.get_context_usage.await_count == 1


@pytest.mark.asyncio
async def test_returns_none_when_percentage_field_missing():
    """get_context_usage returns dict without `percentage` key → None.
    No retry — missing field is a contract problem, not a timing one."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(return_value={"totalTokens": 100})
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct is None
    assert bridge.get_context_usage.await_count == 1


@pytest.mark.asyncio
async def test_returns_none_when_first_call_raises():
    """get_context_usage raises → fail-soft, no retry, return None.
    Existing #160 graceful-omit pattern."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(side_effect=RuntimeError("boom"))
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct is None
    assert bridge.get_context_usage.await_count == 1


@pytest.mark.asyncio
async def test_returns_none_when_retry_call_returns_none():
    """First 0, retry returns None → None (don't render anything)."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(
        side_effect=[{"percentage": 0.0}, None]
    )
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct is None
    assert bridge.get_context_usage.await_count == 2


@pytest.mark.asyncio
async def test_returns_sub_one_percent_from_first_call():
    """0 < pct < 1 first call → return as-is (no retry). Caller's
    _format_context_segment renders `<1%`."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(return_value={"percentage": 0.5})
    pct = await _fetch_context_pct_settled(
        bridge, settle_delay=0.0
    )
    assert pct == 0.5
    assert bridge.get_context_usage.await_count == 1


# ---------------------------------------------------------------------------
# Integration: full render_response with the new helper wired in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_footer_renders_real_pct_after_race_retry():
    """End-to-end: bridge returns 0 first then 12.3 (the prod race).
    Footer must contain `🧠 12%`, not `🧠 0%`."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeBridge, FakeTarget

    class RaceBridge(FakeBridge):
        """Bridge whose first get_context_usage returns 0, second returns 12.3."""
        def __init__(self, events):
            super().__init__(events)
            self._call_count = 0

        async def get_context_usage(self):
            self._call_count += 1
            if self._call_count == 1:
                return {"percentage": 0.0, "totalTokens": 0, "maxTokens": 200_000}
            return {"percentage": 12.3, "totalTokens": 24_600, "maxTokens": 200_000}

    # (autouse `_zero_context_settle_delay` in conftest.py zeroes the helper's
    # settle_delay across the whole suite — no per-test monkeypatch needed.)

    events = [
        AssistantMessage(
            content=[TextBlock(text="hello")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-race",
            total_cost_usd=0.001,
            usage={"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(RaceBridge(events), "hi")

    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🧠 12%" in all_content, (
        f"Footer must show retried 12%, got: {all_content!r}"
    )
    assert "🧠 0%" not in all_content, (
        f"Footer must NOT show stale 0%, got: {all_content!r}"
    )


@pytest.mark.asyncio
async def test_integration_footer_renders_zero_for_genuine_fresh_session():
    """Legit case: bridge.get_context_usage returns 0 on BOTH calls
    (first message in a brand-new session with no context yet) →
    footer renders `🧠 0%`."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeBridge, FakeTarget

    class FreshBridge(FakeBridge):
        async def get_context_usage(self):
            return {"percentage": 0.0, "totalTokens": 0, "maxTokens": 200_000}

    # (autouse `_zero_context_settle_delay` in conftest.py handles this.)

    events = [
        AssistantMessage(
            content=[TextBlock(text="hi")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-fresh",
            total_cost_usd=0.001,
            usage={"input_tokens": 5, "output_tokens": 2},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FreshBridge(events), "hi")

    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🧠 0%" in all_content, (
        f"Fresh-session 0% must render correctly, got: {all_content!r}"
    )


@pytest.mark.asyncio
async def test_integration_footer_renders_sub_one_percent():
    """0 < pct < 1 → `<1%` label preserved via existing _format_context_segment."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeBridge, FakeTarget

    class SubOneBridge(FakeBridge):
        async def get_context_usage(self):
            return {"percentage": 0.5, "totalTokens": 1000, "maxTokens": 200_000}

    # (autouse `_zero_context_settle_delay` in conftest.py handles this.)

    events = [
        AssistantMessage(
            content=[TextBlock(text="hi")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-low",
            total_cost_usd=0.001,
            usage={"input_tokens": 1000, "output_tokens": 200},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(SubOneBridge(events), "hi")

    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🧠 <1%" in all_content, (
        f"Sub-1% must render `<1%`, got: {all_content!r}"
    )


# ---------------------------------------------------------------------------
# v1.18 precision: use totalTokens / maxTokens, not SDK's int-rounded percentage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v118_precision_self_computes_from_total_and_max():
    """SDK ``percentage`` is int-rounded (28192/1000000 ≈ 2.82% → 3 or 0).
    Helper now computes ``totalTokens / maxTokens`` itself for precision.
    """
    from clauded.discord_renderer import _fetch_context_pct_settled

    class FakeBridge:
        async def get_context_usage(self):
            return {
                "percentage": 3,   # SDK int-rounded (would lose precision)
                "totalTokens": 28192,
                "maxTokens": 1000000,
            }

    pct = await _fetch_context_pct_settled(FakeBridge(), settle_delay=0.0)
    # 28192 / 1000000 * 100 = 2.8192 (not the SDK's int 3)
    assert pct is not None
    assert 2.8 <= pct <= 2.9, f"expected ~2.82%, got {pct}"


@pytest.mark.asyncio
async def test_v118_precision_falls_back_to_sdk_percentage_when_total_missing():
    """If totalTokens/maxTokens absent, fall back to SDK's ``percentage`` field."""
    from clauded.discord_renderer import _fetch_context_pct_settled

    class FakeBridge:
        async def get_context_usage(self):
            return {"percentage": 47}  # only the legacy field

    pct = await _fetch_context_pct_settled(FakeBridge(), settle_delay=0.0)
    assert pct == 47.0


@pytest.mark.asyncio
async def test_v118_precision_handles_zero_max_tokens_gracefully():
    """Defensive: maxTokens=0 → fall back to legacy percentage field."""
    from clauded.discord_renderer import _fetch_context_pct_settled

    class FakeBridge:
        async def get_context_usage(self):
            return {"percentage": 50, "totalTokens": 100, "maxTokens": 0}

    pct = await _fetch_context_pct_settled(FakeBridge(), settle_delay=0.0)
    # 0 maxTokens triggers the fallback path
    assert pct == 50.0


@pytest.mark.asyncio
async def test_v118_user_reported_28k_in_1m_window_no_longer_zero():
    """User-reported scenario (5/18): conversation at 28k tokens in 1M
    Opus context window. Pre-fix SDK rounded to 0 → footer showed 🧠 0%
    forever. Post-fix: 2.8% precision."""
    from clauded.discord_renderer import _fetch_context_pct_settled, _format_context_segment

    class OpusBridge:
        async def get_context_usage(self):
            return {
                "percentage": 0,  # SDK rounded 28192/1000000 down
                "totalTokens": 28192,
                "maxTokens": 1000000,
            }

    pct = await _fetch_context_pct_settled(OpusBridge(), settle_delay=0.0)
    assert pct is not None and pct > 0, (
        f"#v1.18: 28k/1M must NOT round to 0; got {pct}"
    )
    # Threshold tier renders with 1-decimal precision in 1-10% range
    seg = _format_context_segment({"context_percentage": pct})
    assert seg == " │ 🧠 2.8%", f"expected ' │ 🧠 2.8%', got {seg!r}"
