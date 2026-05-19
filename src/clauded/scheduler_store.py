"""#241 — persistent storage for scheduled tasks.

Mirrors :mod:`clauded.session_store` patterns:

- JSON file at ``<data_dir>/schedules.json``
- atomic write via ``.tmp`` + ``os.fsync`` + ``os.replace``
- fail-soft on corrupt file (start with empty state, log WARNING)

Schema per #241 §持久化 schema. Keyed on ``schedule_id`` (uuid4).

Each entry stores enough state to:

* Compute the next ``next_fire_at`` after each fire
* Detect ``missed`` fires across bot restarts
* Audit who created what (claude vs user) for the permission gate
* Reload + resume after process restart
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("clauded.scheduler_store")


class SchedulerStore:
    """Map of ``schedule_id`` -> schedule dict, persisted to JSON."""

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "schedules.json"
        self._schedules: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._schedules = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "#241: schedules.json corrupted / unreadable (%s); starting fresh",
                exc,
            )
            self._schedules = {}

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._schedules, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, schedule: dict[str, Any]) -> str:
        """Add or replace a schedule. Returns the ``schedule_id``."""
        sid = schedule.get("schedule_id") or uuid.uuid4().hex
        schedule["schedule_id"] = sid
        self._schedules[sid] = schedule
        self._save()
        return sid

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        return self._schedules.get(schedule_id)

    def delete(self, schedule_id: str) -> bool:
        if schedule_id in self._schedules:
            del self._schedules[schedule_id]
            self._save()
            return True
        return False

    def list_all(self) -> dict[str, dict[str, Any]]:
        return dict(self._schedules)

    def list_for_thread(self, thread_id: int) -> list[dict[str, Any]]:
        return [
            s for s in self._schedules.values()
            if s.get("target_thread_id") == thread_id
        ]

    def list_for_channel(self, channel_id: int) -> list[dict[str, Any]]:
        return [
            s for s in self._schedules.values()
            if s.get("channel_id") == channel_id
        ]

    def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return [
            s for s in self._schedules.values()
            if str(s.get("created_by")) == str(user_id)
        ]

    def count_active_for_user(self, user_id: int) -> int:
        return sum(
            1 for s in self._schedules.values()
            if str(s.get("created_by")) == str(user_id)
            and s.get("state", {}).get("enabled", True)
        )

    def count_active_total(self) -> int:
        return sum(
            1 for s in self._schedules.values()
            if s.get("state", {}).get("enabled", True)
        )

    def update_state(self, schedule_id: str, **state_changes: Any) -> None:
        """Patch a schedule's ``state`` sub-dict and persist."""
        sched = self._schedules.get(schedule_id)
        if sched is None:
            return
        sched.setdefault("state", {}).update(state_changes)
        self._save()

    def all_active(self) -> list[dict[str, Any]]:
        return [
            s for s in self._schedules.values()
            if s.get("state", {}).get("enabled", True)
        ]
