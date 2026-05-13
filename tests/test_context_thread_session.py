"""#197 — `/context` and `/skill list` must look up the live session by
thread id, not the parent (binding) id.

Pre-fix, both cogs called ``bot.session_manager.get_session(channel_id)``
where ``channel_id = resolve_channel_id(interaction)``. In a thread
``resolve_channel_id`` returns ``parent_id`` (correct for binding
lookups), but the session manager is keyed by ``thread_id``. The lookup
always missed → fell through to Path B (cold-start a temp
``ClaudeSDKClient``) → competed with the heavy parent turn → Discord
"application did not respond" timeout.

This file pins:

* Path A succeeds for ``/context`` / ``/skill list`` in a thread with an
  active session (no ``ClaudeSDKClient`` spawned).
* Path B still runs in a bare channel without a session (unchanged).
* Path B is wrapped in a 10s timeout; a friendly "unavailable" embed is
  returned on timeout (no hang).
* ``defer()`` happens BEFORE any binding/session/path lookup so a slow
  op never causes "application did not respond" before deferral.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.config import Config
from clauded.cost_tracker import CostTracker
from clauded.project_manager import ProjectManager
from clauded.session_manager import SessionManager
from clauded.session_store import SessionStore


# ---------------------------------------------------------------------------
# Real ``discord.Thread`` subclass so ``isinstance(ch, discord.Thread)``
# inside ``resolve_channel_id`` returns True. Mirrors the pattern from
# ``tests/test_third_party_thread.py`` (#195).
# ---------------------------------------------------------------------------


class _FakeThread(discord.Thread):
    """Bypass ``discord.Thread.__init__`` (which needs full Discord state)
    while still passing ``isinstance(x, discord.Thread)`` checks.
    """

    def __init__(self) -> None:  # type: ignore[override]
        # Deliberately skip parent ``__init__``; tests set attrs manually.
        pass


def _make_thread(*, thread_id: int, parent_id: int) -> _FakeThread:
    t = _FakeThread()
    object.__setattr__(t, "id", thread_id)
    object.__setattr__(t, "parent_id", parent_id)
    return t


# ---------------------------------------------------------------------------
# Shared bot + interaction fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(tmp_path: Path) -> ClaudedBot:
    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root=str(tmp_path),
        allow_unbound_fallback=True,  # so bare-channel Path B doesn't get refused
    )
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(tmp_path))
    sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "data")))
    bot = ClaudedBot.__new__(ClaudedBot)
    bot.config = cfg
    bot.project_manager = pm
    bot.session_manager = sm
    bot.cost_tracker = CostTracker()
    bot.agent_manager = MagicMock()
    bot._start_time = 0.0
    bot._claude_version = "test"
    bot._debug_logging = False
    bot._pre_tool_notifications = False
    bot._notify_enabled = {}
    bot.allow_unbound_fallback = True
    bot._connection = MagicMock()
    return bot


def _make_thread_interaction(
    bot: ClaudedBot, *, parent_id: int, thread_id: int
) -> MagicMock:
    """Real-Discord thread semantics:

    * ``interaction.channel_id`` = ``thread_id`` (i.e. the thread itself).
    * ``interaction.channel`` is a ``discord.Thread`` with
      ``parent_id`` set; ``resolve_channel_id`` returns ``parent_id``.
    """
    thread = _make_thread(thread_id=thread_id, parent_id=parent_id)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel = thread
    interaction.channel_id = thread_id  # Discord sets this to the thread's id
    interaction.guild_id = 4242
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_channel_interaction(bot: ClaudedBot, *, channel_id: int) -> MagicMock:
    """Bare ``TextChannel`` semantics — ``channel_id == channel.id``."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel = ch
    interaction.channel_id = channel_id
    interaction.guild_id = 4242
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ===========================================================================
# /context — #197 acceptance criteria
# ===========================================================================


@pytest.mark.asyncio
async def test_context_in_thread_with_active_session_uses_path_a(
    bot: ClaudedBot,
) -> None:
    """Thread + active session (keyed by thread_id) → Path A picks up
    the bridge; no ``ClaudeSDKClient`` is constructed."""
    from clauded.cogs import context as ctx_mod

    parent_id = 1000
    thread_id = 2222
    fake_usage = {
        "totalTokens": 146000,
        "maxTokens": 200000,
        "percentage": 73.0,
        "model": "claude-sonnet",
        "categories": [{"name": "messages", "tokens": 146000}],
    }
    mock_bridge = MagicMock()
    mock_bridge.get_context_usage = AsyncMock(return_value=fake_usage)
    # CRITICAL: session keyed by thread_id, NOT parent_id (#197 root cause).
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_thread_interaction(
        bot, parent_id=parent_id, thread_id=thread_id
    )

    with patch.object(ctx_mod, "ClaudeSDKClient") as fake_ctor:
        await ctx_mod.context_cmd.callback(interaction)

    # Negative invariant: Path B NOT taken.
    fake_ctor.assert_not_called()
    mock_bridge.get_context_usage.assert_awaited_once()

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "73.0%" in embed.title
    assert "active session" in embed.description


@pytest.mark.asyncio
async def test_context_in_thread_without_session_falls_to_path_b(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread but no session registered for the thread → Path B fires
    (binding still resolves via parent_id, but session lookup misses)."""
    from clauded.cogs import context as ctx_mod

    parent_id = 1000
    thread_id = 2222
    # No session registered — bridge lookup will return None.

    fake_usage = {
        "totalTokens": 500,
        "maxTokens": 200000,
        "percentage": 0.25,
        "model": "claude-sonnet",
        "categories": [],
    }

    constructed: list = []

    class FakeTempClient:
        def __init__(self, opts):
            constructed.append(opts)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get_context_usage(self):
            return fake_usage

    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_thread_interaction(
        bot, parent_id=parent_id, thread_id=thread_id
    )
    await ctx_mod.context_cmd.callback(interaction)

    assert len(constructed) == 1, "Path B must spawn exactly one temp client"
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "fresh session" in embed.description


@pytest.mark.asyncio
async def test_context_bare_channel_without_session_uses_path_b(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare channel + no session → Path B (unchanged backward-compat)."""
    from clauded.cogs import context as ctx_mod

    fake_usage = {
        "totalTokens": 100,
        "maxTokens": 200000,
        "percentage": 0.05,
        "model": "claude-sonnet",
        "categories": [],
    }

    constructed: list = []

    class FakeTempClient:
        def __init__(self, opts):
            constructed.append(opts)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get_context_usage(self):
            return fake_usage

    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_channel_interaction(bot, channel_id=7777)
    await ctx_mod.context_cmd.callback(interaction)

    assert len(constructed) == 1
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "0.0%" in embed.title or "0.1%" in embed.title


@pytest.mark.asyncio
async def test_context_path_b_timeout_returns_friendly_embed(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path B subprocess ``__aenter__`` hangs → friendly embed within ~10s.

    We patch the cog's ``_PATH_B_TIMEOUT_S`` to ``0.2`` so the test
    completes in well under a second; the assertion is on the friendly
    embed shape, not the wall clock.
    """
    from clauded.cogs import context as ctx_mod
    from clauded.discord_renderer import COLOR_TOOL_FAILURE

    monkeypatch.setattr(ctx_mod, "_PATH_B_TIMEOUT_S", 0.2)

    class HangingClient:
        def __init__(self, opts):
            pass

        async def __aenter__(self):
            # Simulate a subprocess that never makes progress.
            await asyncio.sleep(5.0)
            return self

        async def __aexit__(self, *a):
            return None

        async def get_context_usage(self):  # pragma: no cover — never reached
            return {}

    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", HangingClient)

    interaction = _make_channel_interaction(bot, channel_id=8888)

    loop = asyncio.get_event_loop()
    start = loop.time()
    await asyncio.wait_for(ctx_mod.context_cmd.callback(interaction), timeout=2.0)
    elapsed = loop.time() - start

    # Must surface a friendly embed, not raise / hang.
    assert elapsed < 1.5, f"Path B timeout took {elapsed}s, expected ~0.2s"
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "Context unavailable" in embed.title
    assert "timeout" in embed.description.lower()
    assert "try again" in embed.description.lower()


@pytest.mark.asyncio
async def test_context_defers_before_any_slow_op(bot: ClaudedBot) -> None:
    """Fix C: ``defer()`` must be called BEFORE binding/session/Path-A
    lookups so any exception in those still leaves Discord deferred."""
    from clauded.cogs import context as ctx_mod

    parent_id = 1000
    thread_id = 2222
    fake_usage = {
        "totalTokens": 1,
        "maxTokens": 1000,
        "percentage": 0.1,
        "model": "claude-sonnet",
        "categories": [],
    }
    mock_bridge = MagicMock()
    mock_bridge.get_context_usage = AsyncMock(return_value=fake_usage)
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_thread_interaction(
        bot, parent_id=parent_id, thread_id=thread_id
    )

    # Record order of calls using a shared list — each tracked op
    # appends its name. Then assert defer is first.
    order: list[str] = []
    interaction.response.defer = AsyncMock(side_effect=lambda *a, **k: order.append("defer"))

    real_get_session = bot.session_manager.get_session

    def tracked_get_session(sid):  # type: ignore[no-redef]
        order.append("get_session")
        return real_get_session(sid)

    bot.session_manager.get_session = tracked_get_session  # type: ignore[method-assign]

    real_get_path = bot.project_manager.get_path_or_default

    def tracked_get_path(cid):  # type: ignore[no-redef]
        order.append("get_path")
        return real_get_path(cid)

    bot.project_manager.get_path_or_default = tracked_get_path  # type: ignore[method-assign]

    await ctx_mod.context_cmd.callback(interaction)

    assert order, "defer / get_session must have been called"
    assert order[0] == "defer", (
        f"defer must precede any slow op; got call order {order}"
    )


# ===========================================================================
# /skill list — #197 acceptance criteria (same 3-case minimum)
# ===========================================================================


@pytest.mark.asyncio
async def test_skill_list_in_thread_with_active_session_uses_path_a(
    bot: ClaudedBot,
) -> None:
    """Thread + active session keyed by thread_id → Path A; no temp client."""
    from clauded.cogs import skill as skill_mod

    parent_id = 1500
    thread_id = 2500
    fake_info = {
        "commands": [
            {"name": "crew", "description": "Multi-agent dev workflow (user)"},
        ]
    }
    mock_bridge = MagicMock()
    mock_bridge.get_server_info = AsyncMock(return_value=fake_info)
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_thread_interaction(
        bot, parent_id=parent_id, thread_id=thread_id
    )

    with patch.object(skill_mod, "ClaudeSDKClient") as fake_ctor:
        await skill_mod.skill_list.callback(interaction)

    fake_ctor.assert_not_called()
    mock_bridge.get_server_info.assert_awaited_once()

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.title.startswith("🧰 Skills")


@pytest.mark.asyncio
async def test_skill_list_bare_channel_falls_to_path_b(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare channel + no session → Path B (subprocess) runs."""
    from clauded.cogs import skill as skill_mod

    fake_info = {"commands": [{"name": "crew", "description": "(user)"}]}

    constructed: list = []

    class FakeTempClient:
        def __init__(self, options=None):
            constructed.append(options)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get_server_info(self):
            return fake_info

    monkeypatch.setattr(skill_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_channel_interaction(bot, channel_id=9999)
    await skill_mod.skill_list.callback(interaction)

    assert len(constructed) == 1
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.title.startswith("🧰 Skills")


@pytest.mark.asyncio
async def test_skill_list_path_b_timeout_returns_friendly_embed(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path B subprocess hangs → friendly "Skill list unavailable" embed."""
    from clauded.cogs import skill as skill_mod
    from clauded.discord_renderer import COLOR_TOOL_FAILURE

    monkeypatch.setattr(skill_mod, "_PATH_B_TIMEOUT_S", 0.2)

    class HangingClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            await asyncio.sleep(5.0)
            return self

        async def __aexit__(self, *a):
            return None

        async def get_server_info(self):  # pragma: no cover
            return {}

    monkeypatch.setattr(skill_mod, "ClaudeSDKClient", HangingClient)

    interaction = _make_channel_interaction(bot, channel_id=10101)

    loop = asyncio.get_event_loop()
    start = loop.time()
    await asyncio.wait_for(skill_mod.skill_list.callback(interaction), timeout=2.0)
    elapsed = loop.time() - start

    assert elapsed < 1.5, f"Path B timeout took {elapsed}s, expected ~0.2s"
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "Skill list unavailable" in embed.title
    assert "timeout" in embed.description.lower()


@pytest.mark.asyncio
async def test_skill_list_defers_before_any_slow_op(bot: ClaudedBot) -> None:
    """Fix C: ``defer()`` precedes all slow ops in ``/skill list`` too."""
    from clauded.cogs import skill as skill_mod

    parent_id = 1500
    thread_id = 2500
    mock_bridge = MagicMock()
    mock_bridge.get_server_info = AsyncMock(return_value={"commands": []})
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_thread_interaction(
        bot, parent_id=parent_id, thread_id=thread_id
    )

    order: list[str] = []
    interaction.response.defer = AsyncMock(side_effect=lambda *a, **k: order.append("defer"))

    real_get_session = bot.session_manager.get_session

    def tracked_get_session(sid):
        order.append("get_session")
        return real_get_session(sid)

    bot.session_manager.get_session = tracked_get_session  # type: ignore[method-assign]

    real_get_path = bot.project_manager.get_path_or_default

    def tracked_get_path(cid):
        order.append("get_path")
        return real_get_path(cid)

    bot.project_manager.get_path_or_default = tracked_get_path  # type: ignore[method-assign]

    await skill_mod.skill_list.callback(interaction)

    assert order, "defer / lookups must have run"
    assert order[0] == "defer", f"defer must come first; got {order}"
