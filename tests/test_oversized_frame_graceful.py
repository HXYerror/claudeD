"""#331: a single oversized tool_result frame must degrade gracefully.

When a `Read` of a large image/file produces an NDJSON frame bigger than the
SDK's per-frame buffer, the SDK tears the bridge down. The old behavior showed
a generic "session crashed" embed with a 🔄 Retry button — but retry re-sends
the same message, re-Reads the same file, and crashes identically (an infinite
loop). `send_error_with_retry` now detects this case and posts a specific,
actionable message with NO auto-retry button; the user continues by sending a
fresh message (which resumes from the transcript).
"""

from __future__ import annotations

import pytest

from clauded.discord_renderer import DiscordRenderer, RetryView
from tests.conftest import FakeTarget


def _oversized_exc() -> Exception:
    # Mirrors the SDK's SDKJSONDecodeError message shape from #331.
    return Exception(
        "Failed to decode JSON: {...}... JSON message exceeded maximum "
        "buffer size of 1048576 bytes"
    )


def test_is_oversized_frame_error_classification() -> None:
    assert DiscordRenderer._is_oversized_frame_error(_oversized_exc()) is True
    assert DiscordRenderer._is_oversized_frame_error(
        Exception("Buffer size 5000001 exceeds limit 1048576")
    ) is True
    assert DiscordRenderer._is_oversized_frame_error(
        Exception("connection reset by peer")
    ) is False


@pytest.mark.asyncio
async def test_oversized_frame_gets_specific_message_no_retry() -> None:
    """The oversized-frame case posts a clear message and NO retry button."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    retried = {"n": 0}

    async def on_retry() -> None:  # must NOT be wired to a button here
        retried["n"] += 1

    await renderer.send_error_with_retry(_oversized_exc(), on_retry)

    assert target._sent, "expected a notice to be posted"
    msg = target._sent[-1]
    # Crucially: no RetryView — auto-retry would re-Read the file and re-crash.
    assert not isinstance(getattr(msg, "attached_view", None), RetryView), (
        "#331: oversized-frame case must not offer the re-looping retry button"
    )
    embed = msg.embeds[0]
    blob = f"{embed.title or ''} {embed.description or ''}".lower()
    assert "too large" in blob
    # Actionable: tells the user to just continue.
    assert "next message" in blob or "continue" in blob


@pytest.mark.asyncio
async def test_generic_crash_still_offers_retry() -> None:
    """Regression: a normal crash still gets the 🔄 Retry button."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    async def on_retry() -> None:
        pass

    await renderer.send_error_with_retry(RuntimeError("something generic broke"), on_retry)

    msg = target._sent[-1]
    assert isinstance(getattr(msg, "attached_view", None), RetryView), (
        "generic crashes must keep the retry affordance"
    )
