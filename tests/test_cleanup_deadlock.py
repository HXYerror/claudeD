"""Tests for #146 cleanup deadlock — `bridge.stop()` bounded timeout and
concurrent `_cleanup_task`.

Per PRD §Tests:
1. Stuck `client.disconnect()` is force-dropped after `CLAUDED_BRIDGE_STOP_TIMEOUT`.
2. One stuck bridge + two healthy bridges all GC'd in the same `_cleanup_task` tick.
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


@pytest.mark.asyncio
async def test_stuck_disconnect_force_drops_after_timeout(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `disconnect()` that never returns must be force-dropped after the env timeout."""
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    # Simulate a started bridge with a stuck client
    stuck_client = MagicMock()

    async def _never_returns() -> None:
        await asyncio.sleep(999)

    stuck_client.disconnect = AsyncMock(side_effect=_never_returns)
    bridge._client = stuck_client
    bridge._active = True

    monkeypatch.setenv("CLAUDED_BRIDGE_STOP_TIMEOUT", "0.5")

    # stop() must return within (timeout + small margin), not hang forever.
    await asyncio.wait_for(bridge.stop(), timeout=2.0)

    assert bridge._client is None
    assert bridge._active is False


@pytest.mark.asyncio
async def test_cleanup_concurrent_one_stuck_two_healthy(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stuck bridge must not block concurrent cleanup of two healthy peers."""
    from clauded.bot import ClaudedBot

    monkeypatch.setenv("CLAUDED_BRIDGE_STOP_TIMEOUT", "0.3")
    monkeypatch.setenv("CLAUDED_SESSION_TIMEOUT", "1")  # immediate eligibility

    # Build a bot without going through full init: we only exercise `_cleanup_task`.
    bot = ClaudedBot.__new__(ClaudedBot)
    bot.session_manager = MagicMock()

    sessions: dict[int, MagicMock] = {}
    long_ago = time.time() - 3600

    # Two healthy bridges + one stuck
    for tid in (101, 102, 103):
        b = MagicMock()
        b._last_activity = long_ago
        b._start_time = long_ago
        sessions[tid] = b

    async def _never_returns() -> None:
        await asyncio.sleep(999)

    sessions[102].stop = AsyncMock(side_effect=_never_returns)
    sessions[101].stop = AsyncMock()
    sessions[103].stop = AsyncMock()

    bot.session_manager.list_sessions = MagicMock(return_value=sessions)
    bot.session_manager.save_session_state = MagicMock()

    stopped: list[int] = []

    async def _stop_session(tid: int) -> None:
        # Mimic real stop_session: pop + delegate to bridge.stop() with timeout
        bridge = sessions.get(tid)
        if bridge is None:
            return
        try:
            await asyncio.wait_for(
                bridge.stop(),
                timeout=float(os.environ.get("CLAUDED_BRIDGE_STOP_TIMEOUT", "30")),
            )
        except asyncio.TimeoutError:
            pass
        sessions.pop(tid, None)
        stopped.append(tid)

    bot.session_manager.stop_session = AsyncMock(side_effect=_stop_session)

    # Invoke the cleanup body. tasks.loop wraps it; call the underlying coroutine.
    cleanup = ClaudedBot._cleanup_task
    coro_fn = getattr(cleanup, "coro", None) or getattr(cleanup, "callback", None) or cleanup
    await asyncio.wait_for(coro_fn(bot), timeout=5.0)

    # All three should have been processed; sessions dict drained.
    assert sorted(stopped) == [101, 102, 103]
    assert sessions == {}
