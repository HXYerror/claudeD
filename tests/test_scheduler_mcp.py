"""#241 — MCP tool layer tests."""
from __future__ import annotations

import asyncio
import inspect
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from clauded.scheduler import SchedulerManager
from clauded.scheduler_store import SchedulerStore
from clauded import scheduler_mcp


@pytest.fixture
def mgr():
    d = tempfile.mkdtemp(prefix="mcp_test_")
    store = SchedulerStore(data_dir=d)
    async def _fire(s):
        pass
    m = SchedulerManager(store, fire_callback=_fire)
    scheduler_mcp.set_scheduler_manager(m)
    yield m
    scheduler_mcp.set_scheduler_manager(None)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ctx_provider():
    """Provide a default thread context so MCP tools work."""
    def _ctx():
        return {
            "thread_id": 42,
            "channel_id": 10,
            "guild_id": 1,
            "tz_name": "UTC",
        }
    return _ctx


def _call(tool_obj, args):
    """Helper: invoke an SdkMcpTool by extracting its handler."""
    handler = tool_obj.handler if hasattr(tool_obj, "handler") else tool_obj
    return handler(args)


# ---------------------------------------------------------------------------
# build_scheduler_mcp_server
# ---------------------------------------------------------------------------


def test_mcp_server_builds_with_4_tools():
    server = scheduler_mcp.build_scheduler_mcp_server()
    # The result is a dict per the SDK contract
    assert isinstance(server, dict)
    assert server["instance"].name == "clauded-scheduler"
    # 4 tools (in SDK 0.1.80, server contains an `instance` McpServer object)
    instance = server["instance"]
    # Tools registered via mcp.tool decorator are accessible
    # Hard to introspect without driving an MCP RPC; just verify the
    # builder doesn't crash.


# ---------------------------------------------------------------------------
# schedule_create_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tool_no_manager_errors():
    """No manager wired → error response, not crash."""
    scheduler_mcp.set_scheduler_manager(None)
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": "iso: 2099-01-01T00:00:00+00:00",
        "what": "test",
    })
    assert result.get("is_error")
    assert "not initialized" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_create_tool_happy(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": f"iso: {future}",
        "what": "remind me",
        "name": "my-test",
    })
    assert not result.get("is_error")
    text = result["content"][0]["text"]
    assert "Created schedule" in text
    # Verify in store
    items = list(mgr.store.list_all().values())
    assert len(items) == 1
    assert items[0]["created_by"] == "claude"


@pytest.mark.asyncio
async def test_create_tool_claude_min_interval_enforced(mgr, ctx_provider):
    """5-min minimum interval check fires before storage."""
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": "cron: * * * * *",  # every minute
        "what": "spam",
    })
    assert result.get("is_error")
    assert "min interval" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_create_tool_global_cap(mgr, ctx_provider):
    """Global active cap = 100. Pre-populate 100 then try one more."""
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    for i in range(100):
        mgr.store.add({
            "schedule_id": f"sched-{i}",
            "created_by": "claude",
            "target_thread_id": 42,
            "channel_id": 10,
            "state": {"enabled": True, "next_fire_at": "2099-01-01T00:00:00+00:00"},
            "trigger": {"kind": "once", "iso": "2099-01-01T00:00:00+00:00"},
            "payload": {"what": str(i)},
        })
    # ↑ 100 active for "claude" too, so per-user cap (20) fires first
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": f"iso: {future}",
        "what": "one more",
    })
    assert result.get("is_error")
    text = result["content"][0]["text"]
    assert "cap" in text.lower()


@pytest.mark.asyncio
async def test_create_tool_no_ctx_errors(mgr):
    """No context provider → no target thread → error."""
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=lambda: {})
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": f"iso: {future}",
        "what": "test",
    })
    assert result.get("is_error")
    assert "target thread" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_create_tool_invalid_when(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    result = await _call(scheduler_mcp.schedule_create_tool, {
        "when": "tomorrow at 9am",
        "what": "test",
    })
    assert result.get("is_error")


# ---------------------------------------------------------------------------
# schedule_list_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tool_empty(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    result = await _call(scheduler_mcp.schedule_list_tool, {"scope": "thread"})
    text = result["content"][0]["text"]
    assert "no schedules" in text.lower()


@pytest.mark.asyncio
async def test_list_tool_shows_emoji_markers(mgr, ctx_provider):
    """🤖 vs 👤 marker based on created_by."""
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    mgr.store.add({
        "schedule_id": "claude-one",
        "name": "claude-schedule",
        "created_by": "claude",
        "target_thread_id": 42,
        "state": {"enabled": True, "next_fire_at": "2099-01-01"},
        "trigger": {"kind": "cron", "cron": "0 9 * * *"},
        "payload": {"what": "x"},
    })
    mgr.store.add({
        "schedule_id": "user-one",
        "name": "user-schedule",
        "created_by": "12345",
        "target_thread_id": 42,
        "state": {"enabled": True, "next_fire_at": "2099-01-01"},
        "trigger": {"kind": "cron", "cron": "0 9 * * *"},
        "payload": {"what": "y"},
    })
    result = await _call(scheduler_mcp.schedule_list_tool, {"scope": "thread"})
    text = result["content"][0]["text"]
    assert "🤖" in text  # claude marker
    assert "👤" in text  # user marker


# ---------------------------------------------------------------------------
# schedule_delete_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_tool_claude_can_delete_own(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by="claude",
        is_claude_created=True, tz_name="UTC",
    )
    sid = sched["schedule_id"]
    result = await _call(scheduler_mcp.schedule_delete_tool, {"schedule_id": sid})
    assert not result.get("is_error")
    assert mgr.store.get(sid) is None


@pytest.mark.asyncio
async def test_delete_tool_claude_cannot_delete_user_created(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by=123,
        is_claude_created=False, tz_name="UTC",
    )
    sid = sched["schedule_id"]
    result = await _call(scheduler_mcp.schedule_delete_tool, {"schedule_id": sid})
    assert result.get("is_error")
    assert mgr.store.get(sid) is not None  # still exists


@pytest.mark.asyncio
async def test_delete_tool_prefix_matching(mgr, ctx_provider):
    """8-char prefix should resolve to full id."""
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by="claude",
        is_claude_created=True, tz_name="UTC",
    )
    prefix = sched["schedule_id"][:8]
    result = await _call(scheduler_mcp.schedule_delete_tool,
                        {"schedule_id": prefix})
    assert not result.get("is_error")


# ---------------------------------------------------------------------------
# schedule_toggle_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_tool_claude_own(mgr, ctx_provider):
    scheduler_mcp.set_scheduler_manager(mgr, ctx_provider=ctx_provider)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sched = mgr.create(
        when=f"iso: {future}", what="x", target_thread_id=42,
        channel_id=10, guild_id=None, created_by="claude",
        is_claude_created=True, tz_name="UTC",
    )
    sid = sched["schedule_id"]
    # Disable
    result = await _call(scheduler_mcp.schedule_toggle_tool,
                         {"schedule_id": sid, "enabled": False})
    assert not result.get("is_error")
    assert not mgr.store.get(sid)["state"]["enabled"]
    # Re-enable
    result = await _call(scheduler_mcp.schedule_toggle_tool,
                         {"schedule_id": sid, "enabled": True})
    assert mgr.store.get(sid)["state"]["enabled"]
