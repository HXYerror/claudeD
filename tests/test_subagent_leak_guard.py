"""#audit(subagent-leak): subagent-attributed content (parent_tool_use_id set)
that has no registered sub-thread must be dropped from the MAIN channel — this
is how background/parallel subagents (dispatched via the Task* control-plane,
which never emits a Task/Agent ToolUseBlock in the parent stream) leaked their
raw step chatter into the main conversation. Main-agent content (ptid=None)
must still render.
"""
from __future__ import annotations

import pytest

from tests.conftest import FakeBridge, FakeTarget


def _all_text(target) -> str:
    parts = []
    for m in target._sent:
        if getattr(m, "content", None):
            parts.append(m.content)
        for e in getattr(m, "embeds", None) or []:
            parts.append(str(getattr(e, "title", "") or ""))
            parts.append(str(getattr(e, "description", "") or ""))
    return " ".join(parts)


def _result():
    from claude_agent_sdk.types import ResultMessage
    return ResultMessage(
        subtype="result", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.0,
    )


@pytest.mark.asyncio
async def test_subagent_content_without_thread_dropped_from_main():
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    from clauded.discord_renderer import DiscordRenderer

    events = [
        # Background subagent content: parent_tool_use_id set, but NO preceding
        # Task/Agent ToolUseBlock ever created a sub-thread for it.
        AssistantMessage(
            content=[TextBlock(text="SECRET_SUBAGENT_STEP_should_not_leak")],
            model="m", parent_tool_use_id="toolu-bg-agent-1",
        ),
        AssistantMessage(
            content=[ToolUseBlock(id="x1", name="Bash", input={"command": "cat /secret"})],
            model="m", parent_tool_use_id="toolu-bg-agent-1",
        ),
        _result(),
    ]
    target = FakeTarget()
    await DiscordRenderer(target).render_response(FakeBridge(events), "hi")
    text = _all_text(target)
    assert "SECRET_SUBAGENT_STEP" not in text
    assert "cat /secret" not in text


@pytest.mark.asyncio
async def test_main_agent_content_still_renders():
    """Guard must only drop subagent content — main-agent text (ptid=None) stays."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="MAIN_AGENT_VISIBLE_TEXT")],
            model="m", parent_tool_use_id=None,
        ),
        _result(),
    ]
    target = FakeTarget()
    await DiscordRenderer(target).render_response(FakeBridge(events), "hi")
    assert "MAIN_AGENT_VISIBLE_TEXT" in _all_text(target)
