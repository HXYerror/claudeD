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
        # #210: count legacy rows with a non-null ``model`` field for
        # operator visibility. The field is deprecated as of v1.18 — read
        # paths ignore it and write paths emit None. We do NOT mutate the
        # file here (forensic preservation); the count naturally shrinks
        # as old entries are overwritten or expire via the 1h idle GC.
        legacy_count = sum(
            1
            for v in self._sessions.values()
            if isinstance(v, dict) and v.get("model") is not None
        )
        if legacy_count:
            log.info(
                "#210: %d legacy stored.model entries ignored "
                "(deprecated field; not mutating data)",
                legacy_count,
            )

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._sessions, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def save_session(self, thread_id: int, session_id: str, project_path: str,
                     model: str | None = None, system_prompt: str | None = None,
                     permission_mode_override: str | None = None) -> None:
        self._sessions[str(thread_id)] = {
            "session_id": session_id,
            "project_path": project_path,
            "model": model,
            "system_prompt": system_prompt,
            # #211: persist the user's explicit ``/mode set`` / cycle choice
            # so it survives bot restart (per PRD user decision #4). Unlike
            # ``model`` (which #210 made vestigial because of legacy
            # pollution), ``permission_mode_override`` is a fresh schema
            # field with no prior writes — read-side honors it unmodified.
            "permission_mode_override": permission_mode_override,
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
