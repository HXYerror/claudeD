"""Tests for Discord thread-creation race recovery in ``ClaudedBot``.

The Discord gateway occasionally duplicates ``MESSAGE_CREATE`` events,
which causes ``on_message`` to run twice for the same message. The first
invocation wins the ``message.create_thread()`` call; the second loses
with ``discord.HTTPException`` code ``160004`` ("a thread has already
been created for this message"). Before this fix the loser path posted
a misleading ``❌ Failed to create a thread for this message.`` error
into the channel even though a thread had in fact been created.

This module pins the recovery behaviour: on 160004 we re-fetch the
message, observe ``message.thread`` is set, reuse that thread, and fall
through to the normal bridge-startup path with **no** user-visible error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.config import Config
from clauded.cost_tracker import CostTracker
from clauded.project_manager import ProjectManager
from clauded.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Fake Discord plumbing — kept self-contained so tests are independent of
# the unbound-fallback fixtures' specific FakeMessage semantics.
# ---------------------------------------------------------------------------


@dataclass
class FakeAuthor:
    id: int = 999
    bot: bool = False
    name: str = "alice"

    def __str__(self) -> str:
        return self.name


@dataclass
class FakeUser:
    id: int = 42
    name: str = "bot"


class FakeThread:
    """Minimal stand-in for a created Discord thread."""

    def __init__(self, thread_id: int, name: str = "session") -> None:
        self.id = thread_id
        self.name = name
        self.parent_id: int | None = None
        self.messages: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        self.messages.append({"content": content, **kwargs})
        return MagicMock()


def _make_http_exception(code: int) -> discord.HTTPException:
    """Build a real ``discord.HTTPException`` carrying the given error code.

    The class reads ``code`` out of the JSON payload, so we pass it in
    the dict-shaped ``message`` arg. We don't go through the real
    aiohttp response — only ``status`` and ``reason`` are touched.
    """
    resp = MagicMock()
    resp.status = 400
    resp.reason = "Bad Request"
    return discord.HTTPException(resp, {"code": code, "message": "thread exists"})


class FakeChannel:
    """Stand-in for ``discord.TextChannel`` (the parent of the thread).

    Records ``send()`` calls so the test can assert no error message
    was surfaced. ``fetch_message`` is a MagicMock that the test wires
    up to return the same FakeMessage with ``.thread`` set.
    """

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.parent_id: int | None = None
        self.sent: list[Any] = []
        # ``fetch_message`` is set by the test fixture.
        self.fetch_message: AsyncMock = AsyncMock()

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        self.sent.append({"content": content, **kwargs})
        return MagicMock()


class FakeRaceMessage:
    """Message whose ``create_thread`` raises HTTPException(code=160004)."""

    def __init__(
        self,
        *,
        channel: FakeChannel,
        content: str,
        bot_user_id: int,
        existing_thread: FakeThread,
    ) -> None:
        self.id = 12345
        self.channel = channel
        self.content = content
        self.mentions = [MagicMock(id=bot_user_id)]
        self.role_mentions: list[Any] = []
        self.author = FakeAuthor()
        self.attachments: list[Any] = []
        self.replies: list[str] = []
        # When the race winner's MESSAGE_CREATE handler runs first,
        # Discord internally attaches the created thread to the message;
        # after we re-fetch on 160004 we should observe it here.
        self.thread: FakeThread | None = None
        self._existing_thread = existing_thread
        self._create_thread_calls = 0

    async def reply(self, content: str, **kwargs: Any) -> Any:
        self.replies.append(content)
        return MagicMock()

    async def create_thread(self, *, name: str, **kwargs: Any) -> FakeThread:
        # Loser path: thread already exists → Discord returns 160004.
        self._create_thread_calls += 1
        raise _make_http_exception(160004)

    async def add_reaction(self, _emoji: str) -> None:
        return None

    async def remove_reaction(self, _emoji: str, _user: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Bot fixtures — same shape as test_bot_unbound_message.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root=str(tmp_path),
        # Enable the home-dir fallback so the handler proceeds past the
        # is_bound() gate without needing an actual bind.
        allow_unbound_fallback=True,
    )


@pytest.fixture
def bot(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudedBot:
    """Build a ``ClaudedBot`` without going through ``commands.Bot.__init__``."""
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(tmp_path))

    bot = ClaudedBot.__new__(ClaudedBot)
    bot.config = cfg
    bot.project_manager = pm
    bot.session_manager = SessionManager()
    bot.cost_tracker = CostTracker()
    bot.agent_manager = MagicMock()
    bot._start_time = 0.0
    bot._claude_version = "test"
    bot._debug_logging = False
    bot._pre_tool_notifications = False
    bot._notify_enabled = {}
    bot.allow_unbound_fallback = bot.config.allow_unbound_fallback
    bot._connection = MagicMock()
    fake_user = FakeUser(id=42, name="bot")
    monkeypatch.setattr(ClaudedBot, "user", property(lambda self: fake_user))

    # ``isinstance(channel, discord.TextChannel)`` must accept FakeChannel.
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)

    class _Forum:  # noqa: D401
        pass

    monkeypatch.setattr(discord, "ForumChannel", _Forum)

    return bot


@pytest.fixture
def captured_sessions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every ``SessionManager.create_session`` call and short-circuit
    the renderer so the test focuses on the thread-race recovery branch."""
    captured: list[dict[str, Any]] = []

    async def _fake_create(
        self: SessionManager,
        thread_id: int,
        project_path: str,
        config: Config,
        session_config: Any = None,
    ) -> Any:
        bridge = MagicMock()
        bridge.total_cost = 0.0
        bridge.is_active = True
        bridge.session_id = None
        captured.append(
            {
                "thread_id": thread_id,
                "project_path": project_path,
                "session_config": session_config,
            }
        )
        self._sessions[thread_id] = bridge
        return bridge

    monkeypatch.setattr(SessionManager, "create_session", _fake_create)

    async def _noop_render(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None

    monkeypatch.setattr(ClaudedBot, "_render_with_retry", _noop_render)
    return captured


# ---------------------------------------------------------------------------
# Test: 160004 → reuse the existing thread, no error surface.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_race_160004_reuses_existing_thread(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate MESSAGE_CREATE race:

    1. ``message.create_thread`` raises ``HTTPException(code=160004)`` —
       a competing dispatch already created the thread.
    2. The handler re-fetches the message via ``channel.fetch_message``.
    3. The refetched message exposes the existing thread on ``.thread``.
    4. The handler reuses that thread, surfaces NO error embed, and
       continues into the normal bridge-startup path (``create_session``
       is invoked for that thread id).
    """
    existing_thread = FakeThread(thread_id=77777, name="race-winner-thread")
    channel = FakeChannel(channel_id=9001)
    msg = FakeRaceMessage(
        channel=channel,
        content="<@42> hello",
        bot_user_id=42,
        existing_thread=existing_thread,
    )

    # When the handler refetches after 160004, return a copy of ``msg``
    # whose ``.thread`` is the existing thread (the race winner's).
    # The handler reads several attrs off the refetched message after
    # this point (``add_reaction``, ``remove_reaction``, ``content``,
    # ``attachments``, ``author``) — wire them all to async-safe stubs.
    refetched = MagicMock()
    refetched.id = msg.id
    refetched.thread = existing_thread
    refetched.content = msg.content
    refetched.attachments = []
    refetched.author = msg.author
    refetched.add_reaction = AsyncMock(return_value=None)
    refetched.remove_reaction = AsyncMock(return_value=None)
    channel.fetch_message.return_value = refetched

    await bot._handle_channel_message(msg)

    # ----- Race assertions -----
    # 1) Refetch happened with the original message id.
    channel.fetch_message.assert_awaited_once_with(msg.id)

    # 2) NO error message was surfaced into the channel.
    error_sends = [s for s in channel.sent if isinstance(s.get("content"), str)]
    assert not any(
        "Failed to create a thread" in (s.get("content") or "") for s in error_sends
    ), f"Unexpected error surfaced into channel: {channel.sent!r}"

    # 3) The handler proceeded into bridge startup on the EXISTING thread —
    #    not on a brand-new one. ``create_session`` was invoked with
    #    ``thread_id == existing_thread.id``.
    assert captured_sessions, "Expected create_session to be called after recovery"
    assert captured_sessions[0]["thread_id"] == existing_thread.id

    # 4) ``create_thread`` was called exactly once (the failing attempt);
    #    we did NOT loop or retry.
    assert msg._create_thread_calls == 1


# ---------------------------------------------------------------------------
# Test: 160004 → existing thread already has an ACTIVE session → skip render.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_race_160004_with_active_session_skips_render(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Duplicate MESSAGE_CREATE arrives at OUR PROCESS twice.

    The race winner has already created the thread *and* started a
    bridge session for it. The losing dispatch hits 160004, refetches
    the message, sees ``message.thread`` populated — and must observe
    that a session is *already active* on that thread, then return
    WITHOUT calling ``_render_with_retry``. Otherwise we double-render
    the same user turn and double the API cost (issue #140).
    """
    existing_thread = FakeThread(thread_id=99999, name="race-winner-thread")
    channel = FakeChannel(channel_id=9001)
    msg = FakeRaceMessage(
        channel=channel,
        content="<@42> hello",
        bot_user_id=42,
        existing_thread=existing_thread,
    )

    # Pre-seed the session manager with an ACTIVE bridge for the
    # already-existing thread — this is what the race winner left behind.
    active_bridge = MagicMock()
    active_bridge.is_active = True
    bot.session_manager._sessions[existing_thread.id] = active_bridge

    refetched = MagicMock()
    refetched.id = msg.id
    refetched.thread = existing_thread
    refetched.content = msg.content
    refetched.attachments = []
    refetched.author = msg.author
    refetched.add_reaction = AsyncMock(return_value=None)
    refetched.remove_reaction = AsyncMock(return_value=None)
    channel.fetch_message.return_value = refetched

    import logging

    caplog.set_level(logging.INFO, logger="clauded.bot")
    await bot._handle_channel_message(msg)

    # ----- Duplicate-suppression assertions -----
    # 1) Refetch happened once with the original message id (we still
    #    need the refetch to *discover* the existing-thread state).
    channel.fetch_message.assert_awaited_once_with(msg.id)

    # 2) No NEW session was created — ``create_session`` must NOT have
    #    been invoked by this dispatch (the race winner already did it).
    assert not captured_sessions, (
        f"Expected no create_session for duplicate dispatch, got: {captured_sessions!r}"
    )

    # 3) The active bridge we pre-seeded is still the registered one —
    #    we did not stomp on it.
    assert bot.session_manager._sessions[existing_thread.id] is active_bridge

    # 4) No error message surfaced into the channel.
    assert not any(
        "Failed to create a thread" in (s.get("content") or "")
        for s in channel.sent
    ), f"Unexpected error surfaced into channel: {channel.sent!r}"

    # 5) Suppression was logged.
    assert any(
        "ignoring duplicate MESSAGE_CREATE" in rec.getMessage()
        for rec in caplog.records
    ), f"Expected duplicate-suppression log entry, got: {[r.getMessage() for r in caplog.records]!r}"


# ---------------------------------------------------------------------------
# v1.18 #155 follow-up: TOCTOU inside per-thread lock (engineer R1 important)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_race_session_appears_during_lock_acquire(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two MESSAGE_CREATE dispatches: both see ``get_session() == None`` pre-lock,
    both reach the per-thread lock. The waiter must NOT stomp the winner's
    bridge — the inside-lock re-check should suppress the duplicate.

    Scenario:
    - First dispatch holds the lock and is mid-``create_session``
    - Second dispatch acquires the lock right after; sees a live bridge in
      ``_sessions``; must skip its own ``create_session`` call

    We simulate this by pre-seeding an active bridge into ``_sessions``
    just before invoking ``_handle_channel_message`` — this represents the
    state the second dispatch sees once it acquires the lock.
    """
    winner_thread = FakeThread(thread_id=88888, name="winner-thread")
    channel = FakeChannel(channel_id=9001)

    # Subclass FakeRaceMessage to have create_thread SUCCEED (returning a fresh
    # thread) instead of raising 160004. This forces the handler past the
    # pre-lock 160004 recovery branch and into the per-thread lock where the
    # new inside-lock gate must catch the duplicate.
    class _SuccessMessage(FakeRaceMessage):
        async def create_thread(self, *, name: str, **kwargs: Any) -> FakeThread:
            self._create_thread_calls += 1
            return winner_thread

    msg = _SuccessMessage(
        channel=channel,
        content="<@42> hello",
        bot_user_id=42,
        existing_thread=winner_thread,
    )

    # Pre-seed an active bridge for the thread — this is the state the second
    # dispatch sees inside the lock.
    active_bridge = MagicMock()
    active_bridge.is_active = True
    bot.session_manager._sessions[winner_thread.id] = active_bridge

    import logging
    caplog.set_level(logging.INFO, logger="clauded.bot")
    await bot._handle_channel_message(msg)

    # ----- Inside-lock suppression assertions -----
    # 1) No NEW session was created — the inside-lock re-check caught the duplicate.
    assert not captured_sessions, (
        f"Inside-lock TOCTOU: expected no create_session, got: {captured_sessions!r}"
    )
    # 2) The active bridge we pre-seeded is still the registered one (not stomped).
    assert bot.session_manager._sessions[winner_thread.id] is active_bridge
    # 3) The new inside-lock suppression log fired.
    assert any(
        "Thread-race (inside lock)" in rec.getMessage()
        for rec in caplog.records
    ), f"Expected inside-lock suppression log, got: {[r.getMessage() for r in caplog.records]!r}"
