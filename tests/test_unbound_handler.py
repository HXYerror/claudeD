"""Tests for ``cogs._unbound.reject_if_unbound`` helper.

PRD: ``docs/prd/v1.11-unbound-fallback.md`` R2.
Issue: #126.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from clauded.cogs._unbound import (
    NO_CHANNEL_MESSAGE,
    UNBOUND_REFUSE_MESSAGE,
    reject_if_unbound,
)


def _make_interaction(
    *, channel: object, response_done: bool = False, channel_id: int | None = 1234
) -> MagicMock:
    """Build a minimal ``discord.Interaction`` mock with awaitable response
    methods. ``channel`` is attached directly so ``isinstance`` checks against
    real discord types still work when ``spec=`` is used by the caller.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = channel_id
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


# ---------------------------------------------------------------------------
# eng-1: defensive None-check for ``interaction.channel``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_if_unbound_when_channel_is_none_treats_as_dm() -> None:
    """DM / cache miss → ``interaction.channel`` is None. Per the unified
    ``resolve_channel_id`` policy (architect R1 #C6), this is treated the
    same as an explicit ``DMChannel`` and refused with ``NO_CHANNEL_MESSAGE``
    — ``is_bound`` is never consulted.
    """
    interaction = _make_interaction(channel=None, channel_id=5555)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    bot.project_manager.is_bound.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        NO_CHANNEL_MESSAGE, ephemeral=True
    )


@pytest.mark.asyncio
async def test_reject_if_unbound_when_channel_and_channel_id_are_both_none() -> None:
    """When BOTH ``channel`` and ``channel_id`` are None (DM with no cache),
    refuse with the no-channel message — and never query ``is_bound``.
    """
    interaction = _make_interaction(channel=None, channel_id=None)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    bot.project_manager.is_bound.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        NO_CHANNEL_MESSAGE, ephemeral=True
    )


@pytest.mark.asyncio
async def test_reject_if_unbound_thread_with_no_parent_id_falls_back_to_channel_id() -> None:
    """A discord.Thread whose ``parent_id`` is None (rare race) should fall
    back to ``interaction.channel_id`` rather than blow up.
    """
    thread = MagicMock(spec=discord.Thread)
    thread.id = 6000
    thread.parent_id = None
    interaction = _make_interaction(channel=thread, channel_id=6001)
    bot = _make_bot(is_bound=False)

    result = await reject_if_unbound(interaction, bot)

    assert result is True
    bot.project_manager.is_bound.assert_called_once_with(6001)
