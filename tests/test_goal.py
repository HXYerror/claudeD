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


@pytest.mark.asyncio
async def test_goal_set_marks_during_and_clears_after(monkeypatch) -> None:
    """`/goal set` marks the goal active for the DURATION of the (synchronous)
    loop turn and clears it once the loop resolves — verified behavior: the whole
    /goal loop completes inside the one render turn."""
    import asyncio

    from clauded.cogs import goal as goalmod

    bot = MagicMock()
    bot._active_goals = {}
    bridge = MagicMock()
    bridge.is_active = True
    bot.session_manager.get_session.return_value = bridge
    bot.project_manager.get_path.return_value = "/tmp/proj"
    bot.session_manager.get_lock.return_value = asyncio.Lock()

    captured: dict = {}

    async def _render(**kw):
        captured.update(kw)
        captured["marker_during"] = dict(bot._active_goals)  # snapshot mid-turn

    bot._render_with_retry = AsyncMock(side_effect=_render)

    monkeypatch.setattr(goalmod, "_resolve", lambda inter: (bot, 111, 111))

    async def _no_reject(inter, b):
        return False

    monkeypatch.setattr(goalmod, "reject_if_unbound", _no_reject)
    monkeypatch.setattr(goalmod, "DiscordRenderer", lambda *a, **k: MagicMock())

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.id = 5
    interaction.channel = MagicMock()

    await goalmod.goal_set.callback(interaction, "all tests in test/auth pass")

    assert captured.get("user_text") == "/goal all tests in test/auth pass"
    assert captured.get("marker_during") == {111: "all tests in test/auth pass"}, (
        "goal must be marked active DURING the turn (for concurrent /goal status)"
    )
    assert bot._active_goals == {}, "marker must be cleared once the loop resolves"

