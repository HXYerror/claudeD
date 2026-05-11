"""Direct unit tests for ``clauded._http_retry`` (#147 R2 — tester gap I1).

The R1 tester review flagged that the entire ``_http_retry`` module had zero
direct coverage: ``safe_http`` was only exercised transitively through
``safe_remove_reaction`` / ``safe_add_reaction`` in bot.py, and the
"final WARN log on give-up" contract from PRD §Decision 6 was unverifiable.

These tests pin:
- ``safe_http`` retries transient (5xx, connection) failures and returns
  the eventual success value.
- ``safe_http`` returns ``None`` (does not raise) after exhausting the
  retry budget.
- ``safe_send_message`` returns the message on success and ``None`` on
  give-up (the contract on which #147 R2 C3 depends — crash-with-retry
  embed silently logs out instead of crashing).
- ``safe_remove_reaction`` swallows ``discord.Forbidden`` (a permanent,
  non-transient error — best-effort means we don't surface it).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from clauded._http_retry import (
    safe_http,
    safe_remove_reaction,
    safe_send_message,
)


pytestmark = pytest.mark.asyncio


def _http_exc(status: int) -> discord.HTTPException:
    """Build a ``discord.HTTPException`` with a controlled ``.status``.

    Mirrors the helper in ``tests/test_errors_taxonomy.py`` so version
    drift in discord.py's HTTPException signature doesn't break the test.
    """
    response = MagicMock()
    response.status = status
    response.reason = "synthetic"
    try:
        exc = discord.HTTPException(response, {"code": 0, "message": "synthetic"})
    except TypeError:
        exc = discord.HTTPException.__new__(discord.HTTPException)
        exc.status = status
        exc.text = "synthetic"
        BaseException.__init__(exc, "synthetic")
    exc.status = status  # ensure it's set regardless of constructor path
    return exc


async def test_safe_http_retries_on_429_then_succeeds(monkeypatch):
    """A 429 followed by success should return the success value.

    Pins the "transient → retry, then return on recovery" path. We don't
    actually want to sleep multiple seconds in the unit test, so we patch
    ``asyncio.sleep`` to a no-op.
    """
    monkeypatch.setattr(
        "clauded._http_retry.asyncio.sleep", AsyncMock()
    )
    call = {"n": 0}

    async def _op():
        call["n"] += 1
        if call["n"] < 3:
            raise _http_exc(429)
        return "ok"

    result = await safe_http(_op, label="test", retries=5, backoff=0.01)
    assert result == "ok"
    assert call["n"] == 3, "should have retried twice before success"


async def test_safe_http_returns_none_after_max_retries(monkeypatch):
    """Exhausting the retry budget must return ``None``, NOT raise.

    This is the contract bot.py / discord_renderer.py rely on — a giveup
    is logged at error level but doesn't propagate, so the caller can
    decide whether to surface it (typically: log and move on).
    """
    monkeypatch.setattr(
        "clauded._http_retry.asyncio.sleep", AsyncMock()
    )
    call = {"n": 0}

    async def _op():
        call["n"] += 1
        # Use a real transient: ClientConnectorError. Always fails.
        raise aiohttp.ClientConnectorError(MagicMock(), OSError("network down"))

    result = await safe_http(_op, label="test", retries=3, backoff=0.01)
    assert result is None, f"expected None on give-up, got {result!r}"
    assert call["n"] == 3, f"expected exactly 3 attempts, got {call['n']}"


async def test_safe_http_propagates_non_transient():
    """Non-transient exceptions (e.g. ``RuntimeError``) must propagate.

    ``safe_http`` is a transient-only safety net; programming errors must
    not be silently swallowed.
    """
    async def _op():
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        await safe_http(_op, label="test", retries=3, backoff=0.01)


async def test_safe_send_message_returns_message_on_success():
    """``safe_send_message`` returns the sent ``Message`` on success.

    The crash-with-retry embed path (C3) uses the return value to detect
    a "did not post" condition; a successful send must yield the message.
    """
    channel = MagicMock()
    sent_msg = MagicMock(spec=discord.Message)
    channel.send = AsyncMock(return_value=sent_msg)

    result = await safe_send_message(channel, content="hi")

    assert result is sent_msg
    channel.send.assert_awaited_once_with(content="hi")


async def test_safe_send_message_returns_none_on_giveup(monkeypatch):
    """On transient exhaustion, ``safe_send_message`` returns ``None``.

    Pins the contract that ``send_error_with_retry`` (C3 wiring) relies
    on: a giveup must not raise — the caller logs a warning and the user
    can manually re-send.
    """
    monkeypatch.setattr(
        "clauded._http_retry.asyncio.sleep", AsyncMock()
    )
    channel = MagicMock()

    async def _bad_send(**kwargs):
        raise aiohttp.ClientConnectorError(MagicMock(), OSError("blip"))

    channel.send = AsyncMock(side_effect=_bad_send)

    result = await safe_send_message(channel, content="hi")

    assert result is None
    # safe_http's default retries=5, so 5 attempts were made
    assert channel.send.await_count == 5


async def test_safe_remove_reaction_swallows_forbidden():
    """``safe_remove_reaction`` is best-effort: ``Forbidden`` must NOT raise.

    The double-layer (safe_http re-raises non-transients; the outer
    try/except in safe_remove_reaction swallows them with a debug log)
    is intentional — reactions are decorative UX accents and a missing
    permission shouldn't crash the bot. Pinning the swallow behavior
    here ensures future refactors preserve the contract.
    """
    msg = MagicMock(spec=discord.Message)
    # Build a Forbidden (subclass of HTTPException). Same constructor
    # drama as in the helper above.
    response = MagicMock()
    response.status = 403
    try:
        forbidden = discord.Forbidden(response, {"code": 50013, "message": "Missing Permissions"})
    except TypeError:
        forbidden = discord.Forbidden.__new__(discord.Forbidden)
        forbidden.status = 403
        BaseException.__init__(forbidden, "Missing Permissions")

    msg.remove_reaction = AsyncMock(side_effect=forbidden)
    member = MagicMock()

    # Must NOT raise.
    await safe_remove_reaction(msg, "⏳", member)

    msg.remove_reaction.assert_awaited_once()
