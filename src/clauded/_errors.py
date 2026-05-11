"""Exception taxonomy for separating Discord-side transients from Claude-side fatals.

Used by renderer + bot to decide: pause rendering (transient) vs tear down session (fatal).
"""
from __future__ import annotations
import asyncio
import aiohttp
import discord

# Discord HTTPException status codes that indicate retry-worthy errors
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

def is_transient_discord_error(exc: BaseException) -> bool:
    """Return True if exc is a recoverable Discord-side issue (network blip, rate limit, 5xx)."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, aiohttp.ClientError):
        # Covers ClientConnectorError, ClientResponseError, ServerDisconnectedError, etc.
        return True
    if isinstance(exc, discord.errors.ConnectionClosed):
        return True
    if isinstance(exc, discord.errors.HTTPException):
        return exc.status in _TRANSIENT_HTTP_STATUSES
    return False
