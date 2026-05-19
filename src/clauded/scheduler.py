"""Scheduler core — trigger parsing + SchedulerManager (with fire executor).

This module is part of issue #241 / PRD v1.18 (``docs/prd/v1.18-scheduler.md``).

* Subtask 1: module-level trigger parsing helpers
  (:func:`parse_iso_utc`, :func:`parse_cron_to_next`, :func:`compute_next_fire`,
  :func:`parse_duration`); :class:`SchedulerManager` ``__init__`` / ``create``
  / ``delete`` / ``toggle``.
* Subtask 2 (this revision): :meth:`SchedulerManager.tick`,
  :meth:`~SchedulerManager.catch_up`, :meth:`~SchedulerManager._fire_one`,
  ``_fire_with_retry``, ``_on_fire_success``, ``_on_fire_terminal``,
  ``_check_max_lifetime``, ``_now_utc``.

Intentional constraints:
* All fire actions go through injected callbacks; this module never references
  ``discord.Thread`` / ``discord.Client`` etc. ``discord`` is imported only
  for its exception types (``NotFound`` / ``Forbidden``) so the retry/terminal
  branches can distinguish "stop trying" from "transient retry" without
  leaking discord objects into the manager's contract.
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

import discord  # Subtask 2: catch discord.NotFound / discord.Forbidden as terminal
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
BoundChecker = Callable[[int], bool]


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
        bound_checker: BoundChecker | None = None,
    ) -> None:
        self.store = store
        self.fire_message_callback = fire_message_callback
        self.fire_new_task_callback = fire_new_task_callback
        self.expire_notify_callback = expire_notify_callback
        # M4: optional bound-channel validator. If supplied, ``create``
        # rejects ``kind=new_task`` schedules whose ``target_channel_id``
        # is not bound to a project (avoiding the late "channel not bound"
        # RuntimeError at fire time). Defaults to ``None`` (no check) so
        # tests / non-Discord callers don't need a project_manager.
        self.bound_checker = bound_checker

        if get_lock is None:
            # Default: per-target (thread_id or channel_id) ``asyncio.Lock``s,
            # lazily created the first time each id is seen. Caller-supplied
            # ``get_lock`` (e.g. ``bot.session_manager.get_lock``) overrides
            # this so production fire shares the same per-thread lock the
            # rest of the bot already uses.
            self._lock_map: dict[int, asyncio.Lock] = {}

            def _per_id_lock(target_id: int) -> asyncio.Lock:
                lk = self._lock_map.get(target_id)
                if lk is None:
                    lk = asyncio.Lock()
                    self._lock_map[target_id] = lk
                return lk

            self.get_lock = _per_id_lock
        else:
            self.get_lock = get_lock

        # Bound concurrent in-flight fires across all schedules — PRD §3.8
        # (``MAX_GLOBAL_INFLIGHT = 10``). Acquired inside ``_fire_one``.
        self._inflight_sem = asyncio.Semaphore(MAX_GLOBAL_INFLIGHT)

        # B1: track schedule_ids currently being fired so a slow callback
        # whose tick interval is shorter than its execution time can't be
        # re-dispatched on the next ``tick()`` / ``catch_up()`` pass. The
        # set is read by ``tick``/``catch_up`` before creating a fire task,
        # and ``_fire_one`` removes the id on completion (success, terminal,
        # or unexpected raise) so a transient outage can't strand the entry.
        self._inflight_ids: set[str] = set()

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
            # M4: surface the unbound-channel error at create() time rather
            # than letting the fire callback raise RuntimeError("not bound")
            # 1..30 days later. The checker is optional so library/test
            # callers without a project_manager are unaffected.
            if (
                self.bound_checker is not None
                and not self.bound_checker(target_channel_id)
            ):
                raise ValueError(
                    f"target_channel_id {target_channel_id} is not bound "
                    "to a project (run /project bind <path> first)"
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

    # =================================================================
    # Subtask 2 — fire executor + tick/catch_up + max_lifetime
    # =================================================================

    # ------------------------------------------------------- Time helper

    def _now_utc(self) -> datetime:
        """Return the current UTC datetime.

        Thin wrapper around ``datetime.now(timezone.utc)`` so tests can
        monkeypatch a deterministic clock without reaching into the
        global ``datetime`` module.
        """
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------ Tick

    @staticmethod
    def _parse_next_fire(s: str | None) -> datetime | None:
        """Parse a persisted ``next_fire_at`` ISO string to a UTC datetime.

        Returns ``None`` for missing / malformed values. Naive datetimes
        (no tzinfo) are normalized to UTC so the ``<= _now_utc()`` compare
        in :meth:`tick` / :meth:`catch_up` is always tz-aware.
        """
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    async def tick(self) -> None:
        """Scan all enabled schedules and dispatch any whose due time has arrived.

        Called on a periodic loop (PRD §3.8 ``TICK_INTERVAL_S = 15``). For
        every enabled schedule whose ``state.next_fire_at`` is ``<=`` now,
        an :meth:`_fire_one_safe` task is scheduled via ``asyncio.create_task``
        and this method returns immediately — the semaphore inside
        ``_fire_one`` is the only thing that bounds in-flight concurrency.

        B1: schedules currently being fired (id in ``self._inflight_ids``)
        are skipped — a slow callback whose runtime exceeds
        :data:`TICK_INTERVAL_S` must not be double-dispatched. The
        ``_inflight_ids.add(sid)`` happens HERE (synchronously, before the
        task is created) so the guard is effective on the very next
        ``tick`` iteration; if we deferred the add into ``_fire_one`` the
        check above could pass spuriously between two ticks both racing
        to dispatch the same schedule.
        """
        now = self._now_utc()
        for sched in self.store.list_all().values():
            state = sched.get("state") or {}
            if not state.get("enabled", False):
                continue
            sid = sched.get("schedule_id")
            if sid in self._inflight_ids:
                continue
            next_fire = self._parse_next_fire(state.get("next_fire_at"))
            if next_fire is None:
                continue
            if next_fire <= now:
                if sid is not None:
                    self._inflight_ids.add(sid)
                    asyncio.create_task(self._fire_one_safe(sid, sched))
                else:
                    asyncio.create_task(self._fire_one(sched))

    async def catch_up(self) -> None:
        """One-shot scan called on bot startup to handle missed fires.

        Schedules whose ``next_fire_at`` is past but within
        :data:`MISSED_FIRE_GRACE_S` (300s) get dispatched normally.
        Anything older is marked missed and rolled forward without
        firing (recurring) or disabled (one-shot deep past).

        B1: schedules currently in-flight are skipped here too — catch_up
        only runs once at startup, but the same belt-and-braces guard
        keeps the contract consistent with ``tick()``.
        """
        now = self._now_utc()
        for sched in self.store.list_all().values():
            state = sched.get("state") or {}
            if not state.get("enabled", False):
                continue
            sid = sched.get("schedule_id")
            if sid in self._inflight_ids:
                continue
            next_fire = self._parse_next_fire(state.get("next_fire_at"))
            if next_fire is None:
                continue
            if next_fire > now:
                continue

            age = (now - next_fire).total_seconds()
            if age <= MISSED_FIRE_GRACE_S:
                if sid is not None:
                    self._inflight_ids.add(sid)
                    asyncio.create_task(self._fire_one_safe(sid, sched))
                else:
                    asyncio.create_task(self._fire_one(sched))
                continue

            # Too stale to fire — mark missed and roll forward.
            state["missed_count"] = int(state.get("missed_count", 0)) + 1
            trigger = sched.get("trigger") or {}
            new_next = compute_next_fire(trigger, after=now)
            if new_next is None:
                # one-shot in deep past or unrecoverable trigger
                state["enabled"] = False
                state["next_fire_at"] = None
            else:
                state["next_fire_at"] = new_next.isoformat()
            sched["state"] = state
            self.store.save(sched)
            log.warning(
                "#241 missed fire schedule=%s age=%.0fs",
                sched.get("schedule_id"), age,
            )

    # ---------------------------------------------------- _fire_one core

    async def _fire_one_safe(self, sched_id: str, sched: dict) -> None:
        """Wrapper around :meth:`_fire_one` that guarantees inflight cleanup.

        B1: ``tick()`` / ``catch_up()`` add the ``sched_id`` to
        ``self._inflight_ids`` synchronously *before* creating this task
        (so the very next tick sees the guard). This wrapper makes the
        symmetric removal bulletproof — even if ``_fire_one`` raises an
        unexpected exception (cancelled task, programming error, etc.),
        the id is still discarded so the schedule isn't stranded out of
        the inflight set forever.
        """
        try:
            await self._fire_one(sched)
        finally:
            self._inflight_ids.discard(sched_id)

    async def _fire_one(self, sched: dict) -> None:
        """Acquire concurrency budget + per-target lock, then run the fire.

        The global semaphore caps simultaneous in-flight fires across all
        schedules at :data:`MAX_GLOBAL_INFLIGHT` (10). The per-target lock
        (thread_id for ``kind=message``, channel_id for ``kind=new_task``)
        serializes fires aimed at the same destination so two due
        schedules can't interleave their callbacks.

        B1: inflight-id bookkeeping lives in :meth:`_fire_one_safe` (the
        dispatch-site wrapper) so the add/discard pair brackets the task
        rather than the body of this method. This keeps the ``tick`` /
        ``catch_up`` guard race-free without depending on an internal
        try/finally here.

        M1+M9: after acquiring the per-target lock we re-read the schedule
        state from the store. If a concurrent ``delete()`` / ``toggle()``
        flipped ``enabled=False`` or removed the row, this run is aborted
        cleanly instead of firing the stale snapshot we were dispatched
        with.
        """
        sid = sched.get("schedule_id")
        async with self._inflight_sem:
            kind = sched.get("kind")
            if kind == "message":
                lock_id = sched.get("target_thread_id")
            else:
                lock_id = sched.get("target_channel_id")
            if lock_id is None:
                # Defensive — would already have failed validation in create().
                log.warning(
                    "#241 _fire_one missing lock id schedule=%s kind=%s",
                    sched.get("schedule_id"), kind,
                )
                await self._fire_with_retry(sched)
                return
            async with self.get_lock(lock_id):
                # M1+M9: re-read state under the lock so a delete/toggle
                # that landed between dispatch and lock-acquire wins.
                if sid is not None:
                    fresh = self.store.get(sid)
                    if fresh is None:
                        log.info(
                            "#241 _fire_one aborted (deleted): %s", sid,
                        )
                        return
                    fresh_state = fresh.get("state") or {}
                    if not fresh_state.get("enabled", False):
                        log.info(
                            "#241 _fire_one aborted (disabled): %s",
                            sid,
                        )
                        return
                    # Continue with the freshest snapshot — bookkeeping
                    # in ``_on_fire_*`` writes through the same dict.
                    sched = fresh
                await self._fire_with_retry(sched)

    async def _fire_with_retry(self, sched: dict) -> None:
        """Execute the kind-appropriate callback with 3× retry + terminal disable.

        ``discord.NotFound`` and ``discord.Forbidden`` are terminal — the
        target is gone or we can't post to it, so retrying would only burn
        rate-limit budget. Any other exception is treated as transient and
        retried up to 3 attempts with 1s / 4s / 16s backoff per PRD §2.
        """
        attempts = 0
        last_exc: BaseException | None = None
        backoffs = [1, 4, 16]
        kind = sched.get("kind")
        while attempts < 3:
            attempts += 1
            try:
                if kind == "message":
                    await self.fire_message_callback(sched)
                else:
                    await self.fire_new_task_callback(sched)
                await self._on_fire_success(sched)
                return
            except (discord.NotFound, discord.Forbidden) as exc:
                await self._on_fire_terminal(sched, exc)
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "#241 transient fire failure schedule=%s attempt=%d: %s",
                    sched.get("schedule_id"), attempts, exc,
                )
                if attempts < 3:
                    await asyncio.sleep(backoffs[attempts - 1])
                    continue
        # Retries exhausted — disable with the last exception captured.
        await self._on_fire_terminal(
            sched,
            last_exc if last_exc is not None else RuntimeError(
                "fire failed without exception"
            ),
        )

    # --------------------------------------------- success / terminal

    async def _on_fire_success(self, sched: dict) -> None:
        """Persist post-fire state and schedule the next run (or disable).

        Bumps ``fire_count``, sets ``last_fired_at`` / ``first_fired_at``,
        clears ``last_error``, then either rolls ``next_fire_at`` forward
        (recurring cron) or disables the schedule (one-shot / final fire).
        Calls :meth:`_check_max_lifetime` last so a schedule that just
        hit its lifetime cap gets disabled after the per-fire bookkeeping
        is on disk.
        """
        now = self._now_utc()
        state = sched.setdefault("state", {})
        state["fire_count"] = int(state.get("fire_count", 0)) + 1
        state["last_fired_at"] = now.isoformat()
        state["last_error"] = None
        if state.get("first_fired_at") is None:
            state["first_fired_at"] = now.isoformat()

        # M2: clear the cached "thread for this in-progress fire" id used
        # by ``_fire_schedule_new_task`` to stay retry-idempotent (so a
        # transient post-create_thread failure can reuse the same thread
        # on the next attempt). A successful fire is the natural reset
        # point — the next scheduled occurrence of a recurring kind=new_task
        # MUST create a fresh thread; reusing the previous one would
        # silently turn weekly tasks into "one thread, many turns".
        if "_new_task_thread_id" in state:
            state["_new_task_thread_id"] = None

        trigger = sched.get("trigger") or {}
        recurring = (
            bool(trigger.get("recurring", False))
            and trigger.get("kind") == "cron"
        )
        if not recurring:
            state["enabled"] = False
            state["next_fire_at"] = None
        else:
            new_next = compute_next_fire(trigger, after=now)
            if new_next is None:
                state["enabled"] = False
                state["next_fire_at"] = None
            else:
                state["next_fire_at"] = new_next.isoformat()

        self.store.save(sched)
        log.info(
            "#241 fire success schedule=%s fire_count=%d next=%s",
            sched.get("schedule_id"),
            state["fire_count"],
            state.get("next_fire_at"),
        )

        # Check max_lifetime AFTER the success bookkeeping is persisted so
        # ``first_fired_at`` is guaranteed populated for the comparison.
        self._check_max_lifetime(sched)

    async def _on_fire_terminal(
        self, sched: dict, exc: BaseException
    ) -> None:
        """Disable a schedule that hit an unrecoverable fire failure.

        Used for both terminal Discord exceptions (NotFound / Forbidden)
        and for exhausted retry budgets. The error string lands in
        ``state.last_error`` so ``/schedule list`` can surface it.
        """
        state = sched.setdefault("state", {})
        state["enabled"] = False
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        self.store.save(sched)
        log.warning(
            "#241 fire terminal disable schedule=%s exc=%s",
            sched.get("schedule_id"),
            state["last_error"],
        )

    # ---------------------------------------------------- max_lifetime

    def _check_max_lifetime(self, sched: dict) -> bool:
        """Disable + notify if ``max_lifetime_seconds`` has elapsed since first fire.

        Returns ``True`` iff the schedule was just expired by this call.
        Schedules without ``max_lifetime_seconds`` or without a
        ``first_fired_at`` (haven't fired yet) are never expired here.
        """
        max_life = sched.get("max_lifetime_seconds")
        state = sched.setdefault("state", {})
        ff = state.get("first_fired_at")
        if max_life is None or ff is None:
            return False
        try:
            ff_dt = datetime.fromisoformat(ff)
        except (TypeError, ValueError):
            return False
        if ff_dt.tzinfo is None:
            ff_dt = ff_dt.replace(tzinfo=timezone.utc)
        now = self._now_utc()
        lived = (now - ff_dt).total_seconds()
        if lived < max_life:
            return False

        state["enabled"] = False
        state["last_error"] = "max_lifetime reached"
        self.store.save(sched)
        if self.expire_notify_callback is not None:
            asyncio.create_task(self.expire_notify_callback(sched))
        log.info(
            "#241 max_lifetime expired schedule=%s lived=%.0fs",
            sched.get("schedule_id"),
            lived,
        )
        return True
