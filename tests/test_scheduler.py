"""#241 — scheduler core tests."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.scheduler import (
    CLAUDE_MIN_INTERVAL_S,
    MISSED_FIRE_GRACE_S,
    SchedulerManager,
    compute_next_fire,
    parse_cron_to_next,
    parse_iso_utc,
)
from clauded.scheduler_store import SchedulerStore


# ---------------------------------------------------------------------------
# Trigger parsing
# ---------------------------------------------------------------------------


def test_parse_iso_utc_with_tz():
    dt = parse_iso_utc("2026-05-19T09:00:00+08:00")
    assert dt.tzinfo is timezone.utc
    # 09:00 +08 = 01:00 UTC
    assert dt.hour == 1
    assert dt.year == 2026


def test_parse_iso_utc_strips_iso_prefix():
    dt = parse_iso_utc("iso: 2026-05-19T09:00:00+08:00")
    assert dt.year == 2026


def test_parse_iso_utc_naive_rejected():
    with pytest.raises(ValueError, match="timezone"):
        parse_iso_utc("2026-05-19T09:00:00")


def test_parse_iso_utc_garbage_rejected():
    with pytest.raises(ValueError):
        parse_iso_utc("not an iso date")


def test_parse_cron_to_next_daily_9am():
    base = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    nxt = parse_cron_to_next("0 9 * * *", tz_name="UTC", base=base)
    assert nxt.hour == 9
    assert nxt.year == 2026


def test_parse_cron_to_next_with_tz():
    """0 9 * * * in Shanghai = 01:00 UTC."""
    base = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    nxt = parse_cron_to_next("0 9 * * *", tz_name="Asia/Shanghai", base=base)
    # Verified manually: 2026-05-19 09:00 Asia/Shanghai = 2026-05-19 01:00 UTC
    assert nxt.hour == 1


def test_parse_cron_to_next_strips_cron_prefix():
    base = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    nxt = parse_cron_to_next("cron: 0 9 * * *", tz_name="UTC", base=base)
    assert nxt.hour == 9


def test_parse_cron_invalid_raises():
    with pytest.raises(ValueError):
        parse_cron_to_next("not a cron expr")


def test_parse_cron_invalid_tz_raises():
    with pytest.raises(ValueError, match="timezone"):
        parse_cron_to_next("0 9 * * *", tz_name="Mars/Phobos")


def test_compute_next_fire_once_past_returns_none():
    """One-shot triggers in the past return None — caller disables."""
    trigger = {"kind": "once", "iso": "2020-01-01T00:00:00+00:00"}
    assert compute_next_fire(trigger) is None


def test_compute_next_fire_once_future():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    trigger = {"kind": "once", "iso": future.isoformat()}
    n = compute_next_fire(trigger)
    assert n is not None
    assert abs((n - future).total_seconds()) < 1


def test_compute_next_fire_cron_rolls_forward():
    trigger = {"kind": "cron", "cron": "0 9 * * *", "tz_when_created": "UTC"}
    base = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)  # past 9am
    n = compute_next_fire(trigger, after=base)
    assert n is not None
    assert n.hour == 9
    assert n.day == 20  # next day


# ---------------------------------------------------------------------------
# SchedulerStore
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir():
    d = tempfile.mkdtemp(prefix="sched_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_store_empty_load(store_dir):
    s = SchedulerStore(data_dir=store_dir)
    assert s.list_all() == {}


def test_store_add_get_delete_roundtrip(store_dir):
    s = SchedulerStore(data_dir=store_dir)
    sid = s.add({"name": "test", "target_thread_id": 42})
    assert s.get(sid)["name"] == "test"
    # Reload from disk
    s2 = SchedulerStore(data_dir=store_dir)
    assert s2.get(sid)["name"] == "test"
    s2.delete(sid)
    s3 = SchedulerStore(data_dir=store_dir)
    assert s3.get(sid) is None


def test_store_list_for_thread(store_dir):
    s = SchedulerStore(data_dir=store_dir)
    s.add({"name": "a", "target_thread_id": 100})
    s.add({"name": "b", "target_thread_id": 100})
    s.add({"name": "c", "target_thread_id": 200})
    assert len(s.list_for_thread(100)) == 2
    assert len(s.list_for_thread(200)) == 1


def test_store_count_active(store_dir):
    s = SchedulerStore(data_dir=store_dir)
    s.add({"created_by": "claude", "state": {"enabled": True}})
    s.add({"created_by": "claude", "state": {"enabled": False}})
    s.add({"created_by": "claude", "state": {"enabled": True}})
    assert s.count_active_for_user("claude") == 2
    assert s.count_active_total() == 2


def test_store_corrupt_json_recovers_empty(store_dir):
    """Bad JSON on disk → log warning + start with empty state."""
    Path(store_dir, "schedules.json").write_text("{garbage json")
    s = SchedulerStore(data_dir=store_dir)
    assert s.list_all() == {}


# ---------------------------------------------------------------------------
# SchedulerManager — CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr(store_dir):
    store = SchedulerStore(data_dir=store_dir)
    fire_calls = []
    async def _fire(s):
        fire_calls.append(s)
    m = SchedulerManager(store, fire_callback=_fire)
    m._fire_calls = fire_calls  # for test inspection
    return m


def test_create_iso_future(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}",
        what="hello",
        target_thread_id=42,
        channel_id=10,
        guild_id=None,
        created_by=12345,
        is_claude_created=False,
    )
    assert sched["state"]["enabled"]
    assert sched["state"]["next_fire_at"]
    assert sched["payload"]["what"] == "hello"
    assert sched["created_by"] == "12345"


def test_create_iso_past_rejected(mgr):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        mgr.create(
            when=f"iso: {past}", what="x", target_thread_id=42,
            channel_id=10, guild_id=None, created_by=1,
        )


def test_create_cron_claude_min_interval_enforced(mgr):
    """Cron with <5min interval rejected for claude-created."""
    with pytest.raises(ValueError, match="min interval"):
        mgr.create(
            when="cron: * * * * *",   # every minute
            what="x", target_thread_id=42, channel_id=10,
            guild_id=None, created_by="claude", is_claude_created=True,
        )


def test_create_cron_no_claude_min_for_user(mgr):
    """User-created has no min interval cap."""
    sched = mgr.create(
        when="cron: * * * * *",
        what="x", target_thread_id=42, channel_id=10,
        guild_id=None, created_by=12345, is_claude_created=False,
    )
    assert sched["state"]["enabled"]


def test_create_what_empty_rejected(mgr):
    with pytest.raises(ValueError, match="non-empty"):
        mgr.create(
            when="cron: 0 9 * * *", what="", target_thread_id=42,
            channel_id=10, guild_id=None, created_by=1,
        )


def test_create_what_too_long_rejected(mgr):
    with pytest.raises(ValueError, match="500"):
        mgr.create(
            when="cron: 0 9 * * *", what="x" * 501, target_thread_id=42,
            channel_id=10, guild_id=None, created_by=1,
        )


def test_create_bad_when_format_rejected(mgr):
    with pytest.raises(ValueError, match="cron|iso"):
        mgr.create(
            when="tomorrow at 9am", what="x", target_thread_id=42,
            channel_id=10, guild_id=None, created_by=1,
        )


def test_delete_permissions(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by=123,
    )
    sid = sched["schedule_id"]
    # Other user can't delete
    ok, _ = mgr.delete(sid, requester=999)
    assert not ok
    # Creator can
    ok, _ = mgr.delete(sid, requester=123)
    assert ok
    # Now gone
    assert mgr.store.get(sid) is None


def test_delete_admin_override(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by=123,
    )
    sid = sched["schedule_id"]
    ok, _ = mgr.delete(sid, requester=999, is_admin=True)
    assert ok


def test_claude_can_only_delete_claude_created(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by=123,
        is_claude_created=False,
    )
    sid = sched["schedule_id"]
    ok, reason = mgr.delete(sid, requester="claude", is_admin=False)
    assert not ok
    assert "claude-created" in reason


def test_toggle_disable_enable(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by=123,
    )
    sid = sched["schedule_id"]
    ok, _ = mgr.toggle(sid, False, requester=123)
    assert ok
    assert not mgr.store.get(sid)["state"]["enabled"]
    ok, _ = mgr.toggle(sid, True, requester=123)
    assert ok
    assert mgr.store.get(sid)["state"]["enabled"]


# ---------------------------------------------------------------------------
# SchedulerManager — tick / fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_fires_due_schedules(mgr):
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    mgr.store.add({
        "schedule_id": "test-1",
        "target_thread_id": 42,
        "state": {"enabled": True, "next_fire_at": past, "fire_count": 0},
        "trigger": {"kind": "once", "iso": past},
        "payload": {"what": "fire me"},
    })
    await mgr.tick()
    # Wait for the scheduled task to actually run
    await asyncio.sleep(0.1)
    assert len(mgr._fire_calls) == 1


@pytest.mark.asyncio
async def test_tick_skips_future_schedules(mgr):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    mgr.store.add({
        "schedule_id": "test-future",
        "target_thread_id": 42,
        "state": {"enabled": True, "next_fire_at": future},
        "trigger": {"kind": "once", "iso": future},
        "payload": {"what": "not yet"},
    })
    await mgr.tick()
    await asyncio.sleep(0.1)
    assert len(mgr._fire_calls) == 0


@pytest.mark.asyncio
async def test_tick_skips_disabled(mgr):
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    mgr.store.add({
        "schedule_id": "test-disabled",
        "target_thread_id": 42,
        "state": {"enabled": False, "next_fire_at": past},
        "trigger": {"kind": "once", "iso": past},
        "payload": {"what": "should not fire"},
    })
    await mgr.tick()
    await asyncio.sleep(0.1)
    assert len(mgr._fire_calls) == 0


@pytest.mark.asyncio
async def test_catch_up_within_grace_fires(mgr):
    """next_fire_at < 5min ago → fire immediately."""
    just_past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    mgr.store.add({
        "schedule_id": "catchup-grace",
        "target_thread_id": 42,
        "state": {"enabled": True, "next_fire_at": just_past, "fire_count": 0},
        "trigger": {"kind": "once", "iso": just_past},
        "payload": {"what": "catch me"},
    })
    await mgr.catch_up()
    await asyncio.sleep(0.1)
    assert len(mgr._fire_calls) == 1


@pytest.mark.asyncio
async def test_catch_up_past_grace_marks_missed(mgr):
    """next_fire_at > 5min ago → mark missed, DON'T fire."""
    way_past = (datetime.now(timezone.utc) - timedelta(seconds=MISSED_FIRE_GRACE_S + 60)).isoformat()
    mgr.store.add({
        "schedule_id": "catchup-missed",
        "target_thread_id": 42,
        "state": {
            "enabled": True, "next_fire_at": way_past,
            "fire_count": 0, "missed_count": 0,
        },
        "trigger": {"kind": "cron", "cron": "0 9 * * *", "tz_when_created": "UTC"},
        "payload": {"what": "missed"},
    })
    await mgr.catch_up()
    await asyncio.sleep(0.1)
    # Should NOT have fired
    assert len(mgr._fire_calls) == 0
    # Should have incremented missed_count
    sched = mgr.store.get("catchup-missed")
    assert sched["state"]["missed_count"] == 1
    # Should have rolled next_fire_at forward
    new_nfa = datetime.fromisoformat(sched["state"]["next_fire_at"])
    if new_nfa.tzinfo is None:
        new_nfa = new_nfa.replace(tzinfo=timezone.utc)
    assert new_nfa > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_fire_success_updates_state():
    """After successful fire, state is updated with last_fired_at + fire_count."""
    d = tempfile.mkdtemp(prefix="sched_")
    try:
        store = SchedulerStore(data_dir=d)
        fired = []
        async def _fire(s):
            fired.append(s)
        mgr = SchedulerManager(store, fire_callback=_fire)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        sched = {
            "schedule_id": "fire-state",
            "target_thread_id": 42,
            "state": {"enabled": True, "next_fire_at": past, "fire_count": 5},
            "trigger": {"kind": "once", "iso": past},
            "payload": {"what": "x"},
        }
        store.add(sched)
        await mgr._fire_one(sched)
        updated = store.get("fire-state")
        # one-shot completed → disabled
        assert not updated["state"]["enabled"]
        assert updated["state"]["fire_count"] == 6
        assert updated["state"]["last_fired_at"] is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_fire_recurring_recomputes_next():
    """Cron schedule: after fire, next_fire_at rolls to next occurrence."""
    d = tempfile.mkdtemp(prefix="sched_")
    try:
        store = SchedulerStore(data_dir=d)
        async def _fire(s):
            pass
        mgr = SchedulerManager(store, fire_callback=_fire)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        sched = {
            "schedule_id": "fire-recur",
            "target_thread_id": 42,
            "state": {"enabled": True, "next_fire_at": past, "fire_count": 0},
            "trigger": {"kind": "cron", "cron": "0 9 * * *", "tz_when_created": "UTC"},
            "payload": {"what": "x"},
        }
        store.add(sched)
        await mgr._fire_one(sched)
        updated = store.get("fire-recur")
        assert updated["state"]["enabled"]
        assert updated["state"]["fire_count"] == 1
        nfa = datetime.fromisoformat(updated["state"]["next_fire_at"])
        assert nfa > datetime.now(timezone.utc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_fire_discord_notfound_disables():
    """NotFound (target thread gone) → disable schedule."""
    import discord
    d = tempfile.mkdtemp(prefix="sched_")
    try:
        store = SchedulerStore(data_dir=d)
        async def _fire(s):
            raise discord.NotFound(
                MagicMock(status=404), "thread gone",
            )
        mgr = SchedulerManager(store, fire_callback=_fire)
        future = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        sched = {
            "schedule_id": "notfound",
            "target_thread_id": 42,
            "state": {"enabled": True, "next_fire_at": future, "fire_count": 0},
            "trigger": {"kind": "once", "iso": future},
            "payload": {"what": "x"},
        }
        store.add(sched)
        await mgr._fire_one(sched)
        updated = store.get("notfound")
        assert not updated["state"]["enabled"]
        assert "NotFound" in (updated["state"].get("last_error") or "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Per-thread lock contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_acquires_lock():
    """Manager must use the get_lock callback to serialize per-thread."""
    d = tempfile.mkdtemp(prefix="sched_")
    try:
        store = SchedulerStore(data_dir=d)
        lock_acquired = []

        class TrackingLock:
            def __init__(self):
                self._inner = asyncio.Lock()
            async def __aenter__(self):
                lock_acquired.append(True)
                return await self._inner.__aenter__()
            async def __aexit__(self, *args):
                return await self._inner.__aexit__(*args)

        lock = TrackingLock()

        def _get_lock(tid):
            return lock

        async def _fire(s):
            pass

        mgr = SchedulerManager(
            store, fire_callback=_fire, get_lock=_get_lock,
        )
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        sched = {
            "schedule_id": "lock-test",
            "target_thread_id": 42,
            "state": {"enabled": True, "next_fire_at": past},
            "trigger": {"kind": "once", "iso": past},
            "payload": {"what": "x"},
        }
        store.add(sched)
        await mgr._fire_one(sched)
        assert lock_acquired, "lock was not acquired"
    finally:
        shutil.rmtree(d, ignore_errors=True)
