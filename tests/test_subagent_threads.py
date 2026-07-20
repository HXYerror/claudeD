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
from claude_agent_sdk.types import AssistantMessage, StreamEvent, UserMessage


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

    # Per commit 2608502: text arrives via StreamEvent (real SDK behavior
    # with include_partial_messages=True). AssistantMessage TextBlock is
    # skipped as a streaming duplicate. Test must feed a matching
    # text_delta StreamEvent so the renderer's buffer actually receives
    # the text.
    events = [
        StreamEvent(
            uuid="sev-1",
            session_id="sess-7",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello from main agent"},
            },
        ),
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

    # Per commit 2608502: same as test_non_subagent_messages_stay_in_main —
    # text comes via StreamEvent. The unknown parent_tool_use_id on the
    # AssistantMessage doesn't change that path (parent_tool_use_id is a
    # sub-thread routing key on AssistantMessage only, not on StreamEvent;
    # StreamEvent text always lands on the main thread when no sub-thread
    # mapping exists for the active block).
    events = [
        StreamEvent(
            uuid="sev-2",
            session_id="sess-10",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Unknown parent message"},
            },
        ),
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


# ---------------------------------------------------------------------------
# #172 regression pin: sub-agent footer must read tool_use_result from
# UserMessage, NOT block.content. Drives full render_response loop with the
# real SDK shape (UserMessage with tool_use_result dict) and asserts the
# Subtask Complete embed description contains the mini-footer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_footer_renders_from_user_message_tool_use_result():
    """The bug in #160 Fix C was: helper got block.content (list of text
    blocks) so silently returned None, mini-footer never rendered. Tests
    used synthetic JSON strings as helper input — never the real SDK shape.

    This integration test reproduces the actual SDK 0.1.80 event flow:
      1. AssistantMessage opens Task tool_use
      2. UserMessage carries the ToolResultBlock AND has tool_use_result
         attribute populated with the cost/duration/token stats
      3. ResultMessage closes the turn

    Post-fix: the mini-footer must be in the Subtask Complete embed
    description (the cost-aware version of "Reviewed 5 files; LGTM.").
    """
    parent_channel = FakeChannel()
    main_thread = FakeMainThread(parent_channel=parent_channel, name="sub-cost")

    # Track sub-threads created during this test (the production code calls
    # ``anchor.create_thread(...)`` where ``anchor`` is a FakeMessage). The
    # default FakeMessage.create_thread returns a new FakeSubThread but
    # doesn't store a reference. We patch it locally so the test can find
    # the sub-thread embed afterwards.
    created_sub_threads: list = []
    original_create_thread = FakeMessage.create_thread
    async def tracking_create_thread(self, *, name: str, auto_archive_duration: int = 60):
        thread = await original_create_thread(self, name=name, auto_archive_duration=auto_archive_duration)
        created_sub_threads.append(thread)
        return thread
    FakeMessage.create_thread = tracking_create_thread

    real_tool_use_result = {
        "status": "completed",
        "totalDurationMs": 14300,
        "totalTokens": 2050,
        "totalToolUseCount": 5,
        "usage": {"input_tokens": 850, "output_tokens": 1200},
    }

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="task-1", name="Task",
                    input={"description": "review files", "prompt": "review"},
                ),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        # Critical: UserMessage with tool_use_result, NOT AssistantMessage.
        # The pre-#172 code path read block.content which is the list of
        # text blocks (the human report); the real stats live on
        # UserMessage.tool_use_result as a dict.
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="task-1",
                    content=[{"type": "text", "text": "Reviewed 5 files; LGTM."}],
                    is_error=False,
                ),
            ],
            parent_tool_use_id=None,
            tool_use_result=real_tool_use_result,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=15000,
            duration_api_ms=14000,
            is_error=False,
            num_turns=1,
            session_id="sess-172",
            total_cost_usd=0.005,
        ),
    ]

    bridge = FakeBridge(events)
    renderer = DiscordRenderer(main_thread)
    try:
        await renderer.render_response(bridge, "kick off Task")
    finally:
        # Restore original FakeMessage.create_thread
        FakeMessage.create_thread = original_create_thread

    # Collect ALL embed-bearing messages: main thread + parent channel +
    # any sub-thread created during the run.
    all_msgs = list(main_thread._messages)
    all_msgs.extend(parent_channel._messages)
    for thread in created_sub_threads:
        all_msgs.extend(thread._messages)
    subtask_embeds = []
    for m in all_msgs:
        if m.embeds and "Subtask Complete" in (m.embeds[0].title or ""):
            subtask_embeds.append(m.embeds[0])

    assert subtask_embeds, (
        "Expected at least one Subtask Complete embed; got titles: "
        f"{[(m.embeds[0].title if m.embeds else None) for m in all_msgs]}"
    )
    # Look for the mini-footer in the SUB-THREAD embed (per PRD invariant:
    # main-thread summary stays as pure mention link; footer lives only
    # in the sub-thread).
    sub_thread_embed = None
    for thread in created_sub_threads:
        for m in thread._messages:
            if m.embeds and "Subtask Complete" in (m.embeds[0].title or ""):
                sub_thread_embed = m.embeds[0]
                break
        if sub_thread_embed:
            break
    assert sub_thread_embed is not None, (
        "Sub-thread should have its own Subtask Complete embed with the footer"
    )
    desc = sub_thread_embed.description or ""
    # Pre-#172 desc was just the 300-char excerpt: "Reviewed 5 files; LGTM."
    # Post-#172 it includes a mini-footer like "💰 $0.0050 │ 📥 850 │ ⏱️ 14.3s"
    assert "14.3s" in desc, (
        f"Expected ⏱️ 14.3s in mini-footer (from totalDurationMs=14300); "
        f"got desc: {desc!r}"
    )
    # 850 input tokens from usage.input_tokens
    assert "850" in desc
    # 1.2k output tokens (1200 humanized)
    assert "1.2k" in desc


# ---------------------------------------------------------------------------
# review A3/A4/A5/A6 — bot-side subagent-completion notification chain.
#
# These pin the fix that REPLACED the old #310 ``_subagent_threads`` map
# (session_id → thread_id) with per-agent_id ``_pending_subagents``
# (thread_id → {agent_id: agent_type}). The old map (a) always held exactly
# one entry per session — so `_warn_pending_subagents` false-warned on every
# normal turn — and (b) was del'd after the FIRST subagent stopped, wiping
# routing + the count for later parallel subagents.
#
# The exhaustive callback-level coverage lives in
# tests/test_review_subagent_notify.py; here we pin the single most important
# regression invariant (routing/count survives the first stop) inside the
# canonical subagent test file.
# ---------------------------------------------------------------------------


class _NotifyChannel:
    def __init__(self, cid: int) -> None:
        self.id = cid
        self.embeds: list = []

    async def send(self, content=None, **kwargs):
        if kwargs.get("embed") is not None:
            self.embeds.append(kwargs["embed"])
        return FakeMessage(id=len(self.embeds))


class _NotifyBot:
    """Binds the real subagent notification methods off ClaudedBot."""

    def __init__(self) -> None:
        from clauded.bot import ClaudedBot as _CB

        self._pending_subagents: dict[int, dict[str, str]] = {}
        # #319 T1-B: the real subagent stop callback prunes the live roster via
        # self._roster_clear (bot.py:1733). Bind it + its backing dict so this
        # stub matches the production surface (stale-mock fix — the callback
        # gained _roster_clear after this stub was written).
        self._agent_roster: dict[int, dict[str, str]] = {}
        self._chan: dict[int, _NotifyChannel] = {}
        self._make_subagent_start_cb = _CB._make_subagent_start_cb.__get__(self)
        self._make_subagent_stop_cb = _CB._make_subagent_stop_cb.__get__(self)
        self._warn_pending_subagents = _CB._warn_pending_subagents.__get__(self)
        self._roster_note_start = _CB._roster_note_start.__get__(self)
        self._roster_clear = _CB._roster_clear.__get__(self)
        # _read_subagent_result is a @staticmethod — bind it as a plain
        # callable attribute so ``self._read_subagent_result(...)`` works.
        self._read_subagent_result = _CB._read_subagent_result

    def get_channel(self, cid: int):
        return self._chan.setdefault(cid, _NotifyChannel(cid))

    async def fetch_channel(self, cid: int):
        return self.get_channel(cid)


@pytest.mark.asyncio
async def test_subagent_routing_survives_first_stop():
    """review A4/A5 regression: after the FIRST of two parallel subagents
    stops, the second is STILL tracked (old code del'd the whole map) and a
    second completion notification still routes to the same thread."""
    bot = _NotifyBot()
    thread_id = 4242
    ch = bot.get_channel(thread_id)

    start = bot._make_subagent_start_cb(thread_id)
    stop = bot._make_subagent_stop_cb(thread_id)

    await start({"agent_id": "s1", "agent_type": "general-purpose"})
    await start({"agent_id": "s2", "agent_type": "Explore"})
    assert len(bot._pending_subagents[thread_id]) == 2

    await stop({"agent_id": "s1", "agent_type": "general-purpose"})
    # Routing/count for s2 is intact — this is the core of the old bug.
    assert list(bot._pending_subagents[thread_id]) == ["s2"]
    # And a warning at this point reports exactly the 1 remaining subagent.
    await bot._warn_pending_subagents(thread_id)
    warn_embeds = [e for e in ch.embeds if "still running" in (e.description or "")]
    assert len(warn_embeds) == 1
    assert "1 subagent" in (warn_embeds[0].description or "")

    # Second stop drains to empty; its completion embed still reached the thread.
    n_before = len(ch.embeds)
    await stop({"agent_id": "s2", "agent_type": "Explore"})
    assert bot._pending_subagents.get(thread_id, {}) == {}
    assert len(ch.embeds) == n_before + 1  # the s2 completion embed


@pytest.mark.asyncio
async def test_subagent_stop_no_attributeerror_from_removed_fields():
    """review A3: the SubagentStop callback must not touch stop_reason /
    summary / duration_ms (SubagentStopHookInput has none). Feed a payload
    with ONLY the real fields and assert no AttributeError/KeyError and a
    single embed lands."""
    bot = _NotifyBot()
    thread_id = 4343
    ch = bot.get_channel(thread_id)
    stop = bot._make_subagent_stop_cb(thread_id)

    # Real SubagentStopHookInput shape — no stop_reason/summary/duration_ms.
    await stop(
        {
            "hook_event_name": "SubagentStop",
            "stop_hook_active": False,
            "agent_id": "only",
            "agent_type": "code-reviewer",
            "agent_transcript_path": "/nonexistent/path.jsonl",
            "session_id": "sess-x",
        }
    )
    assert len(ch.embeds) == 1
    assert "code-reviewer" in (ch.embeds[0].title or "")

