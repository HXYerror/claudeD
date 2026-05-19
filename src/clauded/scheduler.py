"""Scheduler core — trigger parsing + SchedulerManager (no fire executor).

This module is part of issue #241 / PRD v1.18 (``docs/prd/v1.18-scheduler.md``).
Subtask 1 covers:

* Module-level trigger parsing helpers
  (:func:`parse_iso_utc`, :func:`parse_cron_to_next`, :func:`compute_next_fire`,
  :func:`parse_duration`).
* :class:`SchedulerManager` — ``__init__``, ``create``, ``delete``, ``toggle``.

Subtask 2 will extend this module with ``tick`` / ``catch_up`` / ``_fire_one``.

Intentional constraints:
* No ``discord`` imports here — fire callbacks are injected by the bot wiring
  layer so this module stays unit-testable in isolation.
* All times persisted as ISO-8601 UTC strings (``...+00:00``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .scheduler_store import SchedulerStore

log = logging.getLogger(__name__)


# --------------------------------------------------------------- Constants

TICK_INTERVAL_S = 15
CLAUDE_MIN_INTERVAL_S = 300
MISSED_FIRE_GRACE_S = 300
MAX_GLOBAL_INFLIGHT = 10
MAX_USER_ACTIVE = 20
MAX_GLOBAL_ACTIVE = 100
MAX_LIFETIME_SECONDS = 31_536_000  # 365 days


# ------------------------------------------------------------ Duration regex

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_DURATION_MULT = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 604_800,
}


# -------------------------------------------------------- Trigger parsing

def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO-8601 datetime, normalized to UTC.

    Accepts both raw (``"2026-05-19T09:00:00+08:00"``) and the prefixed
    form used in tool args / persistence (``"iso: 2026-05-19T09:00:00+08:00"``).

    Naive datetimes (no tzinfo) are rejected — we never silently assume UTC.

    Raises:
        ValueError: garbage input or missing tzinfo.
    """
    if not isinstance(s, str):
        raise ValueError(f"iso datetime must be string, got {type(s).__name__}")
    raw = s[4:] if s.startswith("iso:") else s
    raw = raw.strip()
    if not raw:
        raise ValueError("iso datetime is empty")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid iso datetime: {raw!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"iso datetime must include timezone offset: {raw!r}"
        )
    return dt.astimezone(timezone.utc)


def parse_cron_to_next(
    cron: str,
    tz_name: str,
    base: datetime | None = None,
) -> datetime:
    """Return the next fire time for a cron expression as a UTC datetime.

    ``cron`` may be raw (``"0 9 * * *"``) or prefixed (``"cron: 0 9 * * *"``).
    ``tz_name`` is an IANA zone name (e.g. ``"Asia/Shanghai"``) — bad zones
    raise :class:`ValueError`. ``base`` defaults to ``datetime.now(UTC)``;
    if naive it is treated as UTC.

    Raises:
        ValueError: invalid cron expression or unknown timezone.
    """
    if not isinstance(cron, str):
        raise ValueError(f"cron must be string, got {type(cron).__name__}")
    raw = cron[5:] if cron.startswith("cron:") else cron
    raw = raw.strip()
    if not raw:
        raise ValueError("cron expression is empty")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ValueError(f"invalid timezone: {tz_name!r}") from exc

    if base is None:
        base = datetime.now(timezone.utc)
    elif base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base_local = base.astimezone(tz)

    try:
        it = croniter(raw, base_local)
        nxt = it.get_next(datetime)
    except Exception as exc:  # croniter raises a variety of types
        raise ValueError(f"invalid cron expression: {raw!r} ({exc})") from exc

    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(timezone.utc)


def compute_next_fire(
    trigger: dict,
    after: datetime | None = None,
) -> datetime | None:
    """Return the next fire datetime (UTC) for a persisted ``trigger`` dict.

    For one-shot (``trigger["kind"] == "once"``) returns the stored iso
    datetime if it is in the future relative to ``after`` (default: now),
    otherwise ``None``. For cron (``trigger["kind"] == "cron"``) returns
    the next fire after ``after``. Malformed triggers return ``None``
    (callers should not depend on this swallowing errors — it exists for
    robust scheduling against possibly-stale persisted records).
    """
    now = after if after is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    tk = trigger.get("kind")
    if tk == "once":
        iso_str = trigger.get("iso")
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return None
        dt = dt.astimezone(timezone.utc)
        return dt if dt > now else None

    if tk == "cron":
        cron = trigger.get("cron")
        tz_name = trigger.get("tz_when_created", "UTC")
        if not cron:
            return None
        try:
            return parse_cron_to_next(cron, tz_name, base=now)
        except ValueError:
            return None

    return None


def parse_duration(s: str) -> int:
    """Parse a duration string (``"30d"`` / ``"24h"`` / ``"1w"`` / ...) to seconds.

    Allowed suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days), ``w`` (weeks). Results greater than
    :data:`MAX_LIFETIME_SECONDS` (365 days) raise :class:`ValueError`.

    Raises:
        ValueError: bad format, bad suffix, or above 365-day cap.
    """
    if not isinstance(s, str):
        raise ValueError(
            f"duration must be string, got {type(s).__name__}"
        )
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(
            f"invalid duration: {s!r} (expected '<n><s|m|h|d|w>', e.g. '30d')"
        )
    n = int(m.group(1))
    suffix = m.group(2)
    secs = n * _DURATION_MULT[suffix]
    if secs > MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"duration {s!r} exceeds max 365d ({MAX_LIFETIME_SECONDS}s)"
        )
    return secs


# ----------------------------------------------------------- Manager class

FireCallback = Callable[[dict], Awaitable[None]]
LockProvider = Callable[[int], asyncio.Lock]


class SchedulerManager:
    """Owner of the schedule lifecycle (create / delete / toggle).

    Subtask 1 (this module) implements the validation + persistence side.
    The ``tick`` / ``catch_up`` / ``_fire_one`` methods that drive actual
    firing are added in Subtask 2. The constructor already accepts the
    fire callbacks so wiring can land later without re-arranging this API.
    """

    def __init__(
        self,
        store: SchedulerStore,
        *,
        fire_message_callback: FireCallback,
        fire_new_task_callback: FireCallback,
        expire_notify_callback: FireCallback | None = None,
        get_lock: LockProvider | None = None,
    ) -> None:
        self.store = store
        self.fire_message_callback = fire_message_callback
        self.fire_new_task_callback = fire_new_task_callback
        self.expire_notify_callback = expire_notify_callback

        if get_lock is None:
            # Lazy-instantiate to avoid creating a lock outside any loop
            # before tests / wiring need one.
            self._default_lock: asyncio.Lock | None = None

            def _shared_lock(_thread_id: int) -> asyncio.Lock:
                if self._default_lock is None:
                    self._default_lock = asyncio.Lock()
                return self._default_lock

            self.get_lock = _shared_lock
        else:
            self.get_lock = get_lock

    # ----------------------------------------------------- Permission

    @staticmethod
    def _check_permission(
        sched: dict,
        requester: str | int,
        is_admin: bool,
    ) -> tuple[bool, str]:
        """Apply the PRD permission matrix.

        See ``docs/prd/v1.18-scheduler.md`` (`权限模型`):

        * ``requester == "claude"`` may only act on claude-created schedules
          (admin flag is ignored — claude does not get admin override).
        * otherwise: equal-string match on ``created_by`` is OK, else
          ``is_admin`` overrides, else deny.
        """
        requester_s = str(requester)
        created_by = str(sched.get("created_by"))
        if requester_s == "claude":
            if created_by == "claude":
                return True, "ok"
            return False, "claude can only manage claude-created schedules"
        if requester_s == created_by:
            return True, "ok"
        if is_admin:
            return True, "ok"
        return False, "permission denied"

    # ------------------------------------------------------------ Create

    def create(
        self,
        *,
        kind: Literal["message", "new_task"],
        when: str,
        what: str,
        target_thread_id: int | None = None,
        target_channel_id: int | None = None,
        thread_name: str | None = None,
        name: str | None = None,
        recurring: bool = False,
        max_lifetime: str | None = None,
        created_by: str | int,
        is_claude_created: bool = False,
        guild_id: int | None = None,
        channel_id: int | None = None,
        tz_name: str = "Asia/Shanghai",
    ) -> dict:
        """Create + persist a schedule.

        Returns the full schedule dict (same shape as
        ``docs/prd/v1.18-scheduler.md §5``).

        Raises:
            ValueError: any validation failure (see PRD §3.8 and §4 for the
                full ruleset enforced here).
        """
        # 1. what
        if not isinstance(what, str) or len(what) == 0 or len(what) > 500:
            raise ValueError("what must be non-empty and ≤500 chars")

        # 2. name length
        if name is not None and len(name) > 50:
            raise ValueError("name ≤50 chars")

        # 3. thread_name length
        if thread_name is not None and len(thread_name) > 50:
            raise ValueError("thread_name ≤50 chars")

        # 4. kind-specific target validation
        if kind == "message":
            if (
                not isinstance(target_thread_id, int)
                or isinstance(target_thread_id, bool)
                or target_thread_id <= 0
            ):
                raise ValueError(
                    "kind=message requires positive int target_thread_id"
                )
        elif kind == "new_task":
            if (
                not isinstance(target_channel_id, int)
                or isinstance(target_channel_id, bool)
                or target_channel_id <= 0
            ):
                raise ValueError(
                    "kind=new_task requires positive int target_channel_id"
                )
        else:
            raise ValueError(f"unknown kind: {kind!r}")

        # 5. when prefix + parse
        if not isinstance(when, str):
            raise ValueError("when must start with cron: or iso:")
        if when.startswith("iso:"):
            trigger_kind = "once"
            next_fire = parse_iso_utc(when)
            now_utc = datetime.now(timezone.utc)
            if next_fire <= now_utc:
                raise ValueError("iso must be in future")
            iso_str: str | None = next_fire.isoformat()
            cron_str: str | None = None
        elif when.startswith("cron:"):
            trigger_kind = "cron"
            # parse_cron_to_next validates tz + cron in one go
            next_fire = parse_cron_to_next(when, tz_name)
            cron_str = when[5:].strip()
            iso_str = None
        else:
            raise ValueError("when must start with cron: or iso:")

        # 6. recurring only valid with cron
        if recurring and trigger_kind == "once":
            raise ValueError("recurring only valid with cron")

        # 7. claude min interval for cron
        if trigger_kind == "cron" and is_claude_created:
            assert cron_str is not None  # narrowed by branch
            tz = ZoneInfo(tz_name)  # validated above
            base_local = datetime.now(timezone.utc).astimezone(tz)
            it = croniter(cron_str, base_local)
            t1 = it.get_next(datetime)
            t2 = it.get_next(datetime)
            if t1.tzinfo is None:
                t1 = t1.replace(tzinfo=tz)
                t2 = t2.replace(tzinfo=tz)
            gap = (t2 - t1).total_seconds()
            if gap < CLAUDE_MIN_INTERVAL_S:
                raise ValueError(
                    "min interval 5 min for claude-created schedules"
                )

        # 8. max_lifetime semantics
        max_lifetime_seconds: int | None = None
        if max_lifetime is not None:
            if not recurring:
                raise ValueError(
                    "max_lifetime only valid with recurring=true"
                )
            max_lifetime_seconds = parse_duration(max_lifetime)  # caps at 365d

        # 9. caps (per-user + global)
        user_key = str(created_by)
        if self.store.count_active_for_user(user_key) >= MAX_USER_ACTIVE:
            raise ValueError(
                f"per-user active cap reached ({MAX_USER_ACTIVE})"
            )
        if self.store.count_active_total() >= MAX_GLOBAL_ACTIVE:
            raise ValueError(
                f"global active cap reached ({MAX_GLOBAL_ACTIVE})"
            )

        # 10. build + persist
        sched_id = secrets.token_hex(8)
        now_iso = datetime.now(timezone.utc).isoformat()
        sched: dict = {
            "schedule_id": sched_id,
            "kind": kind,
            "name": name if name else sched_id[:8],
            "created_at": now_iso,
            "created_by": user_key,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "target_thread_id": target_thread_id if kind == "message" else None,
            "target_channel_id": (
                target_channel_id if kind == "new_task" else None
            ),
            "thread_name": thread_name if kind == "new_task" else None,
            "trigger": {
                "kind": trigger_kind,
                "iso": iso_str,
                "cron": cron_str,
                "tz_when_created": tz_name,
                "recurring": recurring,
            },
            "payload": {"what": what},
            "max_lifetime_seconds": max_lifetime_seconds,
            "state": {
                "enabled": True,
                "next_fire_at": next_fire.isoformat(),
                "first_fired_at": None,
                "last_fired_at": None,
                "last_error": None,
                "fire_count": 0,
                "missed_count": 0,
            },
        }
        self.store.add(sched)
        log.info(
            "schedule created id=%s kind=%s trigger=%s recurring=%s by=%s",
            sched_id, kind, trigger_kind, recurring, user_key,
        )
        return sched

    # ------------------------------------------------------------ Delete

    def delete(
        self,
        sched_id: str,
        *,
        requester: str | int,
        is_admin: bool = False,
    ) -> tuple[bool, str]:
        """Delete a schedule. Returns ``(ok, reason)``.

        See :meth:`_check_permission` for the access matrix. Missing IDs
        return ``(False, "not found")``.
        """
        sched = self.store.get(sched_id)
        if sched is None:
            return False, "not found"
        ok, reason = self._check_permission(sched, requester, is_admin)
        if not ok:
            return False, reason
        self.store.delete(sched_id)
        log.info(
            "schedule deleted id=%s by=%s admin=%s",
            sched_id, requester, is_admin,
        )
        return True, "ok"

    # ------------------------------------------------------------ Toggle

    def toggle(
        self,
        sched_id: str,
        enabled: bool,
        *,
        requester: str | int,
        is_admin: bool = False,
    ) -> tuple[bool, str]:
        """Enable / disable a schedule. Returns ``(ok, reason)``.

        Same permission matrix as :meth:`delete`. The fire loop (Subtask 2)
        will pick up the new enabled flag on its next tick.
        """
        sched = self.store.get(sched_id)
        if sched is None:
            return False, "not found"
        ok, reason = self._check_permission(sched, requester, is_admin)
        if not ok:
            return False, reason
        sched.setdefault("state", {})["enabled"] = bool(enabled)
        self.store.save(sched)
        log.info(
            "schedule toggle id=%s enabled=%s by=%s admin=%s",
            sched_id, enabled, requester, is_admin,
        )
        return True, "ok"
