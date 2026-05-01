"""Persist session metadata so sessions can be resumed after bot restart."""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("clauded.session_store")


class SessionStore:
    """Maps thread_id -> session metadata, persisted to JSON."""

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "sessions.json"
        self._sessions: dict[str, dict] = {}
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

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._sessions, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def save_session(self, thread_id: int, session_id: str, project_path: str,
                     model: str | None = None, system_prompt: str | None = None) -> None:
        self._sessions[str(thread_id)] = {
            "session_id": session_id,
            "project_path": project_path,
            "model": model,
            "system_prompt": system_prompt,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_session_info(self, thread_id: int) -> dict | None:
        return self._sessions.get(str(thread_id))

    def remove_session(self, thread_id: int) -> None:
        key = str(thread_id)
        if key in self._sessions:
            del self._sessions[key]
            self._save()

    def list_all(self) -> dict[str, dict]:
        return dict(self._sessions)
