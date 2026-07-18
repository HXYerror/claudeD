"""#audit(#8): fire-and-forget notify tasks must be strongly referenced so
CPython can't GC a task that has suspended on its first await. SchedulerManager
tracks them in ``_bg_tasks`` and each self-removes on completion.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from clauded.scheduler import SchedulerManager


async def _noop(_sched):
    return None


def _manager(expire_cb):
    return SchedulerManager(
        MagicMock(),
        fire_message_callback=_noop,
        fire_new_task_callback=_noop,
        expire_notify_callback=expire_cb,
    )


@pytest.mark.asyncio
async def test_max_lifetime_notify_task_is_tracked_then_drains():
    ran: list[str] = []

    async def expire_cb(sched):
        ran.append(sched.get("schedule_id"))

    mgr = _manager(expire_cb)
    sched = {
        "schedule_id": "s1",
        "max_lifetime_seconds": 1,
        "state": {"first_fired_at": "2020-01-01T00:00:00+00:00", "enabled": True},
    }

    expired = mgr._check_max_lifetime(sched)
    assert expired is True
    # The strong ref is held immediately — before the task gets to run. This is
    # exactly what stops the GC from collecting the still-suspended task.
    assert len(mgr._bg_tasks) == 1

    # Let the task run: it records the call and self-removes from the set.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ran == ["s1"]
    assert len(mgr._bg_tasks) == 0
