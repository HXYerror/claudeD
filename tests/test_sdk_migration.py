"""Tests for v1.10 SDK migration (#111).

Verifies that the 6 fields previously plumbed via ``extra_args`` are now
passed as native ``ClaudeAgentOptions`` keyword arguments (R5 / #118):

- ``effort``           (was ``extra_args["effort"]``)
- ``max_budget_usd``   (was ``extra_args["max-budget-usd"]``)
- ``fork_session``     (was ``extra_args["fork-session"] = None``)
- ``agents``           (was ``extra_args["agents"] = json.dumps(...)``)
- ``fallback_model``   (was ``extra_args["fallback-model"]``)
- ``plugins``          (was ``extra_args["plugin-dir"]``)

Retained ``extra_args`` keys (no native equivalent in 0.1.80) — these MUST
remain in ``extra_args``:

- ``from-pr``, ``worktree``, ``agent`` (singular), ``bare``, ``name``.
"""

from __future__ import annotations

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
