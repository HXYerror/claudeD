"""Injected UserMessage text must NOT render as assistant output.

Live-verified via /goal against a real test bot: the CLI feeds the goal
evaluator's "continue" prompt back as a UserMessage TextBlock ("Stop hook
feedback: …"), which was leaking into the Discord reply and mashing multi-turn
goal replies together. Only AssistantMessage text belongs in the buffer; a
UserMessage turn boundary inserts a separator instead.
"""
from __future__ import annotations

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, UserMessage

from clauded.discord_renderer import DiscordRenderer
from tests.conftest import FakeBridge, FakeTarget


def _asst(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=t)], model="m", parent_tool_use_id=None)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="result", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=2, session_id="s", total_cost_usd=0.0, usage={},
    )


@pytest.mark.asyncio
async def test_injected_usermessage_text_not_rendered() -> None:
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    events = [
        _asst("STEP-ONE"),
        # the goal evaluator's continue-injection, as the CLI delivers it
        UserMessage(content=[TextBlock(
            text="Stop hook feedback:\n[cond]: not met yet, send STEP-TWO next"
        )]),
        _asst("STEP-TWO"),
        _result(),
    ]
    await renderer.render_response(FakeBridge(events), "/goal ...")

    blob = "\n".join(m.content for m in target._sent if m.content)
    assert "STEP-ONE" in blob and "STEP-TWO" in blob, "both goal turns must render"
    assert "Stop hook feedback" not in blob, (
        "injected UserMessage text (goal-evaluator feedback) must not leak into the reply"
    )
    assert "STEP-ONESTEP-TWO" not in blob, "goal turns must be separated, not concatenated"
