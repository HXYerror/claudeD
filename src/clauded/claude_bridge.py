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
        """
        if self._client is None or not self._active:
            raise RuntimeError("ClaudeBridge.send_message called before start()")

        await self._client.query(text)
        async for msg in self._client.receive_response():
            yield msg

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
