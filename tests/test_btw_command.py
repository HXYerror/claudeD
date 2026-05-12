"""Tests for /btw side-question slash command (#163 sub-task 1).

The command is transparent-forward: when invoked in a thread with an active
Claude session, sends `/btw {text}` as a user message via the bridge. The
bundled CLI natively recognizes the prefix and opens a side-track.

Tests pin the precondition checks (must be in thread, must have active
session, text must be non-empty) and the forwarding contract.
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
    bot.allow_unbound_fallback = False
    bot._connection = MagicMock()
    return bot


def _make_thread_interaction(bot: ClaudedBot, thread_id: int = 11111) -> MagicMock:
    """Build an interaction whose .channel is a discord.Thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = 22222
    thread.send = AsyncMock()
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel = thread
    interaction.channel_id = thread_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_text_channel_interaction(bot: ClaudedBot) -> MagicMock:
    """Build an interaction whose .channel is a TextChannel (not a thread)."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 33333
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel = ch
    interaction.channel_id = 33333
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_btw_rejects_when_not_in_thread(bot: ClaudedBot) -> None:
    """/btw outside a thread → friendly error, no rendering."""
    from clauded.cogs.ops import btw_cmd
    interaction = _make_text_channel_interaction(bot)
    await btw_cmd.callback(interaction, "test question")
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "must be used inside a thread" in msg
    assert "❌" in msg


@pytest.mark.asyncio
async def test_btw_rejects_empty_text(bot: ClaudedBot) -> None:
    """/btw with empty text → usage hint, no rendering."""
    from clauded.cogs.ops import btw_cmd
    interaction = _make_thread_interaction(bot)
    await btw_cmd.callback(interaction, "   ")
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "empty" in msg.lower()
    assert "Usage:" in msg


@pytest.mark.asyncio
async def test_btw_rejects_when_no_active_session(bot: ClaudedBot) -> None:
    """/btw with no session in thread → friendly error guiding to /session resume."""
    from clauded.cogs.ops import btw_cmd
    interaction = _make_thread_interaction(bot)
    # session_manager has no entry for this thread_id
    await btw_cmd.callback(interaction, "test")
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "No active Claude session" in msg
    assert "session resume" in msg.lower()


@pytest.mark.asyncio
async def test_btw_rejects_when_session_inactive(bot: ClaudedBot) -> None:
    """/btw with bridge present but not is_active → friendly error."""
    from clauded.cogs.ops import btw_cmd
    interaction = _make_thread_interaction(bot)
    inactive_bridge = MagicMock()
    inactive_bridge.is_active = False
    bot.session_manager._sessions[interaction.channel.id] = inactive_bridge
    await btw_cmd.callback(interaction, "test")
    msg = interaction.response.send_message.call_args[0][0]
    assert "No active Claude session" in msg


@pytest.mark.asyncio
async def test_btw_forwards_with_btw_prefix_to_active_session(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: active session + valid text → render_response called with
    `/btw {text}` as user_text. The CLI's bundled binary handles the prefix
    natively (we test the forward-shape contract, not the SDK).
    """
    from clauded.cogs import ops as ops_mod
    interaction = _make_thread_interaction(bot)
    active_bridge = MagicMock()
    active_bridge.is_active = True
    bot.session_manager._sessions[interaction.channel.id] = active_bridge

    # Monkey-patch DiscordRenderer.render_response to capture the user_text.
    captured = {}
    class FakeRenderer:
        def __init__(self, target):
            captured["target"] = target
        async def render_response(self, bridge, user_text):
            captured["bridge"] = bridge
            captured["user_text"] = user_text
    monkeypatch.setattr(ops_mod, "DiscordRenderer", FakeRenderer, raising=False)
    # ops_mod imports DiscordRenderer inside the function — patch the source.
    from clauded import discord_renderer as dr_mod
    monkeypatch.setattr(dr_mod, "DiscordRenderer", FakeRenderer, raising=False)

    await ops_mod.btw_cmd.callback(interaction, "what time is it")

    # Acknowledged the interaction (forwarding banner)
    interaction.response.send_message.assert_awaited_once()
    ack_msg = interaction.response.send_message.call_args[0][0]
    assert "Forwarding side question" in ack_msg

    # Verify forward-shape: user_text starts with "/btw " and contains the query
    assert captured.get("bridge") is active_bridge
    assert captured.get("user_text", "").startswith("/btw "), (
        f"expected '/btw '-prefixed forward, got: {captured.get('user_text')!r}"
    )
    assert "what time is it" in captured.get("user_text", "")


@pytest.mark.asyncio
async def test_btw_truncates_long_text_in_ack(bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acknowledgement banner truncates text > 200 chars with ellipsis."""
    from clauded.cogs import ops as ops_mod
    from clauded import discord_renderer as dr_mod
    interaction = _make_thread_interaction(bot)
    active_bridge = MagicMock()
    active_bridge.is_active = True
    bot.session_manager._sessions[interaction.channel.id] = active_bridge

    class _NoopRenderer:
        def __init__(self, target):
            pass
        async def render_response(self, bridge, user_text):
            return None
    monkeypatch.setattr(dr_mod, "DiscordRenderer", _NoopRenderer, raising=False)

    long_text = "x" * 300
    await ops_mod.btw_cmd.callback(interaction, long_text)
    ack_msg = interaction.response.send_message.call_args[0][0]
    assert "…" in ack_msg, "long text should be truncated with ellipsis"
    # Verify truncation happens at 200 chars (allows ack_msg to stay short)
    assert "x" * 200 in ack_msg
    assert "x" * 250 not in ack_msg
