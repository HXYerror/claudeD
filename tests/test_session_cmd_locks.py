"""#audit(#2,#3,#4): session commands that drive the shared SDK receive stream
(``/session compact``, ``/session security-review``) or tear down the client
(``/session stop``) MUST hold the per-thread lock. Otherwise they race an
in-flight user turn: two consumers split the single shared SDK receive stream
(lost ResultMessage / interleaved text), or a mid-turn disconnect crashes the
turn's ``receive_response()`` by closing the anyio TaskGroup under it.

Each test records enter/send/exit ordering to prove the stream/teardown op runs
strictly INSIDE the lock (and, for stop, that we interrupt first).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

import discord

from clauded.bot import ClaudedBot
from clauded.cogs.session import (
    session_compact,
    session_security_review,
    session_stop,
)


class _RecordingLock:
    """Async context manager that appends enter/exit to a shared list."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> "_RecordingLock":
        self._events.append("lock-enter")
        return self

    async def __aexit__(self, *exc) -> bool:
        self._events.append("lock-exit")
        return False


def _interaction(channel_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = channel_id
    interaction.channel_id = channel_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _bot_with_active_bridge(events: list[str]) -> tuple[MagicMock, MagicMock]:
    bridge = MagicMock()
    bridge.is_active = True

    async def fake_send(msg):
        events.append(f"send:{msg}")
        return
        yield  # noqa: unreachable — marks fake_send an async generator

    bridge.send_message = fake_send
    bridge.interrupt = AsyncMock(side_effect=lambda: events.append("interrupt"))

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.session_manager.get_lock = MagicMock(return_value=_RecordingLock(events))

    async def fake_stop(_tid):
        events.append("stop_session")
        return True

    bot.session_manager.stop_session = AsyncMock(side_effect=fake_stop)
    return bot, bridge


@pytest.mark.asyncio
async def test_compact_sends_inside_lock():
    events: list[str] = []
    bot, _ = _bot_with_active_bridge(events)
    interaction = _interaction()
    interaction.client = bot
    await session_compact.callback(interaction)
    assert events == ["lock-enter", "send:/compact", "lock-exit"]


@pytest.mark.asyncio
async def test_security_review_sends_inside_lock(monkeypatch):
    events: list[str] = []
    bot, _ = _bot_with_active_bridge(events)
    # Bypass the Group-A unbound guard so we reach the send path.
    monkeypatch.setattr(
        "clauded.cogs.session.reject_if_unbound", AsyncMock(return_value=False)
    )
    interaction = _interaction()
    interaction.client = bot
    await session_security_review.callback(interaction)
    assert events == ["lock-enter", "send:/security-review", "lock-exit"]


@pytest.mark.asyncio
async def test_stop_interrupts_then_stops_under_lock():
    events: list[str] = []
    bot, _ = _bot_with_active_bridge(events)
    interaction = _interaction()
    interaction.client = bot
    await session_stop.callback(interaction)
    # Interrupt the in-flight turn first, THEN stop under the lock.
    assert events == ["interrupt", "lock-enter", "stop_session", "lock-exit"]
