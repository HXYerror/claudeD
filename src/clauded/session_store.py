"""Persist session metadata so sessions can be resumed after bot restart."""

from __future__ import annotations
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

from ._json_store import atomic_write_json

log = logging.getLogger("clauded.session_store")


# #295: fields that used to be persisted per-thread but now have canonical
# sources of truth elsewhere:
#   - ``model``          — deprecated by #210 (read side ignores it; write
#                          side wrote None); no consumer left.
#   - ``system_prompt``  — canonical source is
#                          ``ProjectManager.get_system_prompt(parent_id)``.
#   - ``project_path``   — canonical source is
#                          ``ProjectManager.get_path(parent_id)``.
# ``_load`` strips these on startup so we don't carry legacy shadow data
# forward, and ``save_session`` no longer writes them.
_LEGACY_SHADOW_FIELDS: tuple[str, ...] = ("model", "system_prompt", "project_path")


class SessionStore:
    """Maps thread_id -> session metadata, persisted to JSON."""

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "sessions.json"
        self._sessions: dict[str, dict] = {}
        # #252: RLock so save_session/remove_session can hold the lock
        # across read-modify-write and _save (which reacquires).
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                self._sessions = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Failed to load sessions.json, starting fresh")
            self._sessions = {}
        # #295: one-shot startup migration — strip the shadow fields
        # (``model``, ``system_prompt``, ``project_path``) from every entry
        # so operators aren't stuck with stale copies of data whose
        # canonical source is ``ProjectManager`` (or, for ``model``, an
        # already-deprecated no-op from #210). We ONLY drop these fields;
        # ``session_id``, ``last_active``, ``permission_mode_override`` and
        # any future schema additions are preserved verbatim.
        stripped = 0
        for entry in self._sessions.values():
            if not isinstance(entry, dict):
                continue
            for field in _LEGACY_SHADOW_FIELDS:
                if field in entry:
                    del entry[field]
                    stripped += 1
        if stripped:
            log.info(
                "#295: stripped %d legacy shadow field(s) from sessions.json",
                stripped,
            )
            self._save()

    def _save(self) -> None:
        atomic_write_json(self._path, self._sessions, self._lock)

    def save_session(
        self,
        thread_id: int,
        session_id: str,
        *,
        permission_mode_override: str | None = None,
    ) -> None:
        """Persist minimal per-thread session metadata.

        #295: ``project_path``, ``model`` and ``system_prompt`` are no
        longer persisted. Callers that need those values must read them
        from ``ProjectManager`` (which is the canonical source of truth)
        rather than shadowing them here.
        """
        with self._lock:
            self._sessions[str(thread_id)] = {
                "session_id": session_id,
                # #211: persist the user's explicit ``/mode set`` / cycle
                # choice so it survives bot restart (per PRD user
                # decision #4). Legacy rows without this field safely
                # return None via ``dict.get``.
                "permission_mode_override": permission_mode_override,
                "last_active": datetime.now(timezone.utc).isoformat(),
            }
            self._save()

    def get_session_info(self, thread_id: int) -> dict | None:
        return self._sessions.get(str(thread_id))

    def remove_session(self, thread_id: int) -> None:
        key = str(thread_id)
        with self._lock:
            if key in self._sessions:
                del self._sessions[key]
                self._save()

    def list_all(self) -> dict[str, dict]:
        return dict(self._sessions)
