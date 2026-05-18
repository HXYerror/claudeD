"""Generic HTTP retry helpers shared between renderer and bot.

`safe_http` runs a coroutine factory under exponential backoff, catching only
the transient classes per `_errors.is_transient_discord_error`. On final
exhaustion it returns ``None`` instead of raising — callers can decide whether
to log or surface the failure. Specialized wrappers (`safe_send_message`,
`safe_remove_reaction`, `safe_add_reaction`) capture the common Discord
operations bot.py and discord_renderer.py both need.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import discord

from ._errors import is_transient_discord_error
from . import stream_logger

log = logging.getLogger("clauded.http_retry")

# Single source of truth for transient classification lives in ``_errors``.
from ._errors import TRANSIENT_HTTP_STATUSES  # noqa: F401,E402  re-exported for legacy importers


async def safe_http(
    op: Callable[[], Awaitable[Any]],
    *,
    label: str,
    retries: int = 5,
    backoff: float = 0.5,
    log: Optional[logging.Logger] = None,
) -> Any | None:
    """Run ``op()`` under exponential backoff, swallowing transient failures.

    Returns the operation result on success, or ``None`` on final exhaustion.
    Non-transient exceptions propagate.
    """
    logger = log or globals()["log"]
    last_exc: BaseException | None = None
    for attempt in range(retries):
        try:
            return await op()
        except BaseException as exc:  # noqa: BLE001 — selective re-raise below
            if not is_transient_discord_error(exc):
                raise
            last_exc = exc
            delay = backoff * (2 ** attempt)
            if attempt < retries - 1:
                logger.warning(
                    "safe_http[%s] transient failure attempt %d/%d: %s; sleeping %.2fs",
                    label,
                    attempt + 1,
                    retries,
                    type(exc).__name__,
                    delay,
                )
                # #223: emit stream-debug event so /log dump (#224) can
                # show transient retry storms (Discord 5xx waves).
                if stream_logger.is_enabled():
                    stream_logger.log_event({
                        "type": "DiscordHTTPRetry",
                        "label": label,
                        "attempt": attempt + 1,
                        "retries": retries,
                        "exc_type": type(exc).__name__,
                        "status": getattr(exc, "status", None),
                    })
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "safe_http[%s] giving up after %d attempts: %s",
                    label,
                    retries,
                    type(exc).__name__,
                )
                if stream_logger.is_enabled():
                    stream_logger.log_event({
                        "type": "DiscordHTTPRetry",
                        "label": label,
                        "attempt": attempt + 1,
                        "retries": retries,
                        "exc_type": type(exc).__name__,
                        "status": getattr(exc, "status", None),
                        "giveup": True,
                    })
    logger.debug("safe_http[%s] exhausted (last_exc=%r)", label, last_exc)
    return None


async def safe_send_message(channel_or_thread: Any, **kwargs: Any) -> "discord.Message | None":
    """Send a Discord message under retry. Returns the sent Message or None.

    Used by ``discord_renderer.send_error_with_retry`` (#147 R2 C3) so the
    crash-with-retry embed survives the very transient that caused the crash.
    """
    return await safe_http(lambda: channel_or_thread.send(**kwargs), label="send")


async def safe_remove_reaction(msg: "discord.Message", emoji: Any, member: Any) -> None:
    """Remove a reaction; swallow all errors (best-effort)."""
    try:
        await safe_http(
            lambda: msg.remove_reaction(emoji, member), label="remove_reaction"
        )
    except Exception:  # noqa: BLE001
        # #223 PR-B: was log.debug (invisible in prod). Reaction failures
        # commonly mean gateway perms broken — worth a WARNING.
        log.warning("safe_remove_reaction swallowed exception", exc_info=True)


async def safe_add_reaction(msg: "discord.Message", emoji: Any) -> None:
    """Add a reaction; swallow all errors (best-effort)."""
    try:
        await safe_http(lambda: msg.add_reaction(emoji), label="add_reaction")
    except Exception:  # noqa: BLE001
        log.warning("safe_add_reaction swallowed exception", exc_info=True)
