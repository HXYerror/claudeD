"""#220 — footer 🧠 0% race: SDK control plane lags `ResultMessage` emit
by ~hundreds of ms, so `get_context_usage()` called immediately returns
percentage=0. Fix: settle delay + retry-on-0 via `_fetch_context_pct_settled`.

Tests pass `initial_delay=0.0, retry_delay=0.0` to skip actual sleep.
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
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
    )
    assert pct == 0.0
    assert bridge.get_context_usage.await_count == 2


@pytest.mark.asyncio
async def test_returns_none_when_first_call_returns_none():
    """get_context_usage → None → no retry, return None (fail-soft)."""
    bridge = MagicMock()
    bridge.get_context_usage = AsyncMock(return_value=None)
    pct = await _fetch_context_pct_settled(
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
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
        bridge, initial_delay=0.0, retry_delay=0.0
    )
    assert pct == 0.5
    assert bridge.get_context_usage.await_count == 1


# ---------------------------------------------------------------------------
# Integration: full render_response with the new helper wired in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_footer_renders_real_pct_after_race_retry(monkeypatch):
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

    # Speed up the helper's sleeps in this integration test too
    import clauded.discord_renderer as renderer_mod
    real_helper = renderer_mod._fetch_context_pct_settled
    async def _fast_helper(bridge, *, initial_delay=0.5, retry_delay=0.5, log_label="footer"):
        return await real_helper(
            bridge, initial_delay=0.0, retry_delay=0.0, log_label=log_label
        )
    monkeypatch.setattr(renderer_mod, "_fetch_context_pct_settled", _fast_helper)

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
async def test_integration_footer_renders_zero_for_genuine_fresh_session(monkeypatch):
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

    import clauded.discord_renderer as renderer_mod
    real_helper = renderer_mod._fetch_context_pct_settled
    async def _fast_helper(bridge, *, initial_delay=0.5, retry_delay=0.5, log_label="footer"):
        return await real_helper(
            bridge, initial_delay=0.0, retry_delay=0.0, log_label=log_label
        )
    monkeypatch.setattr(renderer_mod, "_fetch_context_pct_settled", _fast_helper)

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
async def test_integration_footer_renders_sub_one_percent(monkeypatch):
    """0 < pct < 1 → `<1%` label preserved via existing _format_context_segment."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeBridge, FakeTarget

    class SubOneBridge(FakeBridge):
        async def get_context_usage(self):
            return {"percentage": 0.5, "totalTokens": 1000, "maxTokens": 200_000}

    import clauded.discord_renderer as renderer_mod
    real_helper = renderer_mod._fetch_context_pct_settled
    async def _fast_helper(bridge, *, initial_delay=0.5, retry_delay=0.5, log_label="footer"):
        return await real_helper(
            bridge, initial_delay=0.0, retry_delay=0.0, log_label=log_label
        )
    monkeypatch.setattr(renderer_mod, "_fetch_context_pct_settled", _fast_helper)

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
