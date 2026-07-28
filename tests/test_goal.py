"""/goal integration: panel, command registration, goal-aware background reader.

Verifies the pieces we can test without a live CLI: the ◎ panel, the command
group shape, and that an active goal keeps the post-turn background reader warm
across the loop's inter-turn gaps (so goal-loop turns render) and winds it down
once the goal clears.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.bot import ClaudedBot
from clauded.cogs.goal import _goal_panel, goal_group
from tests.conftest import FakeTarget


def test_goal_group_shape() -> None:
    assert goal_group.name == "goal"
    assert {c.name for c in goal_group.commands} == {"set", "status", "clear"}


def test_goal_panel_shows_condition() -> None:
    e = _goal_panel("all tests in test/auth pass and lint is clean")
    assert "Goal active" in (e.title or "")
    assert "all tests in test/auth pass" in (e.description or "")


class _GoalBridge:
    """`receive_pending` yields empty bursts; clears the goal after round 1
    (simulating the CLI's goal loop finishing) so the reader can wind down."""

    def __init__(self, bot, tid):
        self.calls = 0
        self._bg_reader_task = None
        self._bot = bot
        self._tid = tid

    async def receive_pending(self, per_message_timeout=None):
        self.calls += 1
        if self.calls >= 2:
            self._bot._active_goals.pop(self._tid, None)  # goal met → cleared
        for _ in ():  # empty burst
            yield _


class _RendererStub:
    def __init__(self):
        self._task_states = {}

    async def _dispatch_task_event(self, msg, _):
        return False

    async def _flush_panel_now(self):
        pass


@pytest.mark.asyncio
async def test_bg_reader_stays_warm_while_goal_active() -> None:
    """An active goal keeps the reader re-entering even with no Task*/subagent
    pending; it winds down the round after the goal clears."""
    bot = MagicMock()
    bot._pending_subagents = {}
    bot._active_goals = {7: "some measurable condition"}
    bot._relay_bg_message = AsyncMock()

    bridge = _GoalBridge(bot, 7)
    await ClaudedBot._bg_reader_loop(bot, 7, bridge, FakeTarget(), _RendererStub())

    # round 1: goal active → re-enter; round 2: goal cleared → wind down.
    assert bridge.calls == 2, (
        f"active goal must keep the reader warm past round 1; got {bridge.calls}"
    )


@pytest.mark.asyncio
async def test_bg_reader_no_goal_exits_immediately() -> None:
    """Control: with no goal and nothing pending, the reader exits after one round."""
    bot = MagicMock()
    bot._pending_subagents = {}
    bot._active_goals = {}  # no goal
    bot._relay_bg_message = AsyncMock()

    bridge = _GoalBridge(bot, 9)
    await ClaudedBot._bg_reader_loop(bot, 9, bridge, FakeTarget(), _RendererStub())
    assert bridge.calls == 1
