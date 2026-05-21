"""Bridge to a single Claude Code SDK client session.

Each :class:`ClaudeBridge` wraps one ``ClaudeSDKClient`` connected to a
project directory. A bridge is created per Discord thread, used until the
session is stopped (manually or because the thread is unbound), and then
disconnected.

The bridge intercepts ``AskUserQuestion`` via ``can_use_tool``. The
SDK's native control-protocol handler (v0.1.80+) emits the correct
``{"behavior": "allow", "updatedInput": {...}}`` envelope, so no
monkey-patch is needed.

The bridge also supports an ``on_pre_tool_use`` callback that fires *before*
a tool executes (via SDK PreToolUse hooks). This gives callers early
notification — e.g. to post a "Preparing: ToolName…" message in Discord.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    SdkPluginConfig,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    ToolPermissionContext,
)

from .cli_paths import resolve_claude_cli
from .config import Config
from .session_config import SessionConfig
from . import stream_logger

log = logging.getLogger("clauded.claude_bridge")


# Type alias for the PreToolUse notification callback. Receives the tool
# name and the raw tool input dict before execution begins.
OnPreToolUse = Callable[[str, dict[str, Any]], Awaitable[None]]

# Type alias for the PostToolUse notification callback. Receives the tool
# name and the raw tool input dict after execution completes.
OnPostToolUse = Callable[[str, dict[str, Any]], Awaitable[None]]

# Type alias for the Stop hook callback. Receives the raw input data when
# Claude stops.
OnStop = Callable[[dict[str, Any]], Awaitable[None]]


_CHANNEL_MGMT_PROMPT = """
You can create Discord threads and channels by including these markers in your output:
- [CREATE_THREAD: thread name] — creates a new thread in the current channel
- [CREATE_CHANNEL: channel-name] — creates a new text channel in the server

The system will detect these markers and execute them. You will see the result in the chat.
Only use these when the user explicitly asks to create threads or channels.
"""


class ClaudeBridge:
    """Wrapper around a ``ClaudeSDKClient`` for a single project session."""

    def __init__(
        self,
        project_path: str,
        config: Config,
        session_config: SessionConfig | None = None,
    ) -> None:
        sc = session_config or SessionConfig()
        self.project_path = project_path
        self._config = config
        self._session_config = sc
        self._on_pre_tool_use = sc.on_pre_tool_use
        self._on_post_tool_use = sc.on_post_tool_use
        self._on_stop = sc.on_stop
        self.on_ask_user = sc.on_ask_user
        self.system_prompt = sc.system_prompt
        self._env = sc.env
        self._last_activity = time.time()
        self._start_time = time.time()
        self._model_override = sc.model_override
        # #211: per-session override for ``permission_mode``. Initialized
        # from the SessionConfig (which auto-resume / /session resume read
        # from ``data/sessions.json`` so the user's choice survives a bot
        # restart). When None, the SDK call uses ``config.claude_permission_mode``
        # (CLAUDE_PERMISSION_MODE env or ``"default"``).
        self._permission_mode_override: str | None = sc.permission_mode_override
        self._resume_session_id = sc.resume_session_id
        self._effort = sc.effort
        self._allowed_tools = list(sc.allowed_tools) if sc.allowed_tools else []
        self._disallowed_tools = list(sc.disallowed_tools) if sc.disallowed_tools else []
        self._max_budget_usd = sc.max_budget_usd
        self._fork_session = sc.fork_session
        self._add_dirs = sc.add_dirs
        self._from_pr = sc.from_pr
        self._worktree = sc.worktree
        self._agent_name = sc.agent_name
        self._custom_agents = sc.custom_agents
        self._mcp_servers = sc.mcp_servers
        self._max_turns = sc.max_turns
        self._fallback_model = sc.fallback_model
        self._plugin_dirs = list(sc.plugin_dirs) if sc.plugin_dirs else []
        self._settings = sc.settings
        self._user = sc.user
        self._bare = sc.bare
        self._session_name = sc.session_name
        self._client: ClaudeSDKClient | None = None
        self._active = False
        self._session_id: str | None = None
        # Aggregate stats updated whenever we observe a ResultMessage. They
        # are purely informational (surfaced via /session info) so the
        # exact semantics — total cost across the session, last-known turn
        # count and model — are good enough.
        self.total_cost: float = 0.0
        self.num_turns: int = 0
        self._sdk_model: str | None = None

    @property
    def session_id(self) -> str | None:
        """Return the session ID from the last ResultMessage."""
        return self._session_id

    @property
    def config(self) -> Config:
        return self._config

    @config.setter
    def config(self, value: Config) -> None:
        self._config = value

    @property
    def model(self) -> str | None:
        """Return the active model for this session, or ``None`` if none is bound.

        Tier precedence (#198):
        1. ``_model_override`` — user-explicit ``/model switch <name>``
        2. ``_sdk_model`` — model reported by the SDK on the first
           ``ResultMessage`` (display-only; NOT used as SDK input — see
           ``start()`` for why). Once observed, this is the most
           specific/accurate value (a full model id like
           ``claude-sonnet-4-5`` rather than the ``sonnet`` alias).
        3. ``_config.claude_model`` — admin/ops ``CLAUDE_MODEL`` env var
        4. ``None`` — pre-first-turn with no override and no env var. The
           SDK call omits ``model=`` so the CLI's ``~/.claude/settings.json``
           default is used, matching terminal ``claude`` behavior.

        Note: callers that need to distinguish the four cases (e.g.
        ``/model current``) should inspect the tier fields directly rather
        than this collapsed property.
        """
        return (
            self._model_override
            or self._sdk_model
            or self._config.claude_model
        )

    @property
    def explicit_model_override(self) -> str | None:
        """The user-explicit ``/model switch <name>`` value, or ``None``.

        Public read-only accessor for ``_model_override`` so consumers that
        need to persist *only* the user's explicit choice (e.g.,
        :class:`SessionManager.save_session_state`) can avoid the
        collapsed :attr:`model` property, which would write back the
        SDK-observed ``_sdk_model`` and form a cross-restart input loop
        (#198 PRD §Design line 92). Returns ``None`` when the user has
        not switched and the SDK's CLI-default should govern.
        """
        return self._model_override

    @property
    def permission_mode_override(self) -> str | None:
        """The user-explicit ``/mode set`` (or ``/mode cycle``) value, or ``None``.

        #211: read-only accessor used by
        :meth:`SessionManager.save_session_state` so we persist ONLY what
        the user explicitly set — not the env or default fallback. Mirror
        of :attr:`explicit_model_override` but for permission mode.

        Returns ``None`` when the user has never run ``/mode set`` / cycle
        on this thread (or on a previous-restart thread we haven't re-seen
        since); callers fall back to env (``CLAUDE_PERMISSION_MODE``) or
        ``"default"`` via :attr:`effective_permission_mode`.
        """
        return self._permission_mode_override

    @property
    def effective_permission_mode(self) -> str:
        """Return the active permission mode: override > config > ``"default"``.

        #211: collapsed accessor used by the footer renderer and ``/mode
        current`` display. Always returns a non-empty string (the SDK's
        ``PermissionMode`` literal contract), so callers can compare
        ``!= "default"`` without a None check.

        Tier order:
        1. ``_permission_mode_override`` — user ran ``/mode set`` or ``cycle``
        2. ``_config.claude_permission_mode`` — ``CLAUDE_PERMISSION_MODE`` env,
           or its ``"default"`` fallback (set by ``load_config``)
        """
        return (
            self._permission_mode_override
            or self._config.claude_permission_mode
            or "default"
        )

    async def set_permission_mode(self, mode: str) -> None:
        """Runtime switch via the SDK's control-plane.

        #211: surfaces ``ClaudeSDKClient.set_permission_mode`` so user-
        facing ``/mode set`` / ``/mode cycle`` can flip the mode mid-
        session without recreating the bridge (which would lose context).
        Caller is responsible for passing a valid SDK ``PermissionMode``
        literal — the SDK will raise if it doesn't recognize the value.

        Persists the new value to ``_permission_mode_override`` so
        :attr:`effective_permission_mode` and the footer reflect the
        change. The SDK call happens FIRST so a rejection from the
        underlying CLI leaves our override unchanged (no lying display).
        """
        if self._client is None or not self._active:
            raise RuntimeError("bridge not active")
        await self._client.set_permission_mode(mode)
        self._permission_mode_override = mode

    async def set_model(self, model: str) -> None:
        """Runtime model switch via the SDK's control-plane.

        #273: surfaces ``ClaudeSDKClient.set_model`` so user-facing
        ``/model switch`` can flip the model mid-session without
        recreating the bridge (which would lose context). Symmetric
        with :meth:`set_permission_mode`.

        Persists the new value to ``_model_override`` so :attr:`model`
        and downstream consumers reflect the change. The SDK call
        happens FIRST so a rejection from the underlying CLI leaves
        our override unchanged (no lying display).
        """
        if self._client is None or not self._active:
            raise RuntimeError("bridge not active")
        await self._client.set_model(model)
        self._model_override = model

    @property
    def is_active(self) -> bool:
        """True iff the underlying client is currently connected."""
        return self._active

    def _build_mcp_servers(self) -> dict:
        """Merge user-configured MCP servers with the in-process scheduler server.

        #241: always include the ``clauded-scheduler`` in-process MCP server
        so claude can manage schedules in any session/turn (PRD §3.7 — the
        tool surface is intentionally global, not hot-loaded per slash). The
        scheduler server is best-effort: if its build raises (e.g. SDK
        version skew), we log a warning and continue without it rather than
        crashing every session start.
        """
        merged: dict = dict(self._mcp_servers or {})
        try:
            from .scheduler_mcp import build_scheduler_mcp_server
            merged["clauded-scheduler"] = build_scheduler_mcp_server()
        except Exception as exc:
            log.warning(
                "#241: failed to build scheduler MCP server; schedule_* "
                "tools will not be available: %s",
                exc,
            )
        return merged

    async def get_server_info(self) -> dict | None:
        """Return cached server init info, or ``None`` if not connected.

        Public wrapper for ``_client.get_server_info()`` — keeps callers
        out of the bridge's private state (cog/skill.py uses this for
        ``/skill list``).

        This is a cache read of the SDK's ``_initialization_result``;
        the call performs no I/O and is safe to invoke concurrently
        with an in-flight ``send_message`` stream on the same client.
        A future SDK refactor could break that assumption — callers
        should still wrap this in ``try/except`` and degrade gracefully.
        """
        client = self._client
        if client is None or not self._active:
            return None
        # #223: instrument control-plane call so /log dump (#224) and
        # observability can see when SDK init info is requested / what
        # it returns. Failure raises (existing contract); we don't catch.
        log.debug("get_server_info -> requesting")
        try:
            result = await client.get_server_info()
            log.debug(
                "get_server_info -> %s (%d keys)",
                "None" if result is None else "dict",
                len(result or {}),
            )
            if stream_logger.is_enabled():
                stream_logger.log_event({
                    "type": "ControlPlane",
                    "method": "get_server_info",
                    "result_keys": list(result.keys()) if result else None,
                })
            return result
        except Exception:
            log.warning("get_server_info failed", exc_info=True)
            if stream_logger.is_enabled():
                stream_logger.log_event({
                    "type": "ControlPlane",
                    "method": "get_server_info",
                    "error": True,
                })
            raise

    async def get_context_usage(self) -> dict | None:
        """Return current context-window usage, or ``None`` if not connected.

        Public wrapper for ``_client.get_context_usage()`` (added in v1.18
        for ``/context`` slash command, #163 sub-task 3). Like
        ``get_server_info``, this keeps callers out of the bridge's private
        state.

        Unlike ``get_server_info`` (which is a cached init-result read),
        ``get_context_usage`` makes an actual SDK request to compute current
        token counts. It's safe to invoke alongside an active ``send_message``
        stream, but the call may be slower (~tens of ms).
        """
        client = self._client
        if client is None or not self._active:
            return None
        # #223: this was the #220 footer-🧠-always-0% bug's blind spot —
        # neither success nor failure left a log line. Now: success at
        # DEBUG, failure at WARNING with exc_info, plus a ControlPlane
        # event in stream-debug.jsonl when enabled.
        log.debug("get_context_usage -> requesting")
        try:
            result = await client.get_context_usage()
            log.debug("get_context_usage -> %r", result)
            if stream_logger.is_enabled():
                stream_logger.log_event({
                    "type": "ControlPlane",
                    "method": "get_context_usage",
                    "result_pct": (result or {}).get("percentage"),
                    "result_keys": list(result.keys()) if result else None,
                })
            return result
        except Exception:
            log.warning("get_context_usage failed", exc_info=True)
            if stream_logger.is_enabled():
                stream_logger.log_event({
                    "type": "ControlPlane",
                    "method": "get_context_usage",
                    "error": True,
                })
            raise

    async def start(self) -> None:
        """Create and connect the underlying ``ClaudeSDKClient``."""
        full_system_prompt = (self.system_prompt or "") + _CHANNEL_MGMT_PROMPT
        if self._user:
            safe_user = self._user.replace("\n", " ").replace("\r", " ")
            full_system_prompt += "\nThe Discord user talking to you is: " + safe_user

        # extra_args holds CLI-only flags with no native ClaudeAgentOptions
        # equivalent in claude-agent-sdk 0.1.80. Native fields (effort,
        # max_budget_usd, fork_session, agents, fallback_model, plugins) are
        # passed directly below.
        extra_args: dict[str, str | None] = {}
        if self._from_pr:
            extra_args["from-pr"] = self._from_pr
        if self._worktree:
            extra_args["worktree"] = self._worktree
        if self._agent_name:
            extra_args["agent"] = self._agent_name
        if self._bare:
            extra_args["bare"] = None
        if self._session_name:
            extra_args["name"] = self._session_name

        # ------------------------------------------------------------------
        # Feature #60: PreToolUse hook for early notification
        # ------------------------------------------------------------------
        hooks: dict[str, list[HookMatcher]] | None = None
        _hooks_dict: dict[str, list[HookMatcher]] = {}

        if self._on_pre_tool_use is not None:
            on_pre = self._on_pre_tool_use  # capture for closure

            async def _hook_pre_tool(
                input_data: dict[str, Any],
                tool_use_id: str | None,
                context: HookContext,
            ) -> dict[str, Any]:
                tool_name = input_data.get("tool_name", "unknown")
                try:
                    await on_pre(tool_name, input_data)
                except Exception:
                    log.debug("on_pre_tool_use callback raised; ignoring", exc_info=True)
                return {}  # empty dict = continue normally

            _hooks_dict["PreToolUse"] = [HookMatcher(matcher=None, hooks=[_hook_pre_tool])]

        if self._on_post_tool_use is not None:
            on_post = self._on_post_tool_use  # capture for closure

            async def _hook_post_tool(
                input_data: dict[str, Any],
                tool_use_id: str | None,
                context: HookContext,
            ) -> dict[str, Any]:
                tool_name = input_data.get("tool_name", "unknown")
                try:
                    await on_post(tool_name, input_data)
                except Exception:
                    log.debug("on_post_tool_use callback raised; ignoring", exc_info=True)
                return {}

            _hooks_dict["PostToolUse"] = [HookMatcher(matcher=None, hooks=[_hook_post_tool])]

        if self._on_stop is not None:
            on_stop_cb = self._on_stop  # capture for closure

            async def _hook_stop(
                input_data: dict[str, Any],
                tool_use_id: str | None,
                context: HookContext,
            ) -> dict[str, Any]:
                try:
                    await on_stop_cb(input_data)
                except Exception:
                    log.debug("on_stop callback raised; ignoring", exc_info=True)
                return {}

            _hooks_dict["Stop"] = [HookMatcher(matcher=None, hooks=[_hook_stop])]

        # --- PreCompact hook: notified before context compression ---
        async def _hook_pre_compact(
            input_data: dict[str, Any],
            tool_use_id: str | None,
            context: HookContext,
        ) -> dict[str, Any]:
            log.info("Pre-compact triggered")
            return {}

        _hooks_dict["PreCompact"] = [HookMatcher(matcher=None, hooks=[_hook_pre_compact])]

        # --- UserPromptSubmit hook: log user prompt submissions ---
        async def _hook_user_prompt(
            input_data: dict[str, Any],
            tool_use_id: str | None,
            context: HookContext,
        ) -> dict[str, Any]:
            log.debug("UserPromptSubmit: %s", str(input_data)[:200])
            return {}

        _hooks_dict["UserPromptSubmit"] = [HookMatcher(matcher=None, hooks=[_hook_user_prompt])]

        # --- SubagentStop hook: notified when a subagent stops ---
        async def _hook_subagent_stop(
            input_data: dict[str, Any],
            tool_use_id: str | None,
            context: HookContext,
        ) -> dict[str, Any]:
            log.info("Subagent stopped: %s", str(input_data)[:200])
            if self._session_config.on_stop:
                try:
                    await self._session_config.on_stop(input_data)
                except Exception:
                    log.debug("on_stop callback raised in SubagentStop; ignoring", exc_info=True)
            return {}

        _hooks_dict["SubagentStop"] = [HookMatcher(matcher=None, hooks=[_hook_subagent_stop])]

        # Always assign hooks dict (we now unconditionally register PreCompact etc.)
        hooks = _hooks_dict

        # Resolve operator's Claude CLI so the SDK uses the system install
        # rather than the bundled binary (#119). When None, the SDK falls
        # back to its own bundled CLI.
        cli_path = resolve_claude_cli()

        # #198: only pass ``model=`` to the SDK when the user has explicitly
        # chosen one (``/model switch``) or the operator has pinned via
        # ``CLAUDE_MODEL``. Otherwise omit it so the SDK/CLI reads its own
        # default from ``~/.claude/settings.json`` — same as terminal
        # ``claude``. We deliberately do NOT use ``_sdk_model`` here: it's
        # display-only (the model the SDK reported back on the first
        # ``ResultMessage``); using it as input would lock the session into
        # whatever was resolved on turn 1 even if settings.json changes.
        chosen_model = self._model_override or self._config.claude_model

        options = ClaudeAgentOptions(
            cwd=self.project_path,
            env=self._env or {},
            # #211: per-session override > config (env / default). Use the
            # same accessor the runtime ``set_permission_mode()`` updates so
            # an auto-resumed session with a persisted override re-enters the
            # bridge with the right mode on the very first turn (not just
            # after the user re-runs ``/mode set``).
            permission_mode=self.effective_permission_mode,
            model=chosen_model,
            resume=self._resume_session_id,
            # R3 (#116): system_prompt preset dict replaces append_system_prompt
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": full_system_prompt,
            },
            allowed_tools=self._allowed_tools,
            disallowed_tools=self._disallowed_tools,
            extra_args=extra_args,
            add_dirs=self._add_dirs,
            mcp_servers=self._build_mcp_servers(),
            max_turns=self._max_turns,
            # Feature #60: SDK hooks
            hooks=hooks,
            # Feature #61: partial message streaming for token-level deltas
            include_partial_messages=True,
            settings=self._settings,
            # R4 (#117): setting_sources defaults to [] in v1.10 SDK (no
            # auto-load); pass all three explicitly to preserve v1.x
            # behavior of loading user CLAUDE.md, user-level skills, and
            # project settings (#111).
            setting_sources=["user", "project", "local"],
            # AskUserQuestion: wire can_use_tool when on_ask_user is set
            can_use_tool=self._can_use_tool if self.on_ask_user else None,
            # R6 (#119): explicit cli_path; None ⇒ SDK uses bundled CLI
            cli_path=cli_path,
            # R5 (#118): native fields migrated from extra_args
            effort=self._effort,
            max_budget_usd=(
                float(self._max_budget_usd) if self._max_budget_usd is not None else None
            ),
            fork_session=self._fork_session or None,
            agents=self._custom_agents or None,
            fallback_model=self._fallback_model,
            plugins=(
                [SdkPluginConfig(type="local", path=d) for d in self._plugin_dirs]
                if self._plugin_dirs
                else None
            ),
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._client = client
        self._active = True
        log.info(
            "ClaudeBridge started for cwd=%s resume=%s effort=%s hooks=%s partial=%s env=%s user=%s",
            self.project_path,
            self._resume_session_id,
            self._effort,
            bool(hooks),
            True,
            bool(self._env),
            self._user,
        )

    async def send_message(self, content: str | list[dict]) -> AsyncIterator[object]:
        """Send a user message and stream back response messages.

        Accepts two content shapes:

        * ``str`` — legacy plain-text path. Forwarded straight to
          ``client.query(text)`` so we keep working with all existing
          text-only callers.
        * ``list[dict]`` — Anthropic Messages-API content blocks (inline
          images + text). Wrapped into an async-iterable raw user
          message envelope so the SDK transmits it as user message
          ``content`` directly, putting images in the primary vision
          channel instead of via Read-tool tool_result (#242 round 2,
          spike-verified).

        Yields the raw SDK message objects (``AssistantMessage``,
        ``ResultMessage``, ``StreamEvent``, etc.) so callers can decide how
        to render them.

        If the underlying SDK raises, the bridge marks itself inactive so
        callers can detect the dead session and recreate it on the next
        request. The exception is re-raised so the renderer can surface it.
        """
        if self._client is None or not self._active:
            raise RuntimeError("ClaudeBridge.send_message called before start()")

        self._last_activity = time.time()

        try:
            if isinstance(content, str):
                await self._client.query(content)
            else:
                # #242 round 2: structured-content path. SDK Python types
                # don't formally declare image content blocks, but the
                # wire protocol passes the raw dict through to the CLI
                # binary which forwards verbatim to the Anthropic API.
                # Spike-verified working end-to-end (see #242 comment
                # "Spike 3").
                async def _stream():
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": content},
                    }
                await self._client.query(_stream())

            async for msg in self._client.receive_response():
                if isinstance(msg, ResultMessage):
                    self._update_stats(msg)
                yield msg
        except GeneratorExit:
            # Caller broke out of the async-for loop. DON'T try to
            # disconnect here — the SDK's anyio TaskGroup can't be
            # closed from a different task, which causes a crash.
            # The session stays active for future messages.  If cleanup
            # is needed, the caller should explicitly call bridge.stop().
            return
        except BaseException:
            self._active = False
            # Best-effort disconnect; if it fails, we still re-raise the
            # original exception which is what callers care about.
            #
            # #173 fix: wrap ``disconnect()`` in ``asyncio.wait_for(timeout)``
            # matching the ``stop()`` path's protection (#146). The SDK's
            # ``disconnect()`` can deadlock on anyio cross-task cancel scope
            # (verified upstream). Without the timeout, this exception path
            # would hang the current user's Discord turn forever — the same
            # frozen-UI symptom that #145 documented. We reuse the same env
            # var so operators only tune one knob.
            client = self._client
            self._client = None
            if client is not None:
                timeout = float(os.environ.get("CLAUDED_BRIDGE_STOP_TIMEOUT", "30"))
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=timeout)
                except asyncio.TimeoutError:
                    log.warning(
                        "send_message error-path disconnect timed out after %ss; "
                        "force-dropping (subprocess may leak)",
                        timeout,
                    )
                except Exception:  # pragma: no cover - defensive
                    log.exception(
                        "Error disconnecting ClaudeSDKClient after stream failure"
                    )
            raise

    async def interrupt(self) -> bool:
        """Interrupt the current Claude operation. Returns True if interrupted."""
        if not self._active or self._client is None:
            return False
        try:
            await self._client.interrupt()
            return True
        except Exception:
            log.warning("Failed to interrupt Claude session", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop the bridge, force-dropping after CLAUDED_BRIDGE_STOP_TIMEOUT (default 30s).

        #146: ``client.disconnect()`` is anyio-fragile and can hang. We bound
        it with ``asyncio.wait_for``; on timeout we force-drop the reference
        and log a WARN. The subprocess may leak but the cleanup task no
        longer deadlocks.
        """
        if self._client is None:
            return
        timeout = float(os.environ.get("CLAUDED_BRIDGE_STOP_TIMEOUT", "30"))
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Bridge stop timed out; force-dropping reference (subprocess may leak)",
                extra={"timeout_s": timeout, "active": self._active},
            )
        except Exception:  # pragma: no cover - defensive
            log.exception("Error while disconnecting ClaudeSDKClient")
        finally:
            self._client = None
            self._active = False
            log.info("ClaudeBridge stopped for cwd=%s", self.project_path)

    # ------------------------------------------------------------------
    # SDK callbacks
    # ------------------------------------------------------------------

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Handle tool permission requests from the CLI.

        For ``AskUserQuestion``, delegate to the ``on_ask_user`` callback
        which renders Discord UI and collects user responses. The answers
        are returned as ``updated_input`` so the CLI sees them.

        All other tools are auto-approved with ``PermissionResultAllow()``.
        """
        if tool_name == "AskUserQuestion" and self.on_ask_user is not None:
            try:
                updated = await self.on_ask_user(tool_input)
            except Exception:
                log.exception("on_ask_user callback failed")
                return PermissionResultDeny(message="AskUserQuestion handler error")
            if updated is None:
                return PermissionResultDeny(
                    message="User did not respond in time"
                )
            return PermissionResultAllow(updated_input=updated)

        # Auto-approve all other tools
        return PermissionResultAllow()

    def _update_stats(self, msg: ResultMessage) -> None:
        """Pull per-turn totals off a ``ResultMessage`` into instance state.

        The Claude SDK exposes ``total_cost_usd``, ``num_turns`` and
        ``model`` on ``ResultMessage``. We tolerate any of those being
        missing — newer/older SDKs may rename fields, and we'd rather
        surface partial stats than crash the stream.
        """
        # Extract session_id from ResultMessage
        sid = getattr(msg, "session_id", None)
        if isinstance(sid, str) and sid:
            self._session_id = sid

        cost = getattr(msg, "total_cost_usd", None)
        if isinstance(cost, (int, float)):
            # ResultMessage carries the cumulative cost of the whole
            # conversation so we replace rather than accumulate.
            self.total_cost = float(cost)
        turns = getattr(msg, "num_turns", None)
        if isinstance(turns, int):
            self.num_turns = turns
        model = getattr(msg, "model", None)
        if isinstance(model, str) and model:
            self._sdk_model = model


__all__ = [
    "ClaudeBridge",
    "OnPreToolUse",
    "OnPostToolUse",
    "OnStop",
    # Re-export for convenience so callers can ``isinstance`` against the
    # message/block types without importing the SDK directly.
    "AssistantMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ResultMessage",
]
