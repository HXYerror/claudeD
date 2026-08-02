"""#324: pending-aware background reader + richer continuation rendering.

- `_bg_reader_loop` polls in rounds (a FRESH `receive_pending` iterator each
  round — resuming a cancelled anyio receive is unsafe), re-entering only while
  background work is pending, so a paused /goal or background loop survives
  inter-turn silence while an idle session winds down after one quiet gap.
- `_relay_bg_message` surfaces tool-use + a token footer for continuation turns
  (background follow-ups AND /goal loop turns), not just text.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from clauded.bot import ClaudedBot
from tests.conftest import FakeTarget


# ---------------------------------------------------------------------------
# G5: pending-aware re-open loop
# ---------------------------------------------------------------------------


class _RoundBridge:
    """Bridge stub: `receive_pending` yields one scripted burst per call."""

    def __init__(self, rounds):
        self._rounds = rounds
        self.calls = 0
        self._bg_reader_task = None

    async def receive_pending(self, per_message_timeout=None):
        batch = self._rounds[self.calls] if self.calls < len(self._rounds) else []
        self.calls += 1
        for m in batch:
            yield m


class _RendererStub:
    def __init__(self, pending=True):
        self._task_states = {"t1": object()} if pending else {}

    async def _dispatch_task_event(self, msg, _):
        if msg == "TERMINAL":
            self._task_states.clear()  # simulate the task's terminal
            return True
        return False

    async def _flush_panel_now(self):
        pass


def _bot_stub():
    bot = MagicMock()
    bot._pending_subagents = {}
    bot._relay_bg_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_bg_reader_reenters_while_pending() -> None:
    """While a task is still pending, the reader re-opens for another round."""
    bot = _bot_stub()
    renderer = _RendererStub(pending=True)
    # round 1 delivers text (task still pending) → re-enter; round 2 delivers the
    # terminal that clears _task_states → not pending → stop.
    bridge = _RoundBridge([["hello"], ["TERMINAL"]])

    await ClaudedBot._bg_reader_loop(bot, 123, bridge, FakeTarget(), renderer)

    assert bridge.calls == 2, f"reader must re-enter while pending; rounds={bridge.calls}"
    bot._relay_bg_message.assert_awaited()  # "hello" was relayed


@pytest.mark.asyncio
async def test_bg_reader_exits_when_idle() -> None:
    """No pending work → the reader winds down after a single quiet round."""
    bot = _bot_stub()
    renderer = _RendererStub(pending=False)
    bridge = _RoundBridge([[]])  # one empty quiet gap

    await ClaudedBot._bg_reader_loop(bot, 1, bridge, FakeTarget(), renderer)

    assert bridge.calls == 1, "idle reader must exit after one round, not loop"


class _TaskNotif:
    """Terminal background-task notification. Named for the attribute the reader
    keys on (`type(m).__name__`) via a class alias below — a completed/failed/
    stopped task means the agent is about to auto-continue reacting to it."""

    def __init__(self, status="completed"):
        self.status = status
        self.content = None


# The reader detects terminal task-notifs by class name; expose this stub under
# the exact name production checks for.
_TaskNotif.__name__ = "TaskNotificationMessage"


@pytest.mark.asyncio
async def test_bg_reader_survives_continuation_gap_and_relays_tail() -> None:
    """Incident regression: a completed background task triggers an auto-
    continuation that contains a long quiet gap (a build/rebase). _pending() is
    False the whole time (the workflow's subagents/_task_states already cleared),
    so the OLD `if not _pending(): break` tore the reader down in the first quiet
    round and dropped the continuation's tail. The reader must now stay alive —
    across the empty gap rounds — from the terminal notif through the
    continuation's OWN ResultMessage, relaying BOTH the head and the tail."""
    bot = _bot_stub()
    renderer = _RendererStub(pending=False)  # nothing pending: the incident state

    head = AssistantMessage(content=[TextBlock(text="PART1_head")],
                            model="claude-x", parent_tool_use_id=None)
    tail = AssistantMessage(content=[TextBlock(text="PART2_tail")],
                            model="claude-x", parent_tool_use_id=None)
    done = ResultMessage(subtype="result", duration_ms=10, duration_api_ms=5,
                         is_error=False, num_turns=1, session_id="s",
                         total_cost_usd=0.01,
                         usage={"input_tokens": 1, "output_tokens": 1})
    bridge = _RoundBridge([
        [_TaskNotif("failed"), head],  # terminal notif + continuation head
        [],                            # ── the quiet gap (a build) ──
        [],                            # ── still building ──
        [tail, done],                  # continuation tail + its ResultMessage
    ])

    await ClaudedBot._bg_reader_loop(bot, 7, bridge, FakeTarget(), renderer)

    # Survived the two empty gap rounds (would be calls==1 under the old gate),
    # then exited one round after the continuation's ResultMessage.
    assert bridge.calls == 4, (
        f"reader must survive the continuation gap; rounds={bridge.calls}"
    )
    relayed = [c.args[1] for c in bot._relay_bg_message.await_args_list]
    texts = [
        b.text
        for m in relayed
        for b in (getattr(m, "content", None) or [])
        if type(b).__name__ == "TextBlock"
    ]
    assert "PART1_head" in texts, "head must be relayed"
    assert "PART2_tail" in texts, "TAIL must be relayed (the dropped incident text)"


# ---------------------------------------------------------------------------
# G3: richer continuation relay (tool-use + footer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_bg_message_is_text_only_no_tool_spam() -> None:
    """Regression: the bg relay must NOT post a line per tool-use (that spammed
    the channel with '🔧 Read'/'🔧 Bash' — post-#346 fix). Only text relays."""
    target = FakeTarget()
    msg = AssistantMessage(
        content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"}),
                 ToolUseBlock(id="tu2", name="Read", input={"file_path": "/x"}),
                 TextBlock(text="ran the command")],
        model="claude-x", parent_tool_use_id=None,
    )
    await ClaudedBot._relay_bg_message(MagicMock(), target, msg, renderer=None)

    blob = "\n".join(m.content for m in target._sent if m.content)
    assert "🔧" not in blob, "#324: no per-tool spam lines in the background relay"
    assert "background follow-up" in blob and "ran the command" in blob
    # one message (the text), not one-per-tool
    assert len([m for m in target._sent if m.content]) == 1


@pytest.mark.asyncio
async def test_relay_bg_footer_shows_tokens() -> None:
    target = FakeTarget()
    result = ResultMessage(
        subtype="result", duration_ms=10, duration_api_ms=5, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.01,
        usage={"input_tokens": 1234, "output_tokens": 56},
    )
    await ClaudedBot._relay_bg_footer(MagicMock(), target, result)
    blob = " ".join(m.content for m in target._sent if m.content)
    assert "📥 1234" in blob and "📤 56" in blob
