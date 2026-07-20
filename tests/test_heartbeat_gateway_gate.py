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


def _self(*, down_since, active_turns):
    s = MagicMock()
    s._gw_down_since = down_since
    s._active_turns = active_turns
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
