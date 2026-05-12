"""Tests for /context (#163 sub-task 3).

Visualizes Claude context-window usage. Two paths:
- Path A: active bridge piggyback (fast, current session state)
- Path B: temp ClaudeSDKClient (slow, model baseline)
"""
from __future__ import annotations

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
# Pure helpers — no SDK interaction
# ---------------------------------------------------------------------------


def test_format_progress_bar_zero_and_full():
    from clauded.cogs.context import _format_progress_bar
    assert _format_progress_bar(0) == "░" * 20
    assert _format_progress_bar(100) == "█" * 20
    assert _format_progress_bar(50) == "█" * 10 + "░" * 10


def test_format_progress_bar_caps_at_100():
    from clauded.cogs.context import _format_progress_bar
    # SDK could report >100 if context overflows; bar should cap.
    assert _format_progress_bar(150) == "█" * 20
    assert _format_progress_bar(-5) == "░" * 20


def test_format_tokens_humanizes_thousands():
    from clauded.cogs.context import _format_tokens
    assert _format_tokens(523) == "523"
    assert _format_tokens(2235) == "2.2k"
    assert _format_tokens(92531) == "92.5k"


def test_build_embed_uses_red_at_90_pct():
    """High context usage → red color to signal urgency."""
    from clauded.cogs.context import _build_context_embed
    from clauded.discord_renderer import COLOR_TOOL_FAILURE
    usage = {
        "totalTokens": 180000, "maxTokens": 200000,
        "percentage": 90.0, "model": "claude-sonnet",
        "categories": [],
    }
    embed = _build_context_embed(usage, "active session")
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "90.0%" in embed.title


def test_build_embed_uses_yellow_at_75_pct():
    """Medium usage → yellow warn."""
    from clauded.cogs.context import _build_context_embed
    usage = {
        "totalTokens": 150000, "maxTokens": 200000,
        "percentage": 75.0, "model": "claude-sonnet",
        "categories": [],
    }
    embed = _build_context_embed(usage, "active session")
    assert embed.color.value == 0xF59E0B  # yellow


def test_build_embed_includes_top_categories():
    """Categories shown in description, sorted by tokens desc, top-5."""
    from clauded.cogs.context import _build_context_embed
    usage = {
        "totalTokens": 50000, "maxTokens": 200000, "percentage": 25.0,
        "model": "claude-sonnet",
        "categories": [
            {"name": "messages", "tokens": 30000},
            {"name": "system", "tokens": 12000},
            {"name": "tools", "tokens": 5000},
            {"name": "memory", "tokens": 2000},
            {"name": "agents", "tokens": 1000},
        ],
    }
    embed = _build_context_embed(usage, "active session")
    cat_field = next((f for f in embed.fields if f.name == "Top categories"), None)
    assert cat_field is not None
    assert "messages" in cat_field.value
    # Sorted by tokens — messages (30k) appears before system (12k)
    assert cat_field.value.find("messages") < cat_field.value.find("system")


def test_build_embed_truncates_to_top_5_categories():
    """7 categories → only top-5 + 'and N more' line."""
    from clauded.cogs.context import _build_context_embed
    usage = {
        "totalTokens": 100, "maxTokens": 1000, "percentage": 10.0,
        "model": "claude-sonnet",
        "categories": [{"name": f"cat{i}", "tokens": 100 - i} for i in range(7)],
    }
    embed = _build_context_embed(usage, "active session")
    cat_field = next((f for f in embed.fields if f.name == "Top categories"), None)
    assert cat_field is not None
    assert "and 2 more" in cat_field.value
    # cat0 has highest tokens, should appear
    assert "cat0" in cat_field.value
    # cat5 should NOT appear (truncated)
    assert "cat5" not in cat_field.value


def test_build_embed_handles_empty_categories():
    """No categories from SDK → embed still renders progress bar."""
    from clauded.cogs.context import _build_context_embed
    usage = {
        "totalTokens": 0, "maxTokens": 200000, "percentage": 0.0,
        "model": "claude-sonnet", "categories": [],
    }
    embed = _build_context_embed(usage, "fresh session")
    assert embed.title == "📊 Context: 0.0%"
    assert "200.0k" in embed.description
    assert len(embed.fields) == 0


# ---------------------------------------------------------------------------
# Cog callback — Path A / Path B / error paths
# ---------------------------------------------------------------------------


@pytest.fixture
def bot(tmp_path: Path) -> ClaudedBot:
    cfg = Config(
        discord_bot_token="tok", claude_model="sonnet",
        claude_permission_mode="default", projects_root=str(tmp_path),
        allow_unbound_fallback=False,
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
    bot.allow_unbound_fallback = False
    bot._connection = MagicMock()
    return bot


def _make_interaction(bot: ClaudedBot, channel_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel_id = channel_id
    # interaction.channel must satisfy resolve_channel_id (TextChannel-like)
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    interaction.channel = ch
    interaction.guild_id = 4242
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_context_path_a_active_bridge(bot: ClaudedBot) -> None:
    """Active bridge present → uses bridge.get_context_usage (no temp client)."""
    from clauded.cogs.context import context_cmd
    thread_id = 12345
    fake_usage = {
        "totalTokens": 45000, "maxTokens": 200000,
        "percentage": 22.5, "model": "claude-sonnet",
        "categories": [{"name": "messages", "tokens": 45000}],
    }
    mock_bridge = MagicMock()
    mock_bridge.is_active = True
    mock_bridge.get_context_usage = AsyncMock(return_value=fake_usage)
    bot.session_manager._sessions[thread_id] = mock_bridge

    interaction = _make_interaction(bot, thread_id)
    await context_cmd.callback(interaction)

    mock_bridge.get_context_usage.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "22.5%" in embed.title
    assert "active session" in embed.description


@pytest.mark.asyncio
async def test_context_path_b_falls_back_when_no_bridge(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No active bridge → spin up temp ClaudeSDKClient."""
    from clauded.cogs import context as ctx_mod
    fake_usage = {
        "totalTokens": 1000, "maxTokens": 200000,
        "percentage": 0.5, "model": "claude-sonnet", "categories": [],
    }

    class FakeTempClient:
        def __init__(self, opts):
            self.opts = opts
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get_context_usage(self):
            return fake_usage
    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_interaction(bot, channel_id=99999)
    await ctx_mod.context_cmd.callback(interaction)

    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "0.5%" in embed.title
    assert "fresh session" in embed.description


@pytest.mark.asyncio
async def test_context_path_a_failure_falls_to_b(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge present but get_context_usage raises → fall through to Path B."""
    from clauded.cogs import context as ctx_mod
    thread_id = 33333
    mock_bridge = MagicMock()
    mock_bridge.is_active = True
    mock_bridge.get_context_usage = AsyncMock(side_effect=RuntimeError("oops"))
    bot.session_manager._sessions[thread_id] = mock_bridge

    fake_usage = {
        "totalTokens": 500, "maxTokens": 100000,
        "percentage": 0.5, "model": "claude-sonnet", "categories": [],
    }

    class FakeTempClient:
        def __init__(self, opts): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get_context_usage(self): return fake_usage
    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_interaction(bot, thread_id)
    await ctx_mod.context_cmd.callback(interaction)

    mock_bridge.get_context_usage.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "fresh session" in embed.description, (
        "fallback to Path B must label source as 'fresh session'"
    )


@pytest.mark.asyncio
async def test_context_path_b_error_returns_red_embed(
    bot: ClaudedBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temp client construction/query fails → red error embed (not crash)."""
    from clauded.cogs import context as ctx_mod
    from clauded.discord_renderer import COLOR_TOOL_FAILURE
    from claude_agent_sdk import CLIConnectionError

    class FakeTempClient:
        def __init__(self, opts): pass
        async def __aenter__(self):
            raise CLIConnectionError("not running")
        async def __aexit__(self, *a): return None
    monkeypatch.setattr(ctx_mod, "ClaudeSDKClient", FakeTempClient)

    interaction = _make_interaction(bot, channel_id=88888)
    await ctx_mod.context_cmd.callback(interaction)
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "Context unavailable" in embed.title
    assert "CLIConnectionError" in embed.description
