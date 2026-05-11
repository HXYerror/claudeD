"""Cascade-resilience tests — #147 Discord-blip vs Claude-fatal taxonomy.

Per PRD §Tests:
1. Discord blip during render → bridge stays alive, no stop_session.
2. ProcessError during render → bridge torn down, stop_session called.
3. RuntimeError during render → bridge torn down, stop_session called.
4. Network blip > 10s → 🌐 reaction added once and removed on recovery.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest


pytestmark = pytest.mark.asyncio


def _make_bot() -> "object":
    """Build a `ClaudedBot` instance with the minimum surface area used by
    `_render_with_retry`: a `session_manager` with an awaitable `stop_session`
    and `get_lock`, and a `create_session` we can drive from tests."""
    from clauded.bot import ClaudedBot

    bot = ClaudedBot.__new__(ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.stop_session = AsyncMock()

    # get_lock must return an async context manager
    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    bot.session_manager.get_lock = MagicMock(return_value=_Lock())
    bot.session_manager.create_session = AsyncMock()
    return bot


async def _invoke_render(bot, *, renderer, exc_to_raise) -> None:
    """Call `_render_with_retry` with a renderer whose `render_response`
    raises `exc_to_raise`, plus a fake thread."""
    thread = MagicMock()
    thread.id = 12345
    thread.send = AsyncMock()

    bridge = MagicMock()

    # Patch send_error_with_retry on the renderer so the retry-button path
    # doesn't try to invoke real Discord I/O.
    renderer.send_error_with_retry = AsyncMock()

    await bot._render_with_retry(
        renderer=renderer,
        bridge=bridge,
        user_text="hello",
        thread=thread,
        project_path="/tmp/p",
        session_config=None,
    )


async def test_discord_blip_during_render_keeps_bridge_alive():
    """Transient `aiohttp.ClientConnectorError` must NOT trigger stop_session."""
    bot = _make_bot()
    renderer = MagicMock()
    blip = aiohttp.ClientConnectorError(MagicMock(), OSError("blip"))
    renderer.render_response = AsyncMock(side_effect=blip)

    await _invoke_render(bot, renderer=renderer, exc_to_raise=blip)

    bot.session_manager.stop_session.assert_not_called()


async def test_process_error_during_render_tears_down():
    """`claude_agent_sdk.ProcessError` (fatal) MUST trigger stop_session."""
    try:
        from claude_agent_sdk import ProcessError
    except Exception:
        pytest.skip("claude_agent_sdk not available")

    bot = _make_bot()
    renderer = MagicMock()
    try:
        boom = ProcessError("died")
    except TypeError:
        boom = ProcessError.__new__(ProcessError)
        BaseException.__init__(boom, "died")
    renderer.render_response = AsyncMock(side_effect=boom)

    await _invoke_render(bot, renderer=renderer, exc_to_raise=boom)

    bot.session_manager.stop_session.assert_awaited()


async def test_runtime_error_during_render_tears_down():
    """Plain `RuntimeError` (fatal/programming error) MUST trigger stop_session."""
    bot = _make_bot()
    renderer = MagicMock()
    boom = RuntimeError("boom")
    renderer.render_response = AsyncMock(side_effect=boom)

    await _invoke_render(bot, renderer=renderer, exc_to_raise=boom)

    bot.session_manager.stop_session.assert_awaited()


async def test_network_reaction_added_after_10s_then_removed_on_recovery():
    """During a sustained blip, 🌐 is added once; on recovery it's removed."""
    from clauded.discord_renderer import DiscordRenderer

    target = MagicMock()
    target.send = AsyncMock()
    renderer = DiscordRenderer(target)

    # The reaction-bearing message
    msg = MagicMock(spec=discord.Message)
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()

    # First 3 attempts fail with ClientConnectorError, then succeed.
    call_count = {"n": 0}
    success_msg = MagicMock(spec=discord.Message)

    async def _op():
        call_count["n"] += 1
        if call_count["n"] <= 3:
            raise aiohttp.ClientConnectorError(MagicMock(), OSError("blip"))
        return success_msg

    # Patch time.monotonic to advance 5s per call (so by attempt 3 we've exceeded 10s).
    t = {"v": 0.0}

    def _fake_monotonic():
        cur = t["v"]
        t["v"] += 5.0
        return cur

    bot_user = MagicMock()
    bot_user.id = 999

    with patch("clauded.discord_renderer.time.monotonic", side_effect=_fake_monotonic), \
         patch("clauded.discord_renderer.asyncio.sleep", new=AsyncMock()):
        result = await renderer._retry_http(
            _op,
            label="edit",
            content_len=10,
            reaction_target=msg,
            bot_user=bot_user,
        )

    assert result is success_msg
    # 🌐 added once after 10s elapsed
    add_calls = [c for c in msg.add_reaction.call_args_list if "🌐" in str(c)]
    assert len(add_calls) == 1, f"expected exactly one 🌐 add_reaction, got {msg.add_reaction.call_args_list}"
    # 🌐 removed once after recovery
    remove_calls = [c for c in msg.remove_reaction.call_args_list if "🌐" in str(c)]
    assert len(remove_calls) == 1, f"expected exactly one 🌐 remove_reaction, got {msg.remove_reaction.call_args_list}"
