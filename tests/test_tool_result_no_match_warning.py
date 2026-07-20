"""#audit(live-log): the "ToolResultBlock no-matched rolling-log line" warning
(274 log-spam lines) must be suppressed for tools rendered as their OWN
standalone embed (tracked in tool_msgs) — those never append a rolling-log line
by design — while STILL firing for a genuine eviction / out-of-order no-match.
"""
from __future__ import annotations

import pytest

from tests.conftest import FakeBridge, FakeTarget

_LOGGER = "clauded.discord_renderer"


def _result(session="s"):
    from claude_agent_sdk.types import ResultMessage
    return ResultMessage(
        subtype="result", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session, total_cost_usd=0.0,
    )


@pytest.mark.asyncio
async def test_standalone_tool_result_no_warning(caplog):
    """A standalone-rendered tool (TodoWrite → tool_msgs) whose result has no
    rolling-log line must NOT log the no-match warning (was ~264/274 of them)."""
    from claude_agent_sdk.types import AssistantMessage, ToolUseBlock, ToolResultBlock
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[ToolUseBlock(
                id="todo-1", name="TodoWrite",
                input={"todos": [{"content": "do x", "status": "pending"}]},
            )],
            model="m", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="todo-1", content="ok", is_error=False)],
            model="m", parent_tool_use_id=None,
        ),
        _result(),
    ]
    renderer = DiscordRenderer(FakeTarget())
    with caplog.at_level("WARNING", logger=_LOGGER):
        await renderer.render_response(FakeBridge(events), "todo")
    assert "no-matched rolling-log line" not in caplog.text


# Note: the complementary "a GENUINE eviction still warns" property is
# preserved by construction — the fix only ADDS `and tool_id not in tool_msgs`
# to the warning guard, so it can never suppress a warning for a rolling-log
# tool (Bash/Write/… which are never in tool_msgs). Reproducing a real eviction
# needs >15 tool lines, which isn't worth the test weight here.
