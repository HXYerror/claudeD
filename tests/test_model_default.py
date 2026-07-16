"""#198 — model default no-force fix.

These tests pin the contract from ``docs/prd/v1.18-fix-model-default.md``:

1. ``Config.claude_model`` is ``Optional[str]``; ``None`` when env unset.
2. ``ClaudeBridge.model`` returns ``None`` when no tier is set.
3. ``ClaudeAgentOptions(model=...)`` receives ``None`` when no tier is
   set (which the SDK treats as "use CLI default from
   ~/.claude/settings.json"); receives the env value when
   ``CLAUDE_MODEL`` is set; receives the override when ``/model switch``
   was used.
4. ``_sdk_model`` is populated post-``ResultMessage`` and is
   *display-only* — it does NOT feed back into the SDK options on
   subsequent ``start()`` calls.
5. ``/model current`` distinguishes the 4 tier cases per PRD §Design.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ResultMessage

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(claude_model: str | None = None) -> Config:
    """Build a Config with the given claude_model tier value.

    ``None`` simulates ``CLAUDE_MODEL`` unset; a string simulates
    ``CLAUDE_MODEL=<string>``.
    """
    return Config(
        discord_bot_token="tok",
        claude_model=claude_model,
        claude_permission_mode="default",
        projects_root="/tmp",
    )


class _FakeClient:
    """Stand-in for ``ClaudeSDKClient`` that records the options it
    received and is otherwise a no-op."""

    captured_options: list[Any] = []

    def __init__(self, options: Any = None) -> None:
        type(self).captured_options.append(options)

    async def connect(self, prompt: Any = None) -> None:
        pass

    async def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_capture() -> None:
    _FakeClient.captured_options = []


# ---------------------------------------------------------------------------
# Config.claude_model behavior (S1)
# ---------------------------------------------------------------------------


def test_config_claude_model_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CLAUDE_MODEL`` unset → ``claude_model is None``."""
    from clauded.config import load_config

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setattr("clauded.config.load_dotenv", lambda *a, **kw: None)
    cfg = load_config()
    assert cfg.claude_model is None


def test_config_claude_model_uses_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CLAUDE_MODEL=opus`` → ``claude_model == 'opus'``."""
    from clauded.config import load_config

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-abc")
    monkeypatch.setenv("CLAUDE_MODEL", "opus")
    monkeypatch.setattr("clauded.config.load_dotenv", lambda *a, **kw: None)
    cfg = load_config()
    assert cfg.claude_model == "opus"


# ---------------------------------------------------------------------------
# ClaudeBridge.model property (S2)
# ---------------------------------------------------------------------------


def test_bridge_model_property_is_none_when_all_tiers_none() -> None:
    """All-tier-None case: override unset, env unset, no ResultMessage."""
    cfg = _config(claude_model=None)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    assert bridge.model is None


def test_bridge_model_property_returns_override() -> None:
    """``/model switch`` wins over env and sdk."""
    cfg = _config(claude_model="opus")
    sc = SessionConfig(model_override="haiku")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    bridge._sdk_model = "claude-sonnet-4-5"
    assert bridge.model == "haiku"


def test_bridge_model_property_returns_sdk_when_only_observed() -> None:
    """No override, no env → ``_sdk_model`` is what we show."""
    cfg = _config(claude_model=None)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    bridge._sdk_model = "claude-sonnet-4-5"
    assert bridge.model == "claude-sonnet-4-5"


def test_bridge_model_property_returns_env_when_no_override_no_sdk() -> None:
    """env > config tier when there's no override and no SDK observation."""
    cfg = _config(claude_model="opus")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    assert bridge.model == "opus"


# ---------------------------------------------------------------------------
# ClaudeAgentOptions construction (S2 — integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_model_is_none_when_all_tiers_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All tiers None → ``ClaudeAgentOptions(model=None)`` (SDK treats as
    omitted, falling back to ~/.claude/settings.json)."""
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)
    bridge = ClaudeBridge(project_path="/tmp/p", config=_config(claude_model=None))
    await bridge.start()
    assert _FakeClient.captured_options, "ClaudeAgentOptions was not constructed"
    opts = _FakeClient.captured_options[-1]
    assert opts.model is None, f"expected model=None, got {opts.model!r}"


@pytest.mark.asyncio
async def test_options_model_uses_env_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CLAUDE_MODEL=opus`` → ``ClaudeAgentOptions(model='opus')``."""
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)
    bridge = ClaudeBridge(project_path="/tmp/p", config=_config(claude_model="opus"))
    await bridge.start()
    opts = _FakeClient.captured_options[-1]
    assert opts.model == "opus"


@pytest.mark.asyncio
async def test_options_model_uses_override_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/model switch haiku`` with env unset → ``model='haiku'``."""
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)
    sc = SessionConfig(model_override="haiku")
    bridge = ClaudeBridge(
        project_path="/tmp/p", config=_config(claude_model=None), session_config=sc
    )
    await bridge.start()
    opts = _FakeClient.captured_options[-1]
    assert opts.model == "haiku"


@pytest.mark.asyncio
async def test_options_override_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/model switch haiku`` + ``CLAUDE_MODEL=opus`` → override wins."""
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)
    sc = SessionConfig(model_override="haiku")
    bridge = ClaudeBridge(
        project_path="/tmp/p", config=_config(claude_model="opus"), session_config=sc
    )
    await bridge.start()
    opts = _FakeClient.captured_options[-1]
    assert opts.model == "haiku"


@pytest.mark.asyncio
async def test_options_does_not_use_sdk_model_as_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_sdk_model`` is display-only; it must NOT feed back into the SDK
    kwargs. PRD §Design: using it would lock the session to whatever the
    first turn resolved to, even if ~/.claude/settings.json changes."""
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _FakeClient)
    bridge = ClaudeBridge(project_path="/tmp/p", config=_config(claude_model=None))
    bridge._sdk_model = "claude-sonnet-4-5"  # simulate post-first-turn state
    await bridge.start()
    opts = _FakeClient.captured_options[-1]
    assert opts.model is None, (
        f"_sdk_model leaked into ClaudeAgentOptions.model={opts.model!r}; "
        "it must be display-only per PRD §Design"
    )


# ---------------------------------------------------------------------------
# ResultMessage → _sdk_model propagation (regression-pinning)
# ---------------------------------------------------------------------------


def _make_async_client(items: list[Any]) -> AsyncMock:
    """Build an AsyncMock client that yields ``items`` from
    ``receive_response``. Mirrors the pattern in test_claude_bridge.py."""

    async def _iter() -> Any:
        for x in items:
            yield x

    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.receive_messages = _iter
    return client


@pytest.mark.asyncio
async def test_result_message_populates_sdk_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a ResultMessage carrying a ``model`` arrives, ``_sdk_model``
    is set and is visible via the ``bridge.model`` property."""
    rm = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-x",
        total_cost_usd=0.01,
    )
    rm.model = "claude-sonnet-4-5"  # type: ignore[attr-defined]

    client = _make_async_client([rm])
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=_config(claude_model=None))
    await bridge.start()
    assert bridge._sdk_model is None
    assert bridge.model is None  # all tiers None pre-first-turn

    async for _ in bridge.send_message("hi"):
        pass

    assert bridge._sdk_model == "claude-sonnet-4-5"
    assert bridge.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# /model current — 4-case display (S3)
# ---------------------------------------------------------------------------


def _bridge_for_case(
    *,
    override: str | None,
    env_model: str | None,
    sdk_model: str | None,
) -> Any:
    """Build a MagicMock bridge with the requested tier values."""
    bridge = MagicMock()
    bridge._model_override = override
    bridge._sdk_model = sdk_model
    cfg = MagicMock()
    cfg.claude_model = env_model
    bridge._config = cfg
    # bridge.model just collapses the tiers for callers that don't care
    bridge.model = override or sdk_model or env_model
    return bridge


@pytest.mark.asyncio
async def test_model_current_case1_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 1: ``/model switch haiku`` → name + KNOWN_MODELS metadata."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = _bridge_for_case(override="haiku", env_model=None, sdk_model=None)
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
    # No env-/CLI-default suffix on override case
    assert "CLAUDE_MODEL env" not in embed.description
    assert "CLI default" not in embed.description


@pytest.mark.asyncio
async def test_model_current_case2_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 2: ``CLAUDE_MODEL=opus`` → ``opus (CLAUDE_MODEL env)``."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = _bridge_for_case(override=None, env_model="opus", sdk_model=None)
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "opus" in embed.description
    assert "CLAUDE_MODEL env" in embed.description


@pytest.mark.asyncio
async def test_model_current_case3_sdk_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 3: post-first-turn, no override, no env →
    ``<value> (CLI default)``."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = _bridge_for_case(
        override=None, env_model=None, sdk_model="claude-sonnet-4-5"
    )
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "claude-sonnet-4-5" in embed.description
    assert "CLI default" in embed.description


@pytest.mark.asyncio
async def test_model_current_case4_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 4: no override, no env, pre-first-turn → ``(unset — ...)``
    placeholder per PRD §Design."""
    from clauded.cogs.model import model_current
    from clauded.bot import ClaudedBot

    bot_spec = MagicMock(spec=ClaudedBot)
    bot_spec.session_manager = MagicMock()
    bridge = _bridge_for_case(override=None, env_model=None, sdk_model=None)
    bot_spec.session_manager.get_session = MagicMock(return_value=bridge)

    interaction = MagicMock()
    interaction.client = bot_spec
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()
    await model_current.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "unset" in embed.description.lower()
    assert "CLI default" in embed.description


# ---------------------------------------------------------------------------
# _model_source_for_bridge helper (covers tier-detection logic directly)
# ---------------------------------------------------------------------------


def test_model_source_override_wins() -> None:
    from clauded.cogs.model import _model_source_for_bridge

    bridge = _bridge_for_case(override="haiku", env_model="opus", sdk_model="sonnet")
    assert _model_source_for_bridge(bridge) == ("override", "haiku")


def test_model_source_env_when_no_override() -> None:
    from clauded.cogs.model import _model_source_for_bridge

    bridge = _bridge_for_case(override=None, env_model="opus", sdk_model="sonnet")
    assert _model_source_for_bridge(bridge) == ("env", "opus")


def test_model_source_sdk_when_no_override_no_env() -> None:
    from clauded.cogs.model import _model_source_for_bridge

    bridge = _bridge_for_case(override=None, env_model=None, sdk_model="sonnet")
    assert _model_source_for_bridge(bridge) == ("sdk", "sonnet")


def test_model_source_unset_when_all_none() -> None:
    from clauded.cogs.model import _model_source_for_bridge

    bridge = _bridge_for_case(override=None, env_model=None, sdk_model=None)
    assert _model_source_for_bridge(bridge) == ("unset", None)


# ---------------------------------------------------------------------------
# R1 architect finding: persistence-loop fix
# ---------------------------------------------------------------------------
#
# Before this PR, ``save_session_state`` read ``bridge.model`` (collapsed
# property) which falls back to ``_sdk_model`` after the first
# ResultMessage. The persisted value then re-injected as ``model_override``
# on resume, forming a cross-restart loop and ignoring the user's
# ``~/.claude/settings.json`` updates. PRD §Design line 92 explicitly
# warned against feeding ``_sdk_model`` back into the SDK input — this
# test pins the persistence boundary against that regression.


def test_explicit_model_override_returns_user_choice_only():
    """``ClaudeBridge.explicit_model_override`` reflects ONLY
    ``_model_override`` — never ``_sdk_model`` or ``config.claude_model``.
    """
    from unittest.mock import MagicMock
    from clauded.claude_bridge import ClaudeBridge

    cfg = MagicMock()
    cfg.claude_model = "sonnet"  # env-set scenario
    cfg.claude_permission_mode = "default"
    cfg.projects_root = "/tmp"

    bridge = ClaudeBridge(project_path="/tmp", config=cfg)

    # No explicit override yet → None
    assert bridge.explicit_model_override is None
    # _sdk_model populated post-ResultMessage → still None
    bridge._sdk_model = "claude-haiku-4-5"
    assert bridge.explicit_model_override is None
    # User switches → reflects the user value
    bridge._model_override = "opus"
    assert bridge.explicit_model_override == "opus"


def test_save_session_state_persists_only_explicit_override_no_sdk_loop():
    """Pin the persistence-loop fix: ``save_session_state`` writes the
    user-explicit override (or None when unset), never the SDK-observed
    ``_sdk_model``. Without this fix, a user with no env / no /model
    switch would get their CLI default locked in on resume even after
    changing ``settings.json``.
    """
    from unittest.mock import MagicMock
    from clauded.session_manager import SessionManager

    # Build a bridge stub with the canonical "user did not switch" state:
    # - _model_override is None
    # - _sdk_model is what the SDK reported back (CLI default)
    bridge = MagicMock()
    bridge.session_id = "sess-abc"
    bridge.project_path = "/tmp/proj"
    bridge.system_prompt = ""
    bridge._model_override = None
    bridge._sdk_model = "claude-haiku-4-5"
    bridge.explicit_model_override = None  # the new accessor

    sm = SessionManager(MagicMock())
    sm._sessions[42] = bridge

    # Capture what gets passed to the store
    captured: dict = {}

    def _fake_save(thread_id, session_id, *, permission_mode_override=None):
        captured.update(
            thread_id=thread_id, session_id=session_id,
            permission_mode_override=permission_mode_override,
        )

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(42)

    # #295 update: model is no longer in the persisted payload at all.
    # Pre-#295 this test asserted ``captured["model"] is None``; post-#295
    # the field simply isn't part of ``save_session``'s signature, which
    # makes the "no persistence loop" guarantee even stronger. Pin that
    # the call still happened (session_id captured) — proving that
    # save_session_state no longer threads model through.
    assert "model" not in captured, (
        f"save_session_state must not thread model through to save_session; "
        f"got {captured!r}. #295 removed model from the persistence layer."
    )
    assert captured["session_id"] == "sess-abc"


def test_save_session_state_persists_user_override_when_set():
    """#210 update: ``/model switch`` is intentionally ephemeral.

    Pre-#210 contract: persistence carried the user-explicit override
    across restart so resume would re-inject it.

    Post-#210 contract: the read side stopped reading ``stored.model``
    (see ``bot.py`` thread auto-resume and ``cogs/session.py`` /session
    resume). Persisting the override would therefore be dead weight,
    and would also blur the line between new rows (clean) and legacy
    "sonnet"-polluted rows during forensic inspection. New rows now
    always carry ``model=None`` regardless of whether the user has
    switched. Users intentionally re-run ``/model switch`` after
    restart if they want a non-default model — per user decision in
    DDD ("没设置就是 claude code 默认的").
    """
    from unittest.mock import MagicMock
    from clauded.session_manager import SessionManager

    bridge = MagicMock()
    bridge.session_id = "sess-xyz"
    bridge.project_path = "/tmp/proj"
    bridge.system_prompt = ""
    bridge.explicit_model_override = "opus"

    sm = SessionManager(MagicMock())
    sm._sessions[99] = bridge

    captured: dict = {}

    def _fake_save(thread_id, session_id, *, permission_mode_override=None):
        captured["session_id"] = session_id
        captured["permission_mode_override"] = permission_mode_override

    sm._session_store.save_session = _fake_save  # type: ignore[method-assign]
    sm.save_session_state(99)

    # #295: model is not in the payload at all — persistence layer no
    # longer knows about model. #210's "ephemeral override" contract is
    # thus strengthened: no dead field carrying stale state across
    # restarts.
    assert "model" not in captured
    assert captured["session_id"] == "sess-xyz"
