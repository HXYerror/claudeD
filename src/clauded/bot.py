"""Discord bot entrypoint for claudeD.

Wires up event handlers (on_ready, on_message) and registers slash command
groups (`/project`, `/session`). The on_message handler bridges Discord
messages to a per-thread :class:`ClaudeBridge` session.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import sys
import asyncio
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
from .cost_tracker import CostTracker
from .agent_manager import AgentManager
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
# _LOG_DIR and _CACHE_DIR live in _logging_setup.py (v1.18 stage-28 trim);
# imported above. _HEARTBEAT_PATH stays here because it belongs to the
# heartbeat task, not to logging setup.
_HEARTBEAT_PATH = _CACHE_DIR / "heartbeat"


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
    except OSError:
        pass


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

    async def setup_hook(self) -> None:
        """Register slash command groups and sync to Discord."""
        # Cache claude version (#86)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            self._claude_version = stdout.decode().strip() or "unknown"
        except Exception:
            self._claude_version = "unknown"
        self._cleanup_task.start()
        self._heartbeat_task.start()

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
            try:
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
        """
        _touch_heartbeat()

    async def on_ready(self) -> None:  # type: ignore[override]
        user = self.user
        log.info("Bot online as %s (id=%s)", user, getattr(user, "id", "?"))
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
                try:
                    await message.reply(UNBOUND_REFUSE_MESSAGE)
                except discord.HTTPException:
                    log.debug("Could not surface unbound-refuse hint")
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
            try:
                if not is_forum:
                    await channel.send(
                        "❌ I don't have permission to create threads in this channel."
                    )
            except discord.HTTPException:
                log.debug("Could not surface thread-permission error to channel")
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
                try:
                    if not is_forum:
                        await channel.send("❌ Failed to create a thread for this message.")
                except discord.HTTPException:
                    log.debug("Could not surface thread-creation error to channel")
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
                    try:
                        await thread.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                    except Exception:
                        pass  # best-effort; don't break the stream

                async def _post_tool_notify(tool_name: str, input_data: dict) -> None:
                    pass  # logged by bridge already

                async def _stop_notify(input_data: dict) -> None:
                    reason = input_data.get("stop_reason", "unknown")
                    log.info("Session stopped: %s", reason)

                env_vars = self.project_manager.get_env(channel.id)
                _notify = self._notify_enabled.get(thread.id, self._pre_tool_notifications)
                sc = SessionConfig(
                    system_prompt=system_prompt,
                    on_ask_user=handler.handle_ask_user_question,
                    on_pre_tool_use=_pre_tool_notify if _notify else None,
                    on_post_tool_use=_post_tool_notify,
                    on_stop=_stop_notify,
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

            # Feature #66: Add hourglass reaction
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass

            user_text, tmp_dir = await self._compose_user_text(message)
            # Use mention-stripped content instead of raw message content
            if tmp_dir is not None:
                # Attachments present: replace raw content portion with stripped content
                user_text = user_text.replace(message.content, content) if message.content else user_text
            else:
                user_text = content
            renderer = DiscordRenderer(thread, bot=self)
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
                response_cost = cost_after - cost_before
                if response_cost > 0:
                    self.cost_tracker.record(channel.id, response_cost)
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
                try:
                    await message.reply(UNBOUND_REFUSE_MESSAGE)
                except discord.HTTPException:
                    log.debug("Could not surface unbound-refuse hint in thread")
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
                    stored_prompt = stored.get("system_prompt") if stored else None
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
                        try:
                            await _thread_target.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                        except Exception:
                            pass  # best-effort; don't break the stream

                    async def _post_tool_notify_thread(tool_name: str, input_data: dict) -> None:
                        pass  # logged by bridge already

                    async def _stop_notify_thread(input_data: dict) -> None:
                        reason = input_data.get("stop_reason", "unknown")
                        log.info("Session stopped: %s", reason)

                    env_vars = self.project_manager.get_env(parent_id)
                    _notify = self._notify_enabled.get(thread_id, self._pre_tool_notifications)
                    sc = SessionConfig(
                        system_prompt=stored_prompt or system_prompt,
                        model_override=None,  # #210: ephemeral; see note above
                        permission_mode_override=stored_perm_mode,  # #211: persistent
                        resume_session_id=resume_id,
                        on_ask_user=handler.handle_ask_user_question,
                        on_pre_tool_use=_pre_tool_notify_thread if _notify else None,
                        on_post_tool_use=_post_tool_notify_thread,
                        on_stop=_stop_notify_thread,
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

            # Feature #66: Add hourglass reaction
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass

            user_text, tmp_dir = await self._compose_user_text(message)
            renderer = DiscordRenderer(message.channel, bot=self)
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
                response_cost = cost_after - cost_before
                if response_cost > 0:
                    self.cost_tracker.record(parent_id, response_cost)
                self.session_manager.save_session_state(thread_id)
                if _render_ok:
                    await safe_remove_reaction(message, "⏳", self.user)
                    await safe_add_reaction(message, "✅")

    # ------------------------------------------------------------------
    # Helpers used by both channel- and thread-message handlers
    # ------------------------------------------------------------------

    async def _compose_user_text(
        self, message: discord.Message
    ) -> tuple[str, Path | None]:
        """Build the text prompt sent to Claude, including any attachments.

        For each attachment we download it to a per-message temp directory
        and prepend a line announcing the filename and on-disk path so
        Claude can choose to ``Read`` it. The temp directory is returned
        alongside the prompt so the caller can clean it up after Claude
        finishes processing the message. Discord caps attachment size at
        25MB on free guilds, so the on-disk footprint is bounded.
        """
        text = message.content or ""
        attachments = list(message.attachments or [])
        if not attachments:
            return text, None

        tmp_dir = Path(tempfile.mkdtemp(prefix="clauded_att_"))
        notes: list[str] = []
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
            if ext in _IMAGE_EXTENSIONS:
                notes.append(f"[User attached image: {safe_name}]\nImage file saved at: {target}")
            else:
                notes.append(f"[User attached file: {safe_name}]\nFile saved at: {target}")

        if not notes:
            # No attachments actually saved — drop the empty tmp dir now.
            _cleanup_tmp_dir(tmp_dir)
            return text, None
        # Prepend so Claude sees the file references before the user's prose.
        prefix = "\n".join(notes)
        composed = f"{prefix}\n\n{text}" if text else prefix
        return composed, tmp_dir

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
                except Exception:
                    pass
            pre_tool_cb = _pre_tool_notify

        async def _post_tool_notify(tool_name: str, input_data: dict) -> None:
            pass  # logged by bridge already

        async def _stop_notify(input_data: dict) -> None:
            reason = input_data.get("stop_reason", "unknown")
            log.info("Session stopped: %s", reason)

        sc = SessionConfig(
            system_prompt=self.project_manager.get_system_prompt(parent_id),
            add_dirs=self.project_manager.get_extra_dirs(parent_id) or None,
            mcp_servers=self.project_manager.get_mcp_servers(parent_id) or None,
            env=self.project_manager.get_env(parent_id) or None,
            on_ask_user=InteractionHandler(interaction.channel).handle_ask_user_question,
            on_pre_tool_use=pre_tool_cb,
            on_post_tool_use=_post_tool_notify,
            on_stop=_stop_notify,
            **overrides,
        )

        lock = self.session_manager.get_lock(thread_id)
        async with lock:
            await self.session_manager.stop_session(thread_id)
            try:
                bridge = await self.session_manager.create_session(
                    thread_id, project_path, self.config, sc,
                )
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
        user_text: str,
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
            await renderer.render_response(bridge, user_text, author_id=author_id)
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
                    new_renderer = DiscordRenderer(thread, bot=self)
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
    # The in-loop _heartbeat_task takes over once setup_hook fires and
    # refreshes mtime every 30 s thereafter.
    log.info("claudeD starting (launchd label: %s)", _LAUNCHD_LABEL)

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


if __name__ == "__main__":
    main()
