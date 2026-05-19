"""Unit tests for the v1.18 scheduler MCP server (issue #241, Subtask 3).

Per ``docs/prd/v1.18-scheduler.md`` §3.7 + §4 and the Subtask 3 brief, this
exercises the 5 ``schedule_*`` tools registered on the in-process SDK MCP
server. We invoke each tool's ``.handler`` directly (the ``@tool``
decorator wraps the underlying async fn into an ``SdkMcpTool`` dataclass
whose ``.handler`` is the original async callable).

Test groups:
  1. ``build_scheduler_mcp_server`` smoke         (2 tests)
  2. ``set_scheduler_manager`` clear-via-None     (1 test)
  3. ``schedule_message_tool``                    (6 tests)
  4. ``schedule_new_task_tool``                   (3 tests)
  5. ``schedule_list_tool``                       (4 tests)
  6. ``schedule_delete_tool``                     (3 tests)
  7. ``schedule_toggle_tool``                     (2 tests)
  8. global cap                                   (1 test)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clauded import scheduler_mcp
from clauded.scheduler import SchedulerManager
from clauded.scheduler_mcp import (
    _require_mgr,
    build_scheduler_mcp_server,
    schedule_delete_tool,
    schedule_list_tool,
    schedule_message_tool,
    schedule_new_task_tool,
    schedule_toggle_tool,
    set_scheduler_manager,
)
from clauded.scheduler_store import SchedulerStore


# ---------------------------------------------------------------- Helpers

async def _noop_cb(_sched):
    return None


def _future_iso(seconds_ahead: int = 3600) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead)
    return "iso: " + dt.isoformat()


async def _call(tool_obj, args: dict) -> dict:
    """Invoke an SdkMcpTool handler directly.

    The ``@tool`` decorator returns an ``SdkMcpTool`` dataclass whose
    ``.handler`` is the original async function. Fall back to calling
    the object itself in case the SDK shape changes in the future.
    """
    handler = getattr(tool_obj, "handler", tool_obj)
    return await handler(args)


# --------------------------------------------------------------- Fixtures

@pytest.fixture
def store(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return SchedulerStore(data_dir=str(d))


@pytest.fixture
def mgr(store):
    return SchedulerManager(
        store,
        fire_message_callback=_noop_cb,
        fire_new_task_callback=_noop_cb,
        expire_notify_callback=_noop_cb,
    )


@pytest.fixture
def ctx():
    """Default per-turn context."""
    return {
        "thread_id": 42,
        "channel_id": 10,
        "guild_id": 1,
        "tz_name": "UTC",
    }


@pytest.fixture
def wired(mgr, ctx):
    """Wire mgr + ctx_provider; tear down by clearing both."""
    set_scheduler_manager(mgr, ctx_provider=lambda: ctx)
    try:
        yield mgr
    finally:
        set_scheduler_manager(None)


# ============================================================= Group 1
# build_scheduler_mcp_server smoke


class TestBuildServer:
    def test_build_returns_config(self):
        cfg = build_scheduler_mcp_server()
        assert cfg is not None
        # McpSdkServerConfig is a TypedDict — accept dict-like access.
        assert cfg["type"] == "sdk"
        assert cfg["name"] == "clauded-scheduler"
        assert cfg["instance"] is not None

    def test_each_tool_has_handler(self):
        # Direct invocation contract: every @tool object exposes .handler
        for t in (
            schedule_message_tool,
            schedule_new_task_tool,
            schedule_list_tool,
            schedule_delete_tool,
            schedule_toggle_tool,
        ):
            assert hasattr(t, "handler")
            assert callable(t.handler)
            assert hasattr(t, "name")


# ============================================================= Group 2
# set_scheduler_manager clear-via-None


class TestSetSchedulerManager:
    def test_set_then_clear(self, mgr):
        set_scheduler_manager(mgr, ctx_provider=lambda: {})
        assert _require_mgr() is mgr
        set_scheduler_manager(None)
        assert _require_mgr() is None
        # _GLOBAL_CTX also cleared so a stale provider can't leak.
        assert scheduler_mcp._GLOBAL_CTX is None


# ============================================================= Group 3
# schedule_message_tool


class TestScheduleMessageTool:
    @pytest.mark.asyncio
    async def test_happy_path_with_ctx(self, wired):
        res = await _call(
            schedule_message_tool,
            {"when": _future_iso(3600), "what": "remind me"},
        )
        assert "is_error" not in res or res.get("is_error") is False
        text = res["content"][0]["text"]
        assert "Created schedule" in text
        assert "kind=message" in text

    @pytest.mark.asyncio
    async def test_no_manager_wired(self):
        set_scheduler_manager(None)
        res = await _call(
            schedule_message_tool,
            {"when": _future_iso(3600), "what": "x"},
        )
        assert res.get("is_error") is True
        assert "scheduler not initialized" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_no_ctx_and_no_target_thread_id(self, mgr):
        # Wire mgr but ctx_provider returns empty dict.
        set_scheduler_manager(mgr, ctx_provider=lambda: {})
        try:
            res = await _call(
                schedule_message_tool,
                {"when": _future_iso(3600), "what": "x"},
            )
        finally:
            set_scheduler_manager(None)
        assert res.get("is_error") is True
        assert "no target thread" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_bad_target_thread_id_str(self, wired):
        res = await _call(
            schedule_message_tool,
            {
                "when": _future_iso(3600),
                "what": "x",
                "target_thread_id": "not-numeric",
            },
        )
        assert res.get("is_error") is True
        assert "invalid target_thread_id" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_validation_failure_empty_what(self, wired):
        res = await _call(
            schedule_message_tool,
            {"when": _future_iso(3600), "what": ""},
        )
        assert res.get("is_error") is True
        # ValueError from SchedulerManager.create: "what must be non-empty…"
        assert "what" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_claude_min_interval_cron_rejected(self, wired):
        # 1-minute cron — well under 5-min claude min interval.
        res = await _call(
            schedule_message_tool,
            {
                "when": "cron: * * * * *",
                "what": "ping",
                "recurring": True,
            },
        )
        assert res.get("is_error") is True
        assert "min interval" in res["content"][0]["text"]


# ============================================================= Group 4
# schedule_new_task_tool


class TestScheduleNewTaskTool:
    @pytest.mark.asyncio
    async def test_happy_path_with_ctx(self, wired):
        res = await _call(
            schedule_new_task_tool,
            {"when": _future_iso(3600), "what": "write report"},
        )
        assert res.get("is_error", False) is False
        text = res["content"][0]["text"]
        assert "Created schedule" in text
        assert "kind=new_task" in text

    @pytest.mark.asyncio
    async def test_no_ctx_and_no_target_channel_id(self, mgr):
        set_scheduler_manager(mgr, ctx_provider=lambda: {})
        try:
            res = await _call(
                schedule_new_task_tool,
                {"when": _future_iso(3600), "what": "x"},
            )
        finally:
            set_scheduler_manager(None)
        assert res.get("is_error") is True
        assert "no target channel" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_validation_failure_long_what(self, wired):
        res = await _call(
            schedule_new_task_tool,
            {
                "when": _future_iso(3600),
                "what": "x" * 501,  # exceeds 500-char cap
            },
        )
        assert res.get("is_error") is True
        assert "what" in res["content"][0]["text"]


# ============================================================= Group 5
# schedule_list_tool


class TestScheduleListTool:
    @pytest.mark.asyncio
    async def test_empty(self, wired):
        res = await _call(schedule_list_tool, {"scope": "thread"})
        assert res.get("is_error", False) is False
        assert "(no schedules)" in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_markers_message_and_new_task(self, wired, ctx):
        mgr = wired
        # claude-created message into the current thread
        mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="msg",
            target_thread_id=ctx["thread_id"],
            created_by="claude",
            is_claude_created=True,
            guild_id=ctx["guild_id"],
            channel_id=ctx["channel_id"],
            tz_name=ctx["tz_name"],
        )
        # user-created new_task into the current channel
        mgr.create(
            kind="new_task",
            when=_future_iso(7200),
            what="task",
            target_channel_id=ctx["channel_id"],
            created_by="123456789",
            is_claude_created=False,
            guild_id=ctx["guild_id"],
            channel_id=ctx["channel_id"],
            tz_name=ctx["tz_name"],
        )

        res = await _call(schedule_list_tool, {"scope": "all"})
        text = res["content"][0]["text"]
        assert "📨" in text  # message marker
        assert "🧵" in text  # new_task marker
        assert "🤖" in text  # claude creator
        assert "👤" in text  # user creator

    @pytest.mark.asyncio
    async def test_scope_all(self, wired, ctx):
        mgr = wired
        mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="m",
            target_thread_id=999,  # different thread
            created_by="claude",
            is_claude_created=True,
            tz_name=ctx["tz_name"],
        )
        res = await _call(schedule_list_tool, {"scope": "all"})
        assert res.get("is_error", False) is False
        assert "(no schedules)" not in res["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_invalid_scope(self, wired):
        res = await _call(schedule_list_tool, {"scope": "bogus"})
        assert res.get("is_error") is True
        assert "invalid scope" in res["content"][0]["text"]


# ============================================================= Group 6
# schedule_delete_tool


class TestScheduleDeleteTool:
    @pytest.mark.asyncio
    async def test_claude_deletes_own(self, wired, ctx):
        mgr = wired
        sched = mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="m",
            target_thread_id=ctx["thread_id"],
            created_by="claude",
            is_claude_created=True,
            tz_name=ctx["tz_name"],
        )
        res = await _call(
            schedule_delete_tool,
            {"schedule_id": sched["schedule_id"]},
        )
        assert res.get("is_error", False) is False
        assert "Deleted" in res["content"][0]["text"]
        assert mgr.store.get(sched["schedule_id"]) is None

    @pytest.mark.asyncio
    async def test_claude_cannot_delete_user_schedule(self, wired, ctx):
        mgr = wired
        sched = mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="m",
            target_thread_id=ctx["thread_id"],
            created_by="123456789",
            is_claude_created=False,
            tz_name=ctx["tz_name"],
        )
        res = await _call(
            schedule_delete_tool,
            {"schedule_id": sched["schedule_id"]},
        )
        assert res.get("is_error") is True
        assert "claude-created" in res["content"][0]["text"]
        # still in store
        assert mgr.store.get(sched["schedule_id"]) is not None

    @pytest.mark.asyncio
    async def test_missing_schedule_id_arg(self, wired):
        res = await _call(schedule_delete_tool, {})
        assert res.get("is_error") is True
        assert "schedule_id required" in res["content"][0]["text"]


# ============================================================= Group 7
# schedule_toggle_tool


class TestScheduleToggleTool:
    @pytest.mark.asyncio
    async def test_disable_then_enable_roundtrip(self, wired, ctx):
        mgr = wired
        sched = mgr.create(
            kind="message",
            when=_future_iso(3600),
            what="m",
            target_thread_id=ctx["thread_id"],
            created_by="claude",
            is_claude_created=True,
            tz_name=ctx["tz_name"],
        )
        sid = sched["schedule_id"]

        res = await _call(
            schedule_toggle_tool,
            {"schedule_id": sid, "enabled": False},
        )
        assert res.get("is_error", False) is False
        assert mgr.store.get(sid)["state"]["enabled"] is False

        res = await _call(
            schedule_toggle_tool,
            {"schedule_id": sid, "enabled": True},
        )
        assert res.get("is_error", False) is False
        assert mgr.store.get(sid)["state"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_nonexistent_id(self, wired):
        res = await _call(
            schedule_toggle_tool,
            {"schedule_id": "deadbeefdeadbeef", "enabled": False},
        )
        assert res.get("is_error") is True
        assert "not found" in res["content"][0]["text"]


# ============================================================= Group 8
# global active cap


class TestGlobalCap:
    @pytest.mark.asyncio
    async def test_101st_schedule_rejected_via_tool(self, wired, ctx):
        mgr = wired
        # Pre-populate 100 active claude-created schedules directly in the
        # store (bypassing mgr.create — we only want the count_active_total
        # path to fire; we don't want to validate the 100 themselves).
        now_iso = datetime.now(timezone.utc).isoformat()
        future_iso = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        for i in range(100):
            mgr.store.add({
                "schedule_id": f"{i:016x}",
                "kind": "message",
                "name": f"s{i}",
                "created_at": now_iso,
                "created_by": "claude",
                "guild_id": 1,
                "channel_id": 10,
                "target_thread_id": 42,
                "target_channel_id": None,
                "thread_name": None,
                "trigger": {
                    "kind": "once",
                    "iso": future_iso,
                    "cron": None,
                    "tz_when_created": "UTC",
                    "recurring": False,
                },
                "payload": {"what": "x"},
                "max_lifetime_seconds": None,
                "state": {
                    "enabled": True,
                    "next_fire_at": future_iso,
                    "first_fired_at": None,
                    "last_fired_at": None,
                    "last_error": None,
                    "fire_count": 0,
                    "missed_count": 0,
                },
            })

        assert mgr.store.count_active_total() == 100

        res = await _call(
            schedule_message_tool,
            {"when": _future_iso(7200), "what": "one too many"},
        )
        assert res.get("is_error") is True
        assert "cap" in res["content"][0]["text"]
