"""#186 — /model group: list, current, switch + metadata-rich autocomplete."""
import pytest
import discord
from unittest.mock import AsyncMock, MagicMock


def test_known_models_has_required_metadata():
    """Every entry must carry id + context + tier."""
    from clauded.cogs.model import KNOWN_MODELS
    assert KNOWN_MODELS, "KNOWN_MODELS table must not be empty"
    for alias, info in KNOWN_MODELS.items():
        assert "id" in info, f"{alias} missing 'id'"
        assert "context" in info, f"{alias} missing 'context'"
        assert "tier" in info, f"{alias} missing 'tier'"
        assert isinstance(info["context"], int) and info["context"] > 0
        assert info["tier"] in ("fast", "balanced", "deep"), f"unknown tier: {info['tier']}"


def test_fmt_context_renders_k_and_m():
    from clauded.cogs.model import _fmt_context
    assert _fmt_context(200_000) == "200k"
    assert _fmt_context(1_000_000) == "1M"
    assert _fmt_context(150_000) == "150k"
    assert _fmt_context(500) == "500"
    # defensive
    assert _fmt_context(None) == "?"  # type: ignore[arg-type]
    assert _fmt_context("garbage") == "?"  # type: ignore[arg-type]


def test_current_model_for_thread_returns_bridge_model():
    from clauded.cogs.model import _current_model_for_thread
    bot = MagicMock()
    bridge = MagicMock(model="claude-sonnet-4-6")
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    # #247: helper now takes a channel-like object (not a bare int) so it
    # can introspect for thread→parent fallback. Use a plain MagicMock with
    # ``.id`` set; ``isinstance(channel, discord.Thread)`` is False so
    # no fallback path is exercised here.
    channel = MagicMock(spec=[])
    channel.id = 42
    assert _current_model_for_thread(bot, channel) == "claude-sonnet-4-6"


def test_current_model_for_thread_none_when_no_session():
    from clauded.cogs.model import _current_model_for_thread
    bot = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)
    channel = MagicMock(spec=[])
    channel.id = 42
    assert _current_model_for_thread(bot, channel) is None
    # channel None -> short-circuit
    assert _current_model_for_thread(bot, None) is None


def test_resolve_session_bridge_returns_none_for_no_session():
    """_resolve_session_bridge returns None when no session exists."""
    from clauded.cogs.model import _resolve_session_bridge
    bot = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)
    channel = MagicMock()
    channel.id = 42
    result = _resolve_session_bridge(bot, channel)
    assert result is None
    bot.session_manager.get_session.assert_called_with(42)


@pytest.mark.asyncio
async def test_model_list_with_active_session_marks_current():
    """`/model list` highlights the current model with 🟢 marker."""
    from clauded.cogs.model import model_list
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(
        return_value=MagicMock(model="opus")
    )
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 99
    interaction.response.send_message = AsyncMock()
    await model_list.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "Current" in embed.description
    assert "opus" in embed.description
    assert "🟢" in embed.description, f"Current model marker missing; desc={embed.description!r}"


@pytest.mark.asyncio
async def test_model_list_without_session_shows_unknown_current():
    from clauded.cogs.model import model_list
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=None)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 99
    interaction.response.send_message = AsyncMock()
    await model_list.callback(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "No active session" in embed.description
    # Still shows the model list
    assert "sonnet" in embed.description
    assert "opus" in embed.description


@pytest.mark.asyncio
async def test_model_current_with_session_renders_metadata():
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    # #198: model_current now inspects tier fields directly; simulate
    # a user-explicit /model switch by setting _model_override.
    bridge = MagicMock(model="haiku")
    bridge._model_override = "haiku"
    bridge._sdk_model = None
    bridge._config = MagicMock()
    bridge._config.claude_model = None
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "haiku" in embed.description
    assert "fast" in embed.description  # haiku's tier
    assert "200k" in embed.description  # haiku's context


@pytest.mark.asyncio
async def test_model_current_with_no_session_says_so():
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=None)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)
    call = interaction.response.send_message.await_args
    assert "No active session" in call.args[0] or "No active session" in call.kwargs.get("content", "")
    assert call.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_model_current_unknown_model_shows_id_only():
    """If active model isn't in KNOWN_MODELS (full id / future SKU), still
    display its id rather than 'unknown'."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    # #198: simulate /model switch to an unknown id (override tier).
    bridge = MagicMock(model="claude-future-7-9-xl")
    bridge._model_override = "claude-future-7-9-xl"
    bridge._sdk_model = None
    bridge._config = MagicMock()
    bridge._config.claude_model = None
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "claude-future-7-9-xl" in embed.description
    assert "not in known-models" in embed.description


@pytest.mark.asyncio
async def test_model_switch_autocomplete_includes_metadata():
    from clauded.cogs.model import model_switch_autocomplete
    interaction = MagicMock()
    out = await model_switch_autocomplete(interaction, "")
    assert out  # non-empty
    # Each choice name should include the tier + context
    for c in out:
        assert "—" in c.name  # the em-dash separator
        assert "k" in c.name or "M" in c.name  # context format
    # Value remains the simple alias for backward compat
    aliases = {c.value for c in out}
    assert "sonnet" in aliases
    assert "opus" in aliases


@pytest.mark.asyncio
async def test_model_switch_autocomplete_filters_by_current_input():
    from clauded.cogs.model import model_switch_autocomplete
    interaction = MagicMock()
    out = await model_switch_autocomplete(interaction, "haik")
    aliases = {c.value for c in out}
    assert "haiku" in aliases
    assert "sonnet" not in aliases
    assert "opus" not in aliases


def test_model_group_has_three_subcommands():
    """Pin: the group has exactly switch/list/current."""
    from clauded.cogs.model import model_group
    names = {c.name for c in model_group.commands}
    assert names == {"switch", "list", "current"}, f"Unexpected subcommands: {names}"


@pytest.mark.asyncio
async def test_model_switch_active_session_preserves_context():
    """#273: /model switch on an active bridge uses set_model (SDK runtime
    switch) and does NOT call _recreate_session — context preserved."""
    from clauded.cogs.model import model_switch
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bridge = MagicMock()
    bridge.is_active = True
    bridge.set_model = AsyncMock()
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    bot_spec._recreate_session = AsyncMock()
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await model_switch.callback(interaction, "opus")
    bridge.set_model.assert_awaited_once_with("opus")
    bot_spec._recreate_session.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Context preserved" in embed.description
    assert "context was reset" not in embed.description.lower()


@pytest.mark.asyncio
async def test_model_switch_no_session_falls_back_to_recreate():
    """#273: when no active session exists, /model switch falls back to
    _recreate_session (legacy create path)."""
    from clauded.cogs.model import model_switch
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=None)
    recreated_bridge = MagicMock()
    bot_spec._recreate_session = AsyncMock(return_value=recreated_bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await model_switch.callback(interaction, "haiku")
    bot_spec._recreate_session.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_switch_inactive_bridge_falls_back_to_recreate():
    """#273: when bridge exists but is_active is False, fall back to recreate."""
    from clauded.cogs.model import model_switch
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    bridge = MagicMock()
    bridge.is_active = False
    bridge.set_model = AsyncMock()
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    bot_spec._recreate_session = AsyncMock(return_value=MagicMock())
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await model_switch.callback(interaction, "sonnet")
    bridge.set_model.assert_not_called()
    bot_spec._recreate_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_list_pre_first_turn_shows_unset():
    """#247 AC5/AC6: bridge exists but model=None (pre-first-turn) →
    /model list shows '(unset)' not 'No active session'."""
    from clauded.cogs.model import model_list
    from clauded.bot import ClaudedBot
    bot_spec = MagicMock(spec=ClaudedBot)
    # Bridge exists but model is None (pre-first-turn)
    bridge = MagicMock()
    bridge.model = None
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 42
    interaction.response.send_message = AsyncMock()
    await model_list.callback(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "unset" in embed.description.lower(), (
        f"Pre-first-turn should show 'unset', got: {embed.description!r}"
    )
    assert "No active session" not in embed.description, (
        f"Pre-first-turn should NOT say 'No active session': {embed.description!r}"
    )
