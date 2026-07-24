"""Discord bot entrypoint for claudeD.

Wires up event handlers (on_ready, on_message) and registers slash command
groups (`/project`, `/session`). The on_message handler bridges Discord
messages to a per-thread :class:`ClaudeBridge` session.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import tempfile
import time
import traceback
import sys
import asyncio
import contextvars
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config, load_config
from .discord_renderer import DiscordRenderer, COLOR_INFO, COLOR_TOOL_FAILURE
from .interaction_handler import InteractionHandler
from .project_manager import ProjectManager
from .session_manager import SessionManager
from .session_config import SessionConfig
from .session_store import SessionStore
from .scheduler_store import SchedulerStore
from .scheduler import SchedulerManager
from . import scheduler_mcp
from .scheduler_mcp import set_ctx as _scheduler_set_ctx, get_ctx as _scheduler_get_ctx
from .cost_tracker import CostTracker
from .agent_manager import AgentManager
from . import _cli_native
from . import stream_logger
from .cogs._unbound import UNBOUND_HINT_MESSAGE, UNBOUND_REFUSE_MESSAGE
from .cogs._table_view import CopyTableTextView
from ._errors import is_transient_discord_error
from ._http_retry import (
    safe_send_message,
    safe_remove_reaction,
    safe_add_reaction,
)
from ._logging_setup import (
    _LOG_DIR,
    _CACHE_DIR,
    _ensure_runtime_dirs,
    _configure_logging,
)

# Re-export SystemPromptModal so existing ``from clauded.bot import
# SystemPromptModal`` continues to work (tests rely on this).
from .cogs.project import SystemPromptModal  # noqa: F401

log = logging.getLogger("clauded.bot")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# #242 (round 2): Anthropic vision API supports only these media types
# as inline image content blocks. Other image-ish extensions (.bmp, .svg)
# stay on the path-in-text fallback so claude can still try Read tool.
_VISION_INLINE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Map extension -> Anthropic media_type string.
_VISION_MEDIA_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# --------------------------------------------------------------------------
# #241 — module-level ContextVar for the per-turn scheduler context.
#
# Architect R1 + Engineer R1 (BLOCKER #2): per-turn ctx (thread_id /
# channel_id / guild_id / tz_name) used to live on ``ClaudedBot`` as a
# single mutable instance attribute. Every concurrent claude turn — be it
# two ``@bot`` messages in different threads or two scheduled-fire
# callbacks aimed at different targets — overwrites the same dict between
# its register-ctx and its ``renderer.render_response`` await. When the
# in-process scheduler MCP tools resolved "current thread" they read
# whatever fire/turn last touched the attribute, occasionally pointing at
# a sibling turn's thread. Fix: put ctx in a ``contextvars.ContextVar``;
# asyncio copies the running context per ``create_task``, so each task
# has its own snapshot for free.
# --------------------------------------------------------------------------
_scheduler_ctx_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "scheduler_ctx", default={}
)


# --------------------------------------------------------------------------
# #227 helper: build the SessionConfig for the renderer-crash Retry button.
#
# Extracted to module scope so unit tests can drive it directly and assert
# that ``resume_session_id`` flows from the persisted ``stored.session_id``
# into the new SessionConfig — the entire behavioural value of the #227
# fix is this one kwarg propagating end-to-end.
# --------------------------------------------------------------------------
def _build_retry_session_config(
    stored,  # SessionStoreEntry or None  — result of get_stored_session
    base_sc,  # original SessionConfig (a.k.a. _retry_sc in the closure)
    on_ask_user,  # fresh InteractionHandler bound callback
    *,
    thread_id_for_log=None,  # used only for log.warning context
):
    """Build a SessionConfig for the Retry-button crash-recovery path.

    The crucial bit (#227): ``resume_session_id`` is read from ``stored`` so
    the new SDK process resumes the SAME conversation. Without this, retry
    cold-starts and the user loses every prior turn.

    Falls back to cold start (``resume_session_id=None``) when no stored
    entry exists — happens if the renderer crashed before the first
    ResultMessage persisted the session_id, or if the user ran
    ``/session clear`` mid-turn. A WARNING is logged either way so #224's
    ``/log dump`` epic can pick up the diagnostic trail.
    """
    resume_id = stored.get("session_id") if stored else None
    if resume_id is None:
        log.warning(
            "#227: retry has no stored session_id "
            "(crashed pre-ResultMessage or /session clear?); "
            "falling back to cold start for thread=%s",
            thread_id_for_log,
        )

    if base_sc is None:
        return SessionConfig(
            resume_session_id=resume_id,
            on_ask_user=on_ask_user,
        )

    return SessionConfig(
        system_prompt=base_sc.system_prompt,
        model_override=base_sc.model_override,
        permission_mode_override=base_sc.permission_mode_override,
        resume_session_id=resume_id,
        effort=base_sc.effort,
        allowed_tools=list(base_sc.allowed_tools) if base_sc.allowed_tools else [],
        disallowed_tools=list(base_sc.disallowed_tools) if base_sc.disallowed_tools else [],
        max_budget_usd=base_sc.max_budget_usd,
        fork_session=base_sc.fork_session,
        add_dirs=base_sc.add_dirs,
        from_pr=base_sc.from_pr,
        worktree=base_sc.worktree,
        agent_name=base_sc.agent_name,
        custom_agents=base_sc.custom_agents,
        mcp_servers=base_sc.mcp_servers,
        max_turns=base_sc.max_turns,
        fallback_model=base_sc.fallback_model,
        plugin_dirs=list(base_sc.plugin_dirs) if base_sc.plugin_dirs else None,
        settings=base_sc.settings,
        env=base_sc.env,
        user=base_sc.user,
        bare=base_sc.bare,
        session_name=base_sc.session_name,
        on_ask_user=on_ask_user,
        on_pre_tool_use=base_sc.on_pre_tool_use,
        on_post_tool_use=base_sc.on_post_tool_use,
        on_stop=base_sc.on_stop,
    )


# --------------------------------------------------------------------------
# macOS LaunchAgent paths/labels (v1.15).
#
# Mirror of the string literals used by scripts/install-launchagent.sh,
# scripts/health-check.sh, scripts/com.hxy.clauded.plist.template and
# README §Status & logs. Bash scripts keep their own literals (cross-language
# sourcing is overkill for v1.15); when changing any of these, grep the
# repo for the matching string in those files.
# --------------------------------------------------------------------------
_LAUNCHD_LABEL = "com.hxy.clauded"

# #224 R1 simplicity: cooldown between consecutive auto-crash bundle
# dispatches on the SAME thread. 5 minutes is the sweet spot: rare
# enough that a thrashing bug doesn't spam attachments, short enough
# that a 1-hour-later second crash gets its own bundle.
AUTO_CRASH_COOLDOWN_S = 300
# _LOG_DIR and _CACHE_DIR live in _logging_setup.py (v1.18 stage-28 trim);
# imported above. _HEARTBEAT_PATH stays here because it belongs to the
# heartbeat task, not to logging setup.
_HEARTBEAT_PATH = _CACHE_DIR / "heartbeat"


def _gateway_budget_secs() -> float:
    """#audit(#9): how long the gateway may stay CONTINUOUSLY down before the
    heartbeat is allowed to go stale so the external watchdog restarts us.

    Must exceed discord.py's ExponentialBackoff single-sleep for common
    reconnect storms (observed up to ~384s; theoretical 2^10=1024s) so brief
    storms that self-heal are NOT killed, while a permanently-wedged reconnect
    IS. 600s is a conservative middle; overridable for tuning/tests.
    """
    raw = os.environ.get("CLAUDED_GATEWAY_BUDGET_SECS", "600")
    try:
        v = float(raw)
        return v if v > 0 else 600.0
    except ValueError:
        return 600.0


def _gateway_hard_ceiling_secs() -> float:
    """#audit(#9) fail-safe: the ABSOLUTE cap on how long the wedge-restart may
    be DEFERRED by in-flight work.

    Past ``_gateway_budget_secs()`` we normally keep writing the heartbeat while
    a turn OR a background subagent is still running (so a reconnect wedge does
    not kill work that runs in the CLI child and can still post via REST). But a
    dropped SubagentStop can orphan the in-flight counter, and a genuinely dead
    gateway makes the bot useless — so once we've been off-gateway longer than
    THIS ceiling we freeze regardless of in-flight count. Must be >= budget;
    default 1800s (= 3x the 600s budget, well past the observed ~384s backoff).
    Overridable for tuning/tests.
    """
    raw = os.environ.get("CLAUDED_GATEWAY_HARD_CEILING_SECS", "1800")
    try:
        v = float(raw)
        return v if v > 0 else 1800.0
    except ValueError:
        return 1800.0


def _touch_heartbeat() -> None:
    """Refresh ``_HEARTBEAT_PATH`` mtime so the external healthcheck sees us alive.

    Called once from ``main()`` at process start (so login failures don't strand
    the healthcheck with a stale file) and again every 30 s from
    :meth:`ClaudedBot._heartbeat_task` (so a wedged event loop is detected too).
    macOS-only by design — Linux/Windows dev boxes are no-ops to avoid littering
    ``~/Library/`` outside Darwin. ``OSError`` is swallowed so a read-only home
    or unusual test sandbox never crashes startup.

    Directory creation lives in :func:`_ensure_runtime_dirs` (called once at
    startup); this function does just the ``touch``.
    """
    if sys.platform != "darwin":
        return
    try:
        _HEARTBEAT_PATH.touch()
    except OSError as exc:
        # #223 PR-B: silent OSError here means LaunchAgent health checks
        # silently fail (`heartbeat` file stops being touched — external
        # watchdog assumes bot is dead, restarts, no log of why). WARNING.
        log.warning(
            "_HEARTBEAT_PATH.touch() failed; LaunchAgent health may be"
            " misled: %s",
            exc,
        )


def _write_heartbeat_state(active_turns: int) -> None:
    """Write the in-flight-turn count to the heartbeat file (also refreshes mtime).

    T2-D: the external watchdog reads this value to grant a longer grace window
    while a turn is active, so a transient event-loop stall UNDER a turn (e.g.
    memory-pressure thrash) doesn't hard-kill the bot mid-turn — which would
    drop the session_id before it persisted and force a cold resume on the next
    message (the T2 resume bug). Darwin-only; ``OSError`` swallowed like
    :func:`_touch_heartbeat`. Content is a plain integer so the shell watchdog
    can parse it; writing also refreshes mtime, preserving the legacy
    liveness-by-mtime check.
    """
    if sys.platform != "darwin":
        return
    try:
        _HEARTBEAT_PATH.write_text(str(int(active_turns)))
    except OSError as exc:
        log.warning(
            "_write_heartbeat_state failed; LaunchAgent health may be misled: %s",
            exc,
        )


def _cleanup_tmp_dir(tmp_dir: Path | None) -> None:
    """Best-effort cleanup of an attachment temp directory.

    Called after the renderer finishes (success or failure) to avoid
    leaking on-disk attachments for the lifetime of the process. We
    swallow ``OSError`` because the worst case is a stale temp dir that
    the OS will eventually clean up.
    """
    if tmp_dir is None:
        return
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:  # pragma: no cover - rmtree(ignore_errors=True) shouldn't raise
        log.debug("Failed to clean up attachment tempdir %s", tmp_dir)


def _build_intents() -> discord.Intents:
    """Intents needed: messages + content for bridging, guilds for commands."""
    intents = discord.Intents.default()
    intents.message_content = True  # Requires Portal toggle; bot degrades if unavailable
    intents.messages = True
    intents.guilds = True
    return intents


def _build_intents_safe() -> discord.Intents:
    """Fallback intents without privileged message_content."""
    intents = discord.Intents.default()
    intents.messages = True
    intents.guilds = True
    return intents


# Generic, path-free error: never interpolate ``project_path_obj`` (which
# is the operator's home dir) into a message Discord users will see.
_BROKEN_HOME_MESSAGE = (
    "❌ Couldn't determine your home directory. "
    "Run `/project bind <path>` to use this bot."
)


async def _resolve_path_or_friendly_error(
    project_manager: ProjectManager,
    channel_id: int,
    reply: "callable",
) -> tuple[Path, bool] | None:
    """Resolve ``(path, is_bound)`` for a channel, replying on broken-home.

    Returns the tuple on success. On a broken ``$HOME`` (the only case the
    fallback can fail), sends a generic error via ``reply``, logs detail
    at warning, and returns ``None``. Bound paths are validated at bind
    time so are assumed OK.
    """
    project_path_obj, is_bound = project_manager.get_path_or_default(channel_id)
    if is_bound:
        return project_path_obj, True
    # ``Path.home()``-raise is already caught inside ``get_path_or_default``;
    # what's left is the "set but doesn't exist" case, which ``is_dir()``
    # reports as False (not a raise). One simple guard suffices.
    if project_path_obj.is_dir():
        return project_path_obj, False
    log.warning("Unbound fallback failed: %s is not a directory", project_path_obj)
    try:
        await reply(_BROKEN_HOME_MESSAGE)
    except discord.HTTPException:
        log.debug("Could not surface broken-home error")
    return None


class _MagicResponse:
    """Minimal ``aiohttp.ClientResponse`` stand-in for :class:`discord.NotFound`.

    #241: when a scheduled fire targets a thread/channel that's no longer a
    Thread/TextChannel (renamed, type-changed) we want to raise
    ``discord.NotFound`` so :class:`SchedulerManager._fire_with_retry`
    classifies it as terminal and disables the schedule. ``discord.NotFound``
    requires a response object with a ``status`` attr to construct; this is
    the cheapest stub that satisfies that constraint without depending on
    aiohttp at the call site.
    """

    def __init__(self, status: int) -> None:
        self.status = status
        # ``reason`` is occasionally formatted into discord.py's NotFound
        # repr; provide a non-None default so logs stay readable.
        self.reason = "scheduler-fire target invalid"


class ClaudedBot(commands.Bot):
    """Discord bot for the claudeD bridge."""

    def __init__(self, config: Config) -> None:
        super().__init__(command_prefix="!", intents=_build_intents())
        self.config = config
        self.session_manager = SessionManager(session_store=SessionStore())
        self.project_manager = ProjectManager(projects_root=config.projects_root)
        self._start_time = time.time()
        self.cost_tracker = CostTracker()
        self.agent_manager = AgentManager()
        # #294: one-shot migration from shadow config to CLI-native
        # ``.claude/agents/`` + ``.mcp.json``. Runs after both managers
        # have loaded their JSON so we have the full legacy view. Idempotent
        # (skips agents/servers whose file/entry already exists) so process
        # restarts are safe.
        self._migrate_shadow_to_cli_native()
        self._claude_version: str = "unknown"
        self._debug_logging: bool = False
        # v1.18 #160: runtime-toggleable allow_unbound_fallback. Initialized
        # from env-derived ``self.config.allow_unbound_fallback`` and
        # mutated by the ``/unbound-fallback`` admin slash command. Plain
        # mutable bool matches the sibling ``_debug_logging`` /
        # ``_pre_tool_notifications`` pattern in this same class; bot
        # restart re-reads env-default so the flag fails-closed unless
        # ``CLAUDED_ALLOW_UNBOUND_FALLBACK=1`` is set in the bot env.
        self.allow_unbound_fallback: bool = config.allow_unbound_fallback
        self._pre_tool_notifications: bool = False
        self._notify_enabled: dict[int, bool] = {}
        # #185: per-guild slash-sync idempotency guard. on_ready can fire
        # multiple times per process (reconnects); we sync each guild once.
        self._slash_synced: set[int] = set()
        # #195: per-thread "ignored-once" guard so we don't spam logs every
        # message in a 3rd-party thread. Set of thread_ids we've already
        # logged the ownership-skip for; cleared on bot restart.
        self._logged_third_party_thread: set[int] = set()
        # #292 S3: bot-level workflow task registry. DiscordRenderer writes
        # task lifecycle states here (via self._bot ref); /workflow cog reads.
        self._workflow_tasks: dict = {}
        # #audit(#8): strong references to fire-and-forget background tasks
        # (e.g. the resume-failed Discord notice). CPython keeps only a weak
        # ref to a bare create_task result, so a task that suspends on its
        # first await can be GC'd mid-flight and silently dropped. Each task
        # discards itself here on completion via add_done_callback.
        self._bg_tasks: set = set()
        # review A4/A5: per-subagent pending tracking. thread_id →
        # {agent_id: agent_type}. Populated by the SubagentStart hook
        # (_make_subagent_start_cb) and drained per-agent_id by SubagentStop.
        # This REPLACES the old #310 ``_subagent_threads`` (session_id →
        # thread_id) map, which had exactly one entry per session and so
        # both (a) false-warned "1 subagent still running" on every normal
        # turn and (b) got del'd after the FIRST subagent stopped, destroying
        # the count for later parallel subagents. Routing no longer needs a
        # map at all — the completion/start callbacks are built per-thread via
        # _make_subagent_{start,stop}_cb(thread_id), so the closure already
        # captures the correct thread_id.
        #
        # Robustness: if the SubagentStart hook does NOT fire in some CLI
        # versions, this dict simply stays empty → pending count 0 → no
        # warning at all (graceful degradation; still strictly better than
        # the old guaranteed false positive).
        self._pending_subagents: dict[int, dict[str, str]] = {}
        # #audit(#9): live count of in-flight BACKGROUND subagents across all
        # threads. Separate from _pending_subagents (a per-thread notification
        # dict) on purpose: this is a single self-cleaning integer the heartbeat
        # gate reads so a reconnect-wedge restart is DEFERRED while background
        # work is running (the CLI child keeps working + can still post via
        # REST). Kept in lockstep with _pending_subagents inc/dec + the same
        # reaper / fresh-bridge resets, so a dropped SubagentStop self-heals on
        # the next idle-reap instead of latching the watchdog off forever. A
        # hard ceiling (_gateway_hard_ceiling_secs) is the ultimate backstop.
        self._inflight_bg: int = 0
        # T2-D: number of turns currently rendering. Written into the heartbeat
        # file so the external watchdog grants a longer grace window while a
        # turn is in flight (see _write_heartbeat_state + health-check.sh).
        self._active_turns: int = 0
        # #audit(#20): gateway lifecycle observability. discord.py's internal
        # reconnect loop is otherwise entirely silent; these counters +
        # timestamps (updated by on_disconnect / on_resumed) let /health show
        # whether the bot is flapping vs steady and give the logs a breadcrumb.
        self._gw_disconnects: int = 0
        self._gw_resumes: int = 0
        self._gw_last_disconnect_at: float | None = None
        self._gw_last_resumed_at: float | None = None
        # #audit(#9): wall time of the FIRST disconnect not yet followed by a
        # resume/ready; None means "gateway believed up". _heartbeat_task stops
        # refreshing the heartbeat once we've been off-gateway longer than
        # _gateway_budget_secs() so a reconnect-storm wedge (a false-alive to
        # the event-loop-only heartbeat) finally gets restarted.
        self._gw_down_since: float | None = None
        # T1-B: live per-agent roster for the workflow/subagent UX, keyed by
        # thread_id → agent_id → {type, tool, started}. Fed by the
        # SubagentStart / PreToolUse / SubagentStop hooks (all of which fire
        # reliably), so the workflow progress embed can show real per-agent
        # status even though the CLI's ``workflowProgress`` payload never
        # arrives in practice (investigation: 0 occurrences in any log). The
        # renderer reads this via ``bot._agent_roster``.
        self._agent_roster: dict[int, dict[str, dict]] = {}
        # #324: post-turn background stream reader. After a turn ends, the CLI
        # keeps the stream open and PROACTIVELY pushes background-task
        # completions (a standalone <task-notification> turn) + the main agent's
        # continuation. receive_response() stopped at the turn's ResultMessage,
        # so without this reader those never reach Discord. The reader task is
        # OWNED BY EACH BRIDGE (``bridge._bg_reader_task``) and cancelled inside
        # ``bridge.send_message`` / ``bridge.stop`` — so every stream-consuming
        # path enforces the single-consumer invariant, not just the turn loop
        # (#324 review fix). _start_bg_reader/_bg_reader_loop live on the bot
        # only because relaying needs Discord + the renderer.

        # ---------------------------------------------------------------- #241
        # Scheduler core (PRD v1.18 §3.7 / §8 Subtask 4): persistent timer
        # store + manager wired with 3 callbacks (fire-message, fire-new-task,
        # expire-notify). Per-target lock provider is the existing session
        # manager's per-thread lock so fire executions serialize against
        # human turns on the same thread.
        # ----------------------------------------------------------------------
        self.scheduler_store = SchedulerStore()  # default: data/schedules.json
        self.scheduler = SchedulerManager(
            self.scheduler_store,
            fire_message_callback=self._fire_schedule_message,
            fire_new_task_callback=self._fire_schedule_new_task,
            expire_notify_callback=self._notify_schedule_expired,
            get_lock=self.session_manager.get_lock,
            # M4: surface "channel not bound" at create() time rather than
            # 30 days later at fire time. The manager calls this for every
            # kind=new_task create; unbound channels are rejected before
            # they hit the store.
            bound_checker=self.project_manager.is_bound,
        )
        # Per-turn context, set by `_register_scheduler_ctx` immediately
        # before every claude turn so the MCP tool handlers can resolve
        # "current thread" / "current channel" defaults.
        self._scheduler_current_ctx: dict = {}
        scheduler_mcp.set_scheduler_manager(
            self.scheduler,
            ctx_provider=self._scheduler_ctx_provider,
        )
        # `catch_up()` runs once on the first `_scheduler_tick` after the
        # bot is ready so missed-fire bookkeeping happens with a live event
        # loop (cannot be done from __init__).
        self._scheduler_catch_up_done = False

    # ------------------------------------------------------------------ #294
    # One-shot legacy → CLI-native migration
    # ------------------------------------------------------------------
    def _migrate_shadow_to_cli_native(self) -> None:
        """Migrate legacy shadow config to CLI-native storage (#294).

        Two shadow stores need porting to the Claude CLI's own file
        layout so ``claude`` picks up the same agents / MCP servers that
        our slash commands manage:

        * ``data/agents.json`` (per-bot registry) → one
          ``{project_path}/.claude/agents/<name>.md`` per bound project.
          Every bound project gets a copy so channels sharing an agent
          continue to work when the CLI is invoked outside our bridge.
        * ``data/projects.json``'s ``mcp_servers`` field (per-channel) →
          one ``{project_path}/.mcp.json`` merging every channel's
          servers for that path.

        Idempotent: skips agents whose ``.md`` file already exists and
        MCP servers already present in ``.mcp.json`` (so a hand-edited
        file wins). All exceptions are swallowed with a warning so a
        broken filesystem can never brick bot startup — the shadow
        stores stay authoritative until the file write succeeds on a
        later boot.
        """
        # --- agents.json → .claude/agents/*.md ----------------------------
        try:
            legacy_agents = self.agent_manager.list_all()
        except Exception:
            log.exception("#294 migration: could not read agent_manager")
            legacy_agents = {}

        if legacy_agents:
            # Collect every distinct bound project path once so we don't
            # rewrite the same ``.md`` for N channels sharing a path.
            project_paths: set[Path] = set()
            for _cid, path, _mcps in self.project_manager.iter_mcp_bindings():
                project_paths.add(Path(path))
            # ``iter_mcp_bindings`` only returns bindings with mcp_servers;
            # we also want bindings without any MCP config, so pull those
            # from the raw project list.
            try:
                for key, entry in list(self.project_manager._projects.items()):
                    path = entry.get("path")
                    if path:
                        project_paths.add(Path(path))
            except Exception:
                log.exception("#294 migration: could not scan projects for agent copy")

            for project_path in project_paths:
                for name, info_dict in legacy_agents.items():
                    target = _cli_native.agent_md_path(project_path, name)
                    if target.exists():
                        # Idempotent — respect hand edits and rerun-safety.
                        continue
                    try:
                        _cli_native.write_agent_md(
                            project_path,
                            name,
                            info_dict.get("prompt", "") or "",
                            info_dict.get("description", "") or "",
                        )
                        log.info(
                            "#294 migration: wrote agent %r to %s",
                            name,
                            target,
                        )
                    except OSError:
                        log.exception(
                            "#294 migration: agent %r → %s failed", name, target
                        )

        # --- projects.json mcp_servers → .mcp.json ------------------------
        try:
            bindings = self.project_manager.iter_mcp_bindings()
        except Exception:
            log.exception("#294 migration: could not read project_manager")
            bindings = []

        # Multiple channels can share a project path; merge their server
        # dicts into a single ``.mcp.json`` per path. Last channel wins
        # on same-name collisions (rare — the shadow store rejects dupes
        # per-channel but two channels can independently register the
        # same-named server).
        merged_by_path: dict[Path, dict] = {}
        for _cid, path_str, mcps in bindings:
            path = Path(path_str)
            bucket = merged_by_path.setdefault(path, {})
            for sname, sconfig in mcps.items():
                bucket.setdefault(sname, sconfig)

        for project_path, servers in merged_by_path.items():
            for sname, sconfig in servers.items():
                try:
                    _cli_native.add_mcp_server(project_path, sname, sconfig)
                    log.info(
                        "#294 migration: wrote MCP %r → %s/.mcp.json",
                        sname,
                        project_path,
                    )
                except ValueError:
                    # ``.mcp.json`` already lists the server — idempotent skip.
                    log.debug(
                        "#294 migration: MCP %r already present in %s/.mcp.json",
                        sname,
                        project_path,
                    )
                except OSError:
                    log.exception(
                        "#294 migration: MCP %r → %s/.mcp.json failed",
                        sname,
                        project_path,
                    )

    async def setup_hook(self) -> None:
        """Register slash command groups and sync to Discord."""
        # Cache claude version (#86)
        try:
            import shutil
            claude_bin = shutil.which("claude") or "claude"
            proc = await asyncio.create_subprocess_exec(
                claude_bin, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            self._claude_version = stdout.decode().strip() or "unknown"
        except Exception:
            self._claude_version = "unknown"
        self._cleanup_task.start()
        self._heartbeat_task.start()
        # #241: scheduler tick loop. The first iteration also runs
        # `catch_up()` (missed-fire bookkeeping); subsequent iterations
        # just `tick()`. `_before_scheduler_tick` waits for the gateway
        # so we don't try to resolve channels before the cache is warm.
        self._scheduler_tick.start()

        # PRD R3.3 — register the persistent Copy-as-text button view so
        # the button keeps working after a bot restart.
        #
        # Why both registrations:
        # - This ``add_view(CopyTableTextView())`` at startup registers a
        #   global handler for ``custom_id="copy_table_text"`` so clicks
        #   are dispatched even on messages whose original view object is
        #   long gone (post-restart).
        # - ``DiscordRenderer._send_table_renders`` also instantiates a
        #   fresh ``CopyTableTextView()`` on each PNG send, because
        #   discord.py needs a view object on the initial send to inject
        #   the button components into the Discord message.
        self.add_view(CopyTableTextView())

        # ----- Import command objects from cog modules -----
        from .cogs.project import project_group, env_group
        from .cogs.session import session_group
        from .cogs.model import model_group, set_effort, max_turns_cmd, fallback_model_cmd, toggle_bare
        from .cogs.mode import mode_group
        from .cogs.tools import tools_group, budget_group
        from .cogs.agent import agent_group
        from .cogs.mcp import mcp_group
        from .cogs.skill import skill_group
        from .cogs.context import context_cmd
        from .cogs.diff import diff_cmd
        from .cogs.ops import (
            cost_group, health_check, review_pr, plugin_group,
            send_to_claude, pin_message, ratelimit_info,
            debug_toggle, notify_toggle, unbound_fallback_toggle, btw_cmd,
        )
        # #224 epic: /log dump diagnostic bundle (slash + auto-crash).
        from .cogs.log_dump import log_group
        # #241: /schedule group (message/new_task/list/delete/toggle).
        from .cogs.schedule import schedule_group
        # #292 S3: /workflow group (list/kill/detail).
        from .cogs.workflow import workflow_group

        # #audit(live-log): register a tree-wide error handler so an expired
        # interaction (10062) or any command raise is handled gracefully
        # instead of escalating to a CommandInvokeError traceback in the logs.
        self.tree.error(self._on_app_command_error)
        self.tree.add_command(project_group)
        self.tree.add_command(session_group)
        self.tree.add_command(cost_group)
        self.tree.add_command(model_group)
        self.tree.add_command(mode_group)
        self.tree.add_command(set_effort)
        self.tree.add_command(tools_group)
        self.tree.add_command(budget_group)
        self.tree.add_command(health_check)
        self.tree.add_command(review_pr)
        self.tree.add_command(agent_group)
        self.tree.add_command(mcp_group)
        self.tree.add_command(skill_group)
        self.tree.add_command(max_turns_cmd)
        self.tree.add_command(fallback_model_cmd)
        self.tree.add_command(plugin_group)
        self.tree.add_command(send_to_claude)
        self.tree.add_command(env_group)
        self.tree.add_command(pin_message)
        self.tree.add_command(ratelimit_info)
        self.tree.add_command(toggle_bare)
        self.tree.add_command(debug_toggle)
        self.tree.add_command(notify_toggle)
        self.tree.add_command(unbound_fallback_toggle)
        self.tree.add_command(btw_cmd)
        self.tree.add_command(context_cmd)
        self.tree.add_command(diff_cmd)
        # #224: /log dump
        self.tree.add_command(log_group)
        # #241: /schedule group (message/new_task/list/delete/toggle)
        self.tree.add_command(schedule_group)
        # #292 S3: /workflow group (list/kill/detail)
        self.tree.add_command(workflow_group)
        # #185: do NOT sync globally here — historically claudeD synced
        # via both ``tree.sync()`` (global) AND PR-specific per-guild
        # PUT calls, so commands ended up registered in BOTH scopes and
        # showed twice in Discord's autocomplete. Per-guild sync is
        # instant (vs. global's ~1h propagation) which is what a self-
        # hosted bot wants. We sync per-guild from ``on_ready`` instead,
        # where ``self.guilds`` is populated. Use guarded idempotency
        # (``self._slash_synced`` initialized in __init__) so reconnect-
        # driven re-fires don't repeatedly re-sync.
        log.info("Slash commands registered in tree; per-guild sync deferred to on_ready")

    @tasks.loop(minutes=5)
    async def _cleanup_task(self) -> None:
        """Clean up sessions idle for > 1 hour."""
        timeout = int(os.environ.get("CLAUDED_SESSION_TIMEOUT", "3600"))
        now = time.time()
        to_remove = []
        for thread_id, bridge in list(self.session_manager.list_sessions().items()):
            last = getattr(bridge, '_last_activity', getattr(bridge, '_start_time', now))
            if now - last > timeout:
                to_remove.append(thread_id)

        async def _stop_one(tid: int) -> None:
            # review B2: never disconnect a bridge while a turn is in flight. A
            # long max-effort turn doesn't refresh ``_last_activity`` mid-run,
            # so it can look "idle" here; tearing down its client mid-stream
            # races the renderer's ``receive_response()``. If the per-thread
            # lock is held a turn is running → the session isn't really idle,
            # so skip this cycle. Otherwise hold the lock across the stop so a
            # turn can't start underneath us.
            lock = self.session_manager.get_lock(tid)
            if lock.locked():
                log.debug("Auto-expire skipped (turn in flight) thread %s", tid)
                return
            # #323 (review fix): the earlier "veto reap if _pending_subagents /
            # _agent_roster non-empty" guard was REMOVED — it leaked. A session
            # only reaches this reaper after ``timeout`` seconds of ZERO stream
            # activity, and _last_activity is now refreshed on every streamed /
            # drained message (claude_bridge #323), so an actively-working
            # background task stays fresh and never lands here. Any roster /
            # pending entry on a session that IS this idle is therefore a stale
            # orphan (e.g. a dropped SubagentStop) — vetoing on it would block
            # cleanup forever (bridge + CLI subprocess leak). So we reap, and
            # clear the stale bookkeeping below.
            async with lock:
                try:
                    # #324 (review fix): the background reader is cancelled inside
                    # bridge.stop() (invoked by stop_session below), so no explicit
                    # cancel is needed here.
                    # #audit(#9): this is the GC point for orphaned in-flight bg
                    # entries (dropped SubagentStop) — subtract whatever we reap so
                    # the heartbeat gate's counter self-heals here.
                    _reaped = self._pending_subagents.pop(tid, None)
                    if _reaped:
                        self._inflight_bg = max(0, self._inflight_bg - len(_reaped))
                    self._agent_roster.pop(tid, None)
                    # #audit(#6): sweep this thread's workflow-task entries from
                    # the long-lived bot registry. They're removed on terminal
                    # events during a turn, but a task whose terminal never
                    # arrives (killed w/o notification, crash) would otherwise
                    # linger as a phantom "running" row in /workflow list and
                    # grow the dict unbounded; reap is the natural GC point.
                    for _wtid in [
                        k for k, st in self._workflow_tasks.items()
                        if getattr(st, "thread_id", None) == tid
                    ]:
                        self._workflow_tasks.pop(_wtid, None)
                    self.session_manager.save_session_state(tid)
                    await self.session_manager.stop_session(tid)
                    log.info("Auto-expired session for thread %s", tid)
                except Exception:
                    log.exception("Auto-expire failed for thread %s", tid)

        if to_remove:
            # #146: stop sessions concurrently so one stuck bridge doesn't
            # block healthy peers. `return_exceptions=True` is belt-and-
            # suspenders — `_stop_one` already swallows.
            await asyncio.gather(
                *[_stop_one(tid) for tid in to_remove],
                return_exceptions=True,
            )

    @_cleanup_task.before_loop
    async def _before_cleanup(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def _heartbeat_task(self) -> None:
        """Write a heartbeat file every 30s for the external health checker.

        Proves the asyncio event loop is healthy (a wedged loop wouldn't tick).
        ``main()`` separately calls :func:`_touch_heartbeat` once at process
        start so the healthcheck has a fresh file even before Discord login
        completes (otherwise a bad token → ``wait_until_ready`` hangs → stale
        heartbeat → kickstart loop bounded only by ``ThrottleInterval``).

        T2-D: writes the in-flight-turn count as the file content so the
        watchdog can be lenient while a turn is active.
        """
        # #audit(#11): never let the heartbeat body raise — this is the ONE
        # loop the external watchdog keys on. A crash stops the loop, the file
        # goes stale, and the watchdog kickstarts (a self-inflicted restart).
        # _write_heartbeat_state already swallows OSError; guard anything else.
        try:
            # #audit(#9): FREEZE the heartbeat (return without writing → mtime
            # ages past health-check.sh's STALE_THRESHOLD → watchdog restarts)
            # ONLY when the gateway has been continuously down past the reconnect
            # budget AND nothing is in flight. Pre-login (_gw_down_since is None)
            # and brief blips (within budget) refresh exactly as before, so the
            # pre-login window and healthy RESUMEs are never false-killed.
            # Scope: catches a reconnect-STORM wedge (where on_disconnect fired),
            # NOT a silent poll_event hang (no disconnect → _gw_down_since None).
            #
            # "In flight" = foreground turns (_active_turns) OR background
            # subagents (_inflight_bg). Both keep writing so a wedge does NOT
            # kill work that runs in the CLI child and can still post via REST —
            # the restart would be pure loss (see 2026-07-24 R5 workflow). The
            # hard ceiling is the backstop: past it we freeze regardless, so a
            # dropped-SubagentStop orphan or a truly dead gateway can't defer
            # recovery forever.
            off = self._gw_down_since
            budget = _gateway_budget_secs()
            # clamp so a mis-set ceiling can never fire before the budget.
            hard_ceiling = max(_gateway_hard_ceiling_secs(), budget)
            down_for = (time.time() - off) if off is not None else 0.0
            inflight = self._active_turns + max(0, self._inflight_bg)
            past_ceiling = off is not None and down_for > hard_ceiling
            if (
                off is not None
                and down_for > budget
                and (inflight <= 0 or past_ceiling)
            ):
                log.warning(
                    "gateway down %.0fs > budget %.0fs; freezing heartbeat so the "
                    "watchdog restarts (inflight=%d turns=%d bg=%d past_ceiling=%s "
                    "disc=%d resume=%d)",
                    down_for, budget, inflight, self._active_turns,
                    self._inflight_bg, past_ceiling,
                    self._gw_disconnects, self._gw_resumes,
                )
            else:
                _write_heartbeat_state(inflight)
        except Exception:
            log.exception("heartbeat write failed (loop kept alive)")

    @_heartbeat_task.error
    async def _heartbeat_task_error(self, error: BaseException) -> None:
        # #audit(#11): a discord.py tasks.Loop stops PERMANENTLY if its body
        # raises outside the library's _valid_exception set. Revive the
        # watchdog-critical loop instead of letting the heartbeat freeze.
        log.critical("heartbeat loop crashed: %r — restarting", error, exc_info=error)
        try:
            self._heartbeat_task.restart()
        except Exception:
            log.exception("heartbeat loop restart failed")

    @_cleanup_task.error
    async def _cleanup_task_error(self, error: BaseException) -> None:
        # #audit(#11): belt-and-braces — the body already try/excepts, but if
        # the loop ever stops on an unexpected error, revive it.
        log.critical("cleanup loop crashed: %r — restarting", error, exc_info=error)
        try:
            self._cleanup_task.restart()
        except Exception:
            log.exception("cleanup loop restart failed")

    async def on_ready(self) -> None:  # type: ignore[override]
        user = self.user
        log.info("Bot online as %s (id=%s)", user, getattr(user, "id", "?"))
        # #audit(#9): on_ready fires after a fresh IDENTIFY (session invalidated
        # → reconnect — the observed recovery path for the 2026-07-19 DNS storm,
        # NOT on_resumed). Without clearing the gate here, a re-identify recovery
        # would look like a permanent outage and self-kill. Record recovery
        # FIRST — before the per-guild slash-sync loop below, which can raise.
        self._gw_last_resumed_at = time.time()
        self._gw_down_since = None
        # #185: sync slash commands per-guild on first on_ready. Guarded
        # idempotency (``self._slash_synced``) so reconnect-driven
        # re-fires don't waste Discord rate-limit budget. Global sync
        # is intentionally NOT performed — see setup_hook for the
        # historical duplicate-registration root cause.
        guilds_synced: list[str] = []
        for guild in self.guilds:
            if guild.id in self._slash_synced:
                continue
            try:
                # Copy globally-defined tree commands into the guild's
                # scope so the existing ``self.tree.add_command(...)``
                # API in setup_hook continues to work unchanged.
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                self._slash_synced.add(guild.id)
                guilds_synced.append(f"{guild.name}({guild.id}):{len(synced)}")
            except Exception:
                log.exception("Per-guild slash sync failed for %s", guild.id)
        if guilds_synced:
            log.info("Per-guild slash sync done: %s", ", ".join(guilds_synced))

    async def on_disconnect(self) -> None:  # type: ignore[override]
        # #audit(#20): discord.py auto-reconnects were entirely silent. Record a
        # counter + timestamp + WARNING so gateway churn is visible in /health
        # and the logs (the user's "restarts often / gateway drops" symptom).
        self._gw_disconnects += 1
        self._gw_last_disconnect_at = time.time()
        # #audit(#9): anchor to the FIRST unrecovered disconnect. on_disconnect
        # fires on EVERY failed reconnect iteration, so the `is None` guard is
        # load-bearing — without it each backoff step resets the clock and the
        # budget never elapses.
        if self._gw_down_since is None:
            self._gw_down_since = self._gw_last_disconnect_at
        log.warning("Gateway disconnected (#%d this run)", self._gw_disconnects)

    async def on_resumed(self) -> None:  # type: ignore[override]
        # Session RESUMED (not a full re-identify) — the good recovery path.
        self._gw_resumes += 1
        self._gw_last_resumed_at = time.time()
        self._gw_down_since = None  # #audit(#9): recovered via RESUME
        log.info("Gateway session resumed (#%d this run)", self._gw_resumes)

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        # #audit(live-log): without a tree-wide error handler, any slash command
        # that raises — most commonly an EXPIRED interaction (discord.NotFound
        # code 10062, when a busy event loop misses the 3s ACK window) —
        # escalates to a full CommandInvokeError traceback in the logs (13 seen).
        # Swallow the expired case at WARNING; surface anything else as a
        # friendly ephemeral message instead of a stack trace.
        orig = getattr(error, "original", error)
        cmd = getattr(getattr(interaction, "command", None), "qualified_name", "?")
        if isinstance(orig, discord.NotFound) and getattr(orig, "code", None) == 10062:
            log.warning("Slash interaction expired before response (10062); ignoring: /%s", cmd)
            return
        log.error("Slash command /%s failed", cmd, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Command failed — please try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Command failed — please try again.", ephemeral=True
                )
        except Exception:
            log.debug("failed to send app-command error notice", exc_info=True)

    async def on_message(self, message: discord.Message) -> None:  # type: ignore[override]
        # Always ignore self.
        if message.author.id == getattr(self.user, "id", None):
            return
        # Ignore other bots, except an opt-in testbot allowlist for smoke
        # testing. Set CLAUDED_TESTBOT_ID to the bot account's user id to
        # let it drive on_message (e.g. for end-to-end smoke runs).
        # **MUST be unset in production** — if the env value leaks to a
        # hostile bot account in the same guild, that bot could drive
        # arbitrary user-facing turns. Self-skip above prevents the obvious
        # misconfig where env=bot's own id (would loop infinitely).
        if message.author.bot:
            testbot_id = os.environ.get("CLAUDED_TESTBOT_ID")
            if not (testbot_id and str(message.author.id) == testbot_id):
                return

        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)

        log.info(
            "on_message channel=%s thread=%s author=%s len=%d",
            channel.id,
            parent_id,
            message.author,
            len(message.content),
        )

        try:
            if parent_id is None:
                await self._handle_channel_message(message)
            else:
                await self._handle_thread_message(message, parent_id)
        except Exception:
            log.exception("on_message handling failed")

        await self.process_commands(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _handle_channel_message(self, message: discord.Message) -> None:
        """Channel (non-thread) message: open a new thread + session.

        If the channel is bound, scope the session to that project path.
        Otherwise: when ``Config.allow_unbound_fallback`` is True (opt-in),
        fall back to the operator's home directory and surface a one-time
        hint nudging the user to ``/project bind``. When False (default,
        v1.0 behavior), silently ignore the message.
        """
        # Only trigger if bot is mentioned (v1.1 baseline) — unless the
        # channel opted out via ``/project set-mention-required false`` (v1.17 #138).
        # Accept real @mention, role mention, or text containing bot name.
        channel = message.channel
        if self.project_manager.get_mention_required(channel.id):
            bot_mentioned = self.user and self.user.id in [m.id for m in message.mentions]
            role_mentioned = any(r.name and self.user and self.user.name and r.name.lower() == self.user.name.lower() for r in message.role_mentions)
            bot_name_in_text = self.user and self.user.name and self.user.name.lower() in message.content.lower()
            if not bot_mentioned and not role_mentioned and not bot_name_in_text:
                return
        # else: mention gate bypassed — every non-bot message in this channel triggers Claude.

        # SECURITY (sec-1): unbound fallback is off by default. v1.0 behavior
        # is to silently ignore @bot in unbound channels. Operators opt in via
        # CLAUDED_ALLOW_UNBOUND_FALLBACK=1 to enable the $HOME fallback.
        # v1.18: first time we see an unbound channel, reply once with a hint
        # so the user isn't left wondering why bot didn't respond. Subsequent
        # messages in the same channel still silent-return (no spam).
        if (
            not self.project_manager.is_bound(channel.id)
            and not self.allow_unbound_fallback
        ):
            if self.project_manager.should_refuse_unbound(channel.id):
                await safe_send_message(message.channel, content=UNBOUND_REFUSE_MESSAGE, reference=message)
            return

        resolved = await _resolve_path_or_friendly_error(
            self.project_manager, channel.id, message.reply
        )
        if resolved is None:
            return
        project_path_obj, is_bound = resolved
        project_path = str(project_path_obj)

        # #87: support forum channel mode
        channel_mode = self.project_manager.get_channel_mode(channel.id)
        is_forum = channel_mode == "forum" and isinstance(channel, discord.ForumChannel)

        if not is_forum and not isinstance(channel, discord.TextChannel):
            log.warning("Channel %s is not a TextChannel or ForumChannel; skipping", channel.id)
            return

        # Strip the bot mention from the message content
        content = message.content
        if self.user:
            content = content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
        if not content:
            content = "Hello"  # fallback if user only typed @bot with no message

        thread_name = (content or "claude session")[:100] or "claude session"
        try:
            if is_forum:
                # ForumChannel.create_thread returns (Thread, Message)
                thread_with_msg = await channel.create_thread(
                    name=thread_name, content=content
                )
                thread = thread_with_msg.thread
                message = thread_with_msg.message  # the initial forum post message
            else:
                thread = await message.create_thread(name=thread_name)
        except discord.Forbidden:
            log.exception("Missing permission to create threads in channel=%s", channel.id)
            if not is_forum:
                await safe_send_message(
                    channel,
                    content="❌ I don't have permission to create threads in this channel.",
                )
            return
        except discord.HTTPException as e:
            # Discord gateway can duplicate MESSAGE_CREATE → on_message runs
            # twice → the second ``message.create_thread`` raises 160004
            # ("a thread has already been created for this message"). The
            # winning race already created the thread; we just need to
            # re-fetch the message to see it and reuse it. Forum-channel
            # thread creation goes through a different path and isn't
            # subject to this race, so we only handle 160004 here.
            if getattr(e, "code", None) == 160004 and not is_forum:
                try:
                    message = await channel.fetch_message(message.id)
                except discord.HTTPException:
                    log.exception("Could not refetch after thread-race")
                    return
                if message.thread is not None:
                    # Gate: if a session is *already active* on this
                    # thread, this dispatch is a duplicate MESSAGE_CREATE
                    # for the same user message — the race winner has
                    # already started the bridge and rendered. Suppress
                    # the second render to avoid a duplicate Claude turn
                    # (and double API cost). See issue #140.
                    existing_session = self.session_manager.get_session(
                        message.thread.id
                    )
                    if existing_session is not None and existing_session.is_active:
                        log.info(
                            "Thread-race: ignoring duplicate MESSAGE_CREATE "
                            "(session already active on thread %d)",
                            message.thread.id,
                        )
                        return
                    log.info(
                        "Thread-race: reusing existing thread %d",
                        message.thread.id,
                    )
                    thread = message.thread
                else:
                    log.warning("160004 reported but message.thread is None")
                    return
            else:
                log.exception("Failed to create thread for channel=%s", channel.id)
                if not is_forum:
                    await safe_send_message(channel, content="❌ Failed to create a thread for this message.")
                return

        # First-time unbound hint, posted before the bridge starts streaming.
        if not is_bound and self.project_manager.should_hint_unbound(channel.id):
            # safe_send_message handles transient-blip retries internally (#148
            # architect §3). Returns None on permanent failure; we just
            # silently skip the hint in that case.
            await safe_send_message(thread, content=UNBOUND_HINT_MESSAGE)

        # Acquire the per-thread lock *before* creating the session so a
        # concurrent thread message that Discord delivers out of order can't
        # race in and replace+disconnect the bridge we're about to build.
        async with self.session_manager.get_lock(thread.id):
            # Re-check active-session AFTER acquiring the lock (#150 R1
            # engineer important). The pre-lock check at the 160004 recovery
            # branch protects only the gateway-duplicate case; a separate
            # in-flight-create_session race can still leak through when two
            # `MESSAGE_CREATE` dispatches both see `get_session() == None`
            # pre-lock, both reach get_lock(), the waiter then stomps the
            # winner's freshly-bound bridge. Re-checking inside the lock
            # closes the TOCTOU window. Suppress duplicate without doing any
            # of the per-session work (no SessionConfig, no bridge, no
            # render).
            existing = self.session_manager.get_session(thread.id)
            if existing is not None and existing.is_active:
                log.info(
                    "Thread-race (inside lock): duplicate dispatch on thread %d "
                    "— winner already holds an active bridge; skipping",
                    thread.id,
                )
                return
            try:
                handler = InteractionHandler(thread)
                system_prompt = self.project_manager.get_system_prompt(channel.id)
                extra_dirs = self.project_manager.get_extra_dirs(channel.id)
                mcp_servers = self.project_manager.get_mcp_servers(channel.id)
                async def _pre_tool_notify(tool_name: str, input_data: dict) -> None:
                    # T1-B: record the subagent's current tool in the live
                    # roster (best-effort; only subagent tool calls carry an
                    # agent_id via _SubagentContextMixin).
                    aid = input_data.get("agent_id")
                    if aid:
                        self._roster_note_tool(thread.id, aid, tool_name)
                    try:
                        await thread.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                    except Exception:
                        pass  # best-effort; don't break the stream

                async def _post_tool_notify(tool_name: str, input_data: dict) -> None:
                    pass  # logged by bridge already

                async def _stop_notify(input_data: dict) -> None:
                    # review Finding 38: StopHookInput carries NO stop_reason
                    # (only hook_event_name + stop_hook_active). Don't read it.
                    log.info("Session stopped (thread=%d)", thread.id)
                    # #310: warn user if subagents still running
                    await self._warn_pending_subagents(thread.id)

                env_vars = self.project_manager.get_env(channel.id)
                _notify = self._notify_enabled.get(thread.id, self._pre_tool_notifications)
                sc = SessionConfig(
                    system_prompt=system_prompt,
                    on_ask_user=handler.handle_ask_user_question,
                    on_pre_tool_use=_pre_tool_notify if _notify else None,
                    on_post_tool_use=_post_tool_notify,
                    on_stop=_stop_notify,
                    on_subagent_start=self._make_subagent_start_cb(thread.id),
                    on_subagent_stop=self._make_subagent_stop_cb(thread.id),
                    add_dirs=extra_dirs or None,
                    mcp_servers=mcp_servers or None,
                    env=env_vars or None,
                    user=str(message.author),
                )
                bridge = await self.session_manager.create_session(
                    thread.id,
                    project_path,
                    self.config,
                    sc,
                )
                self._wire_session_persist_cb(bridge, thread.id)
            except Exception as exc:
                log.exception("Failed to start ClaudeBridge")
                err_embed = discord.Embed(
                    title="❌ Error",
                    description=f"```\n{str(exc)[:500]}\n```",
                    color=COLOR_TOOL_FAILURE,
                )
                # #148 architect §3 — retry-aware send (transient blip survival).
                await safe_send_message(thread, embed=err_embed)
                return

            # Feature #66: Add hourglass reaction (#245: safe wrapper)
            await safe_add_reaction(message, "⏳")

            user_text, tmp_dir = await self._compose_user_text(message)
            # Use mention-stripped content instead of raw message content.
            # #242 round 2: user_text may now be `list[dict]` (image content
            # blocks) instead of `str`. Adapt the substitution accordingly.
            if tmp_dir is not None:
                if isinstance(user_text, str):
                    user_text = (
                        user_text.replace(message.content, content)
                        if message.content
                        else user_text
                    )
                else:
                    # Content-block list: find the text block (last entry)
                    # and substitute mention-stripped content inside it.
                    if message.content:
                        for blk in user_text:
                            if blk.get("type") == "text" and "text" in blk:
                                blk["text"] = blk["text"].replace(message.content, content)
            else:
                # No attachments — the compose result was a plain str.
                user_text = content
            renderer = DiscordRenderer(thread, bot=self, project_path=Path(project_path) if project_path else None)
            cost_before = bridge.total_cost if bridge else 0.0
            _render_ok = False
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=thread,
                    project_path=project_path,
                    session_config=sc,
                    author_id=message.author.id,
                )
                _render_ok = True
            except Exception:
                # #147: wrap reaction ops so a Discord blip doesn't bury the
                # real exception. Safe helpers swallow transient failures.
                await safe_remove_reaction(message, "⏳", self.user)
                await safe_add_reaction(message, "❌")
                raise
            finally:
                _cleanup_tmp_dir(tmp_dir)
                cost_after = bridge.total_cost if bridge else 0.0
                response_cost = max(0.0, cost_after - cost_before)
                self.cost_tracker.record(channel.id, response_cost)  # #248: always record
                self.session_manager.save_session_state(thread.id)
                if _render_ok:
                    await safe_remove_reaction(message, "⏳", self.user)
                    await safe_add_reaction(message, "✅")

    async def _handle_thread_message(
        self, message: discord.Message, parent_id: int
    ) -> None:
        """Thread message: route to the existing/new session for that thread.

        Inherits the parent channel's bound state. The hint (if any) is the
        channel handler's job; threads never post it themselves. When the
        parent is unbound and the operator has not opted in via
        ``Config.allow_unbound_fallback``, the message is silently ignored
        (v1.0 behavior).
        """
        # #195 (P0): thread-ownership check. v1.1 PRD F1 "thread doesn't need
        # @mention" implicitly assumed the thread was created BY the bot
        # (via _handle_channel_message:create_thread). For 3rd-party
        # threads (any user opens "Create Thread" on the bound channel),
        # bot would silently engage on every message — leaking cwd to
        # random users, burning tokens, risking Discord mod action.
        # Fix: require explicit @mention or matching role-mention to engage
        # when ``thread.owner_id != bot.id``. Bot-created threads still
        # behave per PRD F1 (no @ required).
        thread = message.channel
        if isinstance(thread, discord.Thread):
            bot_id = getattr(self.user, "id", None)
            thread_owner_id = getattr(thread, "owner_id", None)
            if bot_id is not None and thread_owner_id is not None and thread_owner_id != bot_id:
                # 3rd-party thread. Require explicit invite via mention.
                bot_mentioned = any(
                    getattr(m, "id", None) == bot_id for m in message.mentions
                )
                # Role-mention as a softer invite: bot has a role whose
                # name matches the bot's display name. Matches the v1.0
                # mention-required convention.
                bot_name_lower = (getattr(self.user, "name", "") or "").lower()
                role_mentioned = bool(bot_name_lower) and any(
                    (getattr(r, "name", "") or "").lower() == bot_name_lower
                    for r in message.role_mentions
                )
                if not (bot_mentioned or role_mentioned):
                    if thread.id not in self._logged_third_party_thread:
                        self._logged_third_party_thread.add(thread.id)
                        log.info(
                            "ignored message in third-party thread tid=%s "
                            "(owner=%s, bot=%s)",
                            thread.id, thread_owner_id, bot_id,
                        )
                    return

        # SECURITY (sec-1): mirror channel handler's gate.
        # v1.18: also mirror the first-time-refusal hint so unbound *parent*
        # channels of thread messages don't silent-fail either.
        if (
            not self.project_manager.is_bound(parent_id)
            and not self.allow_unbound_fallback
        ):
            if self.project_manager.should_refuse_unbound(parent_id):
                await safe_send_message(
                    message.channel, content=UNBOUND_REFUSE_MESSAGE, reference=message,
                )
            return

        resolved = await _resolve_path_or_friendly_error(
            self.project_manager, parent_id, message.reply
        )
        if resolved is None:
            return
        project_path_obj, is_bound = resolved
        project_path = str(project_path_obj)

        thread_id = message.channel.id
        # Acquire the per-thread lock for the entire send/render cycle so
        # concurrent messages in the same thread are processed in order
        # rather than racing each other into the SDK.
        async with self.session_manager.get_lock(thread_id):
            bridge = self.session_manager.get_session(thread_id)
            sc = None  # Will be set if we create a new session
            if bridge is None or not bridge.is_active:
                try:
                    handler = InteractionHandler(message.channel)
                    system_prompt = self.project_manager.get_system_prompt(parent_id)
                    # Check for stored session to resume
                    stored = self.session_manager.get_stored_session(thread_id)
                    resume_id = stored.get("session_id") if stored else None
                    # #210: deliberately do NOT read stored.get("model").
                    # Legacy entries may carry "sonnet" pollution from
                    # pre-#199 builds; reinjecting it would re-force the
                    # sonnet override that #198 set out to fix. Cross-restart
                    # ``model_override`` is intentionally ephemeral per user
                    # intent ("没设置就是 claude code 默认的"). The SDK falls
                    # back to ~/.claude/settings.json (CLI default) when
                    # model_override is None.
                    # #295: system_prompt is likewise NOT read from stored —
                    # ``ProjectManager.get_system_prompt(parent_id)`` above is
                    # the canonical source; the old shadow copy has been
                    # dropped from sessions.json.
                    # #211: read the user-explicit permission-mode override
                    # from the stored row so a bot restart preserves the
                    # user's last ``/mode set`` / cycle choice (PRD user
                    # decision #4). Missing field on legacy rows → None →
                    # bridge falls back to env / "default".
                    stored_perm_mode = (
                        stored.get("permission_mode_override") if stored else None
                    )
                    extra_dirs = self.project_manager.get_extra_dirs(parent_id)
                    mcp_servers = self.project_manager.get_mcp_servers(parent_id)
                    _thread_target = message.channel

                    async def _pre_tool_notify_thread(tool_name: str, input_data: dict) -> None:
                        # T1-B: record the subagent's current tool in the live
                        # roster (best-effort; subagent tool calls carry agent_id).
                        aid = input_data.get("agent_id")
                        if aid:
                            self._roster_note_tool(thread_id, aid, tool_name)
                        try:
                            await _thread_target.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                        except Exception:
                            pass  # best-effort; don't break the stream

                    async def _post_tool_notify_thread(tool_name: str, input_data: dict) -> None:
                        pass  # logged by bridge already

                    async def _stop_notify_thread(input_data: dict) -> None:
                        # review Finding 38: StopHookInput has NO stop_reason.
                        log.info("Session stopped (thread=%s)", thread_id)
                        # #310: warn user if subagents still running
                        await self._warn_pending_subagents(thread_id)

                    env_vars = self.project_manager.get_env(parent_id)
                    _notify = self._notify_enabled.get(thread_id, self._pre_tool_notifications)
                    sc = SessionConfig(
                        system_prompt=system_prompt,
                        model_override=None,  # #210: ephemeral; see note above
                        permission_mode_override=stored_perm_mode,  # #211: persistent
                        resume_session_id=resume_id,
                        on_ask_user=handler.handle_ask_user_question,
                        on_pre_tool_use=_pre_tool_notify_thread if _notify else None,
                        on_post_tool_use=_post_tool_notify_thread,
                        on_stop=_stop_notify_thread,
                        on_subagent_start=self._make_subagent_start_cb(thread_id),
                        on_subagent_stop=self._make_subagent_stop_cb(thread_id),
                        add_dirs=extra_dirs or None,
                        mcp_servers=mcp_servers or None,
                        env=env_vars or None,
                        user=str(message.author),
                    )
                    bridge = await self.session_manager.create_session(
                        thread_id,
                        project_path,
                        self.config,
                        sc,
                    )
                    self._wire_session_persist_cb(bridge, thread_id)
                except Exception as exc:
                    log.exception("Failed to start ClaudeBridge for thread=%s", thread_id)
                    err_embed = discord.Embed(
                        title="❌ Error",
                        description=f"```\n{str(exc)[:500]}\n```",
                        color=COLOR_TOOL_FAILURE,
                    )
                    # #148 architect §3 — retry-aware send (transient blip survival).
                    await safe_send_message(message.channel, embed=err_embed)
                    return

            # Feature #66: Add hourglass reaction (#245: safe wrapper)
            await safe_add_reaction(message, "⏳")

            user_text, tmp_dir = await self._compose_user_text(message)
            renderer = DiscordRenderer(message.channel, bot=self, project_path=Path(project_path) if project_path else None)
            cost_before = bridge.total_cost if bridge else 0.0
            _render_ok = False
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=message.channel,
                    project_path=project_path,
                    session_config=sc,
                    author_id=message.author.id,
                )
                _render_ok = True
            except Exception:
                # #147: wrap reaction ops so a Discord blip doesn't bury the
                # real exception. Safe helpers swallow transient failures.
                await safe_remove_reaction(message, "⏳", self.user)
                await safe_add_reaction(message, "❌")
                raise
            finally:
                _cleanup_tmp_dir(tmp_dir)
                cost_after = bridge.total_cost if bridge else 0.0
                response_cost = max(0.0, cost_after - cost_before)
                self.cost_tracker.record(parent_id, response_cost)  # #248: always record
                self.session_manager.save_session_state(thread_id)
                if _render_ok:
                    await safe_remove_reaction(message, "⏳", self.user)
                    await safe_add_reaction(message, "✅")

    # ------------------------------------------------------------------
    # Helpers used by both channel- and thread-message handlers
    # ------------------------------------------------------------------

    async def _compose_user_text(
        self, message: discord.Message
    ) -> tuple[str | list[dict], Path | None]:
        """Build the message content sent to Claude.

        Returns ``(content, tmp_dir)``:
        - ``content`` is either:
          * ``str`` — the legacy text-only path (no attachments, or only
            non-image attachments); same shape `bridge.send_message` has
            historically accepted.
          * ``list[dict]`` — Anthropic Messages API "content blocks":
            inline ``image`` blocks (one per image attachment) followed
            by a single ``text`` block holding the user prose plus any
            non-image attachment hint lines.
        - ``tmp_dir`` is the per-message temp directory we wrote
          attachments to (caller cleans up after Claude finishes).

        #242 (round 2): images go INLINE as image content blocks rather
        than "path in text + claude calls Read". Spike test proved the
        path-in-text route triggers OCR-ish tool_result that hallucinates;
        inline image block routes straight to vision API and reads the
        image faithfully. The inline path also saves ~15-20% input_tokens.

        Non-image attachments (PDF, zip, .py, etc.) still go through the
        path-in-text route so Claude can choose to use Read.
        """
        text = message.content or ""
        attachments = list(message.attachments or [])
        if not attachments:
            return text, None

        tmp_dir = Path(tempfile.mkdtemp(prefix="clauded_att_"))
        notes: list[str] = []                # path-in-text for non-images
        image_blocks: list[dict] = []        # inline image content blocks
        for att in attachments:
            # Sanitize the filename: take the basename and drop anything that
            # looks like path traversal. Discord already restricts these but
            # better safe.
            safe_name = os.path.basename(att.filename or "attachment")
            if not safe_name or safe_name in ("", ".", ".."):
                safe_name = f"attachment-{att.id}"
            target = tmp_dir / safe_name
            try:
                await att.save(target)
            except (discord.HTTPException, OSError):
                log.exception("Failed to save attachment %s", safe_name)
                continue
            ext = os.path.splitext(safe_name)[1].lower()
            if ext in _VISION_INLINE_EXTENSIONS:
                # #242 (round 2): inline as image content block. Read
                # the bytes, base64-encode, build the SDK content-block
                # dict. Fail-soft: if reading fails, fall back to
                # path-in-text so Claude has SOMETHING to work with.
                try:
                    import base64

                    def _read_b64(p: Path) -> str:
                        with open(p, "rb") as fh:
                            return base64.b64encode(fh.read()).decode("ascii")

                    # #audit(#5): the file read + base64 encode are blocking
                    # (sync disk I/O + CPU-bound encode). Run them off the event
                    # loop so a multi-MB image upload can't stall the Discord
                    # gateway heartbeat — this path runs inside the per-thread
                    # lock during a turn, so a stall blocks the thread too.
                    b64 = await asyncio.to_thread(_read_b64, target)
                    image_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _VISION_MEDIA_TYPE[ext],
                            "data": b64,
                        },
                    })
                    log.info(
                        "#242: inline image attached %s (%d bytes b64)",
                        safe_name, len(b64),
                    )
                except OSError as exc:
                    log.warning(
                        "Failed to read %s for inline image: %s; "
                        "falling back to path-in-text",
                        safe_name, exc,
                    )
                    notes.append(
                        f"[User attached image: {safe_name}]\n"
                        f"Image file saved at: {target}"
                    )
            elif ext in _IMAGE_EXTENSIONS:
                # Image extension but not vision-supported (.bmp / .svg)
                # — path-in-text so Claude can decide to Read.
                notes.append(
                    f"[User attached image: {safe_name}]\n"
                    f"Image file saved at: {target}"
                )
            else:
                # Non-image attachment (PDF, zip, .py, etc.)
                notes.append(
                    f"[User attached file: {safe_name}]\n"
                    f"File saved at: {target}"
                )

        if not notes and not image_blocks:
            # Nothing actually saved — drop the empty tmp dir now.
            _cleanup_tmp_dir(tmp_dir)
            return text, None

        # Build the text portion (file paths + user prose).
        prefix = "\n".join(notes)
        composed_text = f"{prefix}\n\n{text}" if (prefix and text) else (prefix or text)

        if image_blocks:
            # New path: return content-block list so claude_bridge can
            # wire it into the SDK's structured-message protocol.
            content_blocks: list[dict] = [*image_blocks]
            if composed_text:
                content_blocks.append({"type": "text", "text": composed_text})
            return content_blocks, tmp_dir

        # No images — keep legacy str path so we don't pay the structured-
        # message overhead for plain text turns.
        return composed_text, tmp_dir

    def _get_resume_session_id(self, thread_id: int | None) -> str | None:
        """Get the best available session_id for resume.

        Checks in-memory bridge first (active session), then falls back
        to stored session on disk (survives bot restart). Returns None
        if no session history exists for this thread.
        """
        if thread_id is None:
            return None
        bridge = self.session_manager.get_session(thread_id)
        if bridge is not None and getattr(bridge, "is_active", False):
            sid = getattr(bridge, "session_id", None)
            if sid:
                return sid
        stored = self.session_manager.get_stored_session(thread_id)
        if stored:
            return stored.get("session_id")
        return None

    def _wire_session_persist_cb(self, bridge: "ClaudeBridge", thread_id: int) -> None:
        """#301 R2: persist session_id the moment the first ResultMessage arrives.

        review A4/A5: no longer registers a session_id → thread_id map. The
        old #310 ``_subagent_threads[sid] = thread_id`` line lived here and
        was the root of the per-session leak + false-warning bugs. Subagent
        routing now comes from the per-thread closure in
        _make_subagent_{start,stop}_cb, so nothing needs to be registered.
        """
        # review A6 tail: this runs only when a FRESH bridge is created (reused
        # live bridges skip it). A fresh session means any subagents left in
        # this thread's pending bucket belong to a dead prior session — their
        # SubagentStop can never arrive — so clear them now. Otherwise an
        # orphaned entry (SubagentStart fired, session killed before
        # SubagentStop) would produce a stale "N subagent(s) still running"
        # warning on a later turn's stop.
        # #audit(#9): also decrement the in-flight bg counter for these orphans
        # so a fresh session heals the heartbeat gate's count too.
        _orphaned = self._pending_subagents.pop(thread_id, None)
        if _orphaned:
            self._inflight_bg = max(0, self._inflight_bg - len(_orphaned))
        # #323 (review fix): mirror the reset for _agent_roster. It is otherwise
        # cleared ONLY by the SubagentStop hook, so a single dropped SubagentStop
        # would orphan an entry that makes the idle-reaper's #323 guard veto
        # cleanup FOREVER (bridge + CLI subprocess leak). A fresh bridge means
        # any roster entry belongs to a dead prior session — drop it here.
        self._agent_roster.pop(thread_id, None)

        def _on_sid(sid: str) -> None:
            self.session_manager.save_session_state(thread_id)
        bridge._on_session_id_cb = _on_sid

        # T2-B: surface a silent resume failure. When we asked the CLI to
        # resume a stored session but it started a fresh one instead (session
        # GC'd / cwd mismatch / prior turn killed mid-flight before it could
        # finish), the user's context is gone. Post a subtle one-line notice so
        # "it opened a new session and I don't know why" becomes explicit. The
        # bridge fires this callback synchronously from inside the (async) send
        # loop, so schedule the Discord send as a task.
        def _on_resume_failed(requested_id: str, actual_id: str) -> None:
            async def _notify() -> None:
                try:
                    channel = self.get_channel(thread_id) or await self.fetch_channel(thread_id)
                    if channel is not None:
                        await safe_send_message(
                            channel,
                            content=(
                                "-# ⚠️ Couldn't resume the previous conversation — "
                                "started a fresh session (the prior one may have ended "
                                "abnormally or been cleaned up)."
                            ),
                        )
                except Exception:
                    log.debug("T2-B: resume-failed notice send failed", exc_info=True)
            try:
                t = asyncio.get_running_loop().create_task(_notify())
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)
            except RuntimeError:
                log.debug("T2-B: no running loop for resume-failed notice", exc_info=True)
        bridge._on_resume_failed_cb = _on_resume_failed

    @staticmethod
    def _read_subagent_result(agent_transcript_path: str | None) -> str:
        """review A6: best-effort extract the subagent's final result text.

        ``agent_transcript_path`` (from SubagentStopHookInput) points at a
        JSONL transcript of the subagent's run. We read the TAIL and parse
        the LAST assistant text block → the subagent's final answer, then
        truncate to ~800 chars. Any problem (missing/unreadable/malformed
        file) returns "" so the caller falls back to a generic message and
        never crashes.
        """
        if not agent_transcript_path:
            return ""
        try:
            p = Path(agent_transcript_path)
            if not p.is_file():
                return ""
            # Read the tail only — transcripts can be large. 256 KiB is more
            # than enough to hold the final assistant turn.
            data = p.read_bytes()
            tail = data[-262_144:]
            text = tail.decode("utf-8", errors="replace")
            last_text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    # Partial first line from the tail slice, or non-JSON. Skip.
                    continue
                # Transcript rows are typically {"type": "assistant",
                # "message": {"content": [ {"type": "text", "text": ...}, ...]}}.
                # Be liberal: also accept a top-level "content" list.
                if obj.get("type") not in (None, "assistant"):
                    continue
                msg = obj.get("message", obj)
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    parts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    joined = "\n".join(t for t in parts if t)
                    if joined:
                        last_text = joined
            return last_text.strip()[:800]
        except Exception:
            log.debug("review A6: failed reading subagent transcript", exc_info=True)
            return ""

    async def _warn_pending_subagents(self, thread_id: int) -> None:
        """#310 / review A4/A5: warn if subagents are still running on stop.

        Counts real pending subagents for THIS thread via ``_pending_subagents``
        (one entry per agent_id). On a normal turn with no subagents the count
        is 0 → we send nothing (the old code always saw 1 stale session entry
        and false-warned every turn).
        """
        pending = len(self._pending_subagents.get(thread_id, {}))
        if pending <= 0:
            return
        embed = discord.Embed(
            title="⏹️ Session ended",
            description=(
                f"⚠️ {pending} subagent(s) still running in background "
                f"— will notify when complete"
            ),
            color=COLOR_INFO,
        )
        try:
            channel = self.get_channel(thread_id)
            if channel is None:
                channel = await self.fetch_channel(thread_id)
            if channel:
                await safe_send_message(channel, embed=embed)
        except Exception:
            log.debug("#310: failed to send pending-subagent warning", exc_info=True)

    # ------------------------------------------------------------------
    # T1-B: live per-agent roster (workflow / subagent UX)
    # ------------------------------------------------------------------

    def _roster_note_start(self, thread_id: int, agent_id: str, agent_type: str | None) -> None:
        """Record a newly-started subagent in the live roster."""
        self._agent_roster.setdefault(thread_id, {})[agent_id] = {
            "type": agent_type or "subagent",
            "tool": None,
            "started": time.time(),
        }

    def _roster_note_tool(self, thread_id: int, agent_id: str, tool: str | None) -> None:
        """Update the current tool a subagent is running (best-effort)."""
        bucket = self._agent_roster.get(thread_id)
        if bucket is None:
            return
        entry = bucket.get(agent_id)
        if entry is not None:
            entry["tool"] = tool

    def _roster_clear(self, thread_id: int, agent_id: str) -> None:
        """Drop a finished subagent from the roster; prune empty threads."""
        bucket = self._agent_roster.get(thread_id)
        if bucket is not None:
            bucket.pop(agent_id, None)
            if not bucket:
                self._agent_roster.pop(thread_id, None)

    # ------------------------------------------------------------------
    # #324: post-turn background stream reader (relay CLI-pushed completions)
    # ------------------------------------------------------------------

    def _start_bg_reader(
        self,
        thread_id: int,
        bridge: "ClaudeBridge",
        channel: Any,
        renderer: "DiscordRenderer",
    ) -> None:
        """Start (if not already running) a between-turns background reader.

        OWNED BY THE BRIDGE (``bridge._bg_reader_task``): every stream-consuming
        path — send_message from ANY turn entry point, and bridge.stop() —
        cancels it first, so the single-consumer invariant is enforced in the
        bridge, not per-caller (#324 review fix for the ~6 unguarded paths).

        Relays the CLI's proactively-pushed post-turn messages: a completed
        background task's continuation text AND its Task* terminal — routed
        through the SAME ``renderer`` that started the task, so its "Running"
        embed finalizes instead of leaking (#324 review fix — text-only relay
        used to swallow Task* terminals and re-open #292).
        """
        if bridge is None or not getattr(bridge, "is_active", False):
            return
        existing = getattr(bridge, "_bg_reader_task", None)
        if existing is not None and not existing.done():
            return
        bridge._bg_reader_task = asyncio.create_task(
            self._bg_reader_loop(thread_id, bridge, channel, renderer)
        )

    async def _bg_reader_loop(
        self,
        thread_id: int,
        bridge: "ClaudeBridge",
        channel: Any,
        renderer: "DiscordRenderer",
    ) -> None:
        """Consume late stream messages until idle for CLAUDED_BG_IDLE_TIMEOUT.

        Always cancellable; never runs concurrently with a turn (the bridge
        cancels it at the start of the next send_message and on stop).
        """
        idle = float(os.environ.get("CLAUDED_BG_IDLE_TIMEOUT", "600"))
        try:
            async for msg in bridge.receive_pending(per_message_timeout=idle):
                # #324 review fix: route Task* through the turn's renderer so a
                # background task's terminal (✅/❌) finalizes the embed it
                # started (its task_id lives in this renderer's _task_states);
                # only non-Task* messages fall through to the text relay.
                try:
                    handled = await renderer._dispatch_task_event(msg, {})
                except Exception:
                    handled = False
                    log.debug("#324: bg Task* dispatch failed thread=%s", thread_id, exc_info=True)
                if not handled:
                    await self._relay_bg_message(channel, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("#324: bg reader loop error thread=%s", thread_id, exc_info=True)
        finally:
            if getattr(bridge, "_bg_reader_task", None) is asyncio.current_task():
                bridge._bg_reader_task = None

    async def _relay_bg_message(self, channel: Any, msg: Any) -> None:
        """Relay the agent's post-turn continuation TEXT to Discord.

        Conservative: text only (``AssistantMessage`` ``TextBlock``s). Skips
        UserMessage / ``<task-notification>`` echoes / stream events / tool
        noise so the background follow-up (e.g. "here's the URL") shows up
        without spamming. Best-effort; never raises.
        """
        try:
            from .claude_bridge import AssistantMessage, TextBlock
            if not isinstance(msg, AssistantMessage):
                return
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                return
            parts = [
                b.text for b in content
                if isinstance(b, TextBlock) and (getattr(b, "text", "") or "").strip()
            ]
            text = "\n".join(parts).strip()
            if not text:
                return
            header = "-# 🔄 background follow-up\n"
            first = True
            for i in range(0, len(text), 1800):
                chunk = text[i:i + 1800]
                await safe_send_message(channel, content=(header + chunk) if first else chunk)
                first = False
        except Exception:
            log.debug("#324: relay bg message failed", exc_info=True)

    def _make_subagent_start_cb(self, thread_id: int) -> Any:
        """review A4/A5: build a SubagentStart callback that records a pending
        subagent for this thread.

        Stored in SessionConfig.on_subagent_start, invoked by the SubagentStart
        hook in claude_bridge.py. ``SubagentStartHookInput`` carries agent_id +
        agent_type (and session_id from BaseHookInput). We key pending state by
        agent_id so parallel subagents are counted independently and the
        completion count survives after the first one stops.
        """
        async def _on_subagent_start(input_data: dict) -> None:
            agent_id = input_data.get("agent_id")
            agent_type = input_data.get("agent_type")
            if not agent_id:
                return
            bucket = self._pending_subagents.setdefault(thread_id, {})
            # #audit(#9): count each distinct agent_id once so the heartbeat gate
            # defers the wedge-restart while this bg work runs. Guard on newness
            # so a duplicate SubagentStart can't double-count.
            if agent_id not in bucket:
                self._inflight_bg += 1
            bucket[agent_id] = agent_type or "subagent"
            # T1-B: seed the live roster so the workflow embed can show this
            # agent (type + current tool + elapsed) even without workflowProgress.
            self._roster_note_start(thread_id, agent_id, agent_type)

        return _on_subagent_start

    def _make_subagent_stop_cb(self, thread_id: int) -> Any:
        """#310 / review A3/A6: notify Discord when a subagent completes.

        Stored in SessionConfig.on_subagent_stop, invoked by the SubagentStop
        hook in claude_bridge.py. Sends a ✅ embed to the thread even if the
        main renderer has already returned.

        review A3: ``SubagentStopHookInput`` provides agent_id / agent_type /
        agent_transcript_path — and NO stop_reason / summary / duration_ms.
        The old code read those non-existent fields, so the embed was always
        the contentless "Subagent finished." and the real output never reached
        Discord.

        review A6: we surface the subagent's real result to Discord by reading
        the tail of agent_transcript_path. Re-injecting that result back into
        Claude's own conversation is DEFERRED (was reverted in #310, needs a
        separate design).
        """
        async def _on_subagent_stop(input_data: dict) -> None:
            agent_id = input_data.get("agent_id")
            agent_type = input_data.get("agent_type")
            agent_transcript_path = input_data.get("agent_transcript_path")

            # review A4/A5: drain this agent_id from pending (guard both the
            # thread bucket and the id — SubagentStart may not have fired).
            bucket = self._pending_subagents.get(thread_id)
            if bucket is not None:
                # #audit(#9): decrement the in-flight bg counter only when a
                # tracked entry is actually removed (mirrors the newness guard
                # in _on_subagent_start).
                if bucket.pop(agent_id, None) is not None:
                    self._inflight_bg = max(0, self._inflight_bg - 1)
                if not bucket:
                    self._pending_subagents.pop(thread_id, None)
            # T1-B: drop from the live roster too.
            if agent_id:
                self._roster_clear(thread_id, agent_id)

            # review A6: surface the real result (Discord only).
            result_text = self._read_subagent_result(agent_transcript_path)
            label = agent_type or "subagent"
            if result_text:
                description = result_text
            else:
                description = f"✅ Subagent (`{label}`) completed"

            embed = discord.Embed(
                title=f"✅ Subagent completed ({label})",
                description=description,
                color=COLOR_INFO,
            )

            # Route to the closure thread_id — no session→thread map needed.
            try:
                channel = self.get_channel(thread_id)
                if channel is None:
                    channel = await self.fetch_channel(thread_id)
                if channel is not None:
                    await safe_send_message(channel, embed=embed)
                else:
                    log.warning(
                        "#310: could not find thread %d for subagent notification",
                        thread_id,
                    )
            except Exception:
                log.warning(
                    "#310: failed to send subagent completion notification",
                    exc_info=True,
                )

        return _on_subagent_stop

    async def _recreate_session(
        self,
        interaction: discord.Interaction,
        **overrides,
    ) -> "ClaudeBridge | None":
        """Stop current session and create a new one with overrides.

        Used by /model, /effort, /tools, /budget, etc. to avoid repeating
        the lock-stop-create pattern. Returns the new bridge or None on error.
        """
        await interaction.response.defer()
        thread_id = interaction.channel_id
        parent_id = getattr(interaction.channel, "parent_id", None)
        if parent_id is None:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Use in a thread", color=COLOR_TOOL_FAILURE),
                ephemeral=True,
            )
            return None
        project_path = self.project_manager.get_path(parent_id)
        if not project_path:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Not bound", color=COLOR_TOOL_FAILURE),
                ephemeral=True,
            )
            return None

        # Build pre-tool callback if notifications are enabled
        _target = interaction.channel
        pre_tool_cb = None
        _notify = self._notify_enabled.get(thread_id, self._pre_tool_notifications)
        if _notify:
            async def _pre_tool_notify(tool_name: str, input_data: dict) -> None:
                try:
                    await _target.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                except discord.HTTPException as exc:
                    # #223 PR-B: Discord 5xx during hot-path pre-tool
                    # notify is acceptable noise — keep at DEBUG so prod
                    # logs don't drown.
                    log.debug(
                        "pre_tool_notify HTTPException for %s: %s",
                        tool_name,
                        exc,
                    )
                except Exception as exc:
                    # Non-HTTP failure means a real bug (closed channel,
                    # permission revoked mid-turn, etc.) — worth WARNING.
                    log.warning(
                        "pre_tool_notify failed for %s: %s",
                        tool_name,
                        exc,
                        exc_info=True,
                    )
            pre_tool_cb = _pre_tool_notify

        async def _post_tool_notify(tool_name: str, input_data: dict) -> None:
            pass  # logged by bridge already

        async def _stop_notify(input_data: dict) -> None:
            # review Finding 38: StopHookInput has NO stop_reason.
            log.info("Session stopped (thread=%s)", thread_id)
            # #310: warn user if subagents still running
            await self._warn_pending_subagents(thread_id)

        sc = SessionConfig(
            system_prompt=self.project_manager.get_system_prompt(parent_id),
            add_dirs=self.project_manager.get_extra_dirs(parent_id) or None,
            mcp_servers=self.project_manager.get_mcp_servers(parent_id) or None,
            env=self.project_manager.get_env(parent_id) or None,
            on_ask_user=InteractionHandler(interaction.channel).handle_ask_user_question,
            on_pre_tool_use=pre_tool_cb,
            on_post_tool_use=_post_tool_notify,
            on_stop=_stop_notify,
            on_subagent_start=self._make_subagent_start_cb(thread_id),
            on_subagent_stop=self._make_subagent_stop_cb(thread_id),
            **overrides,
        )

        lock = self.session_manager.get_lock(thread_id)
        async with lock:
            await self.session_manager.stop_session(thread_id)
            try:
                bridge = await self.session_manager.create_session(
                    thread_id, project_path, self.config, sc,
                )
                self._wire_session_persist_cb(bridge, thread_id)
            except Exception as exc:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Error",
                        description=f"```\n{str(exc)[:500]}\n```",
                        color=COLOR_TOOL_FAILURE,
                    ),
                    ephemeral=True,
                )
                return None
        return bridge

    async def _render_with_retry(
        self,
        *,
        renderer: DiscordRenderer,
        bridge,  # ClaudeBridge — typed loosely to avoid an extra import
        user_text: str | list[dict],
        thread: discord.abc.Messageable,
        project_path: str,
        session_config: SessionConfig | None = None,
        author_id: int | None = None,
    ) -> None:
        """Run ``renderer.render_response`` and surface a retry button on crash.

        On exception we drop the (now-dead) bridge so the next message —
        either via the retry button or a fresh user message — recreates a
        clean session.

        ``author_id`` (optional) is threaded into ``render_response`` so the
        :class:`ToolResultsView` can restrict per-button ephemeral followups
        to the original requester (#179 R1 security).
        """
        try:
            # #241: register per-turn scheduler context BEFORE invoking the
            # bridge so the in-process scheduler MCP tools can resolve
            # "current thread / channel / guild" defaults when claude calls
            # `schedule_message` / `schedule_new_task` / `schedule_list`.
            self._register_scheduler_ctx(
                thread_id=getattr(thread, "id", None) or 0,
                channel_id=(
                    getattr(thread, "parent_id", None)
                    or getattr(thread, "id", None)
                ),
                guild_id=getattr(getattr(thread, "guild", None), "id", None),
            )
            tid = getattr(thread, "id", None)
            # T2-D: mark a turn in-flight so the heartbeat tells the watchdog
            # to be lenient (a stall UNDER a turn shouldn't hard-kill the bot
            # and drop the not-yet-persisted session_id).
            # (#324 review fix: no explicit reader-cancel needed here — the
            # bridge cancels any reader inside send_message before this turn
            # consumes the stream, covering every entry point.)
            self._active_turns += 1
            try:
                await renderer.render_response(bridge, user_text, author_id=author_id)
            finally:
                self._active_turns = max(0, self._active_turns - 1)
            # #324: turn done — resume the background reader (owned by the bridge)
            # so the CLI's proactively-pushed post-turn messages (a background
            # task's completion + the main agent's continuation, incl. its Task*
            # terminal) are relayed to this thread until the next turn cancels it.
            if tid is not None:
                self._start_bg_reader(tid, bridge, thread, renderer)
        except Exception as exc:
            if is_transient_discord_error(exc):
                # Render gave up (rare — _retry_http exhausted), but bridge is
                # healthy. Leave session alive; user can send another message
                # to resume. No retry button — bridge isn't dead.
                log.warning(
                    "Render paused after exhausted retries; bridge kept",
                    extra={
                        "thread_id": getattr(thread, "id", None),
                        "exc_class": type(exc).__name__,
                    },
                )
                return
            log.exception("Renderer fatal; offering retry button")
            # #223: dump crash to stream-debug.jsonl so /log dump (#224)
            # can ship the full traceback even if the user reports a bug
            # without the rotating clauded.log lines (often gone after 7
            # rotations — jsonl is the audit trail).
            #
            # #223 R1 product: include session_id so /log dump can
            # cross-reference the SDK conversation (thread_id alone is
            # ambiguous — sessions cycle on resume #160, and
            # stop_session runs right after this dump).
            if stream_logger.is_enabled():
                import traceback as _tb
                stream_logger.log_event({
                    "type": "Crash",
                    "where": "render_response",
                    "thread_id": getattr(thread, "id", None),
                    "session_id": getattr(bridge, "session_id", None),
                    "exc_class": type(exc).__name__,
                    "traceback": _tb.format_exc(),
                })
            # #224 epic Subtask 4: auto-crash bundle dispatch. Rate-limited
            # per thread (5min cooldown) so a thrash-cycle bug doesn't
            # spam the channel with bundles.
            await self._maybe_dispatch_auto_crash_bundle(
                thread=thread,
                exc=exc,
                bridge=bridge,
            )
            thread_id = getattr(thread, "id", None)
            if thread_id is not None:
                await self.session_manager.stop_session(thread_id)

            # Capture session_config + author_id for retry (#80, #161 sec)
            _retry_sc = session_config
            _retry_author_id = author_id

            async def _on_retry() -> None:
                # Re-acquire the lock so a manual click can't race with a
                # follow-up message the user just typed.
                if thread_id is None:
                    return
                async with self.session_manager.get_lock(thread_id):
                    try:
                        # #227 R1 engineer: extract retry-SessionConfig build
                        # into module-level ``_build_retry_session_config``
                        # so it has unit-test coverage that fails under
                        # mental revert. ``stop_session`` above only kills
                        # the in-memory bridge; persistent store entry
                        # survives, so ``get_stored_session`` returns the
                        # session_id we need to resume.
                        stored = self.session_manager.get_stored_session(thread_id)
                        new_handler = InteractionHandler(thread)
                        retry_sc = _build_retry_session_config(
                            stored,
                            _retry_sc,
                            new_handler.handle_ask_user_question,
                            thread_id_for_log=thread_id,
                        )
                        new_bridge = await self.session_manager.create_session(
                            thread_id,
                            project_path,
                            self.config,
                            retry_sc,
                        )
                        self._wire_session_persist_cb(new_bridge, thread_id)
                    except Exception as start_exc:
                        log.exception("Retry: failed to restart ClaudeBridge")
                        err_embed = discord.Embed(
                            title="❌ Error",
                            description=f"```\n{str(start_exc)[:500]}\n```",
                            color=COLOR_TOOL_FAILURE,
                        )
                        # #148 architect §3 — retry-embed survives a Discord
                        # blip; on permanent failure we silently drop.
                        await safe_send_message(thread, embed=err_embed)
                        return
                    new_renderer = DiscordRenderer(thread, bot=self, project_path=Path(project_path) if project_path else None)
                    await self._render_with_retry(
                        renderer=new_renderer,
                        bridge=new_bridge,
                        user_text=user_text,
                        thread=thread,
                        project_path=project_path,
                        session_config=retry_sc,
                        author_id=_retry_author_id,
                    )

            await renderer.send_error_with_retry(exc, _on_retry)

    # ------------------------------------------------------------------
    # #241 — scheduler ctx provider + tick loop + fire callbacks
    # ------------------------------------------------------------------

    def _scheduler_ctx_provider(self) -> dict:
        """Return the most recently registered per-turn scheduler context.

        Consumed by ``scheduler_mcp._GLOBAL_CTX`` so tool handlers running
        inside a claude turn can default ``target_thread_id`` /
        ``target_channel_id`` to the thread/channel the turn originated
        from. Returns ``{}`` if no turn is active (tool would then need an
        explicit arg or surface a ctx-missing error).

        B2: prefers :func:`scheduler_mcp.get_ctx` (the module-level
        ``ContextVar``) over the local bot-instance copy. This makes the
        provider correct under concurrent fires/turns — each task's
        ``ContextVar`` slot was set by its own ``_register_scheduler_ctx``
        call and is invisible to siblings. The bot-instance
        ``_scheduler_ctx_var`` / ``_scheduler_current_ctx`` are kept as
        fallback / mirror for legacy tests reading the attribute directly.
        """
        cv = _scheduler_get_ctx()
        if cv:
            return cv
        return getattr(self, "_scheduler_current_ctx", {}) or {}

    def _register_scheduler_ctx(
        self,
        *,
        thread_id: int | None,
        channel_id: int | None,
        guild_id: int | None,
        tz_name: str = "Asia/Shanghai",
    ) -> None:
        """Set the per-turn scheduler context.

        Called immediately BEFORE every claude turn (natural ``@bot``
        conversations via :meth:`_render_with_retry`, slash-injected turns
        via :mod:`clauded.cogs.schedule`, scheduled-fire turns via the
        ``_fire_schedule_*`` methods). PRD §3.7 — the ctx is what makes
        the in-process MCP tools "know" which thread they're acting on.

        B2: this now writes to BOTH the module-level ContextVar in
        :mod:`scheduler_mcp` (via :func:`scheduler_mcp.set_ctx`) AND the
        bot-instance ``contextvars.ContextVar``. The scheduler_mcp one
        is what tool handlers actually read via :func:`scheduler_mcp.get_ctx`
        / :func:`scheduler_mcp._resolve_ctx`; the bot-instance ContextVar
        is kept for the legacy ``_scheduler_ctx_provider`` fallback. Both
        ``set`` calls only mutate the current asyncio task's context, so
        two concurrent fires / turns can't clobber each other's "current
        thread" between register-ctx and ``render_response``. The
        mirroring instance attribute is kept purely so existing tests
        that read ``bot._scheduler_current_ctx`` directly continue to
        pass — production code should never reach for it; use
        :meth:`_scheduler_ctx_provider` instead.
        """
        ctx = {
            "thread_id": thread_id,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "tz_name": tz_name,
        }
        _scheduler_ctx_var.set(ctx)
        # B2: also publish into scheduler_mcp's ContextVar so tool
        # handlers running in this task pick it up via _resolve_ctx
        # without depending on the bot-instance provider callable.
        _scheduler_set_ctx(ctx)
        # Mirror for legacy direct-attribute readers (tests). The
        # ContextVars are the source of truth.
        self._scheduler_current_ctx = ctx

    @tasks.loop(seconds=15)
    async def _scheduler_tick(self) -> None:
        """Periodic scheduler tick. First iteration also runs ``catch_up()``.

        Exceptions are swallowed + logged so a single bad schedule (e.g. a
        corrupt next_fire_at) can't kill the whole tick loop. The next
        iteration retries automatically — terminal failures are handled
        inside ``SchedulerManager._fire_with_retry`` which disables the
        bad schedule rather than bubbling out.
        """
        try:
            if not self._scheduler_catch_up_done:
                await self.scheduler.catch_up()
                self._scheduler_catch_up_done = True
            await self.scheduler.tick()
        except Exception:
            log.exception("#241 scheduler tick failed; will retry next interval")

    @_scheduler_tick.before_loop
    async def _before_scheduler_tick(self) -> None:
        await self.wait_until_ready()

    @_scheduler_tick.error
    async def _scheduler_tick_error(self, error: BaseException) -> None:
        # #audit(#11): revive the tick loop if it ever stops on an unexpected
        # exception (the body already try/excepts, so this is belt-and-braces).
        log.critical("scheduler tick loop crashed: %r — restarting", error, exc_info=error)
        try:
            self._scheduler_tick.restart()
        except Exception:
            log.exception("scheduler tick loop restart failed")

    # ------------------------------------------------------------------
    # #241 — fire-callback shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_fire_quoted_what(what: str) -> str:
        """Format ``what`` as a Discord block-quoted snippet for the fire prefix.

        B6 / PRD §3.8 (AC13): every line of the injected ``what`` is
        independently wrapped in 「…」 corner brackets and then prefixed
        with Discord's ``>`` quote marker, so the user sees a clearly
        visible record of exactly what string was injected into claude's
        context. Empty input renders an empty block-quote rather than the
        old ``"> "`` (literal "> " plus empty line) fallback.

        Wrapping happens per line (not once around the whole multi-line
        body) so each visible line in the channel reads as a self-contained
        bracketed unit — important when ``what`` contains tool output or
        anything multi-paragraph.
        """
        lines = what.splitlines() if what else []
        if not lines:
            # Empty / single-newline what: render an empty quote so the
            # AC13 marker still shows but no bogus content leaks.
            return "> 「」"
        return "\n".join(f"> 「{line}」" for line in lines)

    async def _send_fire_prefix_and_quote(
        self,
        thread,
        fire_label: str,
        what: str,
        *,
        sched_id_for_log: str = "",
    ) -> None:
        """Best-effort send of the AC13 prefix line + block-quoted ``what``.

        Failures are swallowed + logged: the actual claude turn still
        runs even if these "humans can see what just got injected" sends
        fail (network blip, permissions). The 1900-char ceiling leaves
        headroom under Discord's 2000-char message limit.
        """
        try:
            await thread.send(content=f"-# ⏰ Scheduled fire: {fire_label}")
            quoted = self._format_fire_quoted_what(what)
            await thread.send(content=quoted[:1900])
        except Exception as exc:
            log.warning(
                "#241 prefix/quoted send failed for sched=%s: %s",
                sched_id_for_log, exc,
            )

    async def _safe_fire_render(
        self,
        *,
        renderer: DiscordRenderer,
        bridge,  # ClaudeBridge — typed loosely to avoid an extra import
        what: str,
        thread,
    ) -> None:
        """Run ``renderer.render_response`` for a scheduled fire with crash safety.

        M3: scheduled fires used to call ``renderer.render_response``
        directly, which meant any renderer crash propagated up to
        ``_fire_with_retry`` → 1s/4s/16s backoff → permanent
        terminal-disable for a one-off presentation-layer bug. Worse, no
        #224 auto-crash bundle was dispatched, so the scheduled-fire
        crash never made it into the audit trail.

        This helper mirrors :meth:`_render_with_retry` but without the
        Retry button (a scheduled fire has no human to click it) and
        without bridge-resurrection (the next fire will create or
        resurrect on its own). Renderer crashes are still ``raise``-d so
        ``_fire_with_retry`` can classify them — but the auto-crash
        bundle dispatch happens regardless.
        """
        try:
            # T2-D: scheduled fires are turns too — count them in-flight.
            self._active_turns += 1
            try:
                await renderer.render_response(bridge, what)
            finally:
                self._active_turns = max(0, self._active_turns - 1)
        except Exception as exc:
            if is_transient_discord_error(exc):
                # _retry_http exhausted at the lower layer; let the
                # scheduler treat this as transient + retry.
                log.warning(
                    "#241 fire render exhausted retries (transient); "
                    "bubbling for scheduler retry: %s", exc,
                )
                raise
            log.exception(
                "#241 scheduled-fire renderer crashed; dispatching crash bundle"
            )
            # #224 auto-crash bundle — same as human-driven turns get via
            # ``_render_with_retry``. The scheduled-fire surface is the
            # one most likely to crash without a human nearby, so the
            # audit trail matters more here than elsewhere.
            try:
                await self._maybe_dispatch_auto_crash_bundle(
                    thread=thread,
                    exc=exc,
                    bridge=bridge,
                )
            except Exception:
                log.exception(
                    "#241 auto-crash bundle dispatch itself crashed; "
                    "swallowing so we still raise the original"
                )
            raise

    async def _fire_schedule_message(self, sched: dict) -> None:
        """Kind=message fire: inject ``what`` as a user message into the
        target thread's existing session.

        Steps (per PRD §3.8):
          1. Resolve the target thread; raise :class:`discord.NotFound`
             if the channel is gone (the manager will mark the schedule
             terminal-disabled).
          2. Resolve the parent channel + binding; raise on unbind so
             the schedule auto-disables.
          3. Get-or-resurrect the session (using the stored
             ``resume_session_id`` if the live bridge has died).
          4. Send the fire prefix line + a Discord-quoted view of ``what``
             so the injection is visible to humans (PRD §3.8 / AC13).
          5. Register the per-turn scheduler ctx.
          6. Hand off to :class:`DiscordRenderer.render_response`.
        """
        thread_id = sched.get("target_thread_id")
        if not isinstance(thread_id, int):
            raise ValueError(
                f"#241 schedule missing target_thread_id: "
                f"{sched.get('schedule_id')}"
            )
        thread = self.get_channel(thread_id)
        if thread is None:
            thread = await self.fetch_channel(thread_id)
        if not isinstance(thread, discord.Thread):
            raise discord.NotFound(
                _MagicResponse(404),
                f"target {thread_id} is not a Thread",
            )

        parent_id = thread.parent_id or sched.get("channel_id")
        if not parent_id:
            raise RuntimeError("can't resolve parent channel for schedule fire")
        project_path = self.project_manager.get_path(parent_id)
        if not project_path:
            raise RuntimeError(f"channel {parent_id} not bound")

        # Get-or-resurrect session: if the live bridge died (process
        # restart, /session stop, etc.), use the persisted resume id so
        # the injected message lands in the same conversation history.
        bridge = self.session_manager.get_session(thread_id)
        # #285: probe the bridge to detect ghost (is_active=True but SDK dead)
        if bridge is not None and getattr(bridge, "is_active", False):
            try:
                probe = await asyncio.wait_for(
                    bridge.get_context_usage(), timeout=10,
                )
                if probe is None:
                    raise RuntimeError("get_context_usage returned None")
            except Exception:
                log.warning(
                    "#285 ghost bridge detected for thread=%s; discarding",
                    thread_id,
                )
                bridge = None
        if bridge is None or not getattr(bridge, "is_active", False):
            stored = self.session_manager.get_stored_session(thread_id)
            resume_id = stored.get("session_id") if stored else None
            sc = SessionConfig(
                system_prompt=self.project_manager.get_system_prompt(parent_id),
                resume_session_id=resume_id,
                add_dirs=self.project_manager.get_extra_dirs(parent_id) or None,
                mcp_servers=(
                    self.project_manager.get_mcp_servers(parent_id) or None
                ),
                env=self.project_manager.get_env(parent_id) or None,
                user="scheduled-fire",
            )
            bridge = await self.session_manager.create_session(
                thread_id, project_path, self.config, sc,
            )
            self._wire_session_persist_cb(bridge, thread_id)

        self._register_scheduler_ctx(
            thread_id=thread_id,
            channel_id=parent_id,
            guild_id=getattr(getattr(thread, "guild", None), "id", None),
        )

        fire_label = sched.get("name") or (sched.get("schedule_id", "") or "")[:8]
        what = (sched.get("payload") or {}).get("what", "") or ""
        # B6/AC13: prefix + per-line 「<line>」 quote so humans can see what
        # got injected. M3: helper centralizes the format + best-effort send.
        await self._send_fire_prefix_and_quote(
            thread, fire_label, what,
            sched_id_for_log=sched.get("schedule_id", "") or "",
        )

        renderer = DiscordRenderer(
            thread,
            bot=self,
            project_path=Path(project_path) if project_path else None,
        )
        # M3: wrap render in crash-bundle-safe helper so a one-off renderer
        # exception doesn't terminal-disable a recurring schedule + lose
        # the #224 audit trail.
        await self._safe_fire_render(
            renderer=renderer, bridge=bridge, what=what, thread=thread,
        )

    async def _fire_schedule_new_task(self, sched: dict) -> None:
        """Kind=new_task fire: spawn a fresh thread + fresh session, inject
        ``what`` as that new session's first user prompt.

        Steps (per PRD §3.8):
          1. Resolve the target channel; raise NotFound on disappearance.
          2. Check binding (unbound → raise → manager disables).
          3. Create a public thread (auto_archive=1440min, name capped at
             100 chars).
          4. Announce the new thread in the parent channel.
          5. Build a fresh ``SessionConfig`` (no ``resume_session_id``).
          6. Send the fire prefix + Discord-quoted ``what`` into the new
             thread (PRD AC13).
          7. Register scheduler ctx + hand off to ``DiscordRenderer``.
        """
        channel_id = sched.get("target_channel_id")
        if not isinstance(channel_id, int):
            raise ValueError(
                f"#241 schedule missing target_channel_id: "
                f"{sched.get('schedule_id')}"
            )
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise discord.NotFound(
                _MagicResponse(404),
                f"target {channel_id} not a text channel",
            )

        project_path = self.project_manager.get_path(channel_id)
        if not project_path:
            raise RuntimeError(f"channel {channel_id} not bound")

        # Build the thread name: claude can override via the ``thread_name``
        # tool arg (PRD §3.8); otherwise we synthesize "⏰ {schedule_name}
        # {fire_ts:%m-%d %H:%M}" so the user sees when each fire happened.
        from datetime import datetime as _dt
        fire_label = sched.get("name") or (sched.get("schedule_id", "") or "")[:8]
        thread_name_raw = (
            sched.get("thread_name")
            or f"⏰ {fire_label} {_dt.now().strftime('%m-%d %H:%M')}"
        )
        thread_name = thread_name_raw[:100]

        # M2 idempotency: if a prior attempt of THIS fire already created
        # a thread (transient failure between create_thread and the rest
        # of the callback), reuse it instead of creating a sibling. The
        # cached id lives in ``state._new_task_thread_id`` and is cleared
        # in ``_on_fire_success``, so the *next* scheduled occurrence
        # still creates a fresh thread.
        state = sched.setdefault("state", {})
        cached_thread_id = state.get("_new_task_thread_id")
        thread = None
        if cached_thread_id:
            cached = self.get_channel(cached_thread_id)
            if cached is None:
                try:
                    cached = await self.fetch_channel(cached_thread_id)
                except Exception:
                    cached = None
            if isinstance(cached, discord.Thread):
                thread = cached
                log.info(
                    "#241 new_task fire reusing thread %s for retry sched=%s",
                    cached_thread_id, sched.get("schedule_id"),
                )
            else:
                # Cached id no longer resolves to a thread (deleted,
                # archived to nothing). Drop the cache and create fresh.
                state["_new_task_thread_id"] = None

        if thread is None:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
            # Persist the new thread id immediately so a transient failure
            # after this point reuses it on retry. Best-effort: a save
            # failure logs but doesn't abort the fire.
            try:
                state["_new_task_thread_id"] = thread.id
                self.scheduler_store.save(sched)
            except Exception as exc:
                log.warning(
                    "#241 failed to persist _new_task_thread_id sched=%s: %s",
                    sched.get("schedule_id"), exc,
                )

        # Parent-channel announce — best-effort. PRD §3.8: lets channel
        # members see that the bot just spun up a scheduled-task thread.
        try:
            embed = discord.Embed(
                title="📌 Scheduled-task thread created",
                description=(
                    f"{thread.mention} (schedule "
                    f"`{(sched.get('schedule_id','') or '')[:8]}`)"
                ),
                color=COLOR_INFO,
            )
            await safe_send_message(channel, embed=embed)
        except Exception as exc:
            log.warning(
                "#241 new_task announce failed for sched=%s: %s",
                sched.get("schedule_id"), exc,
            )

        # Fresh session — explicitly no resume id so this thread starts
        # with empty conversation context, matching PRD §3.1 Kind 2
        # semantics ("inject `what` as that session's first user prompt").
        sc = SessionConfig(
            system_prompt=self.project_manager.get_system_prompt(channel_id),
            resume_session_id=None,
            add_dirs=self.project_manager.get_extra_dirs(channel_id) or None,
            mcp_servers=(
                self.project_manager.get_mcp_servers(channel_id) or None
            ),
            env=self.project_manager.get_env(channel_id) or None,
            user="scheduled-fire-newtask",
        )
        bridge = await self.session_manager.create_session(
            thread.id, project_path, self.config, sc,
        )
        self._wire_session_persist_cb(bridge, thread.id)

        self._register_scheduler_ctx(
            thread_id=thread.id,
            channel_id=channel_id,
            guild_id=getattr(channel.guild, "id", None),
        )

        what = (sched.get("payload") or {}).get("what", "") or ""
        # B6/AC13: shared prefix + per-line 「<line>」 quote helper.
        await self._send_fire_prefix_and_quote(
            thread, fire_label, what,
            sched_id_for_log=sched.get("schedule_id", "") or "",
        )

        renderer = DiscordRenderer(
            thread,
            bot=self,
            project_path=Path(project_path) if project_path else None,
        )
        # M3: crash-bundle-safe render. See ``_fire_schedule_message`` for
        # full rationale — renderer crash here would terminal-disable a
        # weekly recurring task on a one-off bug, with no audit trail.
        await self._safe_fire_render(
            renderer=renderer, bridge=bridge, what=what, thread=thread,
        )

    async def _notify_schedule_expired(self, sched: dict) -> None:
        """Notify the schedule's created channel that it was auto-disabled.

        Called by :meth:`SchedulerManager._check_max_lifetime` (max_lifetime
        reached) and, review E3, by :meth:`SchedulerManager._on_fire_terminal`
        (retries exhausted / terminal Discord error). We branch on
        ``state.last_error`` to pick the wording. Best-effort: failures are
        logged but never bubble — the schedule is already disabled.
        """
        channel_id = sched.get("channel_id")
        if not channel_id:
            log.info(
                "#241 expire-notify but no channel_id on sched=%s",
                sched.get("schedule_id"),
            )
            return
        sid = (sched.get("schedule_id", "") or "")[:8]
        name = sched.get("name", "")
        last_error = (sched.get("state") or {}).get("last_error") or ""
        if last_error == "max_lifetime reached":
            title = "⏰ Schedule expired"
            description = (
                f"Schedule `{sid}` (*{name}*) reached its max_lifetime "
                f"and has been auto-disabled."
            )
        else:
            # review E3: crash auto-disable (exhausted retries / terminal error).
            title = "⚠️ Schedule auto-disabled"
            description = (
                f"Schedule `{sid}` (*{name}*) kept failing and has been "
                f"auto-disabled after repeated attempts.\n"
                f"Last error: `{last_error[:300]}`\n"
                f"-# Fix the cause, then recreate/re-enable it."
            )
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            embed = discord.Embed(
                title=title,
                description=description,
                color=COLOR_INFO,
            )
            await safe_send_message(channel, embed=embed)
        except Exception:
            log.exception(
                "#241 expire-notify failed for sched=%s",
                sched.get("schedule_id"),
            )

    # ------------------------------------------------------------------
    # #224 epic Subtask 4 — auto-crash bundle dispatch
    # ------------------------------------------------------------------

    async def _maybe_dispatch_auto_crash_bundle(
        self,
        *,
        thread: "discord.abc.Messageable | None",
        exc: BaseException,
        bridge: "object | None",
    ) -> None:
        """#224: generate and upload an auto-crash diagnostic bundle.

        Rate-limited per-thread (``AUTO_CRASH_COOLDOWN_S``) so a thrash-
        cycle bug doesn't spam the channel with bundles. Best-effort:
        any failure inside this method is logged but never re-raised
        (the caller is already in an ``except Exception`` block recovering
        from the original crash).
        """
        if thread is None:
            return
        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        # Per-thread rate-limiter (dict of thread_id -> last dispatch ts)
        if not hasattr(self, "_auto_crash_last_dispatch"):
            self._auto_crash_last_dispatch: dict[int, float] = {}
        now = time.time()
        last = self._auto_crash_last_dispatch.get(thread_id, 0)
        if now - last < AUTO_CRASH_COOLDOWN_S:
            log.info(
                "#224: skip auto-crash bundle (cooldown active) thread=%s",
                thread_id,
            )
            return
        self._auto_crash_last_dispatch[thread_id] = now

        # Build the bundle. Run in executor so we don't block recovery.
        from .diagnostics import bundle as _bundle_mod
        crash_context = {
            "where": "render_response",
            "thread_id": thread_id,
            "exc_class": type(exc).__name__,
            "exc_message": str(exc)[:500],
            "session_id": getattr(bridge, "session_id", None),
            "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
        }
        loop = asyncio.get_running_loop()
        try:
            out_path = await loop.run_in_executor(
                None,
                functools.partial(
                    _bundle_mod.generate_bundle,
                    bot=self,
                    generated_by="auto-crash",
                    crash_context=crash_context,
                ),
            )
        except Exception:
            log.exception("#224: auto-crash bundle generation failed")
            return

        try:
            embed = discord.Embed(
                title="📋 Diagnostic bundle attached",
                description=(
                    "Renderer crashed mid-turn. Send this `.zip` to PM "
                    "for root-cause analysis.\n\n"
                    f"Crash: `{type(exc).__name__}`"
                ),
                color=COLOR_TOOL_FAILURE,
            )
            await safe_send_message(
                thread,
                embed=embed,
                file=discord.File(out_path),
            )
        except Exception:
            log.exception("#224: auto-crash bundle upload failed")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Console-script entry point: load config and run the bot."""
    # Order matters (#149 R2 engineer suggestion): create runtime dirs FIRST so
    # both _touch_heartbeat() and _configure_logging() can rely on them. Then
    # touch heartbeat BEFORE _configure_logging() — if logging setup ever
    # raises (e.g. disk full), we still want the external healthcheck to see a
    # fresh heartbeat instead of looping kickstart-quiet.
    _ensure_runtime_dirs()
    _touch_heartbeat()
    _configure_logging()

    # T2-A: make restart/crash causes DIAGNOSABLE. Investigation found the bot
    # restarts far too often (crash-loops throttled to launchd's 30s
    # ThrottleInterval), and the cause was invisible: StandardErrorPath is unset
    # (#168) and only INFO+ logging reaches clauded.log. We now capture:
    #   - uncaught Python exceptions (excepthook → clauded.log CRITICAL)
    #   - C-level faults / segfaults (faulthandler → its own file that survives)
    # plus a boot marker (pid) and a "main() exiting" marker below, so a
    # *graceful* shutdown (SIGTERM from a watchdog kickstart, which unwinds
    # cleanly) is distinguishable from a hard crash / OOM-kill (SIGKILL, which
    # leaves NEITHER marker — the tell-tale of an out-of-memory death).
    import faulthandler

    def _log_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical(
            "UNCAUGHT top-level exception — process exiting",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _log_uncaught
    try:
        faulthandler.enable(open(_LOG_DIR / "faulthandler.log", "a"))
    except Exception:
        log.debug("faulthandler enable failed", exc_info=True)

    # The in-loop _heartbeat_task takes over once setup_hook fires and
    # refreshes mtime every 30 s thereafter.
    log.info(
        "claudeD starting (launchd label: %s) pid=%s",
        _LAUNCHD_LABEL, os.getpid(),
    )

    # Resolve and log the operator's Claude CLI so it's visible at boot time.
    from .cli_paths import resolve_claude_cli

    resolved_cli = resolve_claude_cli()
    if resolved_cli:
        log.info("Using Claude CLI at: %s", resolved_cli)
    else:
        log.warning(
            "No system Claude CLI found at $PATH or fallback locations; "
            "falling back to SDK-bundled CLI (may be older than upstream). "
            "Install via `npm install -g @anthropic-ai/claude-code` or set "
            "$PATH to silence this warning."
        )

    config = load_config()
    bot = ClaudedBot(config)
    try:
        bot.run(config.discord_bot_token, log_handler=None)
    except discord.errors.PrivilegedIntentsRequired:
        log.warning(
            "Message Content Intent not enabled in Discord Developer Portal. "
            "Retrying without message_content intent — @mention triggers will "
            "not work, but slash commands will. Enable the intent at "
            "https://discord.com/developers/applications/ for full functionality."
        )
        # Retry with safe intents
        bot2 = ClaudedBot.__new__(ClaudedBot)
        commands.Bot.__init__(bot2, command_prefix="!", intents=_build_intents_safe())
        bot2.config = config
        bot2.project_manager = bot.project_manager
        bot2.session_manager = bot.session_manager
        bot2.cost_tracker = bot.cost_tracker
        bot2.agent_manager = bot.agent_manager
        bot2._start_time = bot._start_time
        bot2.run(config.discord_bot_token, log_handler=None)
    except Exception:
        # T2-A: a crash inside bot.run() (outside discord.py's own handling)
        # now leaves a CRITICAL breadcrumb instead of a silent 30s relaunch.
        log.critical("bot.run() terminated with an exception", exc_info=True)
        raise
    finally:
        # T2-A: reaching here means an ORDERLY shutdown (SIGTERM/graceful close
        # unwound the stack). If clauded.log ever stops WITHOUT this line, the
        # process was hard-killed — SIGKILL / OOM — which no in-process handler
        # can catch; correlate with memory pressure.
        log.info("claudeD main() exiting (pid=%s)", os.getpid())


if __name__ == "__main__":
    main()
