"""In-memory registry of active Claude sessions, keyed by Discord thread id."""

from __future__ import annotations

import asyncio
import logging

from .claude_bridge import ClaudeBridge, OnAskUser, OnPreToolUse, OnPostToolUse, OnStop
from .config import Config
from .session_store import SessionStore

log = logging.getLogger("clauded.session_manager")


class SessionManager:
    """Tracks one :class:`ClaudeBridge` per Discord thread."""

    def __init__(self, session_store: SessionStore | None = None) -> None:
        self._sessions: dict[int, ClaudeBridge] = {}
        # One asyncio.Lock per thread, used by callers to serialize message
        # processing against the same Claude session. The lock outlives any
        # single bridge so concurrent producers don't all race to (re)create
        # a session in parallel.
        self._locks: dict[int, asyncio.Lock] = {}
        self._session_store = session_store or SessionStore()

    def get_lock(self, thread_id: int) -> asyncio.Lock:
        """Return (creating if needed) the lock for ``thread_id``.

        Callers should ``async with manager.get_lock(thread_id):`` around
        any send/render cycle so messages in the same thread don't race.
        """
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    async def create_session(
        self,
        thread_id: int,
        project_path: str,
        config: Config,
        on_ask_user: OnAskUser | None = None,
        on_pre_tool_use: OnPreToolUse | None = None,
        on_post_tool_use: OnPostToolUse | None = None,
        on_stop: OnStop | None = None,
        system_prompt: str | None = None,
        env: dict[str, str] | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        effort: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        fork_session: bool = False,
        add_dirs: list[str] | None = None,
        from_pr: str | None = None,
        worktree: str | None = None,
        agent_name: str | None = None,
        custom_agents: dict | None = None,
        mcp_servers: dict | None = None,
        max_turns: int | None = None,
        fallback_model: str | None = None,
        plugin_dirs: list[str] | None = None,
        settings: str | None = None,
    ) -> ClaudeBridge:
        """Create, start, and register a new session for ``thread_id``.

        If a session already exists for the thread it is stopped first so
        we never leak a connected client.
        """
        existing = self._sessions.pop(thread_id, None)
        if existing is not None:
            log.info("Replacing existing session for thread=%s", thread_id)
            await existing.stop()

        bridge = ClaudeBridge(
            project_path=project_path,
            config=config,
            on_ask_user=on_ask_user,
            on_pre_tool_use=on_pre_tool_use,
            on_post_tool_use=on_post_tool_use,
            on_stop=on_stop,
            system_prompt=system_prompt,
            env=env,
            model_override=model_override,
            resume_session_id=resume_session_id,
            effort=effort,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            max_budget_usd=max_budget_usd,
            fork_session=fork_session,
            add_dirs=add_dirs,
            from_pr=from_pr,
            worktree=worktree,
            agent_name=agent_name,
            custom_agents=custom_agents,
            mcp_servers=mcp_servers,
            max_turns=max_turns,
            fallback_model=fallback_model,
            plugin_dirs=plugin_dirs,
            settings=settings,
        )
        await bridge.start()
        self._sessions[thread_id] = bridge
        # Make sure a lock exists for this thread; future callers will reuse it.
        self.get_lock(thread_id)
        log.info("Created session thread=%s cwd=%s resume=%s", thread_id, project_path, resume_session_id)
        return bridge

    def get_session(self, thread_id: int) -> ClaudeBridge | None:
        """Return the live session for ``thread_id``, or ``None``."""
        return self._sessions.get(thread_id)

    async def stop_session(self, thread_id: int) -> bool:
        """Stop and forget the session for ``thread_id``.

        Returns ``True`` if a session was stopped, ``False`` if there was
        nothing to stop.
        """
        bridge = self._sessions.pop(thread_id, None)
        if bridge is None:
            return False
        await bridge.stop()
        # Reap the lock entry if no one is currently waiting on it.
        # Without this the dict grows unbounded over the bot's lifetime,
        # one entry per thread we've ever served. Holding the lock means
        # there's an in-flight render — leave the entry alone in that case
        # and let the next stop_session sweep it up.
        lock = self._locks.get(thread_id)
        if lock is not None and not lock.locked():
            self._locks.pop(thread_id, None)
        log.info("Stopped session thread=%s", thread_id)
        return True

    def save_session_state(self, thread_id: int) -> None:
        """Persist the current session state for ``thread_id`` to the store."""
        bridge = self._sessions.get(thread_id)
        if bridge and bridge.session_id:
            self._session_store.save_session(
                thread_id, bridge.session_id, bridge.project_path,
                model=bridge.model, system_prompt=bridge.system_prompt,
            )

    def get_stored_session(self, thread_id: int) -> dict | None:
        """Return persisted session metadata for ``thread_id``, or ``None``."""
        return self._session_store.get_session_info(thread_id)

    def list_sessions(self) -> dict[int, ClaudeBridge]:
        """Return a snapshot of all active sessions."""
        return dict(self._sessions)


__all__ = ["SessionManager"]
