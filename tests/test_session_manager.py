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
from clauded.session_manager import SessionManager
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
        on_ask_user: Any = None,
        system_prompt: Any = None,
        model_override: Any = None,
        resume_session_id: Any = None,
        effort: Any = None,
        allowed_tools: Any = None,
        disallowed_tools: Any = None,
        max_budget_usd: Any = None,
        fork_session: Any = False,
        add_dirs: Any = None,
        from_pr: Any = None,
        worktree: Any = None,
        agent_name: Any = None,
        custom_agents: Any = None,
        mcp_servers: Any = None,
    ) -> None:
        self.project_path = project_path
        self.config = config
        self.on_ask_user = on_ask_user
        self.resume_session_id = resume_session_id
        self.effort = effort
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.max_budget_usd = max_budget_usd
        self.fork_session = fork_session
        self.add_dirs = add_dirs
        self.from_pr = from_pr
        self.worktree = worktree
        self.agent_name = agent_name
        self.custom_agents = custom_agents
        self.mcp_servers = mcp_servers
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
# create_session with new params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_with_effort(cfg: Config, tmp_path) -> None:
    """create_session passes effort to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(20, "/tmp/p", cfg, effort="high")
    assert bridge.effort == "high"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_tools(cfg: Config, tmp_path) -> None:
    """create_session passes allowed/disallowed tools to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(
        21, "/tmp/p", cfg,
        allowed_tools=["Bash", "Read"],
        disallowed_tools=["WebSearch"],
    )
    assert bridge.allowed_tools == ["Bash", "Read"]
    assert bridge.disallowed_tools == ["WebSearch"]


@pytest.mark.asyncio
async def test_create_session_with_budget(cfg: Config, tmp_path) -> None:
    """create_session passes max_budget_usd to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(22, "/tmp/p", cfg, max_budget_usd=5.0)
    assert bridge.max_budget_usd == 5.0


@pytest.mark.asyncio
async def test_create_session_with_fork(cfg: Config, tmp_path) -> None:
    """create_session passes fork_session to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(
        23, "/tmp/p", cfg,
        resume_session_id="sess-abc",
        fork_session=True,
    )
    assert bridge.fork_session is True
    assert bridge.resume_session_id == "sess-abc"



@pytest.mark.asyncio
async def test_create_session_with_add_dirs(cfg: Config, tmp_path) -> None:
    """create_session passes add_dirs to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(30, "/tmp/p", cfg, add_dirs=["/tmp/extra"])
    assert bridge.add_dirs == ["/tmp/extra"]
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_from_pr(cfg: Config, tmp_path) -> None:
    """create_session passes from_pr to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(31, "/tmp/p", cfg, from_pr="123")
    assert bridge.from_pr == "123"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_worktree(cfg: Config, tmp_path) -> None:
    """create_session passes worktree to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    bridge = await sm.create_session(32, "/tmp/p", cfg, worktree="feature-branch")
    assert bridge.worktree == "feature-branch"
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_agent(cfg: Config, tmp_path) -> None:
    """create_session passes agent_name and custom_agents to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    agents = {"reviewer": {"description": "Code reviewer", "prompt": "Review code carefully"}}
    bridge = await sm.create_session(
        40, "/tmp/p", cfg,
        agent_name="reviewer",
        custom_agents=agents,
    )
    assert bridge.agent_name == "reviewer"
    assert bridge.custom_agents == agents
    assert bridge.started


@pytest.mark.asyncio
async def test_create_session_with_mcp_servers(cfg: Config, tmp_path) -> None:
    """create_session passes mcp_servers to ClaudeBridge."""
    sm = _make_sm(tmp_path)
    mcp = {"myserver": {"type": "stdio", "command": "npx", "args": ["-y", "server"]}}
    bridge = await sm.create_session(41, "/tmp/p", cfg, mcp_servers=mcp)
    assert bridge.mcp_servers == mcp
    assert bridge.started
