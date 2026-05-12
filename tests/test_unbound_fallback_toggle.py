"""Tests for /unbound-fallback runtime toggle (PR #162).

Real user request 2026-05-12: edit-plist-and-restart workflow is too heavy for
operators who want to flip the security knob ad-hoc. Add slash command that
mutates ``bot.allow_unbound_fallback`` at runtime (no persist).
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
    """Minimal ClaudedBot stub — same shape as test_bot_unbound_message.py."""
    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root=str(tmp_path),
        allow_unbound_fallback=False,  # start OFF per v1.11 default
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
    bot.allow_unbound_fallback = cfg.allow_unbound_fallback
    bot._connection = MagicMock()
    return bot


def _make_interaction(bot: ClaudedBot, is_admin: bool = True) -> MagicMock:
    """Build an interaction with admin guild_permissions by default.

    R1 security #1+#4: callback-side admin re-check. Tests must provide a
    ``user.guild_permissions.administrator`` attribute matching the test
    scenario; default True for happy-path tests.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.guild_id = 1234
    interaction.channel_id = 5678
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    fake_user = MagicMock()
    fake_user.id = 999
    fake_user.name = "alice"
    fake_user.guild_permissions = MagicMock(administrator=is_admin)
    interaction.user = fake_user
    return interaction


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_on(bot: ClaudedBot) -> None:
    """/unbound-fallback True flips ``bot.allow_unbound_fallback`` ON."""
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    assert bot.allow_unbound_fallback is False
    await unbound_fallback_toggle.callback(interaction, True)
    assert bot.allow_unbound_fallback is True
    # Config stays frozen — only the mutable bot attr changed.
    assert bot.config.allow_unbound_fallback is False
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "ON" in sent_embed.title


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_off(bot: ClaudedBot) -> None:
    """/unbound-fallback False flips bot attr OFF."""
    from clauded.cogs.ops import unbound_fallback_toggle
    bot.allow_unbound_fallback = True
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, False)
    assert bot.allow_unbound_fallback is False
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "OFF" in sent_embed.title


@pytest.mark.asyncio
async def test_unbound_fallback_config_remains_frozen(bot: ClaudedBot) -> None:
    """Config stays frozen across toggles; only the mutable bot attribute moves.

    Regression pin for the v1.11 immutability invariant (PRD #110 sec-1
    contract is per-process; Config is the immutable env-snapshot, bot attr
    is the runtime mirror).
    """
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, True)
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        bot.config.allow_unbound_fallback = True  # type: ignore[misc]


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_clears_refused_set(bot: ClaudedBot) -> None:
    """Toggling resets _refused_unbound_channels so the hint surfaces again."""
    from clauded.cogs.ops import unbound_fallback_toggle
    bot.project_manager._refused_unbound_channels.add(11111)
    bot.project_manager._refused_unbound_channels.add(22222)
    assert len(bot.project_manager._refused_unbound_channels) == 2
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, True)
    assert bot.project_manager._refused_unbound_channels == set()


@pytest.mark.asyncio
async def test_unbound_fallback_response_is_ephemeral(bot: ClaudedBot) -> None:
    """Response is ephemeral (admin command — output not in channel)."""
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, True)
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_unbound_fallback_response_mentions_env_persistence(bot: ClaudedBot) -> None:
    """Embed description tells operator how to make it stick across restart."""
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, True)
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "CLAUDED_ALLOW_UNBOUND_FALLBACK" in sent_embed.description
    assert "restart" in sent_embed.description.lower()


@pytest.mark.asyncio
async def test_unbound_fallback_rejects_non_admin(bot: ClaudedBot) -> None:
    """R1 security #1+#4: callback-side admin re-check rejects non-admin
    even when Discord-UI ``default_permissions`` was overridden by a server
    admin to grant the command to a non-admin role."""
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot, is_admin=False)
    await unbound_fallback_toggle.callback(interaction, True)
    # State NOT changed
    assert bot.allow_unbound_fallback is False
    # Friendly error sent, NOT the success embed
    call_args = interaction.response.send_message.call_args
    sent = call_args[0][0] if call_args[0] else call_args.kwargs.get("content", "")
    assert "Administrator permission required" in sent


@pytest.mark.asyncio
async def test_unbound_fallback_logs_security_event(
    bot: ClaudedBot, caplog: pytest.LogCaptureFixture
) -> None:
    """R1 security #5 blocking: every flip of allow_unbound_fallback must
    emit a WARNING-level audit log line with WHO/WHERE/PREV/NEW so operators
    can reconstruct policy changes post-hoc."""
    import logging
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    caplog.set_level(logging.WARNING, logger="clauded.bot")
    await unbound_fallback_toggle.callback(interaction, True)
    audit_logs = [
        r for r in caplog.records
        if "SECURITY: allow_unbound_fallback" in r.getMessage()
    ]
    assert audit_logs, (
        f"Expected SECURITY audit log entry; got: {[r.getMessage() for r in caplog.records]!r}"
    )
    msg = audit_logs[0].getMessage()
    # Verify shape: prev → new, user id, guild, channel
    assert "False -> True" in msg or "False" in msg and "True" in msg
    assert "user=alice" in msg
    assert "id=999" in msg
    assert "guild=1234" in msg
    assert "channel=5678" in msg
