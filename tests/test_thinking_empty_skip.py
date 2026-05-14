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


@pytest.mark.asyncio
async def test_none_thinking_handled_defensively():
    """R1 tester gap: SDK type allows ``thinking: str | None``. While
    ``str | None`` SDK shape is rare in practice, the ``block.thinking or ""``
    guard must handle None without crashing."""
    from clauded.discord_renderer import DiscordRenderer

    # Build a ThinkingBlock with None thinking — bypass dataclass validation
    # by direct instantiation + attribute mutation since dataclasses may
    # reject None at construction.
    real_block = ThinkingBlock(thinking="placeholder", signature="sig-none")
    # MagicMock or direct attr set — actual SDK behavior is to allow this
    # via the dataclass default, but we don't rely on that
    object.__setattr__(real_block, "thinking", None)

    events = [
        AssistantMessage(
            content=[real_block],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-none",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    # MUST NOT raise
    await renderer.render_response(FakeBridge(events), "hi")

    # No thinking embed
    thinking_embeds = [
        m for m in target._sent
        if m.embeds and "Thinking" in (m.embeds[0].title or "")
    ]
    assert not thinking_embeds, "None thinking must be treated as empty (no embed)"


@pytest.mark.asyncio
async def test_empty_thinking_in_subagent_thread_skipped():
    """R1 tester gap: sub-agent path (renderer.py:622) was covered only
    by code-inspection equivalence. Pin it explicitly by exercising a
    Task tool sub-agent flow with parent_tool_use_id set, which routes
    through the sub-agent renderer's ThinkingBlock branch.

    Mirrors the FakeThread monkey-patch pattern from
    test_subtask_complete_render.py since sub-thread creation needs
    a real ``.mention`` / ``.send`` shape.
    """
    from claude_agent_sdk.types import ToolUseBlock, ToolResultBlock
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeMessage

    class _FakeThread:
        """Sub-thread stub with the discord.Thread surface the renderer touches."""
        def __init__(self, name):
            import random
            self.id = random.randint(10_000, 99_999)
            self.name = name
            self.mention = f"<#{self.id}>"
            self.parent = None
            self._sent = []
        async def send(self, *args, **kwargs):
            msg = FakeMessage(msg_id=self.id * 100 + len(self._sent))
            if "content" in kwargs:
                msg.content = kwargs["content"]
            if "embed" in kwargs:
                msg.embeds = [kwargs["embed"]]
            self._sent.append(msg)
            return msg

    threads_created: list[_FakeThread] = []
    orig_create = FakeMessage.create_thread
    async def _record_create(self, name, **kw):
        t = _FakeThread(name=name)
        threads_created.append(t)
        return t
    FakeMessage.create_thread = _record_create  # type: ignore[method-assign]

    try:
        events = [
            AssistantMessage(
                content=[ToolUseBlock(id="task-1", name="Task", input={"description": "test"})],
                model="claude-sonnet-4-5",
                parent_tool_use_id=None,
            ),
            AssistantMessage(
                content=[ThinkingBlock(thinking="", signature="sig-sub-empty")],
                model="claude-sonnet-4-5",
                parent_tool_use_id="task-1",  # ← routes to sub-agent path
            ),
            AssistantMessage(
                content=[ToolResultBlock(tool_use_id="task-1", content="ok", is_error=False)],
                model="claude-sonnet-4-5",
                parent_tool_use_id=None,
            ),
            ResultMessage(
                subtype="result", duration_ms=10, duration_api_ms=5,
                is_error=False, num_turns=1, session_id="sess-sub-empty",
                total_cost_usd=0.0,
            ),
        ]
        target = FakeTarget()
        renderer = DiscordRenderer(target)
        await renderer.render_response(FakeBridge(events), "test subtask")
    finally:
        FakeMessage.create_thread = orig_create  # type: ignore[method-assign]

    # Audit: no empty/pipe-only thinking embed anywhere
    all_msgs = list(target._sent)
    for t in threads_created:
        all_msgs.extend(t._sent)
    for msg in all_msgs:
        for embed in msg.embeds:
            if (embed.title or "") == "💭 Thinking...":
                assert embed.description != "||||", (
                    f"Empty thinking leaked as ||||: {embed.description!r}"
                )
