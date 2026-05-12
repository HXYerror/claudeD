"""Parametrized tests for `_errors.is_transient_discord_error` taxonomy.

Per PRD §Tests: covers all transient and non-transient cases from
`src/clauded/_errors.py`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiohttp
import discord
import pytest

from clauded._errors import is_transient_discord_error


def _http_exception(status: int) -> discord.errors.HTTPException:
    """Build a `discord.errors.HTTPException` with the given status, robustly."""
    try:
        exc = discord.errors.HTTPException(response=MagicMock(status=status), message={})
    except Exception:
        try:
            exc = discord.errors.HTTPException(
                response=MagicMock(status=status), data={}
            )
        except Exception:
            exc = discord.errors.HTTPException.__new__(discord.errors.HTTPException)
            BaseException.__init__(exc)
    exc.status = status
    return exc


@pytest.mark.parametrize(
    "exc_factory, expected",
    [
        # Transient: network/aiohttp errors
        (lambda: aiohttp.ClientConnectorError(MagicMock(), OSError("blip")), True),
        (lambda: aiohttp.ServerDisconnectedError(), True),
        # Transient: asyncio timeouts
        (lambda: asyncio.TimeoutError(), True),
        # Transient: HTTPException with retry-worthy status
        (lambda: _http_exception(429), True),
        (lambda: _http_exception(500), True),
        (lambda: _http_exception(502), True),
        (lambda: _http_exception(503), True),
        (lambda: _http_exception(504), True),
        # NOT transient: HTTPException with client-error status
        (lambda: _http_exception(400), False),
        (lambda: _http_exception(403), False),
        (lambda: _http_exception(404), False),
        # NOT transient: generic programming errors
        (lambda: RuntimeError("boom"), False),
        (lambda: ValueError("nope"), False),
    ],
)
def test_is_transient_discord_error(exc_factory, expected):
    exc = exc_factory()
    assert is_transient_discord_error(exc) is expected, (
        f"Expected {expected} for {type(exc).__name__}"
    )


def test_process_error_not_transient():
    """Claude SDK ProcessError must NOT be considered transient."""
    try:
        from claude_agent_sdk import ProcessError
    except Exception:
        pytest.skip("claude_agent_sdk not available")
    try:
        exc = ProcessError("died")
    except TypeError:
        # ProcessError signature may require additional kwargs in some versions
        exc = ProcessError.__new__(ProcessError)
        BaseException.__init__(exc, "died")
    assert is_transient_discord_error(exc) is False


def test_connection_closed_is_transient():
    """`discord.errors.ConnectionClosed` should be considered transient."""
    # ConnectionClosed signature varies across discord.py versions; bypass __init__
    exc = discord.errors.ConnectionClosed.__new__(discord.errors.ConnectionClosed)
    BaseException.__init__(exc)
    assert is_transient_discord_error(exc) is True


# ---------------------------------------------------------------------------
# v1.18 narrowed aiohttp.ClientError regression pins (#148 R3 engineer #2)
# ---------------------------------------------------------------------------
# The v1.14 PRD risk-table mitigation said "not catching bare ClientError".
# v1.14 shipped bare anyway; v1.18 narrows. These tests pin the new set so a
# future revert to bare ClientError fails loudly.


def test_client_connector_error_is_transient():
    """ClientConnectorError = real network blip → transient."""
    import aiohttp
    # ClientConnectorError signature requires (key, os_error).
    exc = aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)
    BaseException.__init__(exc)
    assert is_transient_discord_error(exc) is True


def test_server_disconnected_is_transient():
    """ServerDisconnectedError = blip during request → transient."""
    import aiohttp
    exc = aiohttp.ServerDisconnectedError()
    assert is_transient_discord_error(exc) is True


def test_server_timeout_is_transient():
    """ServerTimeoutError = slow blip → transient."""
    import aiohttp
    exc = aiohttp.ServerTimeoutError()
    assert is_transient_discord_error(exc) is True


def test_invalid_url_is_NOT_transient():
    """InvalidURL = programming error, NOT transient.
    
    Regression pin for v1.18: bare `aiohttp.ClientError` (v1.14) would have
    classified this as transient and silently swallowed the bug.
    """
    import aiohttp
    exc = aiohttp.InvalidURL("not a url")
    assert is_transient_discord_error(exc) is False


def test_client_response_error_is_NOT_transient():
    """ClientResponseError = 4xx-shape payload error, NOT transient.

    Regression pin: bare ClientError would have caught this; narrowed set
    excludes it so auth/payload bugs surface instead of looping forever.
    """
    import aiohttp
    exc = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=403,
        message="Forbidden",
    )
    assert is_transient_discord_error(exc) is False


def test_is_retryable_alias_matches_taxonomy():
    """_http_retry._is_retryable now delegates to is_transient_discord_error
    (#148 R3 architect dedup). Verify the two return identical answers across
    the parametrize set."""
    from clauded._http_retry import _is_retryable
    import aiohttp
    samples = [
        aiohttp.ServerDisconnectedError(),
        aiohttp.InvalidURL("bad"),
        ValueError("not http"),
        TimeoutError("asyncio"),
    ]
    for exc in samples:
        assert _is_retryable(exc) == is_transient_discord_error(exc), (
            f"divergence on {type(exc).__name__}"
        )
