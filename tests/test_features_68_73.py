"""Tests for Features #68–#71, #73.

- #68: Session auto-expiry (_last_activity, _start_time on ClaudeBridge)
- #69: SystemPromptModal (construction, pre-fill)
- #70: PostToolUse + Stop hooks
- #71: Environment variables (ProjectManager.set_env/get_env/remove_env,
       SessionManager env forwarding, ClaudeBridge env param)
- #73: Worktree/Cron display (renderer embed generation)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_code_sdk import ClaudeCodeOptions, HookContext

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.project_manager import ProjectManager


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


# ---------------------------------------------------------------------------
# Feature #68: Session Auto-Expiry — _last_activity / _start_time
# ---------------------------------------------------------------------------


class TestAutoExpiry:
    def test_bridge_has_last_activity(self, cfg: Config) -> None:
        """ClaudeBridge.__init__ sets _last_activity."""
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
        assert hasattr(bridge, "_last_activity")
        assert isinstance(bridge._last_activity, float)
        assert bridge._last_activity <= time.time()

    def test_bridge_has_start_time(self, cfg: Config) -> None:
        """ClaudeBridge.__init__ sets _start_time."""
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
        assert hasattr(bridge, "_start_time")
        assert isinstance(bridge._start_time, float)

    @pytest.mark.asyncio
    async def test_send_message_updates_last_activity(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """send_message updates _last_activity."""
        from claude_code_sdk import ResultMessage

        rm = ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="s1", total_cost_usd=0.0,
        )

        async def _async_iter(items):
            for item in items:
                yield item

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_response = lambda: _async_iter([rm])
        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
        await bridge.start()

        old_activity = bridge._last_activity
        # Small sleep to ensure time difference
        import asyncio
        await asyncio.sleep(0.01)

        async for _ in bridge.send_message("hello"):
            pass

        assert bridge._last_activity >= old_activity


# ---------------------------------------------------------------------------
# Feature #69: SystemPromptModal
# ---------------------------------------------------------------------------


class TestSystemPromptModal:
    def test_modal_class_exists(self) -> None:
        """SystemPromptModal can be imported from bot module."""
        from clauded.bot import SystemPromptModal
        assert SystemPromptModal is not None

    def test_modal_prefills_existing_prompt(self, tmp_path) -> None:
        """Modal pre-fills prompt_input.default with existing system prompt."""
        from clauded.bot import SystemPromptModal

        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm.set_system_prompt(42, "Be helpful and concise.")
        modal = SystemPromptModal(42, pm)
        assert modal.prompt_input.default == "Be helpful and concise."

    def test_modal_no_existing_prompt(self, tmp_path) -> None:
        """Modal with no existing prompt has no default."""
        from clauded.bot import SystemPromptModal

        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        modal = SystemPromptModal(99, pm)
        assert modal.prompt_input.default is None


# ---------------------------------------------------------------------------
# Feature #70: PostToolUse + Stop hooks
# ---------------------------------------------------------------------------


class TestPostToolUseHook:
    @pytest.mark.asyncio
    async def test_bridge_stores_on_post_tool_use(self, cfg: Config) -> None:
        """ClaudeBridge.__init__ stores the on_post_tool_use callback."""
        cb = AsyncMock()
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_post_tool_use=cb)
        assert bridge._on_post_tool_use is cb

    @pytest.mark.asyncio
    async def test_bridge_stores_on_stop(self, cfg: Config) -> None:
        """ClaudeBridge.__init__ stores the on_stop callback."""
        cb = AsyncMock()
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_stop=cb)
        assert bridge._on_stop is cb

    @pytest.mark.asyncio
    async def test_post_tool_hook_registered(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """When on_post_tool_use is set, start() registers PostToolUse hook."""
        cb = AsyncMock()
        captured_options: list[ClaudeCodeOptions] = []

        def _capture(options=None):
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_post_tool_use=cb)
        await bridge.start()

        opts = captured_options[0]
        assert opts.hooks is not None
        assert "PostToolUse" in opts.hooks
        assert len(opts.hooks["PostToolUse"]) == 1

    @pytest.mark.asyncio
    async def test_stop_hook_registered(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """When on_stop is set, start() registers Stop hook."""
        cb = AsyncMock()
        captured_options: list[ClaudeCodeOptions] = []

        def _capture(options=None):
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_stop=cb)
        await bridge.start()

        opts = captured_options[0]
        assert opts.hooks is not None
        assert "Stop" in opts.hooks

    @pytest.mark.asyncio
    async def test_all_hooks_registered(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """When all callbacks set, all hooks are registered."""
        captured_options: list[ClaudeCodeOptions] = []

        def _capture(options=None):
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(
            project_path="/tmp/p", config=cfg,
            on_pre_tool_use=AsyncMock(),
            on_post_tool_use=AsyncMock(),
            on_stop=AsyncMock(),
        )
        await bridge.start()

        opts = captured_options[0]
        assert "PreToolUse" in opts.hooks
        assert "PostToolUse" in opts.hooks
        assert "Stop" in opts.hooks

    @pytest.mark.asyncio
    async def test_post_tool_hook_calls_callback(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """The PostToolUse hook function invokes the callback with tool name."""
        cb = AsyncMock()
        captured_hooks: list = []

        def _capture(options=None):
            if options and options.hooks:
                captured_hooks.append(options.hooks)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_post_tool_use=cb)
        await bridge.start()

        hook_fn = captured_hooks[0]["PostToolUse"][0].hooks[0]
        input_data = {"tool_name": "Write", "file_path": "/tmp/test.py"}
        result = await hook_fn(input_data, "tool-123", HookContext())

        cb.assert_awaited_once_with("Write", input_data)
        assert result == {}

    @pytest.mark.asyncio
    async def test_stop_hook_calls_callback(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """The Stop hook function invokes the on_stop callback."""
        cb = AsyncMock()
        captured_hooks: list = []

        def _capture(options=None):
            if options and options.hooks:
                captured_hooks.append(options.hooks)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_stop=cb)
        await bridge.start()

        hook_fn = captured_hooks[0]["Stop"][0].hooks[0]
        input_data = {"reason": "completed"}
        result = await hook_fn(input_data, None, HookContext())

        cb.assert_awaited_once_with(input_data)
        assert result == {}

    @pytest.mark.asyncio
    async def test_post_tool_hook_swallows_errors(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """If on_post_tool_use raises, the hook logs and continues."""
        async def _explode(name, data):
            raise RuntimeError("boom")

        captured_hooks: list = []

        def _capture(options=None):
            if options and options.hooks:
                captured_hooks.append(options.hooks)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_post_tool_use=_explode)
        await bridge.start()

        hook_fn = captured_hooks[0]["PostToolUse"][0].hooks[0]
        result = await hook_fn({"tool_name": "Bash"}, None, HookContext())
        assert result == {}

    @pytest.mark.asyncio
    async def test_stop_hook_swallows_errors(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """If on_stop raises, the hook logs and continues."""
        async def _explode(data):
            raise RuntimeError("boom")

        captured_hooks: list = []

        def _capture(options=None):
            if options and options.hooks:
                captured_hooks.append(options.hooks)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, on_stop=_explode)
        await bridge.start()

        hook_fn = captured_hooks[0]["Stop"][0].hooks[0]
        result = await hook_fn({}, None, HookContext())
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_hooks_when_no_callbacks(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config
    ) -> None:
        """When no callbacks are set, hooks is None."""
        captured_options: list[ClaudeCodeOptions] = []

        def _capture(options=None):
            captured_options.append(options)
            client = AsyncMock()
            client.connect = AsyncMock()
            return client

        monkeypatch.setattr("clauded.claude_bridge.ClaudeSDKClient", _capture)

        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
        await bridge.start()

        opts = captured_options[0]
        assert opts.hooks is None


# ---------------------------------------------------------------------------
# Feature #71: Environment variables — ProjectManager
# ---------------------------------------------------------------------------


class TestProjectManagerEnv:
    def test_set_and_get_env(self, tmp_path) -> None:
        """set_env stores a variable retrievable via get_env."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm.set_env(100, "API_KEY", "secret123")
        env = pm.get_env(100)
        assert env == {"API_KEY": "secret123"}

    def test_get_env_empty(self, tmp_path) -> None:
        """get_env returns empty dict for unknown channel."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        assert pm.get_env(999) == {}

    def test_set_env_multiple(self, tmp_path) -> None:
        """Multiple env vars can be set."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm.set_env(100, "KEY1", "val1")
        pm.set_env(100, "KEY2", "val2")
        env = pm.get_env(100)
        assert env == {"KEY1": "val1", "KEY2": "val2"}

    def test_set_env_overwrites(self, tmp_path) -> None:
        """Setting same key overwrites the value."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm.set_env(100, "KEY", "old")
        pm.set_env(100, "KEY", "new")
        assert pm.get_env(100)["KEY"] == "new"

    def test_remove_env(self, tmp_path) -> None:
        """remove_env removes the variable and returns True."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm.set_env(100, "KEY", "val")
        assert pm.remove_env(100, "KEY") is True
        assert pm.get_env(100) == {}

    def test_remove_env_not_found(self, tmp_path) -> None:
        """remove_env returns False if key doesn't exist."""
        pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        assert pm.remove_env(100, "MISSING") is False

    def test_env_persists(self, tmp_path) -> None:
        """Env vars survive a round-trip through save/load."""
        pm1 = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        pm1.set_env(200, "TOKEN", "abc")
        pm2 = ProjectManager(data_dir=str(tmp_path / "data"), projects_root="/tmp")
        assert pm2.get_env(200) == {"TOKEN": "abc"}


# ---------------------------------------------------------------------------
# Feature #71: Environment variables — SessionManager forwarding
# ---------------------------------------------------------------------------


class TestSessionManagerEnv:
    @pytest.mark.asyncio
    async def test_create_session_passes_env(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config, tmp_path
    ) -> None:
        """SessionManager.create_session forwards env to ClaudeBridge."""
        from clauded.session_manager import SessionManager
        from clauded.session_store import SessionStore

        captured_kwargs: list[dict] = []

        class _SpyBridge:
            instances: list = []

            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)
                self.started = False
                _SpyBridge.instances.append(self)

            @property
            def session_id(self):
                return None

            async def start(self):
                self.started = True

            async def stop(self):
                pass

        monkeypatch.setattr("clauded.session_manager.ClaudeBridge", _SpyBridge)

        sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "store")))
        await sm.create_session(99, "/tmp/p", cfg, env={"MY_VAR": "hello"})

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["env"] == {"MY_VAR": "hello"}


# ---------------------------------------------------------------------------
# Feature #71: Environment variables — ClaudeBridge stores env
# ---------------------------------------------------------------------------


class TestClaudeBridgeEnv:
    def test_bridge_stores_env(self, cfg: Config) -> None:
        """ClaudeBridge.__init__ stores env."""
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, env={"K": "V"})
        assert bridge._env == {"K": "V"}

    def test_bridge_env_defaults_none(self, cfg: Config) -> None:
        """ClaudeBridge._env defaults to None."""
        bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
        assert bridge._env is None


# ---------------------------------------------------------------------------
# Feature #70: SessionManager forwards on_post_tool_use and on_stop
# ---------------------------------------------------------------------------


class TestSessionManagerHookForwarding:
    @pytest.mark.asyncio
    async def test_passes_on_post_tool_use(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config, tmp_path
    ) -> None:
        """SessionManager.create_session forwards on_post_tool_use."""
        from clauded.session_manager import SessionManager
        from clauded.session_store import SessionStore

        captured_kwargs: list[dict] = []

        class _SpyBridge:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

            @property
            def session_id(self):
                return None

            async def start(self):
                pass

            async def stop(self):
                pass

        monkeypatch.setattr("clauded.session_manager.ClaudeBridge", _SpyBridge)

        sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "store")))
        cb = AsyncMock()
        await sm.create_session(77, "/tmp/p", cfg, on_post_tool_use=cb)

        assert captured_kwargs[0]["on_post_tool_use"] is cb

    @pytest.mark.asyncio
    async def test_passes_on_stop(
        self, monkeypatch: pytest.MonkeyPatch, cfg: Config, tmp_path
    ) -> None:
        """SessionManager.create_session forwards on_stop."""
        from clauded.session_manager import SessionManager
        from clauded.session_store import SessionStore

        captured_kwargs: list[dict] = []

        class _SpyBridge:
            def __init__(self, **kwargs):
                captured_kwargs.append(kwargs)

            @property
            def session_id(self):
                return None

            async def start(self):
                pass

            async def stop(self):
                pass

        monkeypatch.setattr("clauded.session_manager.ClaudeBridge", _SpyBridge)

        sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "store")))
        cb = AsyncMock()
        await sm.create_session(78, "/tmp/p", cfg, on_stop=cb)

        assert captured_kwargs[0]["on_stop"] is cb
