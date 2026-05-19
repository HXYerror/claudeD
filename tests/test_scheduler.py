"""Unit tests for the v1.18 scheduler core (issue #241, Subtasks 1 + 2).

Covers, per ``/tmp/subtask-1-prompt-v2.md`` and ``/tmp/subtask-2-prompt-v3.md``:

1. ``parse_iso_utc``                (4 tests)
2. ``parse_cron_to_next``           (5 tests)
3. ``parse_duration``               (8 tests)
4. ``compute_next_fire``            (3 tests)
5. ``SchedulerStore``               (7 tests)
6. ``SchedulerManager.create`` kind=message (8 tests)
7. ``SchedulerManager.create`` kind=new_task (3 tests)
8. ``SchedulerManager.create`` caps + max_lifetime (4 tests)
9. ``SchedulerManager.delete``      (4 tests)
10. ``SchedulerManager.toggle``     (2 tests)
11. ``SchedulerManager.tick``       (5 tests)  ─┐
12. ``SchedulerManager.catch_up``   (2 tests)   │ Subtask 2 — fire executor
13. ``SchedulerManager._fire_one``  (8 tests)   │
14. ``_check_max_lifetime``         (2 tests)  ─┘
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.scheduler import (
    MAX_GLOBAL_ACTIVE,
    MAX_GLOBAL_INFLIGHT,
    MAX_USER_ACTIVE,
    SchedulerManager,
    compute_next_fire,
    parse_cron_to_next,
    parse_duration,
    parse_iso_utc,
)
from clauded.scheduler_store import SchedulerStore


# ---------------------------------------------------------------- Fixtures


@pytest.fixture
def tmp_store_dir(tmp_path):
    """Fresh, isolated data dir per test."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def store(tmp_store_dir):
    return SchedulerStore(data_dir=str(tmp_store_dir))


async def _noop_cb(_sched):
    return None


@pytest.fixture
def mgr(store):
    return SchedulerManager(
        store,
        fire_message_callback=_noop_cb,
        fire_new_task_callback=_noop_cb,
        expire_notify_callback=_noop_cb,
    )


def _future_iso(seconds_ahead: int = 3600) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead)
    return "iso: " + dt.isoformat()


# =============================================================== Group 1
# parse_iso_utc


class TestParseIsoUtc:
    def test_tz_aware_happy(self):
        dt = parse_iso_utc("2026-05-19T09:00:00+08:00")
        assert dt.tzinfo is not None
        # 09:00 +08:00 = 01:00 UTC
        assert dt.utcoffset() == timedelta(0)
        assert dt.hour == 1

    def test_iso_prefix_stripped(self):
        dt = parse_iso_utc("iso: 2026-05-19T09:00:00+00:00")
        assert dt.year == 2026 and dt.month == 5 and dt.day == 19
        assert dt.hour == 9 and dt.tzinfo is not None

    def test_naive_rejected(self):
        with pytest.raises(ValueError, match="timezone"):
            parse_iso_utc("2026-05-19T09:00:00")

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            parse_iso_utc("not a date at all")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            parse_iso_utc("iso:")


# =============================================================== Group 2
# parse_cron_to_next


class TestParseCronToNext:
    def test_happy_utc(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        nxt = parse_cron_to_next("0 9 * * *", "UTC", base=base)
        assert nxt > base
        assert nxt.hour == 9 and nxt.minute == 0

    def test_non_utc_timezone(self):
        # Asia/Shanghai is UTC+8; cron "0 9 * * *" daily 09:00 local
        # → 01:00 UTC.
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        nxt = parse_cron_to_next("0 9 * * *", "Asia/Shanghai", base=base)
        assert nxt.tzinfo is not None
        assert nxt.utcoffset() == timedelta(0)
        assert nxt.hour == 1 and nxt.minute == 0

    def test_cron_prefix_stripped(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        nxt = parse_cron_to_next("cron: 0 9 * * *", "UTC", base=base)
        assert nxt.hour == 9

    def test_bad_timezone_raises(self):
        with pytest.raises(ValueError, match="timezone"):
            parse_cron_to_next("0 9 * * *", "Mars/Olympus_Mons")

    def test_bad_cron_raises(self):
        with pytest.raises(ValueError):
            parse_cron_to_next("not a cron expression", "UTC")


# =============================================================== Group 3
# parse_duration


class TestParseDuration:
    def test_30d(self):
        assert parse_duration("30d") == 30 * 86_400

    def test_7d(self):
        assert parse_duration("7d") == 7 * 86_400

    def test_24h(self):
        assert parse_duration("24h") == 24 * 3600

    def test_3600s(self):
        assert parse_duration("3600s") == 3600

    def test_1w(self):
        assert parse_duration("1w") == 7 * 86_400

    def test_400d_over_cap(self):
        with pytest.raises(ValueError, match="365d"):
            parse_duration("400d")

    def test_bad_format(self):
        with pytest.raises(ValueError):
            parse_duration("abc")

    def test_bad_suffix(self):
        with pytest.raises(ValueError):
            parse_duration("5z")


# =============================================================== Group 4
# compute_next_fire


class TestComputeNextFire:
    def test_once_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        trigger = {"kind": "once", "iso": future.isoformat()}
        nxt = compute_next_fire(trigger)
        assert nxt is not None
        # Allow microsecond drift via approximate comparison.
        assert abs((nxt - future).total_seconds()) < 1

    def test_once_past_returns_none(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        trigger = {"kind": "once", "iso": past.isoformat()}
        assert compute_next_fire(trigger) is None

    def test_cron_rolls_forward(self):
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        trigger = {
            "kind": "cron",
            "cron": "0 9 * * *",
            "tz_when_created": "UTC",
        }
        nxt = compute_next_fire(trigger, after=base)
        assert nxt is not None
        assert nxt > base
        # 09:00 UTC tomorrow.
        assert nxt.day == 2 and nxt.hour == 9


# =============================================================== Group 5
# SchedulerStore


class TestSchedulerStore:
    def test_empty_load(self, tmp_store_dir):
        s = SchedulerStore(data_dir=str(tmp_store_dir))
        assert s.list_all() == {}

    def test_add_get_delete_roundtrip(self, store):
        sched = {
            "schedule_id": "abc123",
            "kind": "message",
            "created_by": "u1",
            "state": {"enabled": True},
        }
        sid = store.add(sched)
        assert sid == "abc123"
        assert store.get("abc123") == sched
        assert store.delete("abc123") is True
        assert store.get("abc123") is None
        assert store.delete("abc123") is False

    def test_list_for_thread(self, store):
        store.add({
            "schedule_id": "m1",
            "kind": "message",
            "target_thread_id": 111,
            "created_by": "u",
            "state": {"enabled": True},
        })
        store.add({
            "schedule_id": "m2",
            "kind": "message",
            "target_thread_id": 222,
            "created_by": "u",
            "state": {"enabled": True},
        })
        store.add({
            "schedule_id": "n1",
            "kind": "new_task",
            "target_channel_id": 111,  # same id, different kind → excluded
            "created_by": "u",
            "state": {"enabled": True},
        })
        out = store.list_for_thread(111)
        assert [s["schedule_id"] for s in out] == ["m1"]

    def test_list_for_channel(self, store):
        store.add({
            "schedule_id": "a",
            "kind": "message",
            "channel_id": 500,
            "created_by": "u",
            "state": {"enabled": True},
        })
        store.add({
            "schedule_id": "b",
            "kind": "new_task",
            "channel_id": 999,
            "target_channel_id": 500,
            "created_by": "u",
            "state": {"enabled": True},
        })
        store.add({
            "schedule_id": "c",
            "kind": "message",
            "channel_id": 999,
            "created_by": "u",
            "state": {"enabled": True},
        })
        out = store.list_for_channel(500)
        assert sorted(s["schedule_id"] for s in out) == ["a", "b"]

    def test_count_active(self, store):
        for i in range(3):
            store.add({
                "schedule_id": f"u{i}",
                "created_by": "alice",
                "state": {"enabled": True},
            })
        store.add({
            "schedule_id": "d1",
            "created_by": "alice",
            "state": {"enabled": False},
        })
        store.add({
            "schedule_id": "b1",
            "created_by": "bob",
            "state": {"enabled": True},
        })
        assert store.count_active_for_user("alice") == 3
        assert store.count_active_for_user("bob") == 1
        assert store.count_active_total() == 4

    def test_corrupt_json_fallback(self, tmp_store_dir, caplog):
        path = Path(tmp_store_dir) / "schedules.json"
        path.write_text("{{this is not valid json")
        with caplog.at_level("WARNING"):
            s = SchedulerStore(data_dir=str(tmp_store_dir))
        assert s.list_all() == {}
        # WARNING was emitted (don't raise).
        assert any("schedules.json" in r.message for r in caplog.records)

    def test_reload_preserves_state(self, tmp_store_dir):
        s1 = SchedulerStore(data_dir=str(tmp_store_dir))
        s1.add({
            "schedule_id": "keep",
            "kind": "message",
            "created_by": "u",
            "state": {"enabled": True, "fire_count": 5},
        })
        # Fresh instance pointing at the same dir reads from disk.
        s2 = SchedulerStore(data_dir=str(tmp_store_dir))
        got = s2.get("keep")
        assert got is not None
        assert got["state"]["fire_count"] == 5


# =============================================================== Group 6
# SchedulerManager.create kind=message


class TestCreateMessage:
    def test_happy_iso_future(self, mgr):
        sched = mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="ping",
            target_thread_id=111,
            created_by="u1",
        )
        assert sched["kind"] == "message"
        assert sched["payload"]["what"] == "ping"
        assert sched["target_thread_id"] == 111
        assert sched["target_channel_id"] is None
        assert sched["state"]["enabled"] is True
        assert sched["trigger"]["kind"] == "once"
        # schedule_id is 16-char hex (token_hex(8))
        assert len(sched["schedule_id"]) == 16
        # Default name = sched_id[:8]
        assert sched["name"] == sched["schedule_id"][:8]

    def test_past_iso_rejected(self, mgr):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with pytest.raises(ValueError, match="future"):
            mgr.create(
                kind="message",
                when="iso: " + past.isoformat(),
                what="ping",
                target_thread_id=111,
                created_by="u1",
            )

    def test_what_empty(self, mgr):
        with pytest.raises(ValueError, match="what"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="",
                target_thread_id=111,
                created_by="u1",
            )

    def test_what_too_long(self, mgr):
        with pytest.raises(ValueError, match="what"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="x" * 501,
                target_thread_id=111,
                created_by="u1",
            )

    def test_name_too_long(self, mgr):
        with pytest.raises(ValueError, match="name"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="ping",
                target_thread_id=111,
                created_by="u1",
                name="x" * 51,
            )

    def test_target_thread_id_missing(self, mgr):
        with pytest.raises(ValueError, match="target_thread_id"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="ping",
                created_by="u1",
            )

    def test_claude_every_minute_cron_rejected(self, mgr):
        with pytest.raises(ValueError, match="min interval"):
            mgr.create(
                kind="message",
                when="cron: * * * * *",  # every minute
                what="ping",
                target_thread_id=111,
                created_by="claude",
                is_claude_created=True,
                recurring=True,
            )

    def test_user_every_minute_cron_allowed(self, mgr):
        sched = mgr.create(
            kind="message",
            when="cron: * * * * *",
            what="ping",
            target_thread_id=111,
            created_by="u1",
            is_claude_created=False,
            recurring=True,
        )
        assert sched["trigger"]["cron"] == "* * * * *"
        assert sched["trigger"]["recurring"] is True


# =============================================================== Group 7
# SchedulerManager.create kind=new_task


class TestCreateNewTask:
    def test_happy(self, mgr):
        sched = mgr.create(
            kind="new_task",
            when=_future_iso(3600),
            what="run weekly report",
            target_channel_id=2222,
            thread_name="weekly-report",
            created_by="u1",
        )
        assert sched["kind"] == "new_task"
        assert sched["target_channel_id"] == 2222
        assert sched["thread_name"] == "weekly-report"
        assert sched["target_thread_id"] is None

    def test_target_channel_id_missing(self, mgr):
        with pytest.raises(ValueError, match="target_channel_id"):
            mgr.create(
                kind="new_task",
                when=_future_iso(),
                what="task",
                created_by="u1",
            )

    def test_thread_name_too_long(self, mgr):
        with pytest.raises(ValueError, match="thread_name"):
            mgr.create(
                kind="new_task",
                when=_future_iso(),
                what="task",
                target_channel_id=222,
                thread_name="x" * 51,
                created_by="u1",
            )


# =============================================================== Group 8
# Caps + max_lifetime


class TestCaps:
    def test_per_user_cap_for_claude(self, mgr, store):
        # Pre-populate 20 claude-owned active schedules.
        for i in range(MAX_USER_ACTIVE):
            store.add({
                "schedule_id": f"c{i:02d}",
                "kind": "message",
                "created_by": "claude",
                "state": {"enabled": True},
            })
        with pytest.raises(ValueError, match="per-user"):
            mgr.create(
                kind="message",
                when=_future_iso(3600),
                what="one more",
                target_thread_id=111,
                created_by="claude",
                is_claude_created=True,
            )

    def test_global_cap(self, mgr, store):
        # Spread across users to avoid hitting per-user cap first.
        for i in range(MAX_GLOBAL_ACTIVE):
            store.add({
                "schedule_id": f"g{i:03d}",
                "kind": "message",
                "created_by": f"user_{i % 50}",
                "state": {"enabled": True},
            })
        with pytest.raises(ValueError, match="global"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="extra",
                target_thread_id=111,
                created_by="newbie",
            )

    def test_max_lifetime_without_recurring_rejected(self, mgr):
        with pytest.raises(ValueError, match="max_lifetime"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="task",
                target_thread_id=111,
                created_by="u1",
                recurring=False,
                max_lifetime="30d",
            )

    def test_max_lifetime_400d_rejected(self, mgr):
        with pytest.raises(ValueError, match="365d"):
            mgr.create(
                kind="message",
                when="cron: 0 9 * * *",
                what="task",
                target_thread_id=111,
                created_by="u1",
                recurring=True,
                max_lifetime="400d",
            )

    def test_max_lifetime_happy_persisted_as_seconds(self, mgr):
        sched = mgr.create(
            kind="message",
            when="cron: 0 9 * * *",
            what="task",
            target_thread_id=111,
            created_by="u1",
            recurring=True,
            max_lifetime="30d",
        )
        assert sched["max_lifetime_seconds"] == 30 * 86_400

    def test_recurring_with_iso_rejected(self, mgr):
        with pytest.raises(ValueError, match="recurring"):
            mgr.create(
                kind="message",
                when=_future_iso(),
                what="task",
                target_thread_id=111,
                created_by="u1",
                recurring=True,
            )


# =============================================================== Group 9
# SchedulerManager.delete


class TestDelete:
    def _make(self, mgr, *, created_by="u1"):
        return mgr.create(
            kind="message",
            when=_future_iso(),
            what="ping",
            target_thread_id=111,
            created_by=created_by,
        )

    def test_creator_can_delete(self, mgr, store):
        s = self._make(mgr, created_by="u1")
        ok, _ = mgr.delete(s["schedule_id"], requester="u1")
        assert ok is True
        assert store.get(s["schedule_id"]) is None

    def test_other_cannot_delete(self, mgr, store):
        s = self._make(mgr, created_by="u1")
        ok, reason = mgr.delete(s["schedule_id"], requester="u2")
        assert ok is False
        assert "permission" in reason.lower()
        assert store.get(s["schedule_id"]) is not None

    def test_admin_override(self, mgr, store):
        s = self._make(mgr, created_by="u1")
        ok, _ = mgr.delete(
            s["schedule_id"], requester="other", is_admin=True
        )
        assert ok is True
        assert store.get(s["schedule_id"]) is None

    def test_claude_cannot_delete_user_schedule(self, mgr):
        s_user = self._make(mgr, created_by="u1")
        ok, reason = mgr.delete(s_user["schedule_id"], requester="claude")
        assert ok is False
        assert "claude" in reason.lower()

        # But claude CAN delete its own.
        s_claude = mgr.create(
            kind="message",
            when="cron: 0 9 * * *",
            what="ping",
            target_thread_id=222,
            created_by="claude",
            is_claude_created=True,
        )
        ok2, _ = mgr.delete(s_claude["schedule_id"], requester="claude")
        assert ok2 is True


# =============================================================== Group 10
# SchedulerManager.toggle


class TestToggle:
    def test_disable_then_enable(self, mgr, store):
        s = mgr.create(
            kind="message",
            when=_future_iso(),
            what="ping",
            target_thread_id=111,
            created_by="u1",
        )
        sid = s["schedule_id"]
        ok, _ = mgr.toggle(sid, False, requester="u1")
        assert ok is True
        assert store.get(sid)["state"]["enabled"] is False
        ok, _ = mgr.toggle(sid, True, requester="u1")
        assert ok is True
        assert store.get(sid)["state"]["enabled"] is True

    def test_toggle_permission_matrix(self, mgr):
        s = mgr.create(
            kind="message",
            when=_future_iso(),
            what="ping",
            target_thread_id=111,
            created_by="u1",
        )
        sid = s["schedule_id"]

        ok, _ = mgr.toggle(sid, False, requester="someone-else")
        assert ok is False

        ok, _ = mgr.toggle(sid, False, requester="claude")
        assert ok is False

        ok, _ = mgr.toggle(
            sid, False, requester="admin-user", is_admin=True
        )
        assert ok is True


# =============================================================== Group 11+
# Subtask 2 — fire executor + tick + catch_up + max_lifetime
#
# All these tests construct fully-shaped schedule dicts directly (rather
# than going through ``mgr.create``) so they can pin ``next_fire_at`` /
# ``first_fired_at`` to arbitrary past timestamps without fighting the
# "future iso" guard in ``create``.


def _due_sched_dict(
    *,
    schedule_id: str,
    kind: str = "message",
    target_thread_id: int | None = None,
    target_channel_id: int | None = None,
    next_fire_at: str | None = None,
    enabled: bool = True,
    recurring: bool = False,
    cron: str | None = None,
    iso: str | None = None,
    created_by: str = "u1",
    first_fired_at: str | None = None,
    max_lifetime_seconds: int | None = None,
) -> dict:
    """Build a persisted-shape sched dict for Subtask-2 tests.

    Direct construction bypasses ``mgr.create``'s future-only iso check
    so tests can pin ``next_fire_at`` to the past for tick/catch_up.
    """
    trigger_kind = "cron" if cron else "once"
    return {
        "schedule_id": schedule_id,
        "kind": kind,
        "name": schedule_id[:8],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "guild_id": 1,
        "channel_id": 100,
        "target_thread_id": (
            target_thread_id if kind == "message" else None
        ),
        "target_channel_id": (
            target_channel_id if kind == "new_task" else None
        ),
        "thread_name": "t" if kind == "new_task" else None,
        "trigger": {
            "kind": trigger_kind,
            "iso": iso,
            "cron": cron,
            "tz_when_created": "UTC",
            "recurring": recurring,
        },
        "payload": {"what": "ping"},
        "max_lifetime_seconds": max_lifetime_seconds,
        "state": {
            "enabled": enabled,
            "next_fire_at": next_fire_at,
            "first_fired_at": first_fired_at,
            "last_fired_at": None,
            "last_error": None,
            "fire_count": 0,
            "missed_count": 0,
        },
    }


def _build_mgr(
    store: SchedulerStore,
    *,
    fire_message=None,
    fire_new_task=None,
    expire_notify=None,
    get_lock=None,
) -> SchedulerManager:
    """Build a manager with optional per-test callbacks (default: noop)."""
    return SchedulerManager(
        store,
        fire_message_callback=fire_message or _noop_cb,
        fire_new_task_callback=fire_new_task or _noop_cb,
        expire_notify_callback=expire_notify or _noop_cb,
        get_lock=get_lock,
    )


def _make_notfound() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "x")


def _make_forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "x")


# =============================================================== Group 11
# SchedulerManager.tick


class TestTick:
    @pytest.mark.asyncio
    async def test_tick_fires_due(self, store):
        calls = []

        async def cb(sched):
            calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb)
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="due0001",
                target_thread_id=1001,
                next_fire_at=past,
            )
        )

        await mgr.tick()
        await asyncio.sleep(0.1)
        assert calls == ["due0001"]

    @pytest.mark.asyncio
    async def test_tick_skips_future(self, store):
        calls = []

        async def cb(sched):
            calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb)
        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="fut00001",
                target_thread_id=1002,
                next_fire_at=future,
            )
        )

        await mgr.tick()
        await asyncio.sleep(0.1)
        assert calls == []

    @pytest.mark.asyncio
    async def test_tick_skips_disabled(self, store):
        calls = []

        async def cb(sched):
            calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb)
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="off00001",
                target_thread_id=1003,
                next_fire_at=past,
                enabled=False,
            )
        )

        await mgr.tick()
        await asyncio.sleep(0.1)
        assert calls == []

    @pytest.mark.asyncio
    async def test_tick_dispatches_kind_message(self, store):
        msg_calls = []
        new_calls = []

        async def msg_cb(sched):
            msg_calls.append(sched["schedule_id"])

        async def new_cb(sched):
            new_calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=msg_cb, fire_new_task=new_cb)
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="kind_msg",
                kind="message",
                target_thread_id=1010,
                next_fire_at=past,
            )
        )

        await mgr.tick()
        await asyncio.sleep(0.1)
        assert msg_calls == ["kind_msg"]
        assert new_calls == []

    @pytest.mark.asyncio
    async def test_tick_dispatches_kind_new_task(self, store):
        msg_calls = []
        new_calls = []

        async def msg_cb(sched):
            msg_calls.append(sched["schedule_id"])

        async def new_cb(sched):
            new_calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=msg_cb, fire_new_task=new_cb)
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="kind_new",
                kind="new_task",
                target_channel_id=2020,
                next_fire_at=past,
            )
        )

        await mgr.tick()
        await asyncio.sleep(0.1)
        assert new_calls == ["kind_new"]
        assert msg_calls == []


# =============================================================== Group 12
# SchedulerManager.catch_up


class TestCatchUp:
    @pytest.mark.asyncio
    async def test_catch_up_within_grace_fires(self, store):
        calls = []

        async def cb(sched):
            calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb)
        # 30s late, well inside the 300s grace window
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="grace001",
                target_thread_id=3001,
                next_fire_at=past,
            )
        )

        await mgr.catch_up()
        await asyncio.sleep(0.1)
        assert calls == ["grace001"]

    @pytest.mark.asyncio
    async def test_catch_up_past_grace_marks_missed(self, store):
        calls = []

        async def cb(sched):
            calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb)
        # 600s late on a daily cron — too stale to fire.
        far_past = (
            datetime.now(timezone.utc) - timedelta(seconds=600)
        ).isoformat()
        store.add(
            _due_sched_dict(
                schedule_id="miss0001",
                target_thread_id=3002,
                next_fire_at=far_past,
                recurring=True,
                cron="0 9 * * *",
            )
        )

        await mgr.catch_up()
        await asyncio.sleep(0.1)

        assert calls == []  # NOT fired
        persisted = store.get("miss0001")
        assert persisted["state"]["missed_count"] == 1
        new_next_str = persisted["state"]["next_fire_at"]
        assert new_next_str is not None
        new_next = datetime.fromisoformat(new_next_str)
        # Rolled forward to a future fire time.
        assert new_next > datetime.now(timezone.utc)


# =============================================================== Group 13
# SchedulerManager._fire_one (direct invocation)


class TestFireOne:
    @pytest.mark.asyncio
    async def test_fire_one_success_oneshot_disables(self, store):
        async def cb(_sched):
            return None

        mgr = _build_mgr(store, fire_message=cb)
        sched = _due_sched_dict(
            schedule_id="one0one1",
            target_thread_id=4001,
            recurring=False,
            iso=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        persisted = store.get("one0one1")
        assert persisted["state"]["enabled"] is False
        assert persisted["state"]["fire_count"] == 1
        assert persisted["state"]["last_fired_at"] is not None
        assert persisted["state"]["first_fired_at"] is not None
        assert persisted["state"]["next_fire_at"] is None

    @pytest.mark.asyncio
    async def test_fire_one_success_cron_recomputes(self, store):
        async def cb(_sched):
            return None

        mgr = _build_mgr(store, fire_message=cb)
        sched = _due_sched_dict(
            schedule_id="cronfire",
            target_thread_id=4002,
            recurring=True,
            cron="0 9 * * *",
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        persisted = store.get("cronfire")
        assert persisted["state"]["enabled"] is True
        assert persisted["state"]["fire_count"] == 1
        new_next = datetime.fromisoformat(
            persisted["state"]["next_fire_at"]
        )
        assert new_next > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_fire_one_notfound_disables(self, store):
        expire_calls = []

        async def cb(_sched):
            raise _make_notfound()

        async def expire(sched):
            expire_calls.append(sched["schedule_id"])

        mgr = _build_mgr(
            store,
            fire_message=cb,
            expire_notify=expire,
        )
        sched = _due_sched_dict(
            schedule_id="gone0001",
            target_thread_id=4003,
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)
        await asyncio.sleep(0)  # let any spawned tasks settle

        persisted = store.get("gone0001")
        assert persisted["state"]["enabled"] is False
        assert "NotFound" in (persisted["state"]["last_error"] or "")
        assert expire_calls == []  # terminal != lifetime

    @pytest.mark.asyncio
    async def test_fire_one_forbidden_disables(self, store):
        async def cb(_sched):
            raise _make_forbidden()

        mgr = _build_mgr(store, fire_message=cb)
        sched = _due_sched_dict(
            schedule_id="forbid01",
            target_thread_id=4004,
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        persisted = store.get("forbid01")
        assert persisted["state"]["enabled"] is False
        assert "Forbidden" in (persisted["state"]["last_error"] or "")

    @pytest.mark.asyncio
    async def test_fire_one_transient_retries_three_times(
        self, store, monkeypatch
    ):
        # Don't actually wait 1+4+16 seconds during backoff.
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)

        call_count = {"n": 0}

        async def cb(_sched):
            call_count["n"] += 1
            raise RuntimeError("boom")

        mgr = _build_mgr(store, fire_message=cb)
        sched = _due_sched_dict(
            schedule_id="tx3retry",
            target_thread_id=4005,
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        assert call_count["n"] == 3
        persisted = store.get("tx3retry")
        assert persisted["state"]["enabled"] is False
        assert "boom" in (persisted["state"]["last_error"] or "")
        # M12: pin the EXACT scheduler-backoff sleep sequence.
        # The 5/19 directive requires strict assertions; the previous
        # ``(1,) in sleep_calls and (4,) in sleep_calls`` subset check
        # would have missed a regression that (a) swapped the 1↔4 order,
        # (b) added a spurious 16s sleep before terminal disable (the
        # contract is "no sleep after the 3rd failure — disable immediately"),
        # or (c) regressed to a single retry.
        sleep_calls = [c.args for c in sleep_mock.await_args_list]
        # Filter to just the scheduler retry backoffs in case the
        # production code grows an unrelated asyncio.sleep elsewhere in
        # the path (e.g. for log flush).
        backoff_sleeps = [c for c in sleep_calls if c in ((1,), (4,), (16,))]
        # Exactly 2, in order, with NO 16-second sleep before terminal.
        assert backoff_sleeps == [(1,), (4,)], (
            f"expected [(1,), (4,)] in order, got {backoff_sleeps!r} "
            f"(all sleep calls: {sleep_calls!r})"
        )

    @pytest.mark.asyncio
    async def test_fire_one_transient_recovers_on_attempt_2(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        call_count = {"n": 0}

        async def cb(_sched):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient")

        mgr = _build_mgr(store, fire_message=cb)
        sched = _due_sched_dict(
            schedule_id="recover2",
            target_thread_id=4006,
            recurring=True,
            cron="0 9 * * *",
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        assert call_count["n"] == 2
        persisted = store.get("recover2")
        assert persisted["state"]["fire_count"] == 1
        assert persisted["state"]["enabled"] is True
        assert persisted["state"]["last_error"] is None

    @pytest.mark.asyncio
    async def test_fire_one_acquires_lock(self, store):
        events: list[str] = []

        class TrackingLock:
            def __init__(self):
                self._lock = asyncio.Lock()

            async def __aenter__(self):
                await self._lock.__aenter__()
                events.append("lock_acquired")
                return self

            async def __aexit__(self, *args):
                events.append("lock_released")
                return await self._lock.__aexit__(*args)

        tracking = TrackingLock()

        def get_lock(_lock_id: int):
            return tracking

        async def cb(_sched):
            events.append("fire_invoked")

        mgr = _build_mgr(store, fire_message=cb, get_lock=get_lock)
        sched = _due_sched_dict(
            schedule_id="lockord1",
            target_thread_id=4007,
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
        )
        store.add(sched)

        await mgr._fire_one(sched)

        assert events == [
            "lock_acquired",
            "fire_invoked",
            "lock_released",
        ]

    @pytest.mark.asyncio
    async def test_two_fires_same_lock_id_serialize(self, store):
        """M13 / R1 Tester #3: the per-target lock's actual contract is
        *mutual exclusion of overlapping fires aimed at the same target* —
        not "a lock is acquired somewhere around the call." A buggy
        ``_fire_one`` that ``await``-ed the lock but never wrapped the
        callback in ``async with`` would still pass the
        ``test_fire_one_acquires_lock`` event-trace test.

        This test pins serialization directly: two schedules with the
        same ``target_thread_id`` (and therefore the same lock id) are
        dispatched concurrently. Each callback sleeps 0.05s between
        ``enter`` and ``exit`` events. With the lock honoured, the
        events must be one of two ordered, non-interleaved sequences.
        Without the lock (regression), an interleaved sequence like
        ``[enter:A, enter:B, exit:A, exit:B]`` would land.
        """
        events: list[str] = []

        async def cb(sched):
            sid = sched["schedule_id"]
            events.append(f"enter:{sid}")
            await asyncio.sleep(0.05)
            events.append(f"exit:{sid}")

        # Shared lock pool: one asyncio.Lock per lock-id, returned to both
        # _fire_one calls. _build_mgr's default get_lock already uses a
        # shared map, so we just use it here.
        mgr = _build_mgr(store, fire_message=cb)

        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        sched_a = _due_sched_dict(
            schedule_id="serialA",
            target_thread_id=9999,  # SAME lock id
            next_fire_at=past,
        )
        sched_b = _due_sched_dict(
            schedule_id="serialB",
            target_thread_id=9999,  # SAME lock id
            next_fire_at=past,
        )
        store.add(sched_a)
        store.add(sched_b)

        # Dispatch concurrently — gather forces both _fire_one calls
        # onto the event loop simultaneously, so without the lock they
        # would interleave inside the 0.05s callback sleep.
        await asyncio.gather(
            mgr._fire_one(sched_a),
            mgr._fire_one(sched_b),
        )

        # Strict: events must be one of the two non-interleaved orders.
        # A regression that removed the ``async with self.get_lock(...)``
        # would produce an interleaved sequence like
        # ``["enter:serialA", "enter:serialB", "exit:serialA", "exit:serialB"]``.
        assert events in (
            ["enter:serialA", "exit:serialA", "enter:serialB", "exit:serialB"],
            ["enter:serialB", "exit:serialB", "enter:serialA", "exit:serialA"],
        ), f"fires interleaved (lock contract broken): {events!r}"

    @pytest.mark.asyncio
    async def test_max_global_inflight_cap(self, store):
        counter = {"current": 0, "peak": 0}

        async def cb(_sched):
            counter["current"] += 1
            if counter["current"] > counter["peak"]:
                counter["peak"] = counter["current"]
            await asyncio.sleep(0.05)
            counter["current"] -= 1

        mgr = _build_mgr(store, fire_message=cb)

        past = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        n_sched = 15  # > MAX_GLOBAL_INFLIGHT (10)
        for i in range(n_sched):
            store.add(
                _due_sched_dict(
                    schedule_id=f"capN{i:04d}",
                    # Distinct target_thread_id per schedule so the
                    # per-target lock doesn't serialize them.
                    target_thread_id=5000 + i,
                    next_fire_at=past,
                )
            )

        await mgr.tick()
        # 15 schedules × 0.05s wait / 10 inflight = ~0.1s; sleep longer
        # to be safe so all tasks finish before the test exits.
        await asyncio.sleep(0.5)

        assert counter["peak"] <= MAX_GLOBAL_INFLIGHT
        assert counter["peak"] > 1  # Some concurrency really happened.


# =============================================================== Group 14
# _check_max_lifetime


class TestMaxLifetime:
    @pytest.mark.asyncio
    async def test_max_lifetime_expires(self, store):
        expire_calls = []

        async def cb(_sched):
            return None

        async def expire(sched):
            expire_calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb, expire_notify=expire)
        # first_fired_at 5s in the past + max_lifetime 2s → expired.
        first_fired = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        sched = _due_sched_dict(
            schedule_id="lifeOver",
            target_thread_id=6001,
            recurring=True,
            cron="0 9 * * *",
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
            first_fired_at=first_fired,
            max_lifetime_seconds=2,
        )
        store.add(sched)

        await mgr._fire_one(sched)
        # Give the spawned expire-notify task a chance to run.
        await asyncio.sleep(0.05)

        persisted = store.get("lifeOver")
        assert persisted["state"]["enabled"] is False
        assert persisted["state"]["last_error"] == "max_lifetime reached"
        assert expire_calls == ["lifeOver"]

    @pytest.mark.asyncio
    async def test_max_lifetime_not_yet_reached(self, store):
        expire_calls = []

        async def cb(_sched):
            return None

        async def expire(sched):
            expire_calls.append(sched["schedule_id"])

        mgr = _build_mgr(store, fire_message=cb, expire_notify=expire)
        # first_fired_at is now → 0s elapsed, well below 3600s cap.
        first_fired = datetime.now(timezone.utc).isoformat()
        sched = _due_sched_dict(
            schedule_id="lifeOK01",
            target_thread_id=6002,
            recurring=True,
            cron="0 9 * * *",
            next_fire_at=(
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat(),
            first_fired_at=first_fired,
            max_lifetime_seconds=3600,
        )
        store.add(sched)

        await mgr._fire_one(sched)
        await asyncio.sleep(0.05)

        persisted = store.get("lifeOK01")
        # cron + within lifetime → still enabled, no expire callback.
        assert persisted["state"]["enabled"] is True
        assert persisted["state"]["last_error"] is None
        assert expire_calls == []
