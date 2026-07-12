"""Lifecycle and concurrency tests for :class:`SessionManager`.

These tests exercise the manager's bookkeeping (lock reuse, session
replacement, lock reaping) without spinning up a real Claude SDK client
— ``ClaudeBridge`` is monkeypatched out to a minimal async stub.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from clauded.config import Config
from clauded.session_config import SessionConfig
from clauded.session_manager import SessionManager
from clauded.session_config import SessionConfig
from clauded.session_store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


class _FakeBridge:
    """Drop-in stand-in for :class:`ClaudeBridge`.

    Records ``start``/``stop`` invocations so tests can assert ordering
    without depending on the real SDK.
    """

    instances: list["_FakeBridge"] = []

    def __init__(
        self,
        *,
        project_path: str,
        config: Config,
        session_config: SessionConfig | None = None,
    ) -> None:
        self.project_path = project_path
        self.config = config
        sc = session_config or SessionConfig()
        self._session_config = sc
        self.on_ask_user = sc.on_ask_user
        self.on_pre_tool_use = sc.on_pre_tool_use
        self.on_post_tool_use = sc.on_post_tool_use
        self.on_stop = sc.on_stop
        self.env = sc.env
        self.resume_session_id = sc.resume_session_id
        self.effort = sc.effort
        self.allowed_tools = list(sc.allowed_tools) if sc.allowed_tools else []
        self.disallowed_tools = list(sc.disallowed_tools) if sc.disallowed_tools else []
        self.max_budget_usd = sc.max_budget_usd
        self.fork_session = sc.fork_session
        self.add_dirs = sc.add_dirs
        self.from_pr = sc.from_pr
        self.worktree = sc.worktree
        self.agent_name = sc.agent_name
        self.custom_agents = sc.custom_agents
        self.mcp_servers = sc.mcp_servers
        self.max_turns = sc.max_turns
        self.fallback_model = sc.fallback_model
        self.plugin_dirs = list(sc.plugin_dirs) if sc.plugin_dirs else []
        self.settings = sc.settings
        self.system_prompt = sc.system_prompt
        self.user = sc.user
        self.started = False
        self.stopped = False
        _FakeBridge.instances.append(self)

    @property
    def session_id(self) -> str | None:
        return None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _patch_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the real ClaudeBridge for ``_FakeBridge`` in session_manager."""
    _FakeBridge.instances = []
    monkeypatch.setattr(
        "clauded.session_manager.ClaudeBridge", _FakeBridge
    )


# ---------------------------------------------------------------------------
# Helpers for SessionStore
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_store(tmp_path) -> SessionStore:
    return SessionStore(data_dir=str(tmp_path / "test_store"))


def _make_sm(tmp_path=None) -> SessionManager:
    """Create a SessionManager with a temp SessionStore."""
    import tempfile
    d = tmp_path or tempfile.mkdtemp()
    return SessionManager(session_store=SessionStore(data_dir=str(d)))


# ---------------------------------------------------------------------------
# get_lock
# ---------------------------------------------------------------------------


def test_get_lock_returns_same_lock_for_same_thread(tmp_path) -> None:
    sm = _make_sm(tmp_path)
    lock_a = sm.get_lock(42)
    lock_b = sm.get_lock(42)
    assert lock_a is lock_b
    assert isinstance(lock_a, asyncio.Lock)


def test_get_lock_returns_different_locks_for_different_threads(tmp_path) -> None:
    sm = _make_sm(tmp_path)
    assert sm.get_lock(1) is not sm.get_lock(2)


# ---------------------------------------------------------------------------
# create_session replacement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_replaces_existing(cfg: Config, tmp_path) -> None:
    """A second create_session call stops the old bridge and registers a new one."""
    sm = _make_sm(tmp_path)
    first = await sm.create_session(7, "/tmp/p", cfg)
    second = await sm.create_session(7, "/tmp/p", cfg)

    # Both bridges were started, only the first should be stopped.
    assert first.started and second.started
    assert first.stopped is True
    assert second.stopped is False
    # The current session is the new one.
    assert sm.get_session(7) is second


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_session_returns_false_for_unknown_thread(tmp_path) -> None:
    sm = _make_sm(tmp_path)
    assert await sm.stop_session(999) is False


@pytest.mark.asyncio
async def test_stop_session_reaps_lock_entry(cfg: Config, tmp_path) -> None:
    """After a clean stop, the per-thread lock entry is removed."""
    sm = _make_sm(tmp_path)
    await sm.create_session(11, "/tmp/p", cfg)
    # Touch the lock so we can verify the same key disappears.
    lock = sm.get_lock(11)
    assert 11 in sm._locks  # type: ignore[attr-defined]

    stopped = await sm.stop_session(11)
    assert stopped is True
    assert 11 not in sm._locks  # type: ignore[attr-defined]
    # Sanity: the lock object itself wasn't held.
    assert not lock.locked()


@pytest.mark.asyncio
async def test_stop_session_keeps_lock_when_held(cfg: Config, tmp_path) -> None:
    """If the per-thread lock is currently held, stop_session must not reap it.

    Reaping a held lock would let a follow-up message acquire a fresh
    lock object and race with the in-flight task.
    """
    sm = _make_sm(tmp_path)
    await sm.create_session(13, "/tmp/p", cfg)
    lock = sm.get_lock(13)
    await lock.acquire()
    try:
        stopped = await sm.stop_session(13)
        assert stopped is True
        assert 13 in sm._locks  # type: ignore[attr-defined]
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# create_session with SessionConfig params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_with_effort(cfg: Config, tmp_path) -> None:
    """create_session passes effort to ClaudeBridge via SessionConfig."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(effort="high")
    bridge = await sm.create_session(20, "/tmp/p", cfg, sc)
    assert bridge.effort == "high"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_tools(cfg: Config, tmp_path) -> None:
    """create_session passes allowed/disallowed tools to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(
        allowed_tools=["Bash", "Read"],
        disallowed_tools=["WebSearch"],
    )
    bridge = await sm.create_session(21, "/tmp/p", cfg, sc)
    assert bridge.allowed_tools == ["Bash", "Read"]
    assert bridge.disallowed_tools == ["WebSearch"]


@pytest.mark.asyncio
async def test_create_session_with_budget(cfg: Config, tmp_path) -> None:
    """create_session passes max_budget_usd to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(max_budget_usd=5.0)
    bridge = await sm.create_session(22, "/tmp/p", cfg, sc)
    assert bridge.max_budget_usd == 5.0


@pytest.mark.asyncio
async def test_create_session_with_fork(cfg: Config, tmp_path) -> None:
    """create_session passes fork_session to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(
        resume_session_id="sess-abc",
        fork_session=True,
    )
    bridge = await sm.create_session(23, "/tmp/p", cfg, sc)
    assert bridge.fork_session is True
    assert bridge.resume_session_id == "sess-abc"



@pytest.mark.asyncio
async def test_create_session_with_add_dirs(cfg: Config, tmp_path) -> None:
    """create_session passes add_dirs to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(add_dirs=["/tmp/extra"])
    bridge = await sm.create_session(30, "/tmp/p", cfg, sc)
    assert bridge.add_dirs == ["/tmp/extra"]
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_from_pr(cfg: Config, tmp_path) -> None:
    """create_session passes from_pr to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(from_pr="123")
    bridge = await sm.create_session(31, "/tmp/p", cfg, sc)
    assert bridge.from_pr == "123"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_worktree(cfg: Config, tmp_path) -> None:
    """create_session passes worktree to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(worktree="feature-branch")
    bridge = await sm.create_session(32, "/tmp/p", cfg, sc)
    assert bridge.worktree == "feature-branch"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_agent(cfg: Config, tmp_path) -> None:
    """create_session passes agent_name and custom_agents to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    agents = {"reviewer": {"description": "Code reviewer", "prompt": "Review code carefully"}}
    sc = SessionConfig(agent_name="reviewer", custom_agents=agents)
    bridge = await sm.create_session(40, "/tmp/p", cfg, sc)
    assert bridge.agent_name == "reviewer"
    assert bridge.custom_agents == agents
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_mcp_servers(cfg: Config, tmp_path) -> None:
    """create_session passes mcp_servers to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    mcp = {"myserver": {"type": "stdio", "command": "npx", "args": ["-y", "server"]}}
    sc = SessionConfig(mcp_servers=mcp)
    bridge = await sm.create_session(41, "/tmp/p", cfg, sc)
    assert bridge.mcp_servers == mcp
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_max_turns(cfg: Config, tmp_path) -> None:
    """create_session passes max_turns to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(max_turns=10)
    bridge = await sm.create_session(50, "/tmp/p", cfg, sc)
    assert bridge.max_turns == 10
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_fallback_model(cfg: Config, tmp_path) -> None:
    """create_session passes fallback_model to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(fallback_model="haiku")
    bridge = await sm.create_session(51, "/tmp/p", cfg, sc)
    assert bridge.fallback_model == "haiku"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_plugin_dirs(cfg: Config, tmp_path) -> None:
    """create_session passes plugin_dirs to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(plugin_dirs=["/tmp/plugins"])
    bridge = await sm.create_session(52, "/tmp/p", cfg, sc)
    assert bridge.plugin_dirs == ["/tmp/plugins"]
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_settings(cfg: Config, tmp_path) -> None:
    """create_session passes settings to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(settings='{"key": "value"}')
    bridge = await sm.create_session(53, "/tmp/p", cfg, sc)
    assert bridge.settings == '{"key": "value"}'
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_user(cfg: Config, tmp_path) -> None:
    """create_session passes user to ClaudeBridge via SessionConfig."""
    sm = _make_sm(tmp_path)
    sc = SessionConfig(user="testuser#1234")
    bridge = await sm.create_session(60, "/tmp/p", cfg, sc)
    assert bridge.user == "testuser#1234"
    assert bridge.started


# ---------------------------------------------------------------------------
# #301 R2: session_id persistence via _on_session_id_cb callback
# ---------------------------------------------------------------------------


class _FakeBridgeWithCallback(_FakeBridge):
    """Like ``_FakeBridge`` but supports ``_on_session_id_cb``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._session_id: str | None = None
        self._on_session_id_cb = None

    @property
    def session_id(self) -> str | None:  # type: ignore[override]
        return self._session_id

    def simulate_first_result_message(self, sid: str) -> None:
        """Mimic what ``_update_stats`` does on the first ResultMessage."""
        first_time = self._session_id is None
        self._session_id = sid
        if first_time and self._on_session_id_cb is not None:
            self._on_session_id_cb(sid)


@pytest.mark.asyncio
async def test_session_id_persisted_on_first_result_message(
    cfg: Config, tmp_path, monkeypatch,
) -> None:
    """#301 R2: session_id must be persisted the moment the first
    ResultMessage arrives (via the _on_session_id_cb callback), not after
    render_response finishes."""
    monkeypatch.setattr(
        "clauded.session_manager.ClaudeBridge", _FakeBridgeWithCallback
    )
    store = SessionStore(data_dir=str(tmp_path / "store301"))
    sm = SessionManager(session_store=store)

    bridge = await sm.create_session(99, "/tmp/p", cfg)
    assert bridge.session_id is None  # no session_id yet

    # Wire the callback — same as bot.py does after create_session.
    bridge._on_session_id_cb = lambda sid: sm.save_session_state(99)

    # Simulate streaming: first ResultMessage carries a session_id.
    bridge.simulate_first_result_message("sess-from-stream-001")

    assert bridge.session_id == "sess-from-stream-001"

    # The callback should have persisted it immediately.
    stored = store.get_session_info(99)
    assert stored is not None, "session entry missing from store"
    assert stored["session_id"] == "sess-from-stream-001"

    # A second ResultMessage must NOT re-fire the callback.
    bridge._on_session_id_cb = lambda sid: (_ for _ in ()).throw(
        AssertionError("callback fired twice")
    )
    bridge.simulate_first_result_message("sess-from-stream-002")
    assert bridge.session_id == "sess-from-stream-002"  # updated
