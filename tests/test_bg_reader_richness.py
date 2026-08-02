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
# #max-life-inactivity: the backstop is an INACTIVITY timeout, not a hard cap.
# A workflow that runs LONGER than max_life but is still making progress
# (messages arriving, or its pending set advancing) must keep its Discord relay
# alive; only a genuinely wedged reader is torn down.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Stand-in for `clauded.bot.time`: `monotonic()` returns a controllable
    value (advanced per round by _ClockBridge); every other attribute delegates
    to the real `time` module so nothing else in bot.py breaks. Rebinding the
    name in bot's namespace leaves asyncio's own `time` reference untouched."""

    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def __getattr__(self, name):
        import time as _rt

        return getattr(_rt, name)


class _ClockBridge:
    """Like _RoundBridge, but advances a shared clock by `step` at the START of
    each round and runs an optional per-round side effect (e.g. subagent churn),
    so a workflow whose wall-clock crosses max_life can be simulated instantly."""

    def __init__(self, rounds, clock, step, on_round=None):
        self._rounds = rounds
        self._clock = clock
        self._step = step
        self._on_round = on_round
        self.calls = 0
        self._bg_reader_task = None

    async def receive_pending(self, per_message_timeout=None):
        idx = self.calls
        self.calls += 1
        if self.calls > 50:  # a broken backstop would loop forever — fail loud
            raise RuntimeError("bg reader ignored the inactivity backstop (looped)")
        self._clock.t += self._step
        if self._on_round is not None:
            self._on_round(idx)
        batch = self._rounds[idx] if idx < len(self._rounds) else []
        for m in batch:
            yield m


def _cont_msgs():
    head = AssistantMessage(content=[TextBlock(text="PART1_head")],
                            model="claude-x", parent_tool_use_id=None)
    mid = AssistantMessage(content=[TextBlock(text="PART_mid")],
                           model="claude-x", parent_tool_use_id=None)
    tail = AssistantMessage(content=[TextBlock(text="PART2_tail")],
                            model="claude-x", parent_tool_use_id=None)
    done = ResultMessage(subtype="result", duration_ms=10, duration_api_ms=5,
                         is_error=False, num_turns=1, session_id="s",
                         total_cost_usd=0.01,
                         usage={"input_tokens": 1, "output_tokens": 1})
    return head, mid, tail, done


def _relayed_texts(bot):
    relayed = [c.args[1] for c in bot._relay_bg_message.await_args_list]
    return [
        b.text
        for m in relayed
        for b in (getattr(m, "content", None) or [])
        if type(b).__name__ == "TextBlock"
    ]


@pytest.mark.asyncio
async def test_bg_reader_not_capped_while_messages_flow(monkeypatch) -> None:
    """Incident regression: a workflow whose wall-clock EXCEEDS max_life but that
    keeps delivering messages must NOT be torn down mid-flight. The old HARD cap
    (`time.monotonic() > start + max_life`) fired 12 min before an 81-min
    workflow finished and dropped its report; the inactivity backstop resets on
    every relayed message, so the tail still lands."""
    monkeypatch.setenv("CLAUDED_BG_MAX_LIFETIME", "100")
    clock = _FakeClock()
    monkeypatch.setattr("clauded.bot.time", clock)

    bot = _bot_stub()
    renderer = _RendererStub(pending=False)  # incident state: nothing pending
    head, mid, tail, done = _cont_msgs()
    # step 500 >> max_life 100: under the OLD hard cap the reader would break
    # after round 0 (clock 1500 > deadline 1100) and drop everything after.
    bridge = _ClockBridge([[head], [mid], [tail, done]], clock, step=500)

    await ClaudedBot._bg_reader_loop(bot, 7, bridge, FakeTarget(), renderer)

    assert clock.t - 1000.0 > 100, "sanity: simulated wall-clock must exceed max_life"
    texts = _relayed_texts(bot)
    assert "PART2_tail" in texts, "tail must survive a workflow longer than max_life"
    assert bridge.calls == 3


@pytest.mark.asyncio
async def test_bg_reader_not_capped_on_quiet_subagent_churn(monkeypatch) -> None:
    """Even with NO relayable messages during the workflow's long middle, quiet
    subagent lifecycle churn (the pending set advancing round-to-round) keeps the
    inactivity backstop from firing — the exact signal that would have saved the
    incident, where subagents started/stopped every ~29 min under a 60-min cap."""
    monkeypatch.setenv("CLAUDED_BG_MAX_LIFETIME", "100")
    clock = _FakeClock()
    monkeypatch.setattr("clauded.bot.time", clock)

    bot = _bot_stub()
    bot._pending_subagents = {7: {"a0": "sub"}}
    renderer = _RendererStub(pending=False)
    head, mid, tail, done = _cont_msgs()

    # gap rounds carry no messages but churn the pending set each round; step 40,
    # so by round 2 the clock (1120) has crossed the OLD hard cap (1100).
    def churn(idx):
        if idx == 1:
            bot._pending_subagents[7] = {"a1": "sub"}
        elif idx == 2:
            bot._pending_subagents[7] = {"a2": "sub"}
        elif idx >= 3:
            bot._pending_subagents.pop(7, None)  # workflow done → pending clears

    bridge = _ClockBridge(
        [[head], [], [], [tail, done]], clock, step=40, on_round=churn
    )

    await ClaudedBot._bg_reader_loop(bot, 7, bridge, FakeTarget(), renderer)

    texts = _relayed_texts(bot)
    assert "PART2_tail" in texts, "quiet-but-progressing workflow must keep its relay"
    assert bridge.calls == 4


@pytest.mark.asyncio
async def test_bg_reader_backstop_fires_on_wedged_counter(monkeypatch) -> None:
    """The backstop must still bound a WEDGED reader: pending stuck True with no
    messages and no churn (a leaked subagent counter) is zero progress, so after
    max_life of true inactivity the reader exits instead of looping forever."""
    monkeypatch.setenv("CLAUDED_BG_MAX_LIFETIME", "100")
    clock = _FakeClock()
    monkeypatch.setattr("clauded.bot.time", clock)

    bot = _bot_stub()
    bot._pending_subagents = {7: {"leaked": "sub"}}  # stuck True, never changes
    renderer = _RendererStub(pending=False)
    bridge = _ClockBridge([[], [], [], []], clock, step=60)  # all quiet, no churn

    await ClaudedBot._bg_reader_loop(bot, 7, bridge, FakeTarget(), renderer)

    # round 0: clock 1060, delta 60 ≤ 100 → continue; round 1: clock 1120, delta
    # 120 > 100 → break. The wedged reader is bounded, not infinite.
    assert bridge.calls == 2, f"backstop must fire ~max_life; rounds={bridge.calls}"


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
