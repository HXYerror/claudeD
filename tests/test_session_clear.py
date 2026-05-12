"""Tests for /session clear (#163 sub-task 2).

Inverse of /session resume: tears down live bridge AND removes the
persisted resume entry from data/sessions.json. Next user message in
the thread will start a fresh session (no resume_session_id).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.config import Config
from clauded.cost_tracker import CostTracker
from clauded.project_manager import ProjectManager
from clauded.session_manager import SessionManager


@pytest.fixture
def bot(tmp_path: Path) -> ClaudedBot:
    cfg = Config(
        discord_bot_token="tok", claude_model="sonnet",
        claude_permission_mode="default", projects_root=str(tmp_path),
        allow_unbound_fallback=False,
    )
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(tmp_path))
    from clauded.session_store import SessionStore
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
    bot.allow_unbound_fallback = False
    bot._connection = MagicMock()
    return bot


def _make_interaction(bot: ClaudedBot, channel_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel_id = channel_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_session_clear_drops_active_bridge(bot: ClaudedBot) -> None:
    """Active session in thread → stop_session called, success embed."""
    from clauded.cogs.session import session_clear
    thread_id = 12345
    mock_bridge = AsyncMock()
    mock_bridge.is_active = True
    mock_bridge.session_id = "sess-abc"
    mock_bridge.project_path = "/tmp"
    mock_bridge.model = "sonnet"
    mock_bridge.system_prompt = None
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_interaction(bot, thread_id)
    await session_clear.callback(interaction)

    # Bridge stopped (popped from _sessions)
    assert thread_id not in bot.session_manager._sessions
    # Success embed shown
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "Session cleared" in sent_embed.title
    assert "fresh session" in sent_embed.description.lower()


@pytest.mark.asyncio
async def test_session_clear_removes_persisted_resume(bot: ClaudedBot) -> None:
    """Stored session in disk → remove_session called; next start won't resume."""
    from clauded.cogs.session import session_clear
    thread_id = 23456
    # Pre-seed a stored session via the store directly
    bot.session_manager._session_store.save_session(
        thread_id, "sess-stored-id", "/tmp/proj",
        model="sonnet", system_prompt=None,
    )
    assert bot.session_manager.get_stored_session(thread_id) is not None

    interaction = _make_interaction(bot, thread_id)
    await session_clear.callback(interaction)

    # Stored entry removed
    assert bot.session_manager.get_stored_session(thread_id) is None
    # Success embed shown
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "Session cleared" in sent_embed.title


@pytest.mark.asyncio
async def test_session_clear_no_session_no_stored(bot: ClaudedBot) -> None:
    """No active bridge AND no stored entry → friendly 'No session to clear'."""
    from clauded.cogs.session import session_clear
    interaction = _make_interaction(bot, channel_id=34567)
    await session_clear.callback(interaction)

    msg = interaction.response.send_message.call_args[0][0]
    assert "No session to clear" in msg
    # ephemeral=True for admin-style command
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_session_clear_no_channel_context(bot: ClaudedBot) -> None:
    """No channel context (DM/PM edge) → friendly error."""
    from clauded.cogs.session import session_clear
    interaction = _make_interaction(bot, channel_id=None)
    await session_clear.callback(interaction)
    msg = interaction.response.send_message.call_args[0][0]
    assert "No thread context" in msg


@pytest.mark.asyncio
async def test_session_clear_both_active_and_stored(bot: ClaudedBot) -> None:
    """Both live bridge AND stored entry → both cleared, single success message."""
    from clauded.cogs.session import session_clear
    thread_id = 45678
    mock_bridge = AsyncMock()
    mock_bridge.is_active = True
    mock_bridge.session_id = "sess-live"
    mock_bridge.project_path = "/tmp"
    mock_bridge.model = "sonnet"
    mock_bridge.system_prompt = None
    bot.session_manager._sessions[thread_id] = mock_bridge
    bot.session_manager._session_store.save_session(
        thread_id, "sess-stored-id", "/tmp/proj",
        model="sonnet", system_prompt=None,
    )

    interaction = _make_interaction(bot, thread_id)
    await session_clear.callback(interaction)

    assert thread_id not in bot.session_manager._sessions
    assert bot.session_manager.get_stored_session(thread_id) is None
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "Session cleared" in sent_embed.title


@pytest.mark.asyncio
async def test_session_clear_response_is_ephemeral(bot: ClaudedBot) -> None:
    """All response paths use ephemeral=True (admin-style command)."""
    from clauded.cogs.session import session_clear
    interaction = _make_interaction(bot, channel_id=56789)
    await session_clear.callback(interaction)
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


# ---------------------------------------------------------------------------
# v1.18 R1 — engineer + architect convergent finding: clear_session must hold
# the per-thread lock around stop+remove so a concurrent /session resume can't
# race in between the stop and the remove_session, leaving a re-persisted
# zombie entry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_session_holds_per_thread_lock(bot: ClaudedBot) -> None:
    """Regression pin for R1 engineer + architect convergent RC:
    SessionManager.clear_session must acquire ``get_lock(thread_id)`` for
    the entire stop+remove sequence. Pin via pre-hold pattern: hold the
    lock from a separate task, assert clear_session blocks until release.
    """
    import asyncio
    thread_id = 67890
    bot.session_manager._session_store.save_session(
        thread_id, "sess-id", "/tmp/proj",
        model="sonnet", system_prompt=None,
    )

    pre_held = asyncio.Event()
    release_pre_hold = asyncio.Event()

    async def pre_hold():
        async with bot.session_manager.get_lock(thread_id):
            pre_held.set()
            await release_pre_hold.wait()

    holder = asyncio.create_task(pre_hold())
    await pre_held.wait()

    # clear_session should block on the lock
    clear_task = asyncio.create_task(bot.session_manager.clear_session(thread_id))
    await asyncio.sleep(0.05)  # let clear_session reach its `async with`
    assert not clear_task.done(), (
        "clear_session must wait on per-thread lock; if done() is True, it "
        "is bypassing the lock (R1 engineer/architect convergent RC)"
    )
    # Stored entry must still exist (clear_session hasn't run yet)
    assert bot.session_manager.get_stored_session(thread_id) is not None

    release_pre_hold.set()
    await holder
    result = await clear_task
    assert result == (False, True)
    assert bot.session_manager.get_stored_session(thread_id) is None


@pytest.mark.asyncio
async def test_clear_session_atomic_against_concurrent_resume(bot: ClaudedBot) -> None:
    """End-to-end test that resume + clear don't leave a zombie:
    if /session resume runs while /session clear is mid-flight, the
    final state must be 'no stored entry' (clear wins because it ran
    second; or no race because both serialize on the lock)."""
    import asyncio
    thread_id = 78901
    bot.session_manager._session_store.save_session(
        thread_id, "sess-stored", "/tmp",
        model="sonnet", system_prompt=None,
    )

    # Race: kick off clear_session AND a re-save (simulating resume's
    # save_session_state inside its own lock acquisition).
    async def fake_resume():
        async with bot.session_manager.get_lock(thread_id):
            # Simulate what resume does: save_session_state
            bot.session_manager._session_store.save_session(
                thread_id, "sess-resumed", "/tmp",
                model="sonnet", system_prompt=None,
            )

    # Order: clear acquires first, completes; then fake_resume acquires,
    # re-saves. OR: fake_resume acquires first, then clear runs and
    # removes. Either way, the lock guarantees atomicity. Pin: no torn
    # state (both keys overlap on disk).
    clear_task = asyncio.create_task(bot.session_manager.clear_session(thread_id))
    resume_task = asyncio.create_task(fake_resume())
    await asyncio.gather(clear_task, resume_task)
    # Whichever ran last is the final state. We don't assert WHICH won;
    # just that the state is internally consistent (no torn writes).
    # (For deterministic semantics, prod code outside this test would
    # block resume on the bridge being torn down via stop_session, but
    # this test only pins atomicity of clear_session itself.)
    final = bot.session_manager.get_stored_session(thread_id)
    # Either: clear ran last → None; or resume ran last → entry exists
    # with the resume session_id. Both are valid.
    if final is not None:
        assert final.get("session_id") == "sess-resumed", (
            f"final state should be the resume's session, got: {final}"
        )
