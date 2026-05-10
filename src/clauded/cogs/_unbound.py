"""Unified unbound-channel refusal + hint string for Group A commands."""

from __future__ import annotations

import discord


UNBOUND_REFUSE_MESSAGE = (
    "❌ This channel isn't bound to a project. "
    "Run `/project bind <path>` first, then retry."
)

UNBOUND_HINT_MESSAGE = (
    "💡 This channel isn't bound to a project. "
    "I'll use your home directory (`~`) as the working directory. "
    "Run `/project bind <path>` to scope to a specific project."
)

NO_CHANNEL_MESSAGE = "❌ This command must be run in a channel."


async def reject_if_unbound(interaction: discord.Interaction, bot) -> bool:
    """Refuse Group A commands on unbound channels. Returns True iff refusal sent."""
    ch = interaction.channel
    if ch is None:
        # DM, cache miss, or permission gap — fall back to interaction.channel_id.
        channel_id = interaction.channel_id
    elif isinstance(ch, discord.Thread):
        # Threads inherit the parent channel's bound state.
        channel_id = ch.parent_id or interaction.channel_id
    else:
        channel_id = ch.id

    sender = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )

    if channel_id is None:
        await sender(NO_CHANNEL_MESSAGE, ephemeral=True)
        return True

    if bot.project_manager.is_bound(channel_id):
        return False

    await sender(UNBOUND_REFUSE_MESSAGE, ephemeral=True)
    return True
