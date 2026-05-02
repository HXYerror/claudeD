"""Bridge to a single Claude Code SDK client session.

Each :class:`ClaudeBridge` wraps one ``ClaudeSDKClient`` connected to a
project directory. A bridge is created per Discord thread, used until the
session is stopped (manually or because the thread is unbound), and then
disconnected.

The bridge optionally accepts an ``on_ask_user`` callback that is invoked
whenever Claude calls the ``AskUserQuestion`` tool. The callback returns the
``updated_input`` dict (typically with an extra ``answers`` field) — that is
forwarded to the SDK as a :class:`PermissionResultAllow`. All other tools are
unconditionally allowed.

The bridge also supports an ``on_pre_tool_use`` callback that fires *before*
a tool executes (via SDK PreToolUse hooks). This gives callers early
notification — e.g. to post a "Preparing: ToolName…" message in Discord.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_code_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    ToolPermissionContext,
)

from .config import Config
from .session_config import SessionConfig

log = logging.getLogger("clauded.claude_bridge")


# Type alias for the async callback the bot supplies to handle
# ``AskUserQuestion`` tool invocations. It receives the raw tool input and
# returns either an updated input dict (forwarded to the SDK as
# ``PermissionResultAllow.updated_input``) or ``None`` to deny the call.
OnAskUser = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

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
        self.on_ask_user = sc.on_ask_user
        self._on_pre_tool_use = sc.on_pre_tool_use
        self._on_post_tool_use = sc.on_post_tool_use
        self._on_stop = sc.on_stop
        self.system_prompt = sc.system_prompt
        self._env = sc.env
        self._last_activity = time.time()
        self._start_time = time.time()
        self._model_override = sc.model_override
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
    def model(self) -> str:
        """Return the active model: explicit override, SDK-reported, or config default."""
        return self._model_override or self._sdk_model or self._config.claude_model

    @property
    def is_active(self) -> bool:
        """True iff the underlying client is currently connected."""
        return self._active

    async def start(self) -> None:
        """Create and connect the underlying ``ClaudeSDKClient``."""
        full_system_prompt = (self.system_prompt or "") + _CHANNEL_MGMT_PROMPT

        # Build extra_args for CLI-level flags
        extra_args: dict[str, str | None] = {}
        if self._effort:
            extra_args["effort"] = self._effort
        if self._max_budget_usd is not None:
            extra_args["max-budget-usd"] = str(self._max_budget_usd)
        if self._fork_session:
            extra_args["fork-session"] = None
        if self._from_pr:
            extra_args["from-pr"] = self._from_pr
        if self._worktree:
            extra_args["worktree"] = self._worktree
        if self._custom_agents:
            extra_args["agents"] = json.dumps(self._custom_agents)
        if self._agent_name:
            extra_args["agent"] = self._agent_name
        if self._fallback_model:
            extra_args["fallback-model"] = self._fallback_model
        if self._plugin_dirs:
            # Pass the first plugin dir; CLI supports --plugin-dir
            extra_args["plugin-dir"] = self._plugin_dirs[0]

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

        if _hooks_dict:
            hooks = _hooks_dict

        options = ClaudeCodeOptions(
            cwd=self.project_path,
            env=self._env or {},
            permission_mode=self._config.claude_permission_mode,
            model=self.model,
            can_use_tool=self._can_use_tool if self.on_ask_user else None,
            resume=self._resume_session_id,
            append_system_prompt=full_system_prompt,
            allowed_tools=self._allowed_tools,
            disallowed_tools=self._disallowed_tools,
            extra_args=extra_args,
            add_dirs=self._add_dirs,
            mcp_servers=self._mcp_servers or {},
            max_turns=self._max_turns,
            # Feature #60: SDK hooks
            hooks=hooks,
            # Feature #61: partial message streaming for token-level deltas
            include_partial_messages=True,
            settings=self._settings,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._client = client
        self._active = True
        log.info(
            "ClaudeBridge started for cwd=%s ask_user=%s resume=%s effort=%s hooks=%s partial=%s env=%s user=%s",
            self.project_path,
            bool(self.on_ask_user),
            self._resume_session_id,
            self._effort,
            bool(hooks),
            True,
            bool(self._env),
            self._user,
        )

    async def send_message(self, text: str) -> AsyncIterator[object]:
        """Send a user message and stream back response messages.

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
            await self._client.query(text)

            async for msg in self._client.receive_response():
                if isinstance(msg, ResultMessage):
                    self._update_stats(msg)
                yield msg
        except BaseException:
            self._active = False
            # Best-effort disconnect; if it fails, we still re-raise the
            # original exception is what callers care about.
            client = self._client
            self._client = None
            if client is not None:
                try:
                    await client.disconnect()
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
        """Disconnect the underlying client (idempotent)."""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # pragma: no cover - defensive
            log.exception("Error while disconnecting ClaudeSDKClient")
        finally:
            self._client = None
            self._active = False
            log.info("ClaudeBridge stopped for cwd=%s", self.project_path)

    # ------------------------------------------------------------------
    # SDK callbacks
    # ------------------------------------------------------------------

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

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK ``can_use_tool`` callback.

        Intercepts ``AskUserQuestion`` and forwards everything else.
        """
        if tool_name == "AskUserQuestion" and self.on_ask_user is not None:
            try:
                updated = await self.on_ask_user(tool_input)
            except Exception:
                log.exception("on_ask_user callback raised; denying tool call")
                return PermissionResultDeny(
                    message="Internal error showing question UI."
                )
            if updated is None:
                return PermissionResultDeny(
                    message="No response from user (timed out)."
                )
            return PermissionResultAllow(updated_input=updated)

        return PermissionResultAllow()


__all__ = [
    "ClaudeBridge",
    "OnAskUser",
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
