"""#audit(#9): the heartbeat freezes (stops refreshing mtime) once the gateway
has been continuously down past the reconnect budget with no turn in flight, so
the external watchdog restarts a reconnect-storm-wedged bot — while brief blips,
the pre-login window, and active turns keep refreshing (no false kills).

Drives the loop body directly via ClaudedBot._heartbeat_task.coro(mock_self) and
patches _write_heartbeat_state, so nothing touches the real heartbeat file or a
live gateway.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from clauded.bot import ClaudedBot


def _self(*, down_since, active_turns, inflight_bg=0):
    s = MagicMock()
    s._gw_down_since = down_since
    s._active_turns = active_turns
    s._inflight_bg = inflight_bg
    s._gw_disconnects = 1
    s._gw_resumes = 0
    return s


@pytest.mark.asyncio
async def test_heartbeat_freezes_past_budget_no_active_turn(monkeypatch):
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    s = _self(down_since=time.time() - 601, active_turns=0)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_not_called()  # frozen → mtime ages → watchdog restarts


@pytest.mark.asyncio
async def test_heartbeat_writes_within_budget(monkeypatch):
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    s = _self(down_since=time.time() - 100, active_turns=0)  # brief blip
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once()  # within budget → refresh normally


@pytest.mark.asyncio
async def test_heartbeat_writes_past_budget_when_active_turn(monkeypatch):
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    s = _self(down_since=time.time() - 601, active_turns=1)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once_with(1)  # active turn keeps writing (T2-D safety)


@pytest.mark.asyncio
async def test_heartbeat_writes_pre_login_down_since_none():
    s = _self(down_since=None, active_turns=0)  # pre-login / healthy
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once()  # gate open → always refresh


@pytest.mark.asyncio
async def test_budget_env_override_respected(monkeypatch):
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "60")
    s = _self(down_since=time.time() - 90, active_turns=0)  # >60s budget
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_not_called()  # env-lowered budget → frozen


@pytest.mark.asyncio
async def test_heartbeat_writes_past_budget_when_inflight_bg(monkeypatch):
    """#audit(#9): a background subagent in flight defers the wedge-restart the
    same way a foreground turn does — keep writing (with content = the count)."""
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    s = _self(down_since=time.time() - 601, active_turns=0, inflight_bg=1)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once_with(1)  # bg work keeps the heartbeat alive


@pytest.mark.asyncio
async def test_heartbeat_content_sums_turns_and_bg(monkeypatch):
    """Heartbeat content is _active_turns + _inflight_bg so the external
    watchdog's longer active grace applies to background work too."""
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    s = _self(down_since=time.time() - 601, active_turns=1, inflight_bg=2)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_hard_ceiling_freezes_even_with_inflight_bg(monkeypatch):
    """The fail-safe: past the hard ceiling we freeze regardless of in-flight
    background work, so a dropped SubagentStop / truly dead gateway can't defer
    recovery forever."""
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    monkeypatch.setenv("CLAUDED_GATEWAY_HARD_CEILING_SECS", "1000")
    s = _self(down_since=time.time() - 1001, active_turns=0, inflight_bg=1)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_not_called()  # past ceiling → frozen despite bg work


@pytest.mark.asyncio
async def test_hard_ceiling_env_override_respected(monkeypatch):
    """Below the (default 1800s) ceiling, in-flight bg still defers; a lowered
    ceiling brings the fail-safe forward."""
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    monkeypatch.setenv("CLAUDED_GATEWAY_HARD_CEILING_SECS", "700")
    # 650s: past budget (600) but under ceiling (700) → bg work still defers.
    s = _self(down_since=time.time() - 650, active_turns=0, inflight_bg=1)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_hard_ceiling_clamped_to_budget(monkeypatch):
    """A mis-set ceiling below the budget must NOT fire before the budget — it
    is clamped up to the budget, so within-budget blips still refresh."""
    monkeypatch.setenv("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    monkeypatch.setenv("CLAUDED_GATEWAY_HARD_CEILING_SECS", "60")  # < budget
    s = _self(down_since=time.time() - 100, active_turns=0, inflight_bg=0)
    with patch("clauded.bot._write_heartbeat_state") as w:
        await ClaudedBot._heartbeat_task.coro(s)
        w.assert_called_once()  # within budget → refresh (ceiling clamp holds)
