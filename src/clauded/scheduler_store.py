"""Persist scheduled-timer metadata so `/schedule` survives bot restart.

Schema per PRD §5 (docs/prd/v1.18-scheduler.md). Atomic write follows the
same pattern as :mod:`clauded.session_store` — tmp + fsync + os.replace.
Corrupt JSON falls back to an empty dict + WARNING (never raises).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)


class SchedulerStore:
    """Maps schedule_id -> schedule dict, persisted to ``data/schedules.json``.

    The on-disk shape is exactly the dict produced by
    :meth:`clauded.scheduler.SchedulerManager.create` (see PRD §5). This
    class is intentionally schema-agnostic — it stores and retrieves dicts;
    higher-level validation lives in :class:`SchedulerManager`.
    """

    def __init__(self, data_dir: str = "data") -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "schedules.json"
        self._schedules: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log.warning(
                    "schedules.json top-level is %s, expected dict; starting fresh",
                    type(data).__name__,
                )
                self._schedules = {}
                return
            self._schedules = data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "Failed to load schedules.json (%s); starting fresh", exc
            )
            self._schedules = {}

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._schedules, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # ---------------------------------------------------------------- CRUD

    def add(self, sched: dict) -> str:
        """Insert a new schedule and return its ``schedule_id``.

        If the caller did not pre-populate ``sched["schedule_id"]``, a
        fresh 16-char hex token is generated via :func:`secrets.token_hex`.
        """
        sid = sched.get("schedule_id")
        if not sid:
            sid = secrets.token_hex(8)
            sched["schedule_id"] = sid
        self._schedules[sid] = sched
        self._save()
        return sid

    def get(self, sched_id: str) -> dict | None:
        """Return the schedule dict for ``sched_id`` or None if missing."""
        return self._schedules.get(sched_id)

    def delete(self, sched_id: str) -> bool:
        """Remove ``sched_id`` from the store. Returns False if not present."""
        if sched_id in self._schedules:
            del self._schedules[sched_id]
            self._save()
            return True
        return False

    def save(self, sched: dict) -> None:
        """Persist a mutated schedule back to disk (write-through)."""
        sid = sched.get("schedule_id")
        if not sid:
            raise ValueError("save() requires schedule_id field on sched dict")
        self._schedules[sid] = sched
        self._save()

    # Alias for clarity at call sites.
    update = save

    # ------------------------------------------------------------ Queries

    def list_all(self) -> dict[str, dict]:
        """Return a shallow copy of all schedules keyed by schedule_id."""
        return dict(self._schedules)

    def list_for_thread(self, thread_id: int) -> list[dict]:
        """All ``kind=message`` schedules whose ``target_thread_id`` matches.

        ``kind=new_task`` schedules are never bound to a thread (they create
        threads) and are therefore excluded.
        """
        out: list[dict] = []
        for sched in self._schedules.values():
            if sched.get("kind") != "message":
                continue
            if sched.get("target_thread_id") == thread_id:
                out.append(sched)
        return out

    def list_for_channel(self, channel_id: int) -> list[dict]:
        """All schedules tied to ``channel_id``.

        Includes:
          * any schedule whose ``channel_id`` (creation channel) matches, OR
          * ``kind=new_task`` schedules whose ``target_channel_id`` matches.
        """
        out: list[dict] = []
        for sched in self._schedules.values():
            if sched.get("channel_id") == channel_id:
                out.append(sched)
                continue
            if (
                sched.get("kind") == "new_task"
                and sched.get("target_channel_id") == channel_id
            ):
                out.append(sched)
        return out

    def count_active_for_user(self, user_id: str) -> int:
        """Count enabled schedules created by ``user_id`` (string equality)."""
        target = str(user_id)
        n = 0
        for sched in self._schedules.values():
            if str(sched.get("created_by")) != target:
                continue
            state = sched.get("state") or {}
            if state.get("enabled", False):
                n += 1
        return n

    def count_active_total(self) -> int:
        """Count enabled schedules across all creators (both kinds)."""
        n = 0
        for sched in self._schedules.values():
            state = sched.get("state") or {}
            if state.get("enabled", False):
                n += 1
        return n
