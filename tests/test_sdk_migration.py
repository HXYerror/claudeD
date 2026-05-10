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
    # `which` result must pass the executable-file gate.
    import os as _os
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(_os, "access", lambda p, m: True)

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
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    captured: list[ClaudeAgentOptions] = []
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", _make_capture(captured)
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    assert captured, "ClaudeSDKClient was never constructed"
    assert captured[0].cli_path is None


# ---------------------------------------------------------------------------
# T1 — All 6 R features survive the 4-way merge in a single bridge.start()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_six_r_features_round_trip_in_one_call(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """All 6 R features survive the 4-way merge and coexist in one ClaudeAgentOptions call.

    Pins the integrated state: if a future merge drops one R's contribution,
    this single test fails loudly even if each isolated R test still passes.
    """
    import os as _os

    monkeypatch.setattr(
        shutil, "which", lambda name: "/fake/claude" if name == "claude" else None
    )
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(_os, "access", lambda p, m: True)

    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.__dict__.update(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    sc = SessionConfig(
        effort="high",
        max_budget_usd=5.0,
        fork_session=True,
        custom_agents={"a": {"description": "d", "prompt": "p"}},
        fallback_model="haiku",
        plugin_dirs=["/tmp/p"],
        from_pr="42",
        worktree="wt",
        agent_name="r",
        bare=True,
        session_name="n",
        user="alice",
        system_prompt="hello",
    )
    bridge = ClaudeBridge("/tmp", cfg, sc)
    await bridge.start()

    kw = captured[-1]
    # R3: system_prompt preset dict
    assert kw["system_prompt"]["type"] == "preset"
    assert kw["system_prompt"]["preset"] == "claude_code"
    assert "hello" in kw["system_prompt"]["append"]
    # R4: explicit setting_sources
    assert kw["setting_sources"] == ["user", "project", "local"]
    # R5: native fields
    assert kw["effort"] == "high"
    assert kw["max_budget_usd"] == 5.0
    assert kw["fork_session"] is True
    assert kw["agents"] == {"a": {"description": "d", "prompt": "p"}}
    assert kw["fallback_model"] == "haiku"
    assert len(kw["plugins"]) == 1
    # R5: retained extra_args
    assert kw["extra_args"]["from-pr"] == "42"
    assert kw["extra_args"]["worktree"] == "wt"
    assert kw["extra_args"]["agent"] == "r"
    assert kw["extra_args"]["bare"] is None
    assert kw["extra_args"]["name"] == "n"
    # R6: cli_path resolved
    assert kw["cli_path"] == "/fake/claude"
    # R1+R2: implied — FakeOptions only constructs if claude_agent_sdk imports succeeded


# ---------------------------------------------------------------------------
# T2 — Fallback candidate list + executable check
# ---------------------------------------------------------------------------


def test_resolve_uses_homebrew_fallback_when_path_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When shutil.which returns None, the resolver scans the candidate list."""
    import os as _os
    from clauded.cli_paths import resolve_claude_cli

    monkeypatch.setattr(shutil, "which", lambda name: None)
    real_access = _os.access
    monkeypatch.setattr(
        Path, "is_file", lambda self: str(self) == "/opt/homebrew/bin/claude"
    )
    monkeypatch.setattr(
        _os,
        "access",
        lambda p, m: True
        if str(p) == "/opt/homebrew/bin/claude"
        else real_access(p, m),
    )
    assert resolve_claude_cli() == "/opt/homebrew/bin/claude"


def test_resolve_skips_non_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that exists but isn't executable is NOT returned."""
    import os as _os
    from clauded.cli_paths import resolve_claude_cli

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: True)  # all candidates exist
    monkeypatch.setattr(_os, "access", lambda p, m: False)  # but none executable
    assert resolve_claude_cli() is None


# ---------------------------------------------------------------------------
# T3 — Large agents dict (~300KB) round-trip without serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_agents_dict_round_trips_without_serialization(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """R5 motivation: pre-fix code JSON-stringified agents dict and passed
    via CLI extra_args, risking ARG_MAX (~131KB) silent truncation.

    Verify a ~300KB dict round-trips natively as a Python object — no
    JSON serialization, no CLI flag hop.
    """
    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    big_agents = {
        f"agent_{i}": {"description": "d" * 1000, "prompt": "p" * 5000}
        for i in range(50)
    }
    sc = SessionConfig(custom_agents=big_agents)
    bridge = ClaudeBridge("/tmp", cfg, sc)
    await bridge.start()

    kw = captured[-1]
    # Agents preserved as object identity at dict level
    assert kw["agents"] == big_agents
    # Not leaked into extra_args as JSON
    assert "agents" not in kw["extra_args"]
    extra_args_size = sum(
        len(k) + (len(v) if isinstance(v, str) else 0)
        for k, v in kw["extra_args"].items()
    )
    assert extra_args_size < 4096, (
        f"extra_args too big ({extra_args_size}) — agents may have leaked back"
    )


# ---------------------------------------------------------------------------
# T5 — Empty-collection guards (default ⇒ omit / None, not empty list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_plugin_dirs_does_not_pass_plugins_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """An empty plugin_dirs list MUST signal "use SDK default" (None), not
    "clear all plugins" (empty list)."""
    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    await ClaudeBridge("/tmp", cfg, SessionConfig(plugin_dirs=[])).start()
    kw = captured[-1]
    assert kw.get("plugins") is None, (
        f"plugins=[] leaked as: {kw.get('plugins')!r}"
    )


@pytest.mark.asyncio
async def test_empty_custom_agents_does_not_pass_agents_kwarg(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """An empty custom_agents dict MUST signal "use SDK default" (None), not
    "clear all agents" (empty dict)."""
    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    await ClaudeBridge("/tmp", cfg, SessionConfig(custom_agents={})).start()
    kw = captured[-1]
    assert kw.get("agents") is None, (
        f"agents={{}} leaked as: {kw.get('agents')!r}"
    )


# ---------------------------------------------------------------------------
# T6 — system_prompt content + user-injection sanitation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_content_default_includes_channel_mgmt_no_user(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """No user, no system prompt → append still has CREATE_THREAD marker, no Discord-user line."""
    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    await ClaudeBridge("/tmp", cfg).start()
    kw = captured[-1]
    sp = kw["system_prompt"]
    appended = sp.get("append", "") if isinstance(sp, dict) else (sp or "")
    assert "CREATE_THREAD" in appended
    assert "Discord user" not in appended


@pytest.mark.asyncio
async def test_user_with_newlines_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """A username with \\n / \\r MUST NOT inject extra system prompt lines."""
    captured: list[dict[str, Any]] = []

    class FakeOptions:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    sc = SessionConfig(user="alice\ninjected\rmore")
    await ClaudeBridge("/tmp", cfg, sc).start()
    kw = captured[-1]
    sp = kw["system_prompt"]
    appended = sp.get("append", "") if isinstance(sp, dict) else (sp or "")
    marker = "Discord user talking to you is:"
    assert marker in appended
    after_marker = appended.split(marker, 1)[1]
    assert "\n" not in after_marker
    assert "\r" not in after_marker
    # And the original injection content shouldn't appear at line start
    assert "\ninjected" not in appended
    assert "\rmore" not in appended