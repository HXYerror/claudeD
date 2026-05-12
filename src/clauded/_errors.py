"""Exception taxonomy for separating Discord-side transients from Claude-side fatals.

Used by renderer + bot to decide: pause rendering (transient) vs tear down session (fatal).
Also the single source of truth for the transient-status set + the retry predicate
used by ``_http_retry`` (#148 R3 architect dedup).
"""
from __future__ import annotations
import asyncio
import aiohttp
import discord

# Discord HTTPException status codes that indicate retry-worthy errors.
# Public so ``_http_retry`` can reuse the same set (single source of truth).
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# Subset of ``aiohttp.ClientError`` that's actually transient-shaped. The
# previous ``isinstance(exc, aiohttp.ClientError)`` was too broad and
# violated the PRD #148 risk-table mitigation ("not catching bare
# ClientError"). Programming-error subclasses like ``InvalidURL`` and the
# auth/payload-shape ``ClientResponseError`` are now excluded.
_TRANSIENT_AIOHTTP_CLASSES: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientConnectionError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientOSError,
    aiohttp.ClientPayloadError,
)


def is_transient_discord_error(exc: BaseException) -> bool:
    """Return True if exc is a recoverable Discord-side issue (network blip, rate limit, 5xx).

    Also accepted by ``_http_retry.safe_http`` (called inline) so
    renderer/bot/retry-loop classify identically.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, _TRANSIENT_AIOHTTP_CLASSES):
        return True
    if isinstance(exc, discord.errors.ConnectionClosed):
        return True
    if isinstance(exc, discord.errors.RateLimited):
        return True
    if isinstance(exc, discord.errors.HTTPException):
        status = getattr(exc, "status", None)
        return status in TRANSIENT_HTTP_STATUSES
    return False
