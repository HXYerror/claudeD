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
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_code_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .config import Config

log = logging.getLogger("clauded.claude_bridge")


# Type alias for the async callback the bot supplies to handle
# ``AskUserQuestion`` tool invocations. It receives the raw tool input and
# returns either an updated input dict (forwarded to the SDK as
# ``PermissionResultAllow.updated_input``) or ``None`` to deny the call.
OnAskUser = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


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
        on_ask_user: OnAskUser | None = None,
        system_prompt: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        effort: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        fork_session: bool = False,
        add_dirs: list[str] | None = None,
        from_pr: str | None = None,
        worktree: str | None = None,
        agent_name: str | None = None,
        custom_agents: dict | None = None,
        mcp_servers: dict | None = None,
        max_turns: int | None = None,
        fallback_model: str | None = None,
        plugin_dirs: list[str] | None = None,
        settings: str | None = None,
    ) -> None:
        self.project_path = project_path
        self._config = config
        self.on_ask_user = on_ask_user
        self.system_prompt = system_prompt
        self._model_override = model_override
        self._resume_session_id = resume_session_id
        self._effort = effort
        self._allowed_tools = allowed_tools or []
        self._disallowed_tools = disallowed_tools or []
        self._max_budget_usd = max_budget_usd
        self._fork_session = fork_session
        self._add_dirs = add_dirs
        self._from_pr = from_pr
        self._worktree = worktree
        self._agent_name = agent_name
        self._custom_agents = custom_agents
        self._mcp_servers = mcp_servers
        self._max_turns = max_turns
        self._fallback_model = fallback_model
        self._plugin_dirs = plugin_dirs or []
        self._settings = settings
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

        options = ClaudeCodeOptions(
            cwd=self.project_path,
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
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._client = client
        self._active = True
        log.info(
            "ClaudeBridge started for cwd=%s ask_user=%s resume=%s effort=%s",
            self.project_path,
            bool(self.on_ask_user),
            self._resume_session_id,
            self._effort,
        )

    async def send_message(self, text: str) -> AsyncIterator[object]:
        """Send a user message and stream back response messages.

        Yields the raw SDK message objects (``AssistantMessage``,
        ``ResultMessage``, etc.) so callers can decide how to render them.

        If the underlying SDK raises, the bridge marks itself inactive so
        callers can detect the dead session and recreate it on the next
        request. The exception is re-raised so the renderer can surface it.
        """
        if self._client is None or not self._active:
            raise RuntimeError("ClaudeBridge.send_message called before start()")

        try:
            await self._client.query(text)
            async for msg in self._client.receive_response():
                # Update session stats opportunistically — ResultMessage
                # carries the per-turn totals from the SDK.
                if isinstance(msg, ResultMessage):
                    self._update_stats(msg)
                yield msg
        except Exception:
            log.exception("Claude SDK stream failed; marking bridge inactive")
            self._active = False
            # Best-effort: tear down the underlying client so we don't leak
            # a half-broken connection. Swallow disconnect errors — the
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
    # Re-export for convenience so callers can ``isinstance`` against the
    # message/block types without importing the SDK directly.
    "AssistantMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ResultMessage",
]
