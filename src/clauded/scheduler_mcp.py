"""#241 — in-process MCP scheduler tools for Claude.

Exposes 4 tools to Claude via the SDK's in-process MCP server:

- ``schedule_create`` — claude creates a new schedule
- ``schedule_list``   — claude lists current schedules
- ``schedule_delete`` — claude deletes a claude-created schedule
- ``schedule_toggle`` — claude enables/disables a claude-created schedule

Tool handlers route to a global SchedulerManager (set by ``bot.py`` at
startup via :func:`set_scheduler_manager`). Permissions: claude tool
calls all use ``requester="claude"``, restricting deletion/toggle to
schedules claude itself created (#241 §权限模型).

Per-user / global active-schedule caps are enforced in this layer
(``schedule_create``) since the SDK already routes user_id context via
the calling thread; for now we hard-cap at 100 global / 20 per-user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger("clauded.scheduler_mcp")

# ---------------------------------------------------------------------------
# Global wire-up (set by bot.py at startup)
# ---------------------------------------------------------------------------

_GLOBAL_MGR: Any = None
_GLOBAL_CTX: Any = None


def set_scheduler_manager(mgr: Any, ctx_provider: Any = None) -> None:
    """Wire up the SchedulerManager + an optional ``ctx_provider``.

    ``ctx_provider`` is a callable that returns the current
    ``{thread_id, channel_id, guild_id, tz_name}`` dict — bot.py uses
    a thread-local-ish lookup keyed on the active bridge.

    For v1 we keep it simple: the bot sets a single "default context"
    dict per-thread before launching the bridge; tools read it via
    ``_GLOBAL_CTX``. Reset on each fresh session.
    """
    global _GLOBAL_MGR, _GLOBAL_CTX
    _GLOBAL_MGR = mgr
    _GLOBAL_CTX = ctx_provider


def _resolve_context() -> dict[str, Any]:
    """Return the current invocation context (target_thread_id etc.).

    ``ctx_provider`` is called fresh on each tool invocation so a
    long-lived MCP server picks up the current bridge's thread.
    """
    if _GLOBAL_CTX is None:
        return {}
    if callable(_GLOBAL_CTX):
        return _GLOBAL_CTX() or {}
    if isinstance(_GLOBAL_CTX, dict):
        return _GLOBAL_CTX
    return {}


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@tool(
    "schedule_create",
    "Create a scheduled task that injects a user prompt into the current "
    "thread's Claude session at a future time. The schedule fires "
    "automatically without user interaction. Use this when the user asks "
    "you to remind them at a specific time, or to set up a recurring task. "
    "`when` accepts 'cron: <5-field UTC cron>' (e.g. 'cron: 0 9 * * *' for "
    "9am daily) or 'iso: <ISO datetime with timezone>' (e.g. "
    "'iso: 2026-05-19T09:00:00+08:00'). For claude-created schedules the "
    "minimum interval is 5 minutes.",
    {
        "when": str,
        "what": str,
        "name": str,
        "target_thread_id": str,
        "recurring": bool,
    },
)
async def schedule_create_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Claude-facing tool: create a new schedule."""
    if _GLOBAL_MGR is None:
        return _err("scheduler not initialized (bot wiring issue)")

    when = args.get("when", "").strip()
    what = args.get("what", "").strip()
    name = args.get("name", "").strip() or ""
    target_str = args.get("target_thread_id", "").strip()

    if not when:
        return _err("`when` is required")
    if not what:
        return _err("`what` is required")

    ctx = _resolve_context()
    target_thread_id = None
    if target_str:
        try:
            target_thread_id = int(target_str)
        except (TypeError, ValueError):
            return _err(f"target_thread_id must be int-like; got {target_str!r}")
    else:
        target_thread_id = ctx.get("thread_id")
    if not isinstance(target_thread_id, int):
        return _err(
            "no target thread (call this tool from inside a bot thread, or "
            "pass `target_thread_id`)"
        )
    channel_id = ctx.get("channel_id")
    guild_id = ctx.get("guild_id")
    tz_name = ctx.get("tz_name", "Asia/Shanghai")

    # Per-user (claude) cap
    if _GLOBAL_MGR.store.count_active_for_user("claude") >= 20:
        return _err("claude already has 20 active schedules (per-user cap)")
    if _GLOBAL_MGR.store.count_active_total() >= 100:
        return _err("global cap reached (100 active schedules)")

    try:
        sched = _GLOBAL_MGR.create(
            when=when, what=what, name=name,
            target_thread_id=target_thread_id,
            channel_id=channel_id, guild_id=guild_id,
            created_by="claude",
            tz_name=tz_name, is_claude_created=True,
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("schedule_create unexpected failure")
        return _err(f"unexpected: {type(exc).__name__}: {exc}")

    state = sched.get("state", {})
    return _ok(
        f"Created schedule `{sched['schedule_id']}` "
        f"(name: {sched['name']}). "
        f"Next fire: {state.get('next_fire_at')}."
    )


@tool(
    "schedule_list",
    "List scheduled tasks. `scope` is one of: 'thread' (default, just "
    "current thread), 'channel' (all in this channel), or 'all'.",
    {
        "scope": str,
        "include_disabled": bool,
    },
)
async def schedule_list_tool(args: dict[str, Any]) -> dict[str, Any]:
    if _GLOBAL_MGR is None:
        return _err("scheduler not initialized")
    scope = (args.get("scope") or "thread").lower()
    include_disabled = bool(args.get("include_disabled", False))
    ctx = _resolve_context()

    if scope == "thread":
        thread_id = ctx.get("thread_id")
        if not isinstance(thread_id, int):
            return _err("no thread context for `scope=thread`")
        items = _GLOBAL_MGR.store.list_for_thread(thread_id)
    elif scope == "channel":
        channel_id = ctx.get("channel_id")
        if not isinstance(channel_id, int):
            return _err("no channel context for `scope=channel`")
        items = _GLOBAL_MGR.store.list_for_channel(channel_id)
    else:
        items = list(_GLOBAL_MGR.store.list_all().values())

    if not include_disabled:
        items = [s for s in items if s.get("state", {}).get("enabled", True)]

    if not items:
        return _ok("(no schedules)")

    lines = []
    for s in items:
        state = s.get("state", {})
        trig = s.get("trigger", {})
        when_human = trig.get("cron") or trig.get("iso", "?")
        what = s.get("payload", {}).get("what", "")
        what_preview = (what[:60] + "…") if len(what) > 60 else what
        marker = "🤖" if s.get("created_by") == "claude" else "👤"
        emoji_state = "✅" if state.get("enabled", True) else "⏸"
        lines.append(
            f"{emoji_state} {marker} `{s['schedule_id'][:8]}` "
            f"{s.get('name', '')} · {when_human} · "
            f"next={state.get('next_fire_at', '?')} · "
            f"\"{what_preview}\""
        )
    return _ok("\n".join(lines))


@tool(
    "schedule_delete",
    "Delete a schedule by id. Claude can only delete schedules it itself "
    "created.",
    {
        "schedule_id": str,
    },
)
async def schedule_delete_tool(args: dict[str, Any]) -> dict[str, Any]:
    if _GLOBAL_MGR is None:
        return _err("scheduler not initialized")
    sid = (args.get("schedule_id") or "").strip()
    if not sid:
        return _err("`schedule_id` is required")
    # Allow short-prefix matching: if user passed 8-char prefix, resolve
    if len(sid) < 32:
        matches = [
            full for full in _GLOBAL_MGR.store.list_all().keys()
            if full.startswith(sid)
        ]
        if len(matches) == 0:
            return _err(f"no schedule matches prefix {sid!r}")
        if len(matches) > 1:
            return _err(f"prefix {sid!r} ambiguous: {len(matches)} matches")
        sid = matches[0]
    ok, reason = _GLOBAL_MGR.delete(sid, requester="claude", is_admin=False)
    if not ok:
        return _err(reason)
    return _ok(f"Deleted schedule {sid}")


@tool(
    "schedule_toggle",
    "Enable or disable a schedule. Claude can only toggle schedules it "
    "itself created.",
    {
        "schedule_id": str,
        "enabled": bool,
    },
)
async def schedule_toggle_tool(args: dict[str, Any]) -> dict[str, Any]:
    if _GLOBAL_MGR is None:
        return _err("scheduler not initialized")
    sid = (args.get("schedule_id") or "").strip()
    enabled = bool(args.get("enabled", True))
    if not sid:
        return _err("`schedule_id` is required")
    if len(sid) < 32:
        matches = [
            full for full in _GLOBAL_MGR.store.list_all().keys()
            if full.startswith(sid)
        ]
        if len(matches) == 0:
            return _err(f"no schedule matches prefix {sid!r}")
        if len(matches) > 1:
            return _err(f"prefix {sid!r} ambiguous")
        sid = matches[0]
    ok, reason = _GLOBAL_MGR.toggle(sid, enabled, requester="claude", is_admin=False)
    if not ok:
        return _err(reason)
    return _ok(f"Schedule {sid} {'enabled' if enabled else 'disabled'}")


# ---------------------------------------------------------------------------
# Server constructor
# ---------------------------------------------------------------------------


def build_scheduler_mcp_server():
    """Build the in-process MCP server with the 4 scheduler tools."""
    return create_sdk_mcp_server(
        name="clauded-scheduler",
        version="1.0.0",
        tools=[
            schedule_create_tool,
            schedule_list_tool,
            schedule_delete_tool,
            schedule_toggle_tool,
        ],
    )
