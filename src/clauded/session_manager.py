"""In-memory registry of active Claude sessions, keyed by Discord thread id."""

from __future__ import annotations

import logging

from .claude_bridge import ClaudeBridge, OnAskUser
from .config import Config

log = logging.getLogger("clauded.session_manager")


class SessionManager:
    """Tracks one :class:`ClaudeBridge` per Discord thread."""

    def __init__(self) -> None:
        self._sessions: dict[int, ClaudeBridge] = {}

    async def create_session(
        self,
        thread_id: int,
        project_path: str,
        config: Config,
        on_ask_user: OnAskUser | None = None,
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
        )
        await bridge.start()
        self._sessions[thread_id] = bridge
        log.info("Created session thread=%s cwd=%s", thread_id, project_path)
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
        log.info("Stopped session thread=%s", thread_id)
        return True


__all__ = ["SessionManager"]
