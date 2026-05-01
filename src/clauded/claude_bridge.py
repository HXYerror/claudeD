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

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
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


class ClaudeBridge:
    """Wrapper around a ``ClaudeSDKClient`` for a single project session."""

    def __init__(
        self,
        project_path: str,
        config: Config,
        on_ask_user: OnAskUser | None = None,
    ) -> None:
        self.project_path = project_path
        self.config = config
        self.on_ask_user = on_ask_user
        self._client: ClaudeSDKClient | None = None
        self._active = False
        # Aggregate stats updated whenever we observe a ResultMessage. They
        # are purely informational (surfaced via /session info) so the
        # exact semantics — total cost across the session, last-known turn
        # count and model — are good enough.
        self.total_cost: float = 0.0
        self.num_turns: int = 0
        self.model: str | None = None

    @property
    def is_active(self) -> bool:
        """True iff the underlying client is currently connected."""
        return self._active

    async def start(self) -> None:
        """Create and connect the underlying ``ClaudeSDKClient``."""
        options = ClaudeCodeOptions(
            cwd=self.project_path,
            permission_mode=self.config.claude_permission_mode,
            model=self.config.claude_model,
            can_use_tool=self._can_use_tool if self.on_ask_user else None,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._client = client
        self._active = True
        log.info(
            "ClaudeBridge started for cwd=%s ask_user=%s",
            self.project_path,
            bool(self.on_ask_user),
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
            self.model = model

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
    "ToolUseBlock",
    "ToolResultBlock",
    "ResultMessage",
]
