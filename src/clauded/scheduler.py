"""#241 — scheduler core (tick loop + fire executor).

Tick: every 15 seconds we scan ``SchedulerStore.all_active()`` for any
schedule whose ``next_fire_at <= now``. For each:

1. Acquire ``SessionManager.get_lock(target_thread_id)`` (per-thread
   serial; matches user-message serialization).
2. Fire: inject ``payload.what`` as a user prompt to the target thread's
   active session (reusing the bot's ``_handle_thread_message``-style
   path).
3. Update state: ``last_fired_at``, ``fire_count``, recompute
   ``next_fire_at`` (or disable if it was a one-shot).

Missed handling (bot restart):

* On startup, ``catch_up`` walks all enabled schedules. For each whose
  ``next_fire_at`` is in the past:
  - If past <= 5 minutes ago → fire immediately (catch up)
  - If past > 5 minutes ago → skip + log WARNING + ``missed_count += 1``
    + roll ``next_fire_at`` forward to the next occurrence

Concurrency rules (#241 §行为规则):

* per-thread serial via ``SessionManager.get_lock``
* global in-flight ≤ 10
* per-user active ≤ 20 (enforced in tool layer, not here)
* global active ≤ 100 (enforced in tool layer)

Failure handling:

* ``discord.NotFound`` (thread gone) → disable + log WARNING
* ``discord.Forbidden`` → disable + log WARNING
* other transient → 3× backoff (1s/4s/16s); on giveup → disable + post
  error embed in originating thread
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from croniter import croniter

if TYPE_CHECKING:
    from .scheduler_store import SchedulerStore

log = logging.getLogger("clauded.scheduler")

TICK_INTERVAL_S = 15
MAX_INFLIGHT = 10
MISSED_FIRE_GRACE_S = 5 * 60   # 5 min: fire-late vs skip-and-mark-missed
CLAUDE_MIN_INTERVAL_S = 5 * 60  # #241 §Tool spec: claude-created min interval


# ---------------------------------------------------------------------------
# Trigger parsing — wall-clock helpers
# ---------------------------------------------------------------------------


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO 8601 string; coerce to UTC; raise ValueError on bad input."""
    # accept "iso:" prefix for user convenience
    if s.lower().startswith("iso:"):
        s = s[4:].strip()
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(
            f"ISO datetime must have timezone (Z, +08:00, etc.); got {s!r}"
        )
    return dt.astimezone(timezone.utc)


def parse_cron_to_next(
    cron_expr: str, *, tz_name: str = "UTC", base: datetime | None = None,
) -> datetime:
    """Parse a 5-field cron expression; return next fire datetime in UTC.

    The cron expression is interpreted in ``tz_name``. ``base`` is the
    reference point for "next" (defaults to ``now()``).

    Raises ``ValueError`` on invalid syntax.
    """
    if cron_expr.lower().startswith("cron:"):
        cron_expr = cron_expr[5:].strip()
    try:
        zoneinfo = _zone(tz_name)
    except Exception as exc:
        raise ValueError(f"invalid timezone {tz_name!r}: {exc}")
    if base is None:
        base = datetime.now(zoneinfo)
    elif base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc).astimezone(zoneinfo)
    else:
        base = base.astimezone(zoneinfo)
    try:
        itr = croniter(cron_expr, base)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid cron expression {cron_expr!r}: {exc}")
    nxt = itr.get_next(datetime)
    # croniter returns naive datetime in some versions; reattach tz
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=zoneinfo)
    return nxt.astimezone(timezone.utc)


def _zone(tz_name: str):
    """Resolve a tz name to a tzinfo. Raises if invalid (per #241 spec: bad
    tz should surface as ValueError to the tool layer, NOT silently fall
    back to UTC — user must know).
    """
    if tz_name in ("UTC", "utc", ""):
        return timezone.utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ModuleNotFoundError):
        raise


def compute_next_fire(
    trigger: dict[str, Any], *, after: datetime | None = None,
) -> datetime | None:
    """Given a schedule's ``trigger`` block, return next fire (UTC) or None.

    Returns ``None`` for one-shot (``kind=once``) when ``iso`` is in the
    past — caller should disable the schedule.
    """
    if after is None:
        after = datetime.now(timezone.utc)
    kind = trigger.get("kind")
    if kind == "once":
        iso = trigger.get("iso")
        if not iso:
            return None
        try:
            dt = parse_iso_utc(iso)
        except ValueError:
            return None
        return dt if dt > after else None
    if kind == "cron":
        cron = trigger.get("cron")
        if not cron:
            return None
        tz_name = trigger.get("tz_when_created", "UTC")
        try:
            return parse_cron_to_next(cron, tz_name=tz_name, base=after)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# SchedulerManager
# ---------------------------------------------------------------------------


# Type for the "fire" callback the bot wires up — accepts the schedule dict
# and is responsible for delivering the prompt to the right thread.
FireCallback = Callable[[dict[str, Any]], Awaitable[None]]


class SchedulerManager:
    """Tick loop + fire dispatch over :class:`SchedulerStore`.

    The fire executor itself lives in ``bot.py`` (because it needs
    ``SessionManager`` + ``DiscordRenderer``). We accept a callback at
    construction so the manager stays clean of bot internals.
    """

    def __init__(
        self,
        store: "SchedulerStore",
        *,
        fire_callback: FireCallback,
        get_lock: Callable[[int], asyncio.Lock] | None = None,
    ) -> None:
        self.store = store
        self._fire_cb = fire_callback
        self._get_lock = get_lock
        self._inflight = 0
        self._inflight_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Catch-up on startup
    # ------------------------------------------------------------------

    async def catch_up(self) -> None:
        """Walk all active schedules; immediately fire any whose
        ``next_fire_at`` is in the past but within the grace window.

        Older than grace → mark missed + roll forward without firing.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=MISSED_FIRE_GRACE_S)
        for sched in list(self.store.all_active()):
            state = sched.get("state", {})
            nfa_str = state.get("next_fire_at")
            if not nfa_str:
                # First boot for a freshly-created schedule; compute now.
                nfa = compute_next_fire(sched.get("trigger", {}), after=now)
                if nfa is None:
                    # invalid trigger or past one-shot → disable
                    self.store.update_state(
                        sched["schedule_id"], enabled=False,
                        last_error="trigger expired before first fire",
                    )
                    continue
                self.store.update_state(
                    sched["schedule_id"], next_fire_at=nfa.isoformat(),
                )
                continue
            try:
                nfa = datetime.fromisoformat(nfa_str)
            except ValueError:
                log.warning("malformed next_fire_at on schedule %s: %r",
                            sched["schedule_id"], nfa_str)
                continue
            if nfa.tzinfo is None:
                nfa = nfa.replace(tzinfo=timezone.utc)
            if nfa > now:
                continue
            if nfa < cutoff:
                # Older than grace: skip + mark missed + roll forward
                log.warning(
                    "#241 missed fire schedule=%s (%.0fs late); skipping",
                    sched["schedule_id"],
                    (now - nfa).total_seconds(),
                )
                self.store.update_state(
                    sched["schedule_id"],
                    missed_count=state.get("missed_count", 0) + 1,
                )
                new_nfa = compute_next_fire(sched.get("trigger", {}), after=now)
                if new_nfa is None:
                    self.store.update_state(
                        sched["schedule_id"], enabled=False,
                        last_error="one-shot past grace window",
                    )
                else:
                    self.store.update_state(
                        sched["schedule_id"], next_fire_at=new_nfa.isoformat(),
                    )
                continue
            # Within grace: fire now (catch-up)
            log.info(
                "#241 catch-up fire schedule=%s (%.0fs late, within grace)",
                sched["schedule_id"], (now - nfa).total_seconds(),
            )
            await self._fire_one(sched)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """One scheduler tick: dispatch due schedules. Called by tasks.loop."""
        now = datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for sched in self.store.all_active():
            state = sched.get("state", {})
            nfa_str = state.get("next_fire_at")
            if not nfa_str:
                continue
            try:
                nfa = datetime.fromisoformat(nfa_str)
            except ValueError:
                continue
            if nfa.tzinfo is None:
                nfa = nfa.replace(tzinfo=timezone.utc)
            if nfa <= now:
                due.append(sched)

        if not due:
            return

        # Process due in parallel but cap global in-flight count
        for sched in due:
            async with self._inflight_lock:
                if self._inflight >= MAX_INFLIGHT:
                    log.warning(
                        "#241 in-flight cap reached (%d); deferring schedule=%s",
                        MAX_INFLIGHT, sched["schedule_id"],
                    )
                    continue
                self._inflight += 1
            asyncio.create_task(self._fire_one_release_slot(sched))

    async def _fire_one_release_slot(self, sched: dict[str, Any]) -> None:
        try:
            await self._fire_one(sched)
        finally:
            async with self._inflight_lock:
                self._inflight = max(0, self._inflight - 1)

    async def _fire_one(self, sched: dict[str, Any]) -> None:
        """Fire a single schedule, with per-thread serial lock + retry."""
        thread_id = sched.get("target_thread_id")
        if not isinstance(thread_id, int):
            log.warning("schedule %s missing target_thread_id; disabling",
                        sched.get("schedule_id"))
            self.store.update_state(
                sched["schedule_id"], enabled=False,
                last_error="missing target_thread_id",
            )
            return

        lock = None
        if self._get_lock is not None:
            try:
                lock = self._get_lock(thread_id)
            except Exception:
                lock = None

        async def _do_fire():
            await self._fire_cb(sched)

        if lock is not None:
            async with lock:
                await self._fire_with_retry(sched, _do_fire)
        else:
            await self._fire_with_retry(sched, _do_fire)

    async def _fire_with_retry(
        self, sched: dict[str, Any], do_fire: Callable[[], Awaitable[None]],
    ) -> None:
        """Run the fire callback with backoff; handle terminal vs transient
        errors per #241 §行为规则."""
        # Import locally so non-Discord tests can import this module
        import discord
        backoffs = [1, 4, 16]
        last_exc: BaseException | None = None
        for attempt, delay in enumerate([0, *backoffs]):
            if delay:
                await asyncio.sleep(delay)
            try:
                await do_fire()
                # Success: update state
                now = datetime.now(timezone.utc)
                fire_count = sched.get("state", {}).get("fire_count", 0) + 1
                next_fire = compute_next_fire(
                    sched.get("trigger", {}), after=now,
                )
                changes: dict[str, Any] = {
                    "last_fired_at": now.isoformat(),
                    "fire_count": fire_count,
                    "last_error": None,
                }
                if next_fire is None:
                    # One-shot finished
                    changes["enabled"] = False
                    changes["next_fire_at"] = None
                else:
                    changes["next_fire_at"] = next_fire.isoformat()
                self.store.update_state(sched["schedule_id"], **changes)
                log.info(
                    "#241 fire success schedule=%s fire_count=%d next=%s",
                    sched["schedule_id"], fire_count,
                    changes.get("next_fire_at"),
                )
                return
            except discord.NotFound as exc:
                # Terminal: target gone
                log.warning(
                    "#241 fire schedule=%s target NotFound; disabling: %s",
                    sched["schedule_id"], exc,
                )
                self.store.update_state(
                    sched["schedule_id"], enabled=False,
                    last_error=f"target NotFound: {exc}",
                )
                return
            except discord.Forbidden as exc:
                log.warning(
                    "#241 fire schedule=%s Forbidden; disabling: %s",
                    sched["schedule_id"], exc,
                )
                self.store.update_state(
                    sched["schedule_id"], enabled=False,
                    last_error=f"Forbidden: {exc}",
                )
                return
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "#241 fire schedule=%s attempt %d/%d transient: %s",
                    sched["schedule_id"], attempt + 1, len(backoffs) + 1, exc,
                    exc_info=True,
                )
                continue
        # Exhausted retries
        log.error(
            "#241 fire schedule=%s exhausted retries; disabling: %s",
            sched["schedule_id"], last_exc,
        )
        self.store.update_state(
            sched["schedule_id"], enabled=False,
            last_error=f"exhausted retries: {last_exc}",
        )

    # ------------------------------------------------------------------
    # CRUD wrapper for cog / tool layer
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        when: str,
        what: str,
        target_thread_id: int,
        channel_id: int,
        guild_id: int | None,
        name: str = "",
        recurring: bool = False,
        created_by: str | int,
        tz_name: str = "Asia/Shanghai",
        is_claude_created: bool = False,
    ) -> dict[str, Any]:
        """Create a schedule.

        Validates input + computes next_fire_at + saves to store.

        Raises:
            ValueError: invalid input (bad cron / past iso / etc.)
        """
        # Parse the trigger
        when_s = when.strip()
        if when_s.lower().startswith("cron:"):
            kind = "cron"
            cron_expr = when_s[5:].strip()
            # Validate parse-ability
            now = datetime.now(timezone.utc)
            try:
                next_fire = parse_cron_to_next(
                    cron_expr, tz_name=tz_name, base=now,
                )
            except ValueError as exc:
                raise ValueError(f"invalid cron: {exc}")
            # Claude min interval check
            if is_claude_created:
                # Probe second fire to see interval
                try:
                    second = parse_cron_to_next(
                        cron_expr, tz_name=tz_name, base=next_fire,
                    )
                    interval = (second - next_fire).total_seconds()
                    if interval < CLAUDE_MIN_INTERVAL_S:
                        raise ValueError(
                            f"min interval {CLAUDE_MIN_INTERVAL_S}s for "
                            f"claude-created schedules; got {interval}s"
                        )
                except ValueError:
                    raise
            trigger = {
                "kind": "cron", "cron": cron_expr, "tz_when_created": tz_name,
            }
        elif when_s.lower().startswith("iso:") or "T" in when_s:
            kind = "once"
            try:
                dt = parse_iso_utc(when_s)
            except ValueError as exc:
                raise ValueError(f"invalid iso datetime: {exc}")
            now = datetime.now(timezone.utc)
            if dt <= now:
                raise ValueError(f"iso time must be in the future; got {dt}")
            next_fire = dt
            trigger = {
                "kind": "once", "iso": dt.isoformat(),
                "tz_when_created": tz_name,
            }
            recurring = False  # one-shot
        else:
            raise ValueError(
                f"`when` must start with `cron:` or `iso:`; got {when_s!r}"
            )

        # Validate `what`
        if not what or not what.strip():
            raise ValueError("`what` must be non-empty")
        if len(what) > 500:
            raise ValueError(f"`what` exceeds 500 chars (got {len(what)})")

        # Validate `name`
        if name and len(name) > 50:
            raise ValueError(f"`name` exceeds 50 chars (got {len(name)})")

        sched_id = uuid.uuid4().hex
        sched: dict[str, Any] = {
            "schedule_id": sched_id,
            "name": name or f"schedule-{sched_id[:8]}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": "claude" if is_claude_created else str(created_by),
            "guild_id": guild_id,
            "channel_id": channel_id,
            "target_thread_id": target_thread_id,
            "trigger": trigger,
            "payload": {"what": what.strip()},
            "state": {
                "enabled": True,
                "next_fire_at": next_fire.isoformat(),
                "last_fired_at": None,
                "last_error": None,
                "fire_count": 0,
                "missed_count": 0,
            },
        }
        self.store.add(sched)
        log.info(
            "#241 created schedule=%s when=%s next=%s",
            sched_id, when_s, next_fire.isoformat(),
        )
        return sched

    def delete(self, schedule_id: str, *, requester: str | int,
               is_admin: bool = False) -> tuple[bool, str]:
        """Delete a schedule. Returns ``(deleted, reason_if_not)``.

        Permission rules (#241 §权限模型):
        - claude can only delete claude-created
        - user can only delete own
        - admin can delete any
        """
        sched = self.store.get(schedule_id)
        if sched is None:
            return False, "not found"
        creator = str(sched.get("created_by", ""))
        if is_admin:
            pass  # admin can delete anything
        elif str(requester) == "claude":
            if creator != "claude":
                return False, "claude can only delete claude-created schedules"
        else:
            if creator != str(requester):
                return False, "you can only delete schedules you created"
        return self.store.delete(schedule_id), ""

    def toggle(
        self, schedule_id: str, enabled: bool, *, requester: str | int,
        is_admin: bool = False,
    ) -> tuple[bool, str]:
        """Enable / disable a schedule. Returns ``(ok, reason_if_not)``."""
        sched = self.store.get(schedule_id)
        if sched is None:
            return False, "not found"
        creator = str(sched.get("created_by", ""))
        if not is_admin:
            if str(requester) == "claude":
                if creator != "claude":
                    return False, "claude can only toggle claude-created schedules"
            elif creator != str(requester):
                return False, "you can only toggle schedules you created"
        self.store.update_state(schedule_id, enabled=bool(enabled))
        # If re-enabling and next_fire_at is in the past, recompute
        if enabled:
            sched = self.store.get(schedule_id)
            state = sched.get("state", {}) if sched else {}
            nfa = state.get("next_fire_at")
            now = datetime.now(timezone.utc)
            recompute = False
            if not nfa:
                recompute = True
            else:
                try:
                    dt = datetime.fromisoformat(nfa)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt <= now:
                        recompute = True
                except ValueError:
                    recompute = True
            if recompute and sched:
                new_nfa = compute_next_fire(sched.get("trigger", {}), after=now)
                if new_nfa is not None:
                    self.store.update_state(
                        schedule_id, next_fire_at=new_nfa.isoformat(),
                    )
        return True, ""
