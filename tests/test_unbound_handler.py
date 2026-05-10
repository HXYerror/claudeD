"""Tests for ``cogs._unbound.reject_if_unbound`` helper.

PRD: ``docs/prd/v1.11-unbound-fallback.md`` R2.
Issue: #126.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from clauded.cogs._unbound import UNBOUND_REFUSE_MESSAGE, reject_if_unbound


def _make_interaction(
    *, channel: object, response_done: bool = False
) -> MagicMock:
    """Build a minimal ``discord.Interaction`` mock with awaitable response
    methods. ``channel`` is attached directly so ``isinstance`` checks against
    real discord types still work when ``spec=`` is used by the caller.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=response_done)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot(*, is_bound: bool) -> MagicMock:
    bot = MagicMock()
    bot.project_manager = MagicMock()
    bot.project_manager.is_bound = MagicMock(return_value=is_bound)
    return bot


@pytest.mark.asyncio
async def test_reject_if_unbound_returns_false_when_bound() -> None:
    """Bound top-level channel → returns False, no message sent."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1111
    interaction = _make_interaction(channel=channel)
    bot = _make_bot(is_bound=True)

    result = await reject_if_unbound(interaction, bot)

    assert result is False
    bot.project_manager.is_bound.assert_called_once_with(1111)
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_reject_if_unbound_returns_true_when_unbound_top_channel() -> None:
    """Unbound top-level channel → True + ephemeral refusal via response."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 2222
    interaction = _make_interaction(channel=channel, response_done=False)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    bot.project_manager.is_bound.assert_called_once_with(2222)
    interaction.response.send_message.assert_awaited_once_with(
        UNBOUND_REFUSE_MESSAGE, ephemeral=True
    )
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_reject_if_unbound_returns_true_when_unbound_thread() -> None:
    """Thread inherits parent channel's bound state → looks up parent_id."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = 9999  # thread id should NOT be used
    thread.parent_id = 3333  # parent channel id IS the lookup key
    interaction = _make_interaction(channel=thread, response_done=False)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    bot.project_manager.is_bound.assert_called_once_with(3333)
    interaction.response.send_message.assert_awaited_once_with(
        UNBOUND_REFUSE_MESSAGE, ephemeral=True
    )


@pytest.mark.asyncio
async def test_reject_if_unbound_uses_followup_when_response_done() -> None:
    """If response was already sent (deferred / first message went out), use
    ``followup.send`` instead of ``response.send_message``."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4444
    interaction = _make_interaction(channel=channel, response_done=True)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_awaited_once_with(
        UNBOUND_REFUSE_MESSAGE, ephemeral=True
    )
