"""Tests for sub-agent thread display in DiscordRenderer.

Validates that when Claude spawns a sub-agent via the Task tool, a separate
Discord thread is created for the sub-agent's full interaction, while the
main thread shows a compact summary with a link.
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


class FakeThread:
    """Minimal discord.Thread stand-in."""
    def __init__(self, name: str = "test-thread"):
        self.name = name
        self.id = 999
        self.mention = f"<#{self.id}>"
        self._messages: list[FakeMessage] = []

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 100, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        self._messages.append(msg)
        return msg


class FakeChannel:
    """Minimal discord channel that can create threads."""
    def __init__(self):
        self._messages: list[FakeMessage] = []
        self._threads: list[FakeThread] = []
        self.parent = None  # not a thread itself
        self.guild = None

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(id=len(self._messages) + 1, content=content or "")
        if "embed" in kwargs:
            msg.embeds = [kwargs["embed"]]
        # Attach create_thread to the message
        thread = FakeThread(name="sub-thread")
        self._threads.append(thread)

        async def _create_thread(name="thread", auto_archive_duration=60):
            thread.name = name
            return thread

        msg.create_thread = _create_thread
        self._messages.append(msg)
        return msg


class FakeMainThread:
    """Minimal discord thread (the main conversation thread) with a parent channel."""
    def __init__(self, parent_channel: FakeChannel):
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
# Sub-agent thread creation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_creates_subagent_thread():
    """A Task ToolUseBlock should create a sub-thread and show summary in main."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    events = [
        # Main agent sends a Task tool use
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-1", name="Task", input={"description": "Fix the bug", "prompt": "Please fix"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Task result
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

    # A thread should have been created in parent channel
    assert len(parent_channel._threads) == 1
    sub_thread = parent_channel._threads[0]
    assert "Subtask" in sub_thread.name

    # Main thread should have the compact summary with thread mention
    summary_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(summary_msgs) >= 1
    # First embed should be the summary with thread link
    first_embed = summary_msgs[0].embeds[0]
    assert "Subtask" in first_embed.title

    # Sub-thread should have completion embed
    sub_msgs = sub_thread._messages
    assert len(sub_msgs) >= 1
    # Check for completion embed
    done_embeds = [m for m in sub_msgs if m.embeds and "Complete" in (m.embeds[0].title or "")]
    assert len(done_embeds) == 1


@pytest.mark.asyncio
async def test_subagent_messages_routed_to_thread():
    """Messages with parent_tool_use_id should go to the sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    events = [
        # Main agent creates a task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-2", name="Task", input={"description": "Research topic"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Sub-agent sends text (has parent_tool_use_id)
        AssistantMessage(
            content=[
                TextBlock(text="I'm researching the topic now..."),
            ],
            model="claude-sonnet",
            parent_tool_use_id="task-2",
        ),
        # Sub-agent uses a tool
        AssistantMessage(
            content=[
                ToolUseBlock(id="bash-1", name="Bash", input={"command": "grep -r topic ."}),
            ],
            model="claude-sonnet",
            parent_tool_use_id="task-2",
        ),
        # Sub-agent tool result
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="bash-1", content="found results", is_error=False),
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

    sub_thread = parent_channel._threads[0]

    # Sub-thread should have: prompt (optional), text, tool embed, tool result, completion
    # Text message should be in sub-thread
    text_msgs = [m for m in sub_thread._messages if m.content and "researching" in m.content]
    assert len(text_msgs) >= 1

    # Tool embed should be in sub-thread
    tool_embeds = [m for m in sub_thread._messages if m.embeds and "Bash" in (m.embeds[0].title or "")]
    assert len(tool_embeds) >= 1

    # Main thread should NOT have the sub-agent's text
    main_text = [m for m in main_thread._messages if m.content and "researching" in m.content]
    assert len(main_text) == 0


@pytest.mark.asyncio
async def test_task_failure_updates_both_threads():
    """When a Task fails, both main and sub-thread should reflect the failure."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

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

    sub_thread = parent_channel._threads[0]

    # Sub-thread should have failure embed
    fail_embeds = [m for m in sub_thread._messages if m.embeds and "Failed" in (m.embeds[0].title or "")]
    assert len(fail_embeds) == 1

    # Main thread summary should be updated to show failure
    summary_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(summary_msgs) >= 1
    # The summary embed should be updated (via edit) to show failure
    last_embed = summary_msgs[0].embeds[0]
    # After edit, it should show "Failed" or the thread mention
    assert "Failed" in (last_embed.title or "") or sub_thread.mention in (last_embed.description or "")


@pytest.mark.asyncio
async def test_thread_creation_failure_falls_back_inline():
    """If thread creation fails, fall back to inline subtask display."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    # Make parent channel's send raise HTTPException
    original_send = parent_channel.send

    async def _failing_send(*args, **kwargs):
        raise discord.HTTPException(MagicMock(), "Thread creation failed")

    parent_channel.send = _failing_send

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-4", name="Task", input={"description": "Some task", "prompt": "do it"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-4", content="Done", is_error=False),
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
            session_id="sess-4",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "do something")

    # No threads should have been created
    assert len(parent_channel._threads) == 0

    # Main thread should have inline subtask display
    embed_msgs = [m for m in main_thread._messages if m.embeds]
    assert len(embed_msgs) >= 1
    # Should show inline subtask embed with depth indicator
    first_embed = embed_msgs[0].embeds[0]
    assert "Subtask" in first_embed.title


@pytest.mark.asyncio
async def test_stream_event_routed_to_subagent():
    """StreamEvents with parent_tool_use_id go to sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    events = [
        # Create task
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-5", name="Task", input={"description": "Stream test"}),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # StreamEvent from sub-agent
        StreamEvent(
            uuid="se-1",
            session_id="sess-5",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "streaming text"}},
            parent_tool_use_id="task-5",
        ),
        # Task completes
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="task-5", content="Done", is_error=False),
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
            session_id="sess-5",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "test")

    sub_thread = parent_channel._threads[0]

    # The streaming text should appear in the sub-thread
    text_msgs = [m for m in sub_thread._messages if m.content and "streaming" in m.content]
    assert len(text_msgs) >= 1

    # Main thread should NOT have the streaming text
    main_text = [m for m in main_thread._messages if m.content and "streaming" in m.content]
    assert len(main_text) == 0


@pytest.mark.asyncio
async def test_multiple_subagents_get_separate_threads():
    """Multiple Task tools create separate sub-threads."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

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
        # Sub-agent A sends text
        AssistantMessage(
            content=[TextBlock(text="Alpha working")],
            model="claude-sonnet",
            parent_tool_use_id="task-a",
        ),
        # Sub-agent B sends text
        AssistantMessage(
            content=[TextBlock(text="Beta working")],
            model="claude-sonnet",
            parent_tool_use_id="task-b",
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

    # Two sub-threads should be created
    assert len(parent_channel._threads) == 2

    # Each sub-thread should have its own text
    thread_a = parent_channel._threads[0]
    thread_b = parent_channel._threads[1]

    a_text = [m for m in thread_a._messages if m.content and "Alpha" in m.content]
    b_text = [m for m in thread_b._messages if m.content and "Beta" in m.content]
    assert len(a_text) >= 1
    assert len(b_text) >= 1


@pytest.mark.asyncio
async def test_non_subagent_messages_stay_in_main():
    """Messages without parent_tool_use_id go to the main thread as usual."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

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

    # No sub-threads created
    assert len(parent_channel._threads) == 0

    # Main thread has the text
    text_msgs = [m for m in main_thread._messages if m.content and "Hello from main" in m.content]
    assert len(text_msgs) >= 1


@pytest.mark.asyncio
async def test_subagent_thinking_routed_to_thread():
    """ThinkingBlock in sub-agent messages goes to sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    from clauded.claude_bridge import ThinkingBlock

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

    sub_thread = parent_channel._threads[0]

    # Thinking embed should be in sub-thread
    thinking_embeds = [m for m in sub_thread._messages if m.embeds and "Thinking" in (m.embeds[0].title or "")]
    assert len(thinking_embeds) >= 1

    # Main thread should NOT have the thinking embed (except the summary)
    main_thinking = [m for m in main_thread._messages if m.embeds and "Thinking" in (m.embeds[0].title or "")]
    assert len(main_thinking) == 0


@pytest.mark.asyncio
async def test_task_prompt_posted_in_subthread():
    """When a Task has a prompt, it should be posted in the sub-thread."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="task-7", name="Task", input={
                    "description": "Write tests",
                    "prompt": "Write unit tests for the auth module",
                }),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="task-7", content="Tests written", is_error=False)],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-9",
            total_cost_usd=0.01,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    await renderer.render_response(bridge, "write tests")

    sub_thread = parent_channel._threads[0]

    # Sub-thread should have the prompt embed
    prompt_embeds = [m for m in sub_thread._messages if m.embeds and "Prompt" in (m.embeds[0].title or "")]
    assert len(prompt_embeds) >= 1


@pytest.mark.asyncio
async def test_unknown_parent_tool_use_id_not_routed():
    """Messages with parent_tool_use_id that doesn't match a known Task are not routed."""
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel)

    events = [
        # Message with unknown parent_tool_use_id — should be processed normally in main
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

    # No sub-threads created
    assert len(parent_channel._threads) == 0

    # Message should be in main thread
    text_msgs = [m for m in main_thread._messages if m.content and "Unknown parent" in m.content]
    assert len(text_msgs) >= 1
