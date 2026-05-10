"""Regression-detection invariants for the v1.10 SDK migration (#111, #121).

Pins the 6 invariants required by issue #121 in one file so a future bump
of the SDK that re-introduces any pre-migration shape fails loudly here:

1. **Symbol rename (R1/R2)** — the import ``claude_agent_sdk.ClaudeAgentOptions``
   resolves; ``claude_code_sdk`` / ``ClaudeCodeOptions`` MUST NOT appear in
   source. Catches a partial revert of #115.

2. **system_prompt preset dict (R3, #116)** — bridge passes
   ``system_prompt={"type": "preset", "preset": "claude_code", "append": ...}``.
   Catches re-introduction of ``append_system_prompt=`` which would crash on
   v0.1.80+ (``TypeError: unexpected keyword argument``).

3. **setting_sources explicit (R4, #117)** — bridge passes
   ``setting_sources=["user", "project", "local"]`` so user CLAUDE.md, user
   skills, and project settings still load (v1.10 default flipped to ``[]``).
   Cross-referenced with ``test_claude_bridge.py::test_setting_sources_includes_user_project_local``.

4. **No migrated keys in extra_args (R5, #118)** — none of ``effort``,
   ``max-budget-usd``, ``fork-session``, ``agents``, ``fallback-model``,
   ``plugin-dir`` appear under ``extra_args``; each round-trips through its
   native ``ClaudeAgentOptions`` field instead.

5. **Retained extra_args keys (R5, #118)** — ``from-pr``, ``worktree``,
   ``agent`` (singular), ``bare``, ``name`` MUST stay in ``extra_args`` (no
   native equivalent in 0.1.80). Guards against accidental over-migration.

6. **cli_path resolved when system claude exists (R6, #119)** — when
   ``shutil.which('claude')`` returns a path, that path is forwarded to
   ``ClaudeAgentOptions.cli_path``. Cross-referenced with
   ``test_claude_bridge.py::test_cli_path_resolved_to_system_claude``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _make_capture(captured: list[ClaudeAgentOptions]):
    def _capture_client(options=None):
        captured.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    return _capture_client


# ---------------------------------------------------------------------------
# Each native field round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effort_passed_as_native_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """effort is passed via ClaudeAgentOptions.effort, not extra_args['effort']."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(effort="high"),
    )
    await bridge.start()

    opts = captured[0]
    assert opts.effort == "high"
    assert "effort" not in opts.extra_args


@pytest.mark.asyncio
async def test_max_budget_usd_passed_as_native_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """max_budget_usd is passed as float via the native field."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(max_budget_usd=12.5),
    )
    await bridge.start()

    opts = captured[0]
    assert opts.max_budget_usd == 12.5
    assert isinstance(opts.max_budget_usd, float)
    assert "max-budget-usd" not in opts.extra_args


@pytest.mark.asyncio
async def test_fork_session_passed_as_native_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """fork_session is passed as native bool, not extra_args['fork-session']."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(fork_session=True),
    )
    await bridge.start()

    opts = captured[0]
    assert opts.fork_session is True
    assert "fork-session" not in opts.extra_args


@pytest.mark.asyncio
async def test_fallback_model_passed_as_native_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """fallback_model is passed via the native field."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(fallback_model="haiku"),
    )
    await bridge.start()

    opts = captured[0]
    assert opts.fallback_model == "haiku"
    assert "fallback-model" not in opts.extra_args


@pytest.mark.asyncio
async def test_plugin_dirs_passed_as_native_plugins(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """plugin_dirs are mapped to a list of SdkPluginConfig; old extra_args
    'plugin-dir' is gone."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(plugin_dirs=["/tmp/pluginA", "/tmp/pluginB"]),
    )
    await bridge.start()

    opts = captured[0]
    # SdkPluginConfig is a TypedDict — equality is dict equality.
    assert opts.plugins == [
        {"type": "local", "path": "/tmp/pluginA"},
        {"type": "local", "path": "/tmp/pluginB"},
    ]
    assert "plugin-dir" not in opts.extra_args


# ---------------------------------------------------------------------------
# Required regression test: agents dict (3+ entries) — passed natively, not JSON-stringified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_dict_passed_natively_no_json_stringify(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """Regression for #118 — agents must reach ClaudeAgentOptions as a Python
    dict, NOT as a JSON-stringified blob in extra_args.
    """
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    agents: dict[str, Any] = {
        "reviewer": {"description": "Reviews code", "prompt": "You review code."},
        "tester":   {"description": "Writes tests", "prompt": "You write tests."},
        "doc":      {"description": "Writes docs",  "prompt": "You write docs."},
    }
    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(custom_agents=agents),
    )
    await bridge.start()

    opts = captured[0]
    # Native field receives the raw dict
    assert opts.agents == agents
    assert isinstance(opts.agents, dict)
    # No JSON string anywhere in extra_args
    assert "agents" not in opts.extra_args


# ---------------------------------------------------------------------------
# Retained extra_args keys remain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retained_extra_args_keys_still_in_extra_args(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """from-pr, worktree, agent, bare, name remain in extra_args (no native equivalent)."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    sc = SessionConfig(
        from_pr="42",
        worktree="feature-x",
        agent_name="reviewer",
        bare=True,
        session_name="my-session",
    )
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    opts = captured[0]
    assert opts.extra_args.get("from-pr") == "42"
    assert opts.extra_args.get("worktree") == "feature-x"
    assert opts.extra_args.get("agent") == "reviewer"
    assert "bare" in opts.extra_args and opts.extra_args["bare"] is None
    assert opts.extra_args.get("name") == "my-session"


@pytest.mark.asyncio
async def test_no_migrated_keys_in_extra_args(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """Migrated keys must never appear under their old extra_args names,
    even when ALL fields are populated together."""
    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured))

    sc = SessionConfig(
        effort="medium",
        max_budget_usd=5.0,
        fork_session=True,
        custom_agents={"a": {"description": "d", "prompt": "p"}},
        fallback_model="haiku",
        plugin_dirs=["/tmp/plug"],
    )
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    opts = captured[0]
    forbidden = ("effort", "max-budget-usd", "fork-session", "agents",
                 "fallback-model", "plugin-dir")
    for key in forbidden:
        assert key not in opts.extra_args, (
            f"{key!r} should be a native field, not in extra_args"
        )


# ---------------------------------------------------------------------------
# Invariant #1 — symbol rename guard (R1/R2, #115)
# ---------------------------------------------------------------------------


def test_symbol_rename_claude_agent_sdk_is_canonical() -> None:
    """``ClaudeAgentOptions`` from ``claude_agent_sdk`` must be the symbol the
    bridge uses. The pre-migration name (``ClaudeCodeOptions`` /
    ``claude_code_sdk``) is unimportable on v1.10+ — its presence anywhere
    in source would crash at import time.
    """
    # The post-migration package must be importable.
    import claude_agent_sdk
    assert claude_agent_sdk.__name__ == "claude_agent_sdk"
    assert hasattr(claude_agent_sdk, "ClaudeAgentOptions")

    # The bridge module must reference the new symbol, not the old one.
    from clauded import claude_bridge
    assert claude_bridge.ClaudeAgentOptions is ClaudeAgentOptions
    assert not hasattr(claude_bridge, "ClaudeCodeOptions"), (
        "Pre-migration symbol ClaudeCodeOptions leaked back into the bridge"
    )

    # The pre-migration package must NOT be importable in this env.
    with pytest.raises(ModuleNotFoundError):
        __import__("claude_code_sdk")


# ---------------------------------------------------------------------------
# Invariant #2 — system_prompt preset dict (R3, #116)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_is_preset_dict_not_append_system_prompt(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """Bridge passes ``system_prompt={"type":"preset","preset":"claude_code","append":...}``
    instead of the pre-migration ``append_system_prompt=...`` kwarg.

    The v0.1.80 SDK removed ``append_system_prompt``; passing it raises
    ``TypeError: unexpected keyword argument``. We capture every kwarg via a
    permissive stand-in to assert positively on the new shape AND negatively
    on the old kwarg name.
    """
    captured_kwargs: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)
            self.__dict__.update(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    bridge = ClaudeBridge(
        project_path="/tmp/p", config=cfg,
        session_config=SessionConfig(system_prompt="be concise"),
    )
    await bridge.start()

    assert captured_kwargs, "ClaudeAgentOptions was not constructed"
    kw = captured_kwargs[-1]

    # Positive: new shape.
    sp = kw.get("system_prompt")
    assert isinstance(sp, dict), f"system_prompt should be dict, got {type(sp)!r}"
    assert sp.get("type") == "preset"
    assert sp.get("preset") == "claude_code"
    assert "be concise" in (sp.get("append") or "")

    # Negative: pre-migration kwarg must not be passed.
    assert "append_system_prompt" not in kw, (
        "append_system_prompt was removed in claude-agent-sdk 0.1.80; "
        "passing it crashes ClaudeAgentOptions.__init__"
    )


# ---------------------------------------------------------------------------
# Invariant #3 — setting_sources explicit (R4, #117) [cross-ref]
# ---------------------------------------------------------------------------
# Primary test lives in test_claude_bridge.py::test_setting_sources_includes_user_project_local.
# Mirrored here so #121's regression-suite is self-contained.


@pytest.mark.asyncio
async def test_setting_sources_user_project_local(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """The bridge MUST explicitly request all three setting_sources.

    v1.10 flipped the SDK default from "load all" to ``[]``; without the
    explicit list, user CLAUDE.md, user skills, and project settings are
    silently dropped on session start.
    """
    captured_kwargs: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)
            self.__dict__.update(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    assert captured_kwargs, "ClaudeAgentOptions was not constructed"
    assert captured_kwargs[-1].get("setting_sources") == ["user", "project", "local"]


# ---------------------------------------------------------------------------
# Invariant #6 — cli_path resolved (R6, #119) [cross-ref]
# ---------------------------------------------------------------------------
# Primary test lives in test_claude_bridge.py::test_cli_path_resolved_to_system_claude.
# Mirrored here for the regression-suite consolidation.


@pytest.mark.asyncio
async def test_cli_path_set_when_system_claude_resolves(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When ``shutil.which('claude')`` returns a path, the bridge forwards it
    to ``ClaudeAgentOptions.cli_path`` so the SDK uses the system CLI rather
    than its (potentially older) bundled binary.
    """
    monkeypatch.setattr(
        shutil, "which",
        lambda name: "/fake/sys/claude" if name == "claude" else None,
    )

    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured)
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    assert captured, "ClaudeSDKClient was never constructed"
    assert captured[0].cli_path == "/fake/sys/claude"


@pytest.mark.asyncio
async def test_cli_path_none_when_no_claude_resolvable(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When neither $PATH nor fallback locations contain claude, cli_path is
    left at the SDK default (``None`` => use bundled).
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured)
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    assert captured, "ClaudeSDKClient was never constructed"
    assert captured[0].cli_path is None
