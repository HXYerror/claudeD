"""In-memory registry of active Claude sessions, keyed by Discord thread id."""

from __future__ import annotations

import asyncio
import logging

from .claude_bridge import ClaudeBridge
from .config import Config
from .session_config import SessionConfig
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
        session_config: SessionConfig | None = None,
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
            session_config=session_config,
        )
        await bridge.start()
        self._sessions[thread_id] = bridge
        # Make sure a lock exists for this thread; future callers will reuse it.
        self.get_lock(thread_id)
        resume_id = session_config.resume_session_id if session_config else None
        log.info("Created session thread=%s cwd=%s resume=%s", thread_id, project_path, resume_id)
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
        # Reap idle lock entries so the dict doesn't grow unbounded over the
        # bot's lifetime. review E6: sweep ALL currently-unlocked entries, not
        # just this thread_id — ``get_lock`` is also called for scheduler
        # lock_ids that never open a session and so never reach this reaper via
        # their own thread_id (the original leak). A held lock (``locked()``)
        # means an in-flight render, so we leave it; the next sweep collects it.
        # Safe because callers acquire atomically — ``async with
        # get_lock(tid):`` has no await between create and acquire (asyncio
        # Lock's uncontended acquire is synchronous), so an unlocked entry has
        # no paused acquirer that could be stranded on a stale lock object.
        for tid in [t for t, lk in self._locks.items() if not lk.locked()]:
            self._locks.pop(tid, None)
        log.info("Stopped session thread=%s", thread_id)
        return True

    def save_session_state(self, thread_id: int) -> None:
        """Persist the current session state for ``thread_id`` to the store.

        #295: The store now holds only ``session_id`` and
        ``permission_mode_override`` (plus ``last_active``). The former
        shadow fields (``project_path``, ``system_prompt``, ``model``)
        have canonical sources on ``ProjectManager`` / are already
        deprecated (#210), so we no longer duplicate them here.

        #211 semantics preserved: we persist ONLY the user-explicit
        override (``bridge.permission_mode_override``). Missing/None
        means the user has not run ``/mode set`` / cycle on this thread
        and readers should fall back to env / CLI settings.
        """
        bridge = self._sessions.get(thread_id)
        if bridge and bridge.session_id:
            self._session_store.save_session(
                thread_id,
                bridge.session_id,
                permission_mode_override=getattr(
                    bridge, "permission_mode_override", None
                ),
            )

    def get_stored_session(self, thread_id: int) -> dict | None:
        """Return persisted session metadata for ``thread_id``, or ``None``."""
        return self._session_store.get_session_info(thread_id)

    async def clear_session(self, thread_id: int) -> tuple[bool, bool]:
        """Tear down live bridge AND drop persisted resume entry atomically.

        Used by ``/session clear`` (#163 sub-task 2). Holds the per-thread
        lock for the entire stop+remove sequence so a concurrent
        ``/session resume`` (which also takes the lock) can't race in and
        re-persist the session between our stop and remove calls.

        Returns a ``(had_active, had_stored)`` tuple so the caller can
        choose between a success embed and a 'no session to clear'
        message without re-querying after the side effect.
        """
        async with self.get_lock(thread_id):
            had_stored = self._session_store.get_session_info(thread_id) is not None
            bridge = self._sessions.pop(thread_id, None)
            had_active = bridge is not None
            if bridge is not None:
                await bridge.stop()
            # Remove AFTER stop so save_session_state during teardown can't
            # re-persist what we're about to delete.
            self._session_store.remove_session(thread_id)
            return had_active, had_stored

    def list_sessions(self) -> dict[int, ClaudeBridge]:
        """Return a snapshot of all active sessions."""
        return dict(self._sessions)


__all__ = ["SessionManager"]
