"""#186 — /model group: list, current, switch + metadata-rich autocomplete."""
import pytest
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
    bridge = MagicMock(model="claude-sonnet-4-5")
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    assert _current_model_for_thread(bot, 42) == "claude-sonnet-4-5"


def test_current_model_for_thread_none_when_no_session():
    from clauded.cogs.model import _current_model_for_thread
    bot = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)
    assert _current_model_for_thread(bot, 42) is None
    # thread_id None -> short-circuit
    assert _current_model_for_thread(bot, None) is None


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
