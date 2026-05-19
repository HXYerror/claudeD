"""#241 — in-process MCP server exposing 5 ``schedule_*`` tools to claude.

PRD: ``docs/prd/v1.18-scheduler.md`` §3.7 + §4. Wired into
``ClaudeBridge.start()`` in Subtask 4 (do not import here from ``bot`` or
``claude_bridge`` — this module is intentionally standalone so it can be
imported from anywhere without dragging Discord types into pure-logic
code paths).

Five tools exposed:
  * ``schedule_message``   — kind=message  (inject into existing thread)
  * ``schedule_new_task``  — kind=new_task (spawn new thread + fresh session)
  * ``schedule_list``      — list schedules with kind / creator markers
  * ``schedule_delete``    — delete by 16-char hex id (claude→own only)
  * ``schedule_toggle``    — enable/disable                (claude→own only)

Module-level state (``_GLOBAL_MGR``, ``_GLOBAL_CTX``) is wired by the bot
before any turn runs; tests reset it via ``set_scheduler_manager(None)``.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from .scheduler import SchedulerManager

log = logging.getLogger(__name__)


# --------------------------------------------------------- Module state

_GLOBAL_MGR: SchedulerManager | None = None
_GLOBAL_CTX: Callable[[], dict] | None = None

# B2: per-task scheduler context. ``ContextVar.set`` mutates only the
# current asyncio task's copy of the context, so two concurrent fires or
# turns no longer race on a single bot-instance dict between
# ``set_ctx`` and the eventual tool call. ``set_ctx`` is called by the
# bot immediately before kicking off the turn / fire; ``_resolve_ctx``
# prefers the ContextVar value when set, falling back to the legacy
# ``_GLOBAL_CTX`` callable for code paths (older tests, callers wiring a
# custom ctx_provider) that haven't migrated yet.
_scheduler_ctx_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "scheduler_ctx", default={}
)


def set_ctx(ctx: dict) -> None:
    """Set the per-task scheduler context (B2 ContextVar entry-point).

    Stores ``ctx`` in :data:`_scheduler_ctx_var`. Because ``ContextVar``
    is task-local, the value seen by tool handlers running inside the
    *current* asyncio task isolated from any sibling fire/turn that may
    be running concurrently. Pass an empty dict to clear.
    """
    _scheduler_ctx_var.set(ctx or {})


def get_ctx() -> dict:
    """Return the current task's scheduler context (B2 ContextVar getter).

    Defaults to ``{}`` so callers can dispatch on truthiness. Reads from
    :data:`_scheduler_ctx_var` only — :func:`_resolve_ctx` handles the
    fallback to the legacy ``_GLOBAL_CTX`` provider.
    """
    return _scheduler_ctx_var.get() or {}


def set_scheduler_manager(
    mgr: SchedulerManager | None,
    *,
    ctx_provider: Callable[[], dict] | None = None,
) -> None:
    """Wire (or clear) the manager + per-turn context provider.

    ``ctx_provider()`` should return a dict with ``thread_id`` /
    ``channel_id`` / ``guild_id`` / ``tz_name`` keys describing the
    *current* claude turn. The bot sets this immediately BEFORE invoking
    the bridge so the tool handlers know which thread/channel the turn
    originated from. Pass ``mgr=None`` (typically in test teardown) to
    detach.
    """
    global _GLOBAL_MGR, _GLOBAL_CTX
    _GLOBAL_MGR = mgr
    _GLOBAL_CTX = ctx_provider


def _require_mgr() -> SchedulerManager | None:
    """Return the wired manager, or ``None`` if not set yet."""
    return _GLOBAL_MGR


def _resolve_ctx() -> dict:
    """Return the active scheduler context for the current tool call.

    B2: the ``ContextVar`` is consulted first — a non-empty value there
    means a recent ``set_ctx`` in this task already provided the context.
    If empty, fall back to the legacy ``_GLOBAL_CTX`` callable wired via
    :func:`set_scheduler_manager` (kept for compatibility with code paths
    or tests that haven't migrated to ``set_ctx``). Returns ``{}`` on
    missing/raising provider.
    """
    cv_ctx = _scheduler_ctx_var.get() or {}
    if cv_ctx:
        return cv_ctx
    if _GLOBAL_CTX is None:
        return {}
    try:
        return _GLOBAL_CTX() or {}
    except Exception:
        log.exception("#241 ctx_provider raised")
        return {}


def _err(text: str) -> dict:
    """Standard error-result shape for SDK MCP tool handlers."""
    return {"is_error": True, "content": [{"type": "text", "text": text}]}


def _ok(text: str) -> dict:
    """Standard ok-result shape for SDK MCP tool handlers."""
    return {"content": [{"type": "text", "text": text}]}


# ----------------------------------------------------- schedule_message

@tool(
    "schedule_message",
    "Create a recurring or one-shot timer that injects a user message "
    "into a Discord thread's existing session when it fires. "
    "Use this when the user wants a reminder/message dropped into an "
    "ongoing conversation at a scheduled time. "
    "Required: when (str, 'cron: ...' or 'iso: ...'), what (str, ≤500). "
    "Optional: target_thread_id, name (≤50), recurring (bool), "
    "max_lifetime (e.g. '30d', ≤365d, only valid with recurring=true).",
    {
        "when": str,
        "what": str,
        "target_thread_id": str,
        "name": str,
        "recurring": bool,
        "max_lifetime": str,
    },
)
async def schedule_message_tool(args: dict) -> dict:
    """Create a ``kind=message`` schedule from claude tool args."""
    mgr = _require_mgr()
    if mgr is None:
        return _err("scheduler not initialized")
    ctx = _resolve_ctx()

    # target thread: explicit arg wins; else current turn's thread
    tid_arg = args.get("target_thread_id")
    if not tid_arg and not ctx.get("thread_id"):
        return _err(
            "no target thread (turn context missing and no "
            "target_thread_id arg)"
        )
    try:
        target_thread_id = int(tid_arg) if tid_arg else int(ctx["thread_id"])
    except (TypeError, ValueError):
        return _err(f"invalid target_thread_id: {tid_arg!r}")

    when = args.get("when")
    what = args.get("what")
    if not isinstance(when, str):
        return _err("when is required (str: 'cron: ...' or 'iso: ...')")
    if not isinstance(what, str):
        return _err("what is required (str ≤500)")

    try:
        sched = mgr.create(
            kind="message",
            when=when,
            what=what,
            target_thread_id=target_thread_id,
            name=args.get("name"),
            recurring=bool(args.get("recurring", False)),
            max_lifetime=args.get("max_lifetime"),
            created_by="claude",
            is_claude_created=True,
            guild_id=ctx.get("guild_id"),
            channel_id=ctx.get("channel_id"),
            tz_name=ctx.get("tz_name", "Asia/Shanghai"),
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        log.exception("#241 schedule_message_tool failed")
        return _err(f"internal error: {exc}")

    return _ok(
        f"Created schedule {sched['schedule_id']} "
        f"(kind=message, next_fire_at={sched['state']['next_fire_at']})"
    )


# ---------------------------------------------------- schedule_new_task

@tool(
    "schedule_new_task",
    "Create a recurring or one-shot timer that spawns a NEW Discord "
    "thread + fresh claude session at fire time, with `what` as the "
    "first user prompt. Use for independent scheduled tasks (vs "
    "schedule_message which injects into an existing conversation). "
    "Required: when, what. Optional: target_channel_id, thread_name "
    "(≤50, for the new thread), name (schedule label), recurring, "
    "max_lifetime.",
    {
        "when": str,
        "what": str,
        "target_channel_id": str,
        "thread_name": str,
        "name": str,
        "recurring": bool,
        "max_lifetime": str,
    },
)
async def schedule_new_task_tool(args: dict) -> dict:
    """Create a ``kind=new_task`` schedule from claude tool args."""
    mgr = _require_mgr()
    if mgr is None:
        return _err("scheduler not initialized")
    ctx = _resolve_ctx()

    # target channel: explicit arg wins; else current turn's channel
    cid_arg = args.get("target_channel_id")
    try:
        if cid_arg:
            target_channel_id = int(cid_arg)
        elif ctx.get("channel_id"):
            target_channel_id = int(ctx["channel_id"])
        else:
            return _err(
                "no target channel (turn context missing and no "
                "target_channel_id arg)"
            )
    except (TypeError, ValueError):
        return _err(f"invalid target_channel_id: {cid_arg!r}")

    when = args.get("when")
    what = args.get("what")
    if not isinstance(when, str):
        return _err("when is required (str: 'cron: ...' or 'iso: ...')")
    if not isinstance(what, str):
        return _err("what is required (str ≤500)")

    try:
        sched = mgr.create(
            kind="new_task",
            when=when,
            what=what,
            target_channel_id=target_channel_id,
            thread_name=args.get("thread_name"),
            name=args.get("name"),
            recurring=bool(args.get("recurring", False)),
            max_lifetime=args.get("max_lifetime"),
            created_by="claude",
            is_claude_created=True,
            guild_id=ctx.get("guild_id"),
            channel_id=ctx.get("channel_id"),
            tz_name=ctx.get("tz_name", "Asia/Shanghai"),
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        log.exception("#241 schedule_new_task_tool failed")
        return _err(f"internal error: {exc}")

    return _ok(
        f"Created schedule {sched['schedule_id']} "
        f"(kind=new_task, next_fire_at={sched['state']['next_fire_at']})"
    )


# -------------------------------------------------------- schedule_list

@tool(
    "schedule_list",
    "List schedules. Scope: 'thread' (default, current thread, "
    "kind=message only) | 'channel' (current channel: message+new_task) "
    "| 'all' (everywhere). Optional include_disabled (default false).",
    {
        "scope": str,
        "include_disabled": bool,
    },
)
async def schedule_list_tool(args: dict) -> dict:
    """List schedules in the requested scope with kind / creator markers."""
    mgr = _require_mgr()
    if mgr is None:
        return _err("scheduler not initialized")
    ctx = _resolve_ctx()
    scope = args.get("scope", "thread")
    include_disabled = bool(args.get("include_disabled", False))

    if scope == "thread":
        tid = ctx.get("thread_id")
        if not tid:
            return _err("scope=thread requires turn context")
        try:
            items = mgr.store.list_for_thread(int(tid))
        except (TypeError, ValueError):
            return _err(f"invalid ctx thread_id: {tid!r}")
    elif scope == "channel":
        cid = ctx.get("channel_id")
        if not cid:
            return _err("scope=channel requires turn context")
        try:
            items = mgr.store.list_for_channel(int(cid))
        except (TypeError, ValueError):
            return _err(f"invalid ctx channel_id: {cid!r}")
    elif scope == "all":
        items = list(mgr.store.list_all().values())
    else:
        return _err(f"invalid scope: {scope!r}")

    if not include_disabled:
        items = [s for s in items if s.get("state", {}).get("enabled", False)]

    if not items:
        return _ok("(no schedules)")

    lines: list[str] = []
    for s in items:
        kind = s.get("kind", "?")
        kind_marker = "📨" if kind == "message" else "🧵"
        cb = s.get("created_by", "")
        cb_marker = "🤖" if cb == "claude" else "👤"
        name = s.get("name", "") or ""
        sid = (s.get("schedule_id", "") or "")[:8]
        what_preview = (s.get("payload", {}).get("what", "") or "")[:50]
        state = s.get("state", {}) or {}
        next_at = state.get("next_fire_at", "")
        fire_count = state.get("fire_count", 0)
        enabled = state.get("enabled", False)
        enabled_marker = "" if enabled else " (disabled)"
        # M10: include missed_count + last_fired_at + max_lifetime so
        # claude can reason about whether a recurring schedule has been
        # firing reliably or has accumulated missed_fires (catch_up
        # rollforwards) since last user interaction. The MCP tool surface
        # has no embed-size budget like /schedule list, so we always emit.
        missed = state.get("missed_count", 0) or 0
        last_fired = state.get("last_fired_at") or "—"
        max_life = s.get("max_lifetime_seconds")
        max_life_str = f"{max_life}s" if max_life else "—"
        lines.append(
            f"{kind_marker} {cb_marker} `{sid}` {name} — next={next_at} "
            f"fires={fire_count} missed={missed} last={last_fired} "
            f"max_lifetime={max_life_str}{enabled_marker}\n"
            f"   what: {what_preview!r}"
        )

    return _ok("\n".join(lines))


# ------------------------------------------------------ schedule_delete

@tool(
    "schedule_delete",
    "Delete a schedule by id (16-char hex). Claude can only delete "
    "claude-created schedules.",
    {"schedule_id": str},
)
async def schedule_delete_tool(args: dict) -> dict:
    """Delete a schedule (claude scope only)."""
    mgr = _require_mgr()
    if mgr is None:
        return _err("scheduler not initialized")
    sid = args.get("schedule_id")
    if not sid:
        return _err("schedule_id required")
    ok, reason = mgr.delete(sid, requester="claude", is_admin=False)
    return _ok(f"Deleted {sid}") if ok else _err(reason)


# ------------------------------------------------------ schedule_toggle

@tool(
    "schedule_toggle",
    "Enable or disable a schedule. Claude can only toggle "
    "claude-created schedules.",
    {"schedule_id": str, "enabled": bool},
)
async def schedule_toggle_tool(args: dict) -> dict:
    """Enable / disable a schedule (claude scope only)."""
    mgr = _require_mgr()
    if mgr is None:
        return _err("scheduler not initialized")
    sid = args.get("schedule_id")
    if not sid:
        return _err("schedule_id required")
    enabled = bool(args.get("enabled", True))
    ok, reason = mgr.toggle(sid, enabled, requester="claude", is_admin=False)
    if not ok:
        return _err(reason)
    return _ok(f"Set {sid} enabled={enabled}")


# ------------------------------------------------------------ Builder

def build_scheduler_mcp_server():
    """Build the in-process MCP server.

    Returns an ``McpSdkServerConfig`` dict (``{"type":"sdk","name":...,
    "instance":<server>}``) suitable for direct insertion into
    ``ClaudeAgentOptions.mcp_servers`` (Subtask 4 wires this into
    :meth:`ClaudeBridge.start`).
    """
    return create_sdk_mcp_server(
        name="clauded-scheduler",
        version="1.0.0",
        tools=[
            schedule_message_tool,
            schedule_new_task_tool,
            schedule_list_tool,
            schedule_delete_tool,
            schedule_toggle_tool,
        ],
    )
