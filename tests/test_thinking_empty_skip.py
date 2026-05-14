"""#208 — empty ThinkingBlock must be skipped, not rendered as `||||`.

User saw two embeds with title "💭 Thinking..." and a body of literal
four pipe characters. Root cause: SDK emitted ThinkingBlock with
empty `thinking` string; renderer wrapped it in `||...||` spoiler
markers but Discord's spoiler parser requires non-empty content
between markers, so `||||` falls through as literal text.

Fix (Option A, user-selected): skip empty/whitespace-only ThinkingBlocks.
"""
import pytest
from unittest.mock import MagicMock

from tests.conftest import FakeBridge, FakeTarget
from claude_agent_sdk.types import (
    AssistantMessage,
    ThinkingBlock,
    TextBlock,
    ResultMessage,
)


@pytest.mark.asyncio
async def test_empty_thinking_block_skipped_no_embed_sent():
    """ThinkingBlock(thinking="") → no embed sent in main thread path."""
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="", signature="sig-empty")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-empty",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    # NO thinking embed should be sent
    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert not thinking_embeds, (
        f"Empty ThinkingBlock must NOT render; got: "
        f"{[(m.embeds[0].title, m.embeds[0].description) for m in thinking_embeds]}"
    )


@pytest.mark.asyncio
async def test_whitespace_only_thinking_block_skipped():
    """ThinkingBlock(thinking="   \\n  ") → still skipped."""
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="   \n  ", signature="sig-ws")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-ws",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert not thinking_embeds


@pytest.mark.asyncio
async def test_non_empty_thinking_block_still_renders():
    """Backward compat: real thinking content renders unchanged."""
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="actual reasoning here", signature="sig")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-real",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert len(thinking_embeds) == 1
    embed = thinking_embeds[0].embeds[0]
    # Spoiler wrapper present, content sandwiched
    assert embed.description.startswith("||")
    assert embed.description.endswith("||")
    assert "actual reasoning here" in embed.description


@pytest.mark.asyncio
async def test_thinking_with_only_pipes_still_renders_after_escape():
    """Edge: thinking="||" is non-empty after .strip(); should render
    with the existing escape behavior (|| → \\|\\|)."""
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="||", signature="sig-pipes")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-pipes",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert len(thinking_embeds) == 1
    # Inner pipes escaped to \|\|; outer spoiler markers preserved
    assert thinking_embeds[0].embeds[0].description == "||\\|\\|||"


@pytest.mark.asyncio
async def test_empty_then_real_thinking_sequence_only_real_renders():
    """SDK sometimes emits empty placeholder then real content — only
    the real one should render."""
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ThinkingBlock(thinking="", signature="sig1")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ThinkingBlock(thinking="the real reasoning", signature="sig2")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-seq",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert len(thinking_embeds) == 1, (
        f"Expected exactly 1 thinking embed (the real one); got {len(thinking_embeds)}: "
        f"{[m.embeds[0].description for m in thinking_embeds]}"
    )
    assert "the real reasoning" in thinking_embeds[0].embeds[0].description
