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
