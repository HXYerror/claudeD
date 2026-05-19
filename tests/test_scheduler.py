"""Unit tests for the v1.18 scheduler core (issue #241, Subtask 1).

Covers, per ``/tmp/subtask-1-prompt-v2.md`` test plan:

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
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clauded.scheduler import (
    MAX_GLOBAL_ACTIVE,
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
