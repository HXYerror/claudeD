"""Unit tests for :class:`ClaudeBridge`.

We can't actually start a Claude session in tests, so we monkeypatch
``ClaudeSDKClient`` with an :class:`AsyncMock` and drive the bridge
through its public methods. The goal is to cover three behaviors:

1. An exception in the SDK stream flips ``is_active`` to False.
2. That same exception triggers a best-effort ``client.disconnect()``.
3. ``ResultMessage`` events update the cumulative ``total_cost`` /
   ``num_turns`` / ``model`` attributes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ResultMessage

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _make_client(receive_messages_factory: Any) -> AsyncMock:
    """Build an AsyncMock standing in for ``ClaudeSDKClient``.

    ``receive_messages`` is a *sync* method that returns an *async*
    iterator, so we wire it as a regular MagicMock side-effect rather
    than an AsyncMock.
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.receive_messages = receive_messages_factory
    return client


async def _async_iter(items: list[Any]):
    for item in items:
        yield item


async def _async_iter_then_raise(items: list[Any], exc: BaseException):
    for item in items:
        yield item
    raise exc


# ---------------------------------------------------------------------------
# send_message: exception path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_exception_marks_inactive(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When the SDK raises, the bridge must flip ``is_active`` to False."""
    boom = RuntimeError("sdk crashed mid-stream")
    client = _make_client(lambda: _async_iter_then_raise([], boom))

    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()
    assert bridge.is_active is True

    with pytest.raises(RuntimeError, match="sdk crashed"):
        async for _ in bridge.send_message("hi"):
            pass

    assert bridge.is_active is False


@pytest.mark.asyncio
async def test_send_message_exception_disconnects_client(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """A stream failure must call ``disconnect`` on the underlying client."""
    boom = RuntimeError("bang")
    client = _make_client(lambda: _async_iter_then_raise([], boom))

    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    with pytest.raises(RuntimeError):
        async for _ in bridge.send_message("hello"):
            pass

    client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# ResultMessage stats propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_message_updates_stats(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """``total_cost`` / ``num_turns`` / ``model`` are updated from ResultMessage."""
    rm = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=4,
        session_id="sess-1",
        total_cost_usd=0.1234,
    )
    # The bridge reads ``model`` off the ResultMessage via getattr; the SDK
    # type doesn't carry one, so we attach it directly.
    rm.model = "claude-sonnet-4-5"  # type: ignore[attr-defined]

    client = _make_client(lambda: _async_iter([rm]))
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()
    assert bridge.total_cost == 0.0
    assert bridge.num_turns == 0
    assert bridge.model == "sonnet"  # defaults to config claude_model

    received: list[Any] = []
    async for event in bridge.send_message("ping"):
        received.append(event)

    assert received == [rm]
    assert bridge.total_cost == pytest.approx(0.1234)
    assert bridge.num_turns == 4
    assert bridge.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# SessionConfig integration
# ---------------------------------------------------------------------------


def test_bridge_accepts_session_config(cfg: Config) -> None:
    """ClaudeBridge can be constructed with a SessionConfig."""
    sc = SessionConfig(
        system_prompt="Be concise",
        model_override="opus",
        effort="high",
        max_budget_usd=10.0,
        user="testuser",
    )
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    assert bridge.system_prompt == "Be concise"
    assert bridge.model == "opus"
    assert bridge._effort == "high"
    assert bridge._max_budget_usd == 10.0
    assert bridge._user == "testuser"


def test_bridge_default_session_config(cfg: Config) -> None:
    """ClaudeBridge uses default SessionConfig when none is provided."""
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    assert bridge.system_prompt is None
    # #291: default effort is now "max" (configurable via CLAUDED_DEFAULT_EFFORT)
    assert bridge._effort == "max"
    assert bridge._user is None


# ---------------------------------------------------------------------------
# GeneratorExit handling (Limitation 1 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_generator_exit_keeps_session_active(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """Breaking out of the async-for loop must NOT disconnect or deactivate."""
    rm = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="sess-ge",
        total_cost_usd=0.01,
    )

    async def _many_messages():
        yield rm
        yield rm  # second message — caller will break before consuming this

    client = _make_client(_many_messages)
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()
    assert bridge.is_active is True

    # Break out of the loop after the first message
    async for msg in bridge.send_message("hi"):
        break

    # Session must still be active and client must NOT have been disconnected
    assert bridge.is_active is True
    client.disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# can_use_tool disabled when not bypassPermissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_use_tool_set_when_on_ask_user(monkeypatch):
    """can_use_tool should be set whenever on_ask_user is provided (any permission_mode)."""
    from clauded.config import Config
    from clauded.session_config import SessionConfig
    from clauded.claude_bridge import ClaudeBridge

    cfg = Config(discord_bot_token="t", claude_model="sonnet",
                 claude_permission_mode="default", projects_root="/tmp")
    
    captured = []
    class FakeClient:
        def __init__(self, options=None): captured.append(options)
        async def connect(self, prompt=None): pass
    
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)
    
    sc = SessionConfig(on_ask_user=lambda x: x)  # has on_ask_user
    bridge = ClaudeBridge("/tmp", cfg, sc)
    await bridge.start()
    
    # With the SDK permission format monkey-patch, can_use_tool is always
    # wired when on_ask_user is set — regardless of permission_mode.
    assert captured[0].can_use_tool is not None


@pytest.mark.asyncio
async def test_can_use_tool_none_when_no_on_ask_user(monkeypatch):
    """can_use_tool should be None when on_ask_user is not provided."""
    from clauded.config import Config
    from clauded.session_config import SessionConfig
    from clauded.claude_bridge import ClaudeBridge

    cfg = Config(discord_bot_token="t", claude_model="sonnet",
                 claude_permission_mode="default", projects_root="/tmp")

    captured = []
    class FakeClient:
        def __init__(self, options=None): captured.append(options)
        async def connect(self, prompt=None): pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    sc = SessionConfig()  # no on_ask_user
    bridge = ClaudeBridge("/tmp", cfg, sc)
    await bridge.start()

    assert captured[0].can_use_tool is None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# setting_sources regression (#111, #117)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_sources_includes_user_project_local(monkeypatch, cfg):
    """Regression #111/#117: setting_sources must explicitly list all three.

    The v1.10 SDK changed the default for ``setting_sources`` from "load all"
    to ``None``/``[]`` (load nothing). Without an explicit list, user-level
    CLAUDE.md, user skills, and project settings are silently dropped. The
    bridge must pass ``["user", "project", "local"]`` to preserve v1.x
    behavior.

    We monkey-patch ``ClaudeAgentOptions`` itself with a permissive
    stand-in that captures all kwargs, so this test is independent of
    other Wave-2 SDK changes (e.g. ``append_system_prompt`` rename).
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
# Explicit cli_path resolution (#119, R6)
# ---------------------------------------------------------------------------


def test_resolve_claude_cli_uses_shutil_which(monkeypatch):
    """The resolver returns whatever ``shutil.which('claude')`` finds first."""
    import shutil
    import os
    from pathlib import Path
    from clauded.cli_paths import resolve_claude_cli

    monkeypatch.setattr(
        shutil, "which", lambda name: "/sys/bin/claude" if name == "claude" else None
    )
    # The which result must pass the executable-file gate.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)
    assert resolve_claude_cli() == "/sys/bin/claude"


def test_resolve_claude_cli_returns_none_when_unfound(monkeypatch):
    """When neither $PATH nor fallback locations contain claude, return None."""
    import shutil
    from pathlib import Path
    from clauded.cli_paths import resolve_claude_cli

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    assert resolve_claude_cli() is None


@pytest.mark.asyncio
async def test_cli_path_resolved_to_system_claude(monkeypatch, cfg):
    """When ``shutil.which('claude')`` succeeds, ClaudeAgentOptions gets cli_path."""
    import shutil
    import os
    from pathlib import Path
    from clauded.claude_bridge import ClaudeBridge

    monkeypatch.setattr(
        shutil, "which", lambda name: "/fake/claude" if name == "claude" else None
    )
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "access", lambda p, m: True)

    captured = []

    class FakeClient:
        def __init__(self, options=None):
            captured.append(options)

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    bridge = ClaudeBridge("/tmp", cfg)
    await bridge.start()

    assert captured, "ClaudeSDKClient was never constructed"
    assert captured[0].cli_path == "/fake/claude"


@pytest.mark.asyncio
async def test_cli_path_omitted_when_not_found(monkeypatch, cfg):
    """When no claude CLI is resolvable, cli_path stays at the SDK default."""
    import shutil
    from pathlib import Path

    from clauded.claude_bridge import ClaudeBridge

    monkeypatch.setattr(shutil, "which", lambda name: None)
    # Force fallback lookups in resolve_claude_cli to fail too, otherwise a
    # real /opt/homebrew/bin/claude on the dev box would pollute the test.
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    captured = []

    class FakeClient:
        def __init__(self, options=None):
            captured.append(options)

        async def connect(self, prompt=None):
            pass

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", FakeClient)

    bridge = ClaudeBridge("/tmp", cfg)
    await bridge.start()

    assert captured, "ClaudeSDKClient was never constructed"
    # The SDK's ClaudeAgentOptions defaults cli_path to None when not passed,
    # which is exactly the "use bundled" signal we want.
    assert captured[0].cli_path is None
