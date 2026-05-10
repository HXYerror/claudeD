"""Tests for the v1.11 unbound-channel fallback in ``ClaudedBot``.

The on-message handlers (``_handle_channel_message`` /
``_handle_thread_message``) used to early-return when the channel had
no ``/project bind``. v1.11 (#110) changes that: unbound channels fall
back to the operator's home directory and surface a one-shot hint about
``/project bind`` as the first thread message.

These tests drive the handlers directly with fake Discord objects, and
monkeypatch ``SessionManager.create_session`` + ``DiscordRenderer`` so
the handler's bridge/render side effects don't actually try to talk to
Claude.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
# Fake Discord plumbing
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


class FakeReaction:
    pass


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


class FakeChannel:
    """Stand-in for ``discord.TextChannel`` (the parent of the thread)."""

    # Marker that ``isinstance(self, discord.TextChannel)`` returns True
    # via the test setup below.
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.parent_id: int | None = None
        self.sent: list[Any] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        self.sent.append({"content": content, **kwargs})
        return MagicMock()


class FakeMessage:
    """Stand-in for ``discord.Message``."""

    def __init__(
        self,
        *,
        channel: Any,
        content: str,
        bot_user_id: int,
        author: FakeAuthor | None = None,
    ) -> None:
        self.channel = channel
        self.content = content
        # Pretend the bot was @mentioned so ``_handle_channel_message``
        # passes the gate.
        self.mentions = [MagicMock(id=bot_user_id)]
        self.role_mentions: list[Any] = []
        self.author = author or FakeAuthor()
        self.attachments: list[Any] = []
        self.replies: list[str] = []
        self._created_thread: FakeThread | None = None

    async def reply(self, content: str, **kwargs: Any) -> Any:
        self.replies.append(content)
        return MagicMock()

    async def create_thread(self, *, name: str, **kwargs: Any) -> FakeThread:
        thread = FakeThread(thread_id=hash(name) & 0xFFFF, name=name)
        thread.parent_id = self.channel.id
        self._created_thread = thread
        return thread

    async def add_reaction(self, _emoji: str) -> None:
        return None

    async def remove_reaction(self, _emoji: str, _user: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Bot fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root=str(tmp_path),
    )


@pytest.fixture
def bot(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudedBot:
    """Build a ``ClaudedBot`` without going through ``commands.Bot.__init__``.

    Discord's commands.Bot constructor wants a running event loop and an
    actual token; for unit tests we just need the attributes the handlers
    touch.
    """
    # Steer ProjectManager at a temp data dir so we don't pollute repo data/.
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
    # Bot identity used by the mention check
    bot._connection = MagicMock()  # placeholder; tests don't talk to it
    fake_user = FakeUser(id=42, name="bot")
    # ``self.user`` is a property on commands.Bot; set it directly.
    monkeypatch.setattr(ClaudedBot, "user", property(lambda self: fake_user))

    # Make ``isinstance(channel, discord.TextChannel)`` succeed for FakeChannel.
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    # ForumChannel check uses ``channel_mode == "forum"``, which our fixture
    # never sets, so the actual class never matters. Patch it to a sentinel
    # so the isinstance() check is well-defined.
    class _Forum:  # noqa: D401
        pass

    monkeypatch.setattr(discord, "ForumChannel", _Forum)

    return bot


@pytest.fixture
def captured_sessions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every ``SessionManager.create_session`` call's project_path.

    Returns a list that the test inspects; each entry holds the args the
    handler passed in. The fake bridge is just enough to satisfy the
    handler's ``bridge.total_cost`` access.
    """
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
        # ``save_session_state`` skips when session_id is falsy — keep it None
        # so the test doesn't hit SessionStore's JSON encoder with mocks.
        bridge.session_id = None
        captured.append(
            {
                "thread_id": thread_id,
                "project_path": project_path,
                "session_config": session_config,
            }
        )
        # Register so ``get_session`` returns it (used by thread handler).
        self._sessions[thread_id] = bridge
        return bridge

    monkeypatch.setattr(SessionManager, "create_session", _fake_create)

    # Skip the actual rendering — the handler's renderer call would try to
    # iterate the (mock) bridge's ``send_message``. We don't care; the test
    # only inspects what was captured before render.
    async def _noop_render(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None

    monkeypatch.setattr(ClaudedBot, "_render_with_retry", _noop_render)
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbound_channel_first_message_falls_back_to_home_with_hint(
    bot: ClaudedBot, captured_sessions: list[dict[str, Any]]
) -> None:
    """First @bot in an unbound channel: bridge cwd=$HOME + hint posted."""
    channel = FakeChannel(channel_id=1001)
    msg = FakeMessage(channel=channel, content="<@42> hello", bot_user_id=42)

    await bot._handle_channel_message(msg)

    assert captured_sessions, "Expected create_session to be called"
    assert captured_sessions[0]["project_path"] == str(Path.home().resolve())

    thread = msg._created_thread
    assert thread is not None, "Thread should have been created"
    # Hint must be the first message posted in the thread, before anything
    # the bridge would emit.
    assert thread.messages, "Hint message expected on the thread"
    first = thread.messages[0]["content"]
    assert "isn't bound to a project" in first
    assert "/project bind" in first
    assert "home directory" in first


@pytest.mark.asyncio
async def test_unbound_channel_second_message_no_hint_repeated(
    bot: ClaudedBot, captured_sessions: list[dict[str, Any]]
) -> None:
    """Second @bot in same unbound channel: cwd=$HOME, no hint repeated."""
    channel = FakeChannel(channel_id=1002)

    msg1 = FakeMessage(channel=channel, content="<@42> first", bot_user_id=42)
    await bot._handle_channel_message(msg1)
    msg2 = FakeMessage(channel=channel, content="<@42> second", bot_user_id=42)
    await bot._handle_channel_message(msg2)

    assert len(captured_sessions) == 2
    assert all(
        c["project_path"] == str(Path.home().resolve()) for c in captured_sessions
    )

    thread2 = msg2._created_thread
    assert thread2 is not None
    # Second invocation must NOT post the hint into its (separate) thread.
    for m in thread2.messages:
        assert "isn't bound to a project" not in (m["content"] or "")


@pytest.mark.asyncio
async def test_bound_channel_uses_bound_path_no_hint(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """Bound channel: bridge gets bound path; no hint posted."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    bot.project_manager.bind(2001, str(proj))

    channel = FakeChannel(channel_id=2001)
    msg = FakeMessage(channel=channel, content="<@42> hi", bot_user_id=42)

    await bot._handle_channel_message(msg)

    assert captured_sessions
    assert captured_sessions[0]["project_path"] == str(proj.resolve())

    thread = msg._created_thread
    assert thread is not None
    for m in thread.messages:
        assert "isn't bound to a project" not in (m["content"] or "")


@pytest.mark.asyncio
async def test_post_bind_no_hint_even_if_previously_unbound(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """Unbound → fired hint → /project bind → next call uses bound, no hint."""
    channel = FakeChannel(channel_id=3001)

    # First message: unbound. Hint fires.
    msg1 = FakeMessage(channel=channel, content="<@42> first", bot_user_id=42)
    await bot._handle_channel_message(msg1)
    thread1 = msg1._created_thread
    assert thread1 is not None
    assert any(
        "isn't bound to a project" in (m["content"] or "") for m in thread1.messages
    )

    # Now bind.
    proj = tmp_path / "later"
    proj.mkdir()
    bot.project_manager.bind(3001, str(proj))

    # Second message: bound. cwd=bound, no hint in this thread.
    msg2 = FakeMessage(channel=channel, content="<@42> second", bot_user_id=42)
    await bot._handle_channel_message(msg2)
    thread2 = msg2._created_thread
    assert thread2 is not None
    for m in thread2.messages:
        assert "isn't bound to a project" not in (m["content"] or "")
    assert captured_sessions[-1]["project_path"] == str(proj.resolve())


@pytest.mark.asyncio
async def test_handle_thread_message_inherits_parent_fallback(
    bot: ClaudedBot, captured_sessions: list[dict[str, Any]]
) -> None:
    """Thread message inside unbound parent: bridge cwd=$HOME, no hint here."""
    parent_channel = FakeChannel(channel_id=4001)
    thread = FakeChannel(channel_id=4002)  # treat thread as a channel-like obj
    thread.parent_id = parent_channel.id

    # Sanity-check that the test-level fake thread satisfies what the
    # handler reads from ``message.channel``.
    msg = FakeMessage(channel=thread, content="follow-up", bot_user_id=42)
    await bot._handle_thread_message(msg, parent_id=parent_channel.id)

    assert captured_sessions
    assert captured_sessions[0]["project_path"] == str(Path.home().resolve())
    # The thread handler does NOT post the hint — it's the channel handler's job.
    assert thread.sent == []


@pytest.mark.asyncio
async def test_broken_home_dir_friendly_error(
    bot: ClaudedBot,
    captured_sessions: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If $HOME isn't a directory, reply with a friendly error; no session."""
    bogus = Path("/this/path/does/not/exist/clauded-test")

    def fake_get_path_or_default(channel_id: int):
        return bogus, False

    monkeypatch.setattr(
        bot.project_manager, "get_path_or_default", fake_get_path_or_default
    )

    channel = FakeChannel(channel_id=5001)
    msg = FakeMessage(channel=channel, content="<@42> hi", bot_user_id=42)

    await bot._handle_channel_message(msg)

    # No bridge created.
    assert captured_sessions == []
    # No thread created.
    assert msg._created_thread is None
    # Friendly error replied.
    assert msg.replies, "A reply should have been sent for the broken home"
    assert "home directory" in msg.replies[0]
    assert "/project bind" in msg.replies[0]
