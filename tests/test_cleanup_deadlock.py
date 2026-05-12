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
    """One stuck bridge must not block concurrent cleanup of two healthy peers.

    R2 (C1 from tester R1): the original assertion only checked that all
    three sessions were drained — it would pass even under serial execution
    because the two healthy mocks returned instantly. Strengthen the test
    by:

    1. Making the two healthy bridges each ``await asyncio.sleep(HEALTHY)``
       so a serial implementation would total ``STUCK + HEALTHY + HEALTHY``
       wall-time, but the concurrent gather should be ~``STUCK``.
    2. Measuring elapsed wall-time and asserting it stays under 1.5×
       ``STUCK`` (the cap is the stuck timeout, not the sum).

    Serial budget under the regression: 0.3 + 0.2 + 0.2 = 0.7s (FAIL the
    < 0.45s bound). Concurrent: max(0.3, 0.2, 0.2) = 0.3s (PASS).
    """
    from clauded.bot import ClaudedBot

    STUCK_TIMEOUT = 0.3
    HEALTHY_DELAY = 0.2
    # Concurrent cap: 1.5 × stuck timeout. Serial would need ≥ 0.7s.
    CONCURRENCY_BOUND = STUCK_TIMEOUT * 1.5

    monkeypatch.setenv("CLAUDED_BRIDGE_STOP_TIMEOUT", str(STUCK_TIMEOUT))
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

    async def _healthy_slow() -> None:
        # Genuine delay so serial execution would compound.
        await asyncio.sleep(HEALTHY_DELAY)

    sessions[102].stop = AsyncMock(side_effect=_never_returns)
    sessions[101].stop = AsyncMock(side_effect=_healthy_slow)
    sessions[103].stop = AsyncMock(side_effect=_healthy_slow)

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

    elapsed_start = time.monotonic()
    await asyncio.wait_for(coro_fn(bot), timeout=5.0)
    elapsed = time.monotonic() - elapsed_start

    # All three should have been processed; sessions dict drained.
    assert sorted(stopped) == [101, 102, 103]
    assert sessions == {}
    # Concurrency assertion: if `_cleanup_task` regressed to serial
    # `for tid in to_remove: await _stop_one(tid)`, the wall time would be
    # roughly STUCK_TIMEOUT + 2 × HEALTHY_DELAY = 0.7s, far above the cap.
    assert elapsed < CONCURRENCY_BOUND, (
        f"cleanup took {elapsed:.3f}s; expected concurrent execution under "
        f"{CONCURRENCY_BOUND:.3f}s (serial would be "
        f"~{STUCK_TIMEOUT + 2 * HEALTHY_DELAY:.3f}s)"
    )


@pytest.mark.asyncio
async def test_send_message_exception_path_disconnect_force_drops_after_timeout(
    cfg: "Config", monkeypatch: pytest.MonkeyPatch
) -> None:
    """#173 regression pin: when send_message hits an exception and the
    cleanup `disconnect()` call hangs (anyio cross-task cancel scope dead-
    lock — same root cause as #146), the call MUST time out per the env
    var and force-drop the client. Without this fix, the current user's
    Discord turn would hang forever (the #145 frozen-UI symptom).

    Construct a bridge where:
      - send_message's inner SDK call raises (synthetic ValueError to
        force the BaseException branch at claude_bridge.py:411)
      - The same client's disconnect() never returns (mirrors the #146
        anyio deadlock)

    Assert: send_message returns within (timeout + small margin), and
    the bridge has been force-dropped (client=None, active=False).
    """
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)

    class FakeStuckClient:
        async def query(self, *args, **kwargs):
            raise ValueError("synthetic SDK failure")
        async def receive_response(self):
            # If query raised before stream starts, this won't be called;
            # included for completeness.
            raise ValueError("synthetic SDK failure")
            yield  # pragma: no cover
        async def disconnect(self):
            await asyncio.sleep(999)  # mirrors the anyio deadlock

    bridge._client = FakeStuckClient()
    bridge._active = True

    monkeypatch.setenv("CLAUDED_BRIDGE_STOP_TIMEOUT", "0.5")

    # send_message must raise (re-raises the original ValueError after
    # attempting cleanup) AND must NOT hang forever on the stuck disconnect.
    with pytest.raises(ValueError, match="synthetic SDK failure"):
        # Use wait_for as a safety net so a regression doesn't hang the
        # test suite. Cap at 3s — generous vs the 0.5s timeout.
        async def _drive():
            async for _ in bridge.send_message("test prompt"):
                pass
        await asyncio.wait_for(_drive(), timeout=3.0)

    # After the timeout fired, bridge must be force-dropped
    assert bridge._client is None
    assert bridge._active is False
