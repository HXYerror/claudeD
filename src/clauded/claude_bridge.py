"""Bridge to a single Claude Code SDK client session.

Each :class:`ClaudeBridge` wraps one ``ClaudeSDKClient`` connected to a
project directory. A bridge is created per Discord thread, used until the
session is stopped (manually or because the thread is unbound), and then
disconnected.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from .config import Config

log = logging.getLogger("clauded.claude_bridge")


class ClaudeBridge:
    """Wrapper around a ``ClaudeSDKClient`` for a single project session."""

    def __init__(self, project_path: str, config: Config) -> None:
        self.project_path = project_path
        self.config = config
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
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._client = client
        self._active = True
        log.info("ClaudeBridge started for cwd=%s", self.project_path)

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


__all__ = [
    "ClaudeBridge",
    # Re-export for convenience so callers can ``isinstance`` against the
    # message/block types without importing the SDK directly.
    "AssistantMessage",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ResultMessage",
]
