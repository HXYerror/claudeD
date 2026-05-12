"""Tests for /unbound-fallback runtime toggle (PR #160).

Real user request 2026-05-12: edit-plist-and-restart workflow is too heavy for
operators who want to flip the security knob ad-hoc. Add slash command that
mutates ``bot.config.allow_unbound_fallback`` at runtime (no persist).
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
    bot._allow_unbound_fallback_runtime = None
    bot._connection = MagicMock()
    return bot


def _make_interaction(bot: ClaudedBot) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_on(bot: ClaudedBot) -> None:
    """/unbound-fallback True flips effective ``bot.allow_unbound_fallback`` ON."""
    from clauded.cogs.ops import unbound_fallback_toggle
    interaction = _make_interaction(bot)
    # Baseline: Config frozen-default False, runtime override None.
    assert bot.allow_unbound_fallback is False
    assert bot._allow_unbound_fallback_runtime is None
    await unbound_fallback_toggle.callback(interaction, True)
    assert bot._allow_unbound_fallback_runtime is True
    assert bot.allow_unbound_fallback is True
    # Config itself stays frozen — only the runtime override mutated.
    assert bot.config.allow_unbound_fallback is False
    interaction.response.send_message.assert_awaited_once()
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "ON" in sent_embed.title


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_off(bot: ClaudedBot) -> None:
    """/unbound-fallback False flips override OFF."""
    from clauded.cogs.ops import unbound_fallback_toggle
    bot._allow_unbound_fallback_runtime = True  # start ON
    interaction = _make_interaction(bot)
    await unbound_fallback_toggle.callback(interaction, False)
    assert bot._allow_unbound_fallback_runtime is False
    assert bot.allow_unbound_fallback is False
    sent_embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "OFF" in sent_embed.title


@pytest.mark.asyncio
async def test_unbound_fallback_property_respects_config_default(bot: ClaudedBot) -> None:
    """When no runtime override, ``bot.allow_unbound_fallback`` mirrors
    ``Config.allow_unbound_fallback``. Pin the chain so future refactor
    of Config field doesn't accidentally bypass the property."""
    # Config is frozen — can't reassign field. Build a new Config with True.
    from clauded.config import Config
    bot.config = Config(
        discord_bot_token="tok", claude_model="sonnet",
        claude_permission_mode="default",
        projects_root=bot.config.projects_root,
        allow_unbound_fallback=True,
    )
    bot._allow_unbound_fallback_runtime = None
    assert bot.allow_unbound_fallback is True
    # Override takes precedence:
    bot._allow_unbound_fallback_runtime = False
    assert bot.allow_unbound_fallback is False


@pytest.mark.asyncio
async def test_unbound_fallback_toggle_clears_refused_set(bot: ClaudedBot) -> None:
    """Toggling resets _refused_unbound_channels so the hint surfaces again
    under the new policy (otherwise a once-shown channel stays silent forever)."""
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
