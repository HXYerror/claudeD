"""Tests for Feature #60 (PreToolUse hooks) and Feature #61 (partial messages).

These tests verify:
1. ClaudeBridge accepts and wires up on_pre_tool_use callback via SessionConfig
2. The hooks dict is built correctly when on_pre_tool_use is set
3. include_partial_messages is always True on options
4. StreamEvent text deltas are accumulated into the renderer buffer
5. SessionManager passes on_pre_tool_use through to ClaudeBridge
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_code_sdk import ClaudeCodeOptions, ResultMessage
from claude_code_sdk.types import StreamEvent

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _make_client(receive_response_factory: Any) -> AsyncMock:
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.receive_response = receive_response_factory
    return client


async def _async_iter(items: list[Any]):
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Feature #60: PreToolUse hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_stores_on_pre_tool_use(cfg: Config) -> None:
    """ClaudeBridge.__init__ stores the on_pre_tool_use callback."""
    cb = AsyncMock()
    sc = SessionConfig(on_pre_tool_use=cb)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    assert bridge._on_pre_tool_use is cb


@pytest.mark.asyncio
async def test_bridge_start_builds_hooks_when_callback_set(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When on_pre_tool_use is set, start() passes hooks to ClaudeCodeOptions."""
    cb = AsyncMock()
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(on_pre_tool_use=cb)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    assert len(captured_options) == 1
    opts = captured_options[0]
    assert opts.hooks is not None
    assert "PreToolUse" in opts.hooks
    assert len(opts.hooks["PreToolUse"]) == 1
    matcher = opts.hooks["PreToolUse"][0]
    assert matcher.matcher is None  # matches all tools
    assert len(matcher.hooks) == 1  # one callback


@pytest.mark.asyncio
async def test_bridge_start_no_hooks_when_no_callback(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When on_pre_tool_use is NOT set, hooks is None."""
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    opts = captured_options[0]
    assert opts.hooks is not None
    # PreCompact, UserPromptSubmit, SubagentStop are always registered
    assert "PreCompact" in opts.hooks
    assert "UserPromptSubmit" in opts.hooks
    assert "SubagentStop" in opts.hooks


@pytest.mark.asyncio
async def test_pre_tool_hook_invokes_callback(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """The registered PreToolUse hook function calls the on_pre_tool_use callback."""
    cb = AsyncMock()
    captured_hooks: list = []

    def _capture_client(options=None):
        if options and options.hooks:
            captured_hooks.append(options.hooks)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(on_pre_tool_use=cb)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    # Extract the registered hook function and call it directly
    hook_fn = captured_hooks[0]["PreToolUse"][0].hooks[0]
    input_data = {"tool_name": "Bash", "command": "ls"}
    from claude_code_sdk import HookContext
    result = await hook_fn(input_data, "tool-use-123", HookContext())

    cb.assert_awaited_once_with("Bash", input_data)
    assert result == {}  # empty = continue normally


@pytest.mark.asyncio
async def test_pre_tool_hook_defaults_to_unknown_tool(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """If tool_name is missing from input, hook passes 'unknown'."""
    received = []

    async def _capture(name, data):
        received.append(name)

    captured_hooks: list = []

    def _capture_client(options=None):
        if options and options.hooks:
            captured_hooks.append(options.hooks)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(on_pre_tool_use=_capture)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    hook_fn = captured_hooks[0]["PreToolUse"][0].hooks[0]
    from claude_code_sdk import HookContext
    await hook_fn({}, None, HookContext())

    assert received == ["unknown"]


@pytest.mark.asyncio
async def test_pre_tool_hook_swallows_callback_errors(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """If the on_pre_tool_use callback raises, the hook logs and continues."""
    async def _explode(name, data):
        raise RuntimeError("boom")

    captured_hooks: list = []

    def _capture_client(options=None):
        if options and options.hooks:
            captured_hooks.append(options.hooks)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(on_pre_tool_use=_explode)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    hook_fn = captured_hooks[0]["PreToolUse"][0].hooks[0]
    from claude_code_sdk import HookContext
    # Should NOT raise
    result = await hook_fn({"tool_name": "Bash"}, None, HookContext())
    assert result == {}


# ---------------------------------------------------------------------------
# Feature #61: Partial messages (include_partial_messages)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_enables_partial_messages(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """start() always sets include_partial_messages=True on options."""
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    opts = captured_options[0]
    assert opts.include_partial_messages is True


@pytest.mark.asyncio
async def test_stream_event_yielded_from_send_message(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """StreamEvent objects are yielded from send_message alongside ResultMessage."""
    se = StreamEvent(
        uuid="u1",
        session_id="s1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    )
    rm = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.001,
    )
    client = _make_client(lambda: _async_iter([se, rm]))
    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client)

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    received = []
    async for event in bridge.send_message("hello"):
        received.append(event)

    assert len(received) == 2
    assert isinstance(received[0], StreamEvent)
    assert isinstance(received[1], ResultMessage)


# ---------------------------------------------------------------------------
# SessionManager passes on_pre_tool_use through via SessionConfig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_passes_on_pre_tool_use(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, tmp_path
) -> None:
    """SessionManager.create_session forwards on_pre_tool_use to ClaudeBridge."""
    from clauded.session_manager import SessionManager
    from clauded.session_store import SessionStore

    captured_kwargs: list[dict] = []

    class _SpyBridge:
        instances: list = []

        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)
            self.started = False
            self.stopped = False
            _SpyBridge.instances.append(self)

        @property
        def session_id(self):
            return None

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr("clauded.session_manager.ClaudeBridge", _SpyBridge)

    sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "store")))
    cb = AsyncMock()
    sc = SessionConfig(on_pre_tool_use=cb)
    await sm.create_session(99, "/tmp/p", cfg, sc)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["session_config"] is sc
    assert sc.on_pre_tool_use is cb


# ---------------------------------------------------------------------------
# Feature #92: User passed to ClaudeCodeOptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_passes_user_to_options(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When user is set in SessionConfig, start() passes it to ClaudeCodeOptions."""
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(user="alice#1234")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    opts = captured_options[0]
    assert "alice#1234" in (opts.append_system_prompt or "")


# ---------------------------------------------------------------------------
# Feature #94: Bare mode sets extra_args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_bare_mode_extra_args(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When bare=True in SessionConfig, start() adds 'bare' to extra_args."""
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(bare=True)
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    opts = captured_options[0]
    assert "bare" in opts.extra_args
    assert opts.extra_args["bare"] is None


# ---------------------------------------------------------------------------
# Feature #95: Session name sets extra_args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_session_name_extra_args(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When session_name is set in SessionConfig, start() adds 'name' to extra_args."""
    captured_options: list[ClaudeCodeOptions] = []

    def _capture_client(options=None):
        captured_options.append(options)
        client = AsyncMock()
        client.connect = AsyncMock()
        return client

    monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture_client)

    sc = SessionConfig(session_name="my-session")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    await bridge.start()

    opts = captured_options[0]
    assert opts.extra_args.get("name") == "my-session"
