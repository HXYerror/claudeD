"""#211 — `/mode` group + bridge runtime switch + persistence + footer.

Pins the contract from ``docs/prd/v1.18-permission-mode-cmd.md``:

- ``_CYCLE_ORDER`` + ``MODE_EMOJI`` shape (decisions #5, #6).
- ``_next_mode`` cycle correctness + invalid-current fallback.
- ``effective_permission_mode`` 3-tier resolution (override > env > default).
- ``/mode set`` calls ``bridge.set_permission_mode`` + persists.
- ``/mode cycle`` 5-step walk back to ``default``.
- ``/mode current`` source-tag matrix.
- Persistence end-to-end (write + auto-resume read + /session resume read).
- Footer second-line presence/absence by mode.
- ``/health`` + ``/session info`` show the mode.
- SDK contract pin: cycle modes ⊂ SDK ``PermissionMode`` Literal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(perm_mode: str = "default") -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model=None,
        claude_permission_mode=perm_mode,
        projects_root="/tmp",
    )


def _bridge(
    *, override: str | None = None, env_mode: str = "default", active: bool = True
) -> Any:
    """Build a MagicMock bridge with the requested tier state."""
    bridge = MagicMock()
    bridge._permission_mode_override = override
    cfg = MagicMock()
    cfg.claude_permission_mode = env_mode
    bridge._config = cfg
    bridge.is_active = active
    bridge.project_path = "/tmp/proj"
    bridge.num_turns = 1
    bridge.total_cost = 0.0
    # Mirror the real bridge property semantics:
    bridge.permission_mode_override = override
    bridge.effective_permission_mode = override or env_mode or "default"
    bridge.set_permission_mode = AsyncMock()
    return bridge


# ---------------------------------------------------------------------------
# S1 — _CYCLE_ORDER + MODE_EMOJI shape pins.
# ---------------------------------------------------------------------------


def test_cycle_order_has_exactly_four_modes() -> None:
    """Decision #5: 4 surfaced modes only. ``dontAsk`` / ``auto`` not in list."""
    from clauded.cogs.mode import _CYCLE_ORDER

    assert _CYCLE_ORDER == [
        "default", "acceptEdits", "plan", "bypassPermissions",
    ], f"_CYCLE_ORDER drifted: {_CYCLE_ORDER}"


def test_mode_emoji_has_exactly_three_non_default_entries() -> None:
    """Decision #2: ``default`` has NO emoji entry (footer skips when default)."""
    from clauded.cogs.mode import MODE_EMOJI

    assert set(MODE_EMOJI.keys()) == {"acceptEdits", "plan", "bypassPermissions"}, (
        f"MODE_EMOJI keys drifted: {set(MODE_EMOJI.keys())}"
    )
    assert "default" not in MODE_EMOJI


def test_permission_mode_literals_match_sdk_contract() -> None:
    """Mirror of #193's literal-pin pattern: the modes we surface must be a
    subset of the SDK's PermissionMode Literal so a future SDK rename breaks
    loudly here rather than silently mis-routing tool calls."""
    from clauded.cogs.mode import _CYCLE_ORDER, MODE_EMOJI
    from claude_agent_sdk.types import PermissionMode
    import typing

    sdk_modes = set(typing.get_args(PermissionMode))
    cycle_modes = set(_CYCLE_ORDER)
    assert cycle_modes.issubset(sdk_modes), (
        f"_CYCLE_ORDER has modes not in SDK PermissionMode Literal: "
        f"{cycle_modes - sdk_modes}"
    )
    emoji_modes = set(MODE_EMOJI.keys())
    assert emoji_modes.issubset(sdk_modes), (
        f"MODE_EMOJI has keys not in SDK PermissionMode Literal: "
        f"{emoji_modes - sdk_modes}"
    )


# ---------------------------------------------------------------------------
# S1 — _next_mode pure-function tests.
# ---------------------------------------------------------------------------


def test_next_mode_walks_cycle_order() -> None:
    from clauded.cogs.mode import _next_mode

    assert _next_mode("default") == "acceptEdits"
    assert _next_mode("acceptEdits") == "plan"
    assert _next_mode("plan") == "bypassPermissions"
    assert _next_mode("bypassPermissions") == "default"


def test_next_mode_unknown_input_snaps_to_default() -> None:
    """``dontAsk``/``auto``/typos → snap to first element of cycle."""
    from clauded.cogs.mode import _next_mode

    assert _next_mode("dontAsk") == "default"
    assert _next_mode("auto") == "default"
    assert _next_mode("") == "default"
    assert _next_mode("garbage") == "default"


# ---------------------------------------------------------------------------
# S1 — _mode_source_for_bridge tier dispatch.
# ---------------------------------------------------------------------------


def test_mode_source_override_wins() -> None:
    from clauded.cogs.mode import _mode_source_for_bridge

    bridge = _bridge(override="plan", env_mode="acceptEdits")
    src, val = _mode_source_for_bridge(bridge)
    assert src == "override"
    assert val == "plan"


def test_mode_source_env_when_no_override() -> None:
    from clauded.cogs.mode import _mode_source_for_bridge

    bridge = _bridge(override=None, env_mode="acceptEdits")
    src, val = _mode_source_for_bridge(bridge)
    assert src == "env"
    assert val == "acceptEdits"


def test_mode_source_default_when_env_is_default() -> None:
    """``CLAUDE_PERMISSION_MODE`` unset → config has ``"default"`` →
    source is ``default``, not ``env``."""
    from clauded.cogs.mode import _mode_source_for_bridge

    bridge = _bridge(override=None, env_mode="default")
    src, val = _mode_source_for_bridge(bridge)
    assert src == "default"
    assert val == "default"


# ---------------------------------------------------------------------------
# S2 — bridge.effective_permission_mode + set_permission_mode + accessors.
# ---------------------------------------------------------------------------


def test_bridge_effective_permission_mode_resolution() -> None:
    """Real ClaudeBridge tier semantics: override > config > 'default'."""
    from clauded.claude_bridge import ClaudeBridge

    # Case 1: override set — wins regardless of config
    sc = SessionConfig(permission_mode_override="plan")
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="default"), session_config=sc,
    )
    assert bridge.effective_permission_mode == "plan"

    # Case 2: no override, env-set config
    sc = SessionConfig()
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="acceptEdits"),
        session_config=sc,
    )
    assert bridge.effective_permission_mode == "acceptEdits"

    # Case 3: nothing set anywhere → "default"
    sc = SessionConfig()
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="default"),
        session_config=sc,
    )
    assert bridge.effective_permission_mode == "default"


def test_bridge_permission_mode_override_accessor_returns_explicit_only() -> None:
    """Public accessor must return ONLY the explicit override (None when
    user hasn't set one), parallel to ``explicit_model_override``. This is
    what ``save_session_state`` reads for persistence."""
    from clauded.claude_bridge import ClaudeBridge

    sc = SessionConfig()
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="plan"), session_config=sc,
    )
    # Env says "plan" but user hasn't run /mode set → accessor returns None
    assert bridge.permission_mode_override is None
    # But effective is still "plan" (env tier)
    assert bridge.effective_permission_mode == "plan"


@pytest.mark.asyncio
async def test_bridge_set_permission_mode_calls_sdk_and_persists() -> None:
    from clauded.claude_bridge import ClaudeBridge

    sc = SessionConfig()
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(), session_config=sc,
    )
    fake_client = MagicMock()
    fake_client.set_permission_mode = AsyncMock()
    bridge._client = fake_client
    bridge._active = True

    await bridge.set_permission_mode("plan")

    fake_client.set_permission_mode.assert_awaited_once_with("plan")
    # Persisted to override field
    assert bridge._permission_mode_override == "plan"
    assert bridge.permission_mode_override == "plan"
    assert bridge.effective_permission_mode == "plan"


@pytest.mark.asyncio
async def test_bridge_set_permission_mode_rejected_when_inactive() -> None:
    """SDK rejection must not silently lie about the active mode."""
    from clauded.claude_bridge import ClaudeBridge

    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(), session_config=SessionConfig(),
    )
    # No _client → not active
    bridge._client = None
    bridge._active = False
    with pytest.raises(RuntimeError):
        await bridge.set_permission_mode("plan")
    # Override untouched
    assert bridge._permission_mode_override is None


@pytest.mark.asyncio
async def test_bridge_set_permission_mode_sdk_error_keeps_old_override() -> None:
    """If the SDK call raises, the override field must NOT advance — otherwise
    the footer would lie about the active mode."""
    from clauded.claude_bridge import ClaudeBridge

    sc = SessionConfig(permission_mode_override="acceptEdits")
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(), session_config=sc,
    )
    fake_client = MagicMock()
    fake_client.set_permission_mode = AsyncMock(side_effect=ValueError("nope"))
    bridge._client = fake_client
    bridge._active = True

    with pytest.raises(ValueError):
        await bridge.set_permission_mode("plan")
    # Override stays at the previous value
    assert bridge._permission_mode_override == "acceptEdits"


# ---------------------------------------------------------------------------
# S2 — bridge.start() passes effective mode to SDK.
# ---------------------------------------------------------------------------


class _FakeSDKClient:
    captured_options: list[Any] = []

    def __init__(self, options: Any = None) -> None:
        type(self).captured_options.append(options)

    async def connect(self, prompt: Any = None) -> None:
        pass

    async def disconnect(self) -> None:
        pass


@pytest.fixture
def _reset_sdk_capture() -> None:
    _FakeSDKClient.captured_options = []


@pytest.mark.asyncio
async def test_bridge_start_passes_override_to_sdk(
    monkeypatch: pytest.MonkeyPatch, _reset_sdk_capture: None
) -> None:
    """Persisted override → ClaudeAgentOptions(permission_mode=<override>)."""
    from clauded.claude_bridge import ClaudeBridge

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeSDKClient)
    sc = SessionConfig(permission_mode_override="plan")
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="default"), session_config=sc,
    )
    await bridge.start()
    assert _FakeSDKClient.captured_options
    assert _FakeSDKClient.captured_options[-1].permission_mode == "plan"


@pytest.mark.asyncio
async def test_bridge_start_falls_back_to_config_when_no_override(
    monkeypatch: pytest.MonkeyPatch, _reset_sdk_capture: None
) -> None:
    """No override → config env tier (or default) flows to SDK."""
    from clauded.claude_bridge import ClaudeBridge

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeSDKClient)
    sc = SessionConfig()  # no override
    bridge = ClaudeBridge(
        project_path="/tmp", config=_config(perm_mode="acceptEdits"),
        session_config=sc,
    )
    await bridge.start()
    assert _FakeSDKClient.captured_options[-1].permission_mode == "acceptEdits"


# ---------------------------------------------------------------------------
# S3 — persistence: write + read paths.
# ---------------------------------------------------------------------------


def test_session_store_round_trips_permission_mode_override(tmp_path: Path) -> None:
    """SessionStore.save_session writes the new field; get reads it back."""
    from clauded.session_store import SessionStore

    store = SessionStore(data_dir=str(tmp_path))
    store.save_session(
        thread_id=42, session_id="sess-1", project_path="/tmp/p",
        model=None, system_prompt="",
        permission_mode_override="bypassPermissions",
    )
    info = store.get_session_info(42)
    assert info is not None
    assert info["permission_mode_override"] == "bypassPermissions"


def test_session_store_legacy_row_without_field_returns_none(
    tmp_path: Path,
) -> None:
    """Pre-#211 rows lack the field → ``stored.get("permission_mode_override")``
    returns None (no crash, no KeyError)."""
    from clauded.session_store import SessionStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy = {
        "1": {
            "session_id": "old",
            "project_path": "/tmp",
            "model": None,
            "system_prompt": "",
            "last_active": "x",
            # NOTE: no permission_mode_override field — legacy row
        }
    }
    (data_dir / "sessions.json").write_text(json.dumps(legacy))
    store = SessionStore(data_dir=str(data_dir))
    info = store.get_session_info(1)
    assert info is not None
    # .get returns None for the missing key (safe-defaulted by callers)
    assert info.get("permission_mode_override") is None


def test_save_session_state_persists_explicit_override() -> None:
    """SessionManager.save_session_state passes bridge.permission_mode_override
    through to the store. Explicit-override case."""
    from clauded.session_manager import SessionManager

    bridge = MagicMock()
    bridge.session_id = "sess"
    bridge.project_path = "/tmp"
    bridge.system_prompt = ""
    bridge.permission_mode_override = "plan"

    sm = SessionManager(MagicMock())
    sm._sessions[1] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, project_path, *, model=None,
                   system_prompt=None, permission_mode_override=None):
        captured["permission_mode_override"] = permission_mode_override
        captured["model"] = model

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(1)
    assert captured["permission_mode_override"] == "plan"
    # #211 doesn't change the #210 model=None invariant
    assert captured["model"] is None


def test_save_session_state_persists_none_when_no_user_override() -> None:
    """User has never run /mode set → store gets None."""
    from clauded.session_manager import SessionManager

    bridge = MagicMock()
    bridge.session_id = "sess"
    bridge.project_path = "/tmp"
    bridge.system_prompt = ""
    bridge.permission_mode_override = None  # never set

    sm = SessionManager(MagicMock())
    sm._sessions[2] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, project_path, *, model=None,
                   system_prompt=None, permission_mode_override=None):
        captured["permission_mode_override"] = permission_mode_override

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(2)
    assert captured["permission_mode_override"] is None


# ---------------------------------------------------------------------------
# S3 — read path: auto-resume threads the field into SessionConfig.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_resume_threads_permission_mode_override() -> None:
    """``bot._handle_thread_message`` resume block must inject
    ``stored.get("permission_mode_override")`` into SessionConfig so the
    next bridge inherits the user's persisted choice."""
    import discord
    from clauded.bot import ClaudedBot

    stored = {
        "session_id": "sess-old",
        "project_path": "/tmp",
        "model": None,
        "system_prompt": "",
        "permission_mode_override": "bypassPermissions",
        "last_active": "x",
    }

    bot = MagicMock()
    bot._user = MagicMock(id=42, name="ClaudeBot")
    bot._user.name = "ClaudeBot"
    bot.user = bot._user
    bot.allow_unbound_fallback = False
    bot.config = _config(perm_mode="default")
    bot.project_manager = MagicMock()
    bot.project_manager.is_bound = MagicMock(return_value=True)
    bot.project_manager.should_refuse_unbound = MagicMock(return_value=False)
    bot.project_manager.get_path_or_default = MagicMock(
        return_value=(Path("/tmp"), True)
    )
    bot.project_manager.get_system_prompt = MagicMock(return_value="")
    bot.project_manager.get_extra_dirs = MagicMock(return_value=[])
    bot.project_manager.get_mcp_servers = MagicMock(return_value=None)
    bot.project_manager.get_env = MagicMock(return_value=None)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)
    bot.session_manager.get_stored_session = MagicMock(return_value=stored)
    bot.session_manager.create_session = AsyncMock()
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(),
    ))
    bot._logged_third_party_thread = set()
    bot._pre_tool_notifications = False
    bot._notify_enabled = {}
    bot._compose_user_text = AsyncMock(return_value=("hi", None))

    msg = MagicMock()

    class _FakeThread(discord.Thread):
        def __init__(self) -> None:
            pass

    thread = _FakeThread()
    object.__setattr__(thread, "id", 99)
    object.__setattr__(thread, "owner_id", 42)
    object.__setattr__(thread, "parent_id", 1234)
    msg.channel = thread
    msg.id = 1
    msg.content = "hi"
    msg.author = MagicMock(id=1)
    msg.author.__str__ = MagicMock(return_value="alice")
    msg.mentions = []
    msg.role_mentions = []
    msg.reply = AsyncMock()

    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass

    assert bot.session_manager.create_session.await_count >= 1
    sc = bot.session_manager.create_session.await_args_list[0].args[3]
    assert isinstance(sc, SessionConfig)
    assert sc.permission_mode_override == "bypassPermissions", (
        f"#211 read path: persisted permission_mode_override did NOT thread "
        f"through to SessionConfig; got {sc.permission_mode_override!r}"
    )


@pytest.mark.asyncio
async def test_session_resume_threads_permission_mode_override() -> None:
    """/session resume must inject ``stored.get("permission_mode_override")``."""
    from clauded.cogs.session import session_resume
    from clauded.bot import ClaudedBot

    stored = {
        "session_id": "sess-old",
        "project_path": "/tmp",
        "model": None,
        "system_prompt": "",
        "permission_mode_override": "plan",
    }

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_stored_session = MagicMock(return_value=stored)
    bot.session_manager.stop_session = AsyncMock()
    bot.session_manager.create_session = AsyncMock()
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(),
    ))
    bot.config = _config()

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel_id = 99
    interaction.channel = MagicMock()
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    import clauded.cogs.session as session_mod
    session_mod.reject_if_unbound = AsyncMock(return_value=False)

    await session_resume.callback(interaction)
    assert bot.session_manager.create_session.await_count == 1
    sc = bot.session_manager.create_session.await_args.args[3]
    assert sc.permission_mode_override == "plan"


# ---------------------------------------------------------------------------
# S1/S5 — /mode set + /mode cycle + /mode current command callbacks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_set_calls_bridge_and_persists() -> None:
    from clauded.cogs.mode import mode_set
    from clauded.bot import ClaudedBot
    import discord

    bridge = _bridge(override=None, env_mode="default")
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.session_manager.save_session_state = MagicMock()

    interaction = MagicMock()
    interaction.client = bot
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.response.send_message = AsyncMock()

    choice = discord.app_commands.Choice(name="plan 🔒", value="plan")
    await mode_set.callback(interaction, choice)

    bridge.set_permission_mode.assert_awaited_once_with("plan")
    bot.session_manager.save_session_state.assert_called_once_with(1)
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs.get("ephemeral") is True
    embed = kwargs.get("embed")
    assert embed is not None
    assert "plan" in (embed.title or "")


@pytest.mark.asyncio
async def test_mode_set_no_session_replies_with_hint() -> None:
    from clauded.cogs.mode import mode_set
    from clauded.bot import ClaudedBot
    import discord

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=None)

    interaction = MagicMock()
    interaction.client = bot
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.response.send_message = AsyncMock()

    choice = discord.app_commands.Choice(name="plan", value="plan")
    await mode_set.callback(interaction, choice)
    args = interaction.response.send_message.await_args
    msg = args.args[0]
    assert "No active session" in msg
    assert args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_mode_cycle_five_steps_returns_to_default() -> None:
    """5 successive cycles from ``default`` → all 4 modes + back to ``default``."""
    from clauded.cogs.mode import mode_cycle
    from clauded.bot import ClaudedBot

    # We simulate a bridge whose effective mode advances each call.
    state = {"mode": "default"}

    async def _set_pm(mode: str) -> None:
        state["mode"] = mode

    bridge = MagicMock()
    bridge.is_active = True
    bridge.set_permission_mode = AsyncMock(side_effect=_set_pm)
    type(bridge).effective_permission_mode = property(
        lambda self: state["mode"]
    )

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.session_manager.save_session_state = MagicMock()

    interaction = MagicMock()
    interaction.client = bot
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    import discord as _discord
    interaction.channel = MagicMock(spec=_discord.Thread)
    interaction.channel.id = 7
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.response.send_message = AsyncMock()

    expected_sequence = [
        "acceptEdits", "plan", "bypassPermissions", "default", "acceptEdits",
    ]
    for expected in expected_sequence:
        await mode_cycle.callback(interaction)
        assert state["mode"] == expected, (
            f"cycle drift: expected {expected!r}, got {state['mode']!r}"
        )
    # 5 SDK calls, 5 persists
    assert bridge.set_permission_mode.await_count == 5
    assert bot.session_manager.save_session_state.call_count == 5


@pytest.mark.asyncio
async def test_mode_current_displays_source_override() -> None:
    from clauded.cogs.mode import mode_current
    from clauded.bot import ClaudedBot
    import discord

    bridge = _bridge(override="plan", env_mode="default")
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await mode_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "plan" in embed.description
    assert "override" in embed.description


@pytest.mark.asyncio
async def test_mode_current_displays_source_env() -> None:
    from clauded.cogs.mode import mode_current
    from clauded.bot import ClaudedBot
    import discord

    bridge = _bridge(override=None, env_mode="acceptEdits")
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await mode_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "acceptEdits" in embed.description
    assert "env" in embed.description


@pytest.mark.asyncio
async def test_mode_current_displays_source_default() -> None:
    from clauded.cogs.mode import mode_current
    from clauded.bot import ClaudedBot
    import discord

    bridge = _bridge(override=None, env_mode="default")
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await mode_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "default" in embed.description


def test_mode_group_has_three_subcommands() -> None:
    """Pin: the group has exactly set/cycle/current (per PRD §Goals)."""
    from clauded.cogs.mode import mode_group

    names = {c.name for c in mode_group.commands}
    assert names == {"set", "cycle", "current"}, f"Unexpected: {names}"


# ---------------------------------------------------------------------------
# S4 — footer renders mode line ONLY when non-default.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_includes_mode_line_when_non_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a minimal render that yields a single ResultMessage, then
    inspect the final ``_safe_edit`` call's content — it must contain
    a second ``-#`` line with the mode emoji + name."""
    from clauded.discord_renderer import DiscordRenderer
    from clauded.claude_bridge import ResultMessage

    target = MagicMock()
    target.send = AsyncMock(return_value=MagicMock())
    renderer = DiscordRenderer(target)
    renderer._safe_edit = AsyncMock(return_value=True)
    renderer._safe_send = AsyncMock(return_value=MagicMock())

    # Simulate a "no text body" turn where there's no live cursor; the
    # footer takes the standalone-send path. We just want to verify the
    # ``-# 🔒 plan`` line is present in whatever the renderer sends.
    bridge = MagicMock()
    bridge.effective_permission_mode = "plan"

    # Synthesize a minimal ResultMessage
    result = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=500,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.005,
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    async def _events(_):
        yield result

    bridge.send_message = _events
    # Drive the renderer
    await renderer.render_response(bridge, "hello")
    # Find the footer in the sent messages
    sent_content_blobs: list[str] = []
    for call in renderer._safe_send.await_args_list:
        sent_content_blobs.append(call.kwargs.get("content", ""))
    for call in renderer._safe_edit.await_args_list:
        sent_content_blobs.append(call.kwargs.get("content", ""))
    joined = "\n".join(sent_content_blobs)
    assert "🔒 plan" in joined, (
        f"#211 footer: expected '🔒 plan' line in non-default mode; got:\n{joined!r}"
    )


@pytest.mark.asyncio
async def test_footer_omits_mode_line_when_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode → no second footer line, no mode emoji anywhere."""
    from clauded.discord_renderer import DiscordRenderer
    from clauded.claude_bridge import ResultMessage

    target = MagicMock()
    target.send = AsyncMock(return_value=MagicMock())
    renderer = DiscordRenderer(target)
    renderer._safe_edit = AsyncMock(return_value=True)
    renderer._safe_send = AsyncMock(return_value=MagicMock())

    bridge = MagicMock()
    bridge.effective_permission_mode = "default"

    result = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=500,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.005,
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    async def _events(_):
        yield result

    bridge.send_message = _events
    await renderer.render_response(bridge, "hello")
    sent_content_blobs: list[str] = []
    for call in renderer._safe_send.await_args_list:
        sent_content_blobs.append(call.kwargs.get("content", ""))
    for call in renderer._safe_edit.await_args_list:
        sent_content_blobs.append(call.kwargs.get("content", ""))
    joined = "\n".join(sent_content_blobs)
    # None of the 3 non-default emojis should appear:
    assert "🔒" not in joined
    assert "✏️" not in joined
    assert "⚡" not in joined


# ---------------------------------------------------------------------------
# S5 — /health + /session info include the mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_displays_permission_mode_when_session_active() -> None:
    from clauded.cogs.ops import health_check
    from clauded.bot import ClaudedBot
    import discord

    bridge = _bridge(override="plan", env_mode="default")
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.list_sessions = MagicMock(return_value={1: bridge})
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.project_manager = MagicMock()
    bot.project_manager._projects = {}
    bot._start_time = 0
    bot._claude_version = "1.0"

    interaction = MagicMock()
    interaction.client = bot
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await health_check.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    # Find the Permission mode field
    field_names = [f.name for f in embed.fields]
    assert "Permission mode" in field_names, (
        f"#211: /health embed missing 'Permission mode' field; got {field_names}"
    )
    mode_field = next(f for f in embed.fields if f.name == "Permission mode")
    assert "plan" in mode_field.value
    assert "override" in mode_field.value


@pytest.mark.asyncio
async def test_session_info_displays_permission_mode() -> None:
    from clauded.cogs.session import session_info
    from clauded.bot import ClaudedBot

    bridge = _bridge(override=None, env_mode="acceptEdits")
    # session_info reads model-side fields too — set them defensively
    bridge._model_override = None
    bridge._sdk_model = None
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.config = _config(perm_mode="acceptEdits")

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel_id = 1
    interaction.response.send_message = AsyncMock()
    await session_info.callback(interaction)

    body = interaction.response.send_message.await_args.args[0]
    assert "Mode:" in body
    assert "acceptEdits" in body
    assert "env" in body


# ---------------------------------------------------------------------------
# R1 architect — persistence-dichotomy user-visible labels (PR #221 R1)
# ---------------------------------------------------------------------------


def test_format_mode_display_includes_persisted_lifetime_for_override():
    """R1 architect important #1: `/mode current` source label must
    surface the persistence semantic so users don't experience the
    /mode-persistent vs /model-ephemeral asymmetry as a bug."""
    from clauded.cogs.mode import _format_mode_display
    out = _format_mode_display("plan", "override")
    assert "persisted" in out, (
        f"override source should be labeled 'persisted'; got: {out!r}"
    )
    # Mode + emoji + source all present
    assert "plan" in out
    assert "🔒" in out


def test_format_mode_display_includes_env_pinned_for_env():
    from clauded.cogs.mode import _format_mode_display
    out = _format_mode_display("acceptEdits", "env")
    assert "env-pinned" in out, (
        f"env source should be labeled 'env-pinned'; got: {out!r}"
    )


def test_format_mode_display_includes_cli_default_for_default():
    from clauded.cogs.mode import _format_mode_display
    out = _format_mode_display("default", "default")
    assert "CLI default" in out, (
        f"default source should be labeled 'CLI default'; got: {out!r}"
    )


def test_mode_emoji_imports_cleanly_from_discord_renderer():
    """R1 architect important #2: cyclic import resolved. MODE_EMOJI
    lives in `discord_renderer.py` (rendering concern); `cogs/mode.py`
    imports it back without a lazy fallback. Pin the import shape
    against accidental re-introduction of the cycle."""
    # Both sides reference the SAME dict object (no aliasing surprise).
    from clauded.discord_renderer import MODE_EMOJI as renderer_emoji
    from clauded.cogs.mode import MODE_EMOJI as cog_emoji
    assert renderer_emoji is cog_emoji
    # Exactly 3 entries, default deliberately excluded
    assert set(renderer_emoji.keys()) == {"acceptEdits", "plan", "bypassPermissions"}


# ---------------------------------------------------------------------------
# R1 security HIGH — admin gate + audit log (PR #221 R1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_set_refuses_non_admin_caller():
    """R1 security HIGH: /mode set must refuse callers without
    administrator permission. Before the fix, any guild member could
    `/mode set bypassPermissions` and persist the elevation across
    bot restart.
    """
    from clauded.cogs.mode import mode_set
    from clauded.bot import ClaudedBot
    from unittest.mock import MagicMock, AsyncMock
    import discord

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock()  # never reached
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    # Non-admin caller: guild_permissions.administrator = False
    interaction.user.guild_permissions = MagicMock(administrator=False)
    interaction.response.send_message = AsyncMock()

    choice = discord.app_commands.Choice(name="bypassPermissions", value="bypassPermissions")
    await mode_set.callback(interaction, choice)

    # Refusal must be the FIRST send_message call
    interaction.response.send_message.assert_awaited()
    call = interaction.response.send_message.await_args
    msg = call.args[0] if call.args else call.kwargs.get("content", "")
    assert "Administrator" in msg or "admin" in msg.lower(), (
        f"Expected admin-required refusal; got: {msg!r}"
    )
    # Crucially: the bridge lookup was NOT reached (no privilege escalation
    # before the gate)
    bot_spec.session_manager.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_mode_cycle_refuses_non_admin_caller():
    """R1 security HIGH: /mode cycle has same admin gate."""
    from clauded.cogs.mode import mode_cycle
    from clauded.bot import ClaudedBot
    from unittest.mock import MagicMock, AsyncMock

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock()
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.user.guild_permissions = MagicMock(administrator=False)
    interaction.response.send_message = AsyncMock()

    await mode_cycle.callback(interaction)

    interaction.response.send_message.assert_awaited()
    call = interaction.response.send_message.await_args
    msg = call.args[0] if call.args else call.kwargs.get("content", "")
    assert "Administrator" in msg or "admin" in msg.lower()
    bot_spec.session_manager.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_mode_current_allows_non_admin_read():
    """/mode current is read-only — non-admin should still be able to
    see the current mode. Mirror of /health visibility."""
    from clauded.cogs.mode import mode_current
    from clauded.bot import ClaudedBot
    from unittest.mock import MagicMock, AsyncMock

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = MagicMock()
    bridge._permission_mode_override = "plan"
    bridge._config = MagicMock(claude_permission_mode="default")
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.user.guild_permissions = MagicMock(administrator=False)
    interaction.response.send_message = AsyncMock()

    await mode_current.callback(interaction)

    interaction.response.send_message.assert_awaited()
    # Should NOT be the "Administrator required" refusal
    call = interaction.response.send_message.await_args
    if call.args:
        content_str = str(call.args[0])
    else:
        content_str = call.kwargs.get("content", "")
        embed = call.kwargs.get("embed")
        if embed is not None:
            content_str += str(getattr(embed, "title", ""))
            content_str += str(getattr(embed, "description", ""))
    assert "Administrator" not in content_str, (
        f"/mode current must NOT gate on admin (it's read-only); got: {content_str!r}"
    )


@pytest.mark.asyncio
async def test_mode_set_emits_security_audit_log(caplog):
    """R1 security HIGH: every successful permission_mode write logs
    WARNING with WHO/WHERE/WHAT-CHANGED for forensic reconstruction.
    Matches ops.unbound_fallback_toggle precedent.
    """
    import logging
    from clauded.cogs.mode import mode_set
    from clauded.bot import ClaudedBot
    from unittest.mock import MagicMock, AsyncMock
    import discord

    bot_spec = MagicMock(spec=ClaudedBot)
    bridge = MagicMock()
    bridge.is_active = True
    bridge.effective_permission_mode = "default"
    bridge.set_permission_mode = AsyncMock()
    bot_spec.session_manager = MagicMock()
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)
    bot_spec.session_manager.save_session_state = MagicMock()

    interaction = MagicMock()
    interaction.client = bot_spec
    # #250: resolve_session_id requires isinstance(channel, discord.Thread)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 42
    interaction.guild_id = 99
    interaction.channel_id = 42
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.user.name = "test_admin"
    interaction.user.id = 1234
    interaction.response.send_message = AsyncMock()

    choice = discord.app_commands.Choice(name="bypassPermissions", value="bypassPermissions")
    caplog.set_level(logging.WARNING, logger="clauded.cogs.mode")
    await mode_set.callback(interaction, choice)

    # Find the SECURITY: audit log
    audit = [r for r in caplog.records if "SECURITY:" in r.getMessage() and "permission_mode" in r.getMessage()]
    assert audit, f"No SECURITY: audit log emitted; records: {[r.getMessage() for r in caplog.records]}"
    log_msg = audit[0].getMessage()
    # WHO
    assert "test_admin" in log_msg
    assert "1234" in log_msg
    # WHERE
    assert "99" in log_msg  # guild
    assert "42" in log_msg  # channel
    # WHAT-CHANGED
    assert "default" in log_msg
    assert "bypassPermissions" in log_msg
