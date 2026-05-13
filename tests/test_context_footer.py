"""#182 — context-window usage `🧠 N%` segment in cost footer."""
import pytest
import sys
sys.path.insert(0, "src")
from unittest.mock import MagicMock, AsyncMock

from clauded.discord_renderer import _format_context_segment


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
    # 1.0% rounds to 1%
    assert _format_context_segment({"context_percentage": 1.0}) == " │ 🧠 1%"


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

    class FakeBridge:
        def __init__(self, events, ctx):
            self._events = events
            self.is_active = True
            self._client = MagicMock()
            self._ctx = ctx
        async def send_message(self, _text):
            for ev in self._events:
                yield ev
        async def get_context_usage(self):
            return self._ctx

    class FakeMessage:
        def __init__(self):
            self.content = ""
            self.embeds = []
        async def edit(self, **kwargs):
            if "content" in kwargs:
                self.content = kwargs["content"]
            return self
        async def delete(self):
            return None

    class FakeTarget:
        def __init__(self):
            self.id = 1
            self._sent = []
        async def send(self, *args, **kwargs):
            msg = FakeMessage()
            if "content" in kwargs:
                msg.content = kwargs["content"]
            self._sent.append(msg)
            return msg

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
    # Realistic ContextUsageResponse shape from SDK 0.1.80
    ctx_response = {
        "percentage": 73.4,
        "totalTokens": 145000,
        "maxTokens": 200000,
        "rawMaxTokens": 200000,
        "model": "claude-sonnet-4-5",
    }
    bridge = FakeBridge(events, ctx_response)
    await renderer.render_response(bridge, "hello")

    # Collect all message content
    all_content = " ".join(m.content for m in target._sent if m.content)
    # The footer should contain 🧠 73%
    assert "🧠 73%" in all_content, (
        f"Expected '🧠 73%' in footer; got: {all_content!r}"
    )


@pytest.mark.asyncio
async def test_footer_omits_context_segment_on_get_context_usage_failure():
    """If bridge.get_context_usage raises, footer renders without 🧠
    segment and no exception escapes."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    class FailingBridge:
        is_active = True
        _client = MagicMock()
        def __init__(self, events):
            self._events = events
        async def send_message(self, _text):
            for ev in self._events:
                yield ev
        async def get_context_usage(self):
            raise RuntimeError("synthetic CLI failure")

    class FakeMessage:
        def __init__(self):
            self.content = ""
            self.embeds = []
        async def edit(self, **kwargs):
            if "content" in kwargs:
                self.content = kwargs["content"]
            return self
        async def delete(self):
            return None

    class FakeTarget:
        id = 1
        def __init__(self):
            self._sent = []
        async def send(self, *args, **kwargs):
            msg = FakeMessage()
            if "content" in kwargs:
                msg.content = kwargs["content"]
            self._sent.append(msg)
            return msg

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
    bridge = FailingBridge(events)
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

    class NoneBridge:
        is_active = True
        _client = MagicMock()
        def __init__(self, events):
            self._events = events
        async def send_message(self, _text):
            for ev in self._events:
                yield ev
        async def get_context_usage(self):
            return None

    class FakeMessage:
        def __init__(self):
            self.content = ""
            self.embeds = []
        async def edit(self, **kwargs):
            if "content" in kwargs:
                self.content = kwargs["content"]
            return self
        async def delete(self):
            return None

    class FakeTarget:
        id = 1
        def __init__(self):
            self._sent = []
        async def send(self, *args, **kwargs):
            msg = FakeMessage()
            if "content" in kwargs:
                msg.content = kwargs["content"]
            self._sent.append(msg)
            return msg

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
    bridge = NoneBridge(events)
    await renderer.render_response(bridge, "hi")
    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🧠" not in all_content
    assert "🔥" not in all_content
