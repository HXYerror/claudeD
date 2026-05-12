"""Tests for sub-agent thread display in DiscordRenderer.

Validates that when Claude spawns a sub-agent via the Task tool, a separate
thread is created in the parent channel with a name that includes the main
thread name. Sub-agent content routes to the sub-thread, and a compact
summary embed is posted in the main thread.
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
from claude_agent_sdk.types import AssistantMessage, StreamEvent


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

    async def create_thread(self, *, name: str, auto_archive_duration: int = 60):
        """Create a FakeSubThread from this message (anchor message)."""
        thread = FakeSubThread(name=name)
        return thread


class FakeSubThread:
    """A thread created for a sub-agent."""
    def __init__(self, name: str = "sub-thread"):
        self.name = name
        self._messages: list[FakeMessage] = []
        self.parent = None
        self.guild = None
        self.mention = f"<#fake-{name}>"

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 500, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._messages.append(msg)
        return msg


class FakeChannel:
    """Minimal discord channel (parent of threads)."""
    def __init__(self):
        self._messages: list[FakeMessage] = []
        self.parent = None
        self.guild = None
        self._created_threads: list[FakeSubThread] = []

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 1, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._messages.append(msg)
        return msg


class FakeMainThread:
    """Minimal discord thread (the main conversation thread)."""
    def __init__(self, parent_channel: FakeChannel | None = None, name: str = "session"):
        self.parent = parent_channel
        self.name = name
        self._messages: list[FakeMessage] = []
        self.guild = None
        self.mention = f"<#main-{name}>"

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
# Sub-agent thread creation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_creates_sub_thread():
    """A Task ToolUseBlock should create a sub-thread in the parent channel."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="hello")

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

    # An anchor message should have been sent to the parent channel
    assert len(parent_channel._messages) >= 1
    anchor = parent_channel._messages[0]
    assert anchor.embeds
    # Thread name should include the main thread name
    anchor_title = anchor.embeds[0].title
    assert "[hello]" in anchor_title
    assert "Fix the bug" in anchor_title

    # Main thread should have a summary embed with Subtask title
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    first_embed = embed_msgs[0].embeds[0]
    assert "Subtask" in first_embed.title


@pytest.mark.asyncio
async def test_task_completion_updates_main_thread():
    """When a Task completes, the main thread summary embed should be updated."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="hello")

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

    # The main thread summary embed should have been edited to show completion
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    final_embed = embed_msgs[0].embeds[0]
    assert "Complete" in (final_embed.title or "")


@pytest.mark.asyncio
async def test_subagent_messages_route_to_sub_thread():
    """Messages with parent_tool_use_id should route to the sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="research-session")

    events = [
        # Main agent creates a task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-2", name="Task", input={"description": "Research topic"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Sub-agent sends text (has parent_tool_use_id) — should go to sub-thread
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

    # Text should NOT be in main thread (only embeds there)
    text_msgs = [m for m in main_thread._messages if m.content and "researching" in m.content]
    assert len(text_msgs) == 0

    # The anchor message was sent to parent channel — thread was created
    assert len(parent_channel._messages) >= 1


@pytest.mark.asyncio
async def test_task_failure_shows_in_main_thread():
    """When a Task fails, the main thread summary should reflect the failure."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="deploy-session")

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

    # Main thread should have the failure embed
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    last_embed = embed_msgs[0].embeds[0]
    assert "❌" in (last_embed.title or "") or "Failed" in (last_embed.title or "")


@pytest.mark.asyncio
async def test_multiple_subtasks_get_separate_threads():
    """Multiple Task tools create separate sub-threads."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="multi")

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

    # Two anchor messages in parent channel (one per subtask)
    assert len(parent_channel._messages) >= 2
    # Both anchor embeds should contain [multi] in the title
    for msg in parent_channel._messages[:2]:
        assert "[multi]" in msg.embeds[0].title

    # Two summary embeds in main thread (edited to "Complete")
    subtask_embeds = [
        m for m in main_thread._messages
        if m.embeds and (
            "Subtask" in (m.embeds[0].title or "")
            or "Complete" in (m.embeds[0].title or "")
        )
    ]
    assert len(subtask_embeds) >= 2


@pytest.mark.asyncio
async def test_non_subagent_messages_stay_in_main():
    """Messages without parent_tool_use_id go to the main thread as usual."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="main")

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
async def test_subagent_thinking_renders_in_sub_thread():
    """ThinkingBlock in sub-agent messages renders in the sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="think-session")

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

    # Thinking embed should NOT be in main thread (it went to sub-thread)
    thinking_in_main = [m for m in main_thread._messages if m.embeds and "Thinking" in (m.embeds[0].title or "")]
    assert len(thinking_in_main) == 0


@pytest.mark.asyncio
async def test_unknown_parent_tool_use_id_not_routed():
    """Messages with parent_tool_use_id that doesn't match a known Task are processed normally."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="main")

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

    # Message should be in main thread (unknown ptid falls through)
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
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="agent-test")

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

    # Should have anchor message in parent channel with [agent-test] in name
    assert len(parent_channel._messages) >= 1
    anchor_title = parent_channel._messages[0].embeds[0].title
    assert "[agent-test]" in anchor_title


@pytest.mark.asyncio
async def test_subagent_tools_render_in_sub_thread():
    """Sub-agent tool use blocks should render in the sub-thread, not main thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="tools-session")

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

    # Tool activity embed should NOT be in main thread (it went to sub-thread)
    tool_embeds_in_main = [m for m in main_thread._messages if m.embeds and "Tool Activity" in (m.embeds[0].title or "")]
    assert len(tool_embeds_in_main) == 0


@pytest.mark.asyncio
async def test_thread_name_includes_main_thread_name():
    """Sub-thread name should include the main thread name as a prefix."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="PM comparison")

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-t", name="Task", input={"description": "Analyze feature sets"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-t", content="Analysis done", is_error=False),
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
            session_id="sess-t",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "compare PMs")

    # Anchor embed title should include [PM comparison]
    assert len(parent_channel._messages) >= 1
    anchor_title = parent_channel._messages[0].embeds[0].title
    assert "[PM comparison]" in anchor_title
    assert "Analyze feature sets" in anchor_title


@pytest.mark.asyncio
async def test_fallback_inline_when_thread_creation_fails():
    """If thread creation raises HTTPException, fallback to inline separator."""

    class FailChannel:
        """Channel where send raises HTTPException."""
        parent = None
        guild = None
        _messages = []

        async def send(self, *args, **kwargs):
            raise discord.HTTPException(MagicMock(), "Cannot create thread")

    class FakeMainThreadWithFailParent:
        def __init__(self):
            self.parent = FailChannel()
            self.name = "test-session"
            self._messages: list[FakeMessage] = []
            self.guild = None
            self.mention = "<#test>"

        async def send(self, content=None, **kwargs):
            msg = FakeMessage(id=len(self._messages) + 200, content=content or "")
            if "embed" in kwargs:
                msg.embeds = [kwargs["embed"]]
            self._messages.append(msg)
            return msg

    main_thread = FakeMainThreadWithFailParent()

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-f", name="Task", input={"description": "Will fail"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-f", content="Done anyway", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=500,
            duration_api_ms=400,
            is_error=False,
            num_turns=1,
            session_id="sess-f",
            total_cost_usd=0.005,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "fallback test")

    # Should have inline fallback separator embed
    embed_msgs = [m for m in main_thread._messages if m.embeds and "Subtask" in (m.embeds[0].title or "")]
    assert len(embed_msgs) >= 1
