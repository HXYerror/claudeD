"""Tests for sub-agent inline display in DiscordRenderer.

Validates that when Claude spawns a sub-agent via the Task tool, a separator
embed is posted inline in the main thread. All sub-agent content renders
in the main thread — no separate threads are created.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import discord

from clauded.discord_renderer import (
    COLOR_INFO,
    COLOR_TOOL_FAILURE,
    COLOR_TOOL_RUNNING,
    COLOR_TOOL_SUCCESS,
    DiscordRenderer,
)
from clauded.claude_bridge import (
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)
from claude_code_sdk.types import AssistantMessage, StreamEvent


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeMessage:
    """Minimal discord.Message stand-in."""
    id: int = 1
    content: str = ""
    embeds: list = field(default_factory=list)

    async def edit(self, **kwargs):
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]


class FakeChannel:
    """Minimal discord channel."""
    def __init__(self):
        self._messages: list[FakeMessage] = []
        self.parent = None
        self.guild = None

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 1, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._messages.append(msg)
        return msg


class FakeMainThread:
    """Minimal discord thread (the main conversation thread)."""
    def __init__(self, parent_channel: FakeChannel | None = None):
        self.parent = parent_channel
        self._messages: list[FakeMessage] = []
        self.guild = None

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 200, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._messages.append(msg)
        return msg


class FakeBridge:
    """Minimal ClaudeBridge that yields pre-configured events."""
    def __init__(self, events: list):
        self._events = events

    async def send_message(self, text: str):
        for ev in self._events:
            yield ev


# ---------------------------------------------------------------------------
# _build_tool_embed tests
# ---------------------------------------------------------------------------

class TestBuildToolEmbed:
    def test_bash_embed(self):
        block = ToolUseBlock(id="t1", name="Bash", input={"command": "ls -la"})
        embed = DiscordRenderer._build_tool_embed(block)
        assert embed.title == "🔄 Bash"
        assert "ls -la" in embed.description
        assert embed.color.value == COLOR_TOOL_RUNNING

    def test_write_embed(self):
        block = ToolUseBlock(id="t2", name="Write", input={"file_path": "/tmp/test.py"})
        embed = DiscordRenderer._build_tool_embed(block)
        assert embed.title == "🔄 Write"
        assert "/tmp/test.py" in embed.description

    def test_edit_embed(self):
        block = ToolUseBlock(id="t3", name="Edit", input={"file_path": "/tmp/foo.py"})
        embed = DiscordRenderer._build_tool_embed(block)
        assert embed.title == "🔄 Edit"
        assert "/tmp/foo.py" in embed.description

    def test_read_embed(self):
        block = ToolUseBlock(id="t4", name="Read", input={"file_path": "/tmp/bar.py"})
        embed = DiscordRenderer._build_tool_embed(block)
        assert embed.title == "🔄 Read"
        assert "/tmp/bar.py" in embed.description

    def test_unknown_tool_embed(self):
        block = ToolUseBlock(id="t5", name="CustomTool", input={})
        embed = DiscordRenderer._build_tool_embed(block)
        assert embed.title == "🔄 CustomTool"
        assert embed.description == "Executing..."


# ---------------------------------------------------------------------------
# Sub-agent inline display tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_shows_inline_separator():
    """A Task ToolUseBlock should show a separator embed in the main thread."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-1", name="Task", input={"description": "Fix the bug", "prompt": "Please fix"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-1", content="Bug fixed!", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "fix the bug")

    # Should have separator embed in main thread (edited to completion)
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    first_embed = embed_msgs[0].embeds[0]
    assert "Subtask" in first_embed.title
    # After completion, embed is edited — description shows result, not original desc
    assert first_embed.description is not None


@pytest.mark.asyncio
async def test_task_completion_updates_separator():
    """When a Task completes, the separator embed should be updated."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-1", name="Task", input={"description": "Fix the bug"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-1", content="Bug fixed!", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "fix the bug")

    # The separator embed should have been edited to show completion
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    # After edit, the embed should show "Subtask Complete"
    final_embed = embed_msgs[0].embeds[0]
    assert "Complete" in (final_embed.title or "")


@pytest.mark.asyncio
async def test_subagent_messages_stay_in_main_thread():
    """Messages with parent_tool_use_id should render in the main thread (no routing)."""
    main_thread = FakeMainThread()

    events = [
        # Main agent creates a task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-2", name="Task", input={"description": "Research topic"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Sub-agent sends text (has parent_tool_use_id) — should go to main thread
        AssistantMessage(
            content=[
                TextBlock(text="I'm researching the topic now..."),
            ],
            model="claude-sonnet",
            parent_tool_use_id="task-2",
        ),
        # Task completes
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-2", content="Research complete", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=2000,
            duration_api_ms=1800,
            is_error=False,
            num_turns=2,
            session_id="sess-2",
            total_cost_usd=0.02,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "research this")

    # Text should be in main thread
    text_msgs = [m for m in main_thread._messages if m.content and "researching" in m.content]
    assert len(text_msgs) >= 1


@pytest.mark.asyncio
async def test_task_failure_shows_in_main_thread():
    """When a Task fails, the main thread separator should reflect the failure."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-3", name="Task", input={"description": "Deploy app"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-3", content="Deploy failed: timeout", is_error=True),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=5000,
            duration_api_ms=4500,
            is_error=False,
            num_turns=1,
            session_id="sess-3",
            total_cost_usd=0.03,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "deploy")

    # Main thread should have the failure embed (edited from separator)
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    last_embed = embed_msgs[0].embeds[0]
    assert "❌" in (last_embed.title or "")


@pytest.mark.asyncio
async def test_multiple_subtasks_get_separate_separators():
    """Multiple Task tools create separate separator embeds in main thread."""
    main_thread = FakeMainThread()

    events = [
        # First task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-a", name="Task", input={"description": "Task Alpha"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Second task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-b", name="Task", input={"description": "Task Beta"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Both complete
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="task-a", content="A done", is_error=False)],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="task-b", content="B done", is_error=False)],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=3000,
            duration_api_ms=2800,
            is_error=False,
            num_turns=3,
            session_id="sess-6",
            total_cost_usd=0.05,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "multi-task")

    # Two separator embeds for the two subtasks (both edited to "Complete")
    subtask_embeds = [m for m in main_thread._messages if m.embeds and "Subtask" in (m.embeds[0].title or "")]
    assert len(subtask_embeds) >= 2

    # After completion, both should show "Complete"
    assert "Complete" in subtask_embeds[0].embeds[0].title
    assert "Complete" in subtask_embeds[1].embeds[0].title


@pytest.mark.asyncio
async def test_non_subagent_messages_stay_in_main():
    """Messages without parent_tool_use_id go to the main thread as usual."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[TextBlock(text="Hello from main agent")],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=500,
            duration_api_ms=400,
            is_error=False,
            num_turns=1,
            session_id="sess-7",
            total_cost_usd=0.005,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "hello")

    # Main thread has the text
    text_msgs = [m for m in main_thread._messages if m.content and "Hello from main" in m.content]
    assert len(text_msgs) >= 1


@pytest.mark.asyncio
async def test_subagent_thinking_renders_in_main():
    """ThinkingBlock in sub-agent messages renders in main thread."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-6", name="Task", input={"description": "Think deeply"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ThinkingBlock(thinking="Let me think about this carefully...", signature="sig")],
            model="claude-sonnet",
            parent_tool_use_id="task-6",
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="task-6", content="Thought complete", is_error=False)],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-8",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "think")

    # Thinking embed should be in main thread (rendered inline)
    thinking_embeds = [m for m in main_thread._messages if m.embeds and "Thinking" in (m.embeds[0].title or "")]
    assert len(thinking_embeds) >= 1


@pytest.mark.asyncio
async def test_unknown_parent_tool_use_id_not_routed():
    """Messages with parent_tool_use_id that doesn't match a known Task are processed normally."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[TextBlock(text="Unknown parent message")],
            model="claude-sonnet",
            parent_tool_use_id="unknown-id",
        ),
        ResultMessage(
            subtype="result",
            duration_ms=500,
            duration_api_ms=400,
            is_error=False,
            num_turns=1,
            session_id="sess-10",
            total_cost_usd=0.005,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "test")

    # Message should be in main thread
    text_msgs = [m for m in main_thread._messages if m.content and "Unknown parent" in m.content]
    assert len(text_msgs) >= 1


@pytest.mark.asyncio
async def test_subagent_detail_view_removed():
    """_SubagentDetailView should no longer exist in the module."""
    import clauded.discord_renderer as mod
    assert not hasattr(mod, "_SubagentDetailView")


@pytest.mark.asyncio
async def test_agent_tool_treated_same_as_task():
    """The 'Agent' tool name should be handled the same as 'Task'."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="agent-1", name="Agent", input={"description": "Agent subtask"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="agent-1", content="Agent done", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-11",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "agent test")

    # Should have separator embed
    embed_msgs = [m for m in main_thread._messages if m.embeds and "Subtask" in (m.embeds[0].title or "")]
    assert len(embed_msgs) >= 1


@pytest.mark.asyncio
async def test_subagent_tools_render_in_main_thread():
    """Sub-agent tool use blocks should render in the main thread."""
    main_thread = FakeMainThread()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-x", name="Task", input={"description": "Run tools"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Sub-agent uses a tool
        AssistantMessage(
            content=[
                ToolUseBlock(id="bash-1", name="Bash", input={"command": "echo hello"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id="task-x",
        ),
        # Sub-agent tool result
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="bash-1", content="hello", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id="task-x",
        ),
        # Task completes
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-x", content="Done", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=2000,
            duration_api_ms=1800,
            is_error=False,
            num_turns=2,
            session_id="sess-12",
            total_cost_usd=0.02,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "run tools")

    # Tool activity embed should be in main thread
    tool_embeds = [m for m in main_thread._messages if m.embeds and "Tool Activity" in (m.embeds[0].title or "")]
    assert len(tool_embeds) >= 1
    # Bash should appear in the tool log
    tool_desc = tool_embeds[0].embeds[0].description or ""
    assert "Bash" in tool_desc
