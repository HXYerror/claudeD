"""Unified unbound-channel refusal + hint string for Group A commands.

Policy convention:

* **Group A (state-mutating)** cogs call :func:`reject_if_unbound` (e.g.
  ``/agent create``, ``/clear``, ``/budget``) — hard refusal in unbound
  channels because the command needs a concrete project context.
* **Group B (read-only / discovery)** cogs call
  :func:`resolve_channel_id` + ``project_manager.get_path_or_default``
  and degrade gracefully (e.g. ``/cost``, ``/health``, ``/skill list``).
"""

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


def resolve_channel_id(interaction: discord.Interaction) -> int | None:
    """Resolve the channel id used for project/session lookups.

    Returns ``None`` for DM channels (explicit or by cache-miss
    fallback) so callers can surface a friendly "must be run in a
    channel" error. Threads resolve to their parent channel so a
    thread inherits its parent's bound state.
    """
    ch = interaction.channel
    if ch is None or isinstance(ch, discord.DMChannel):
        # DM, cache miss, or permission gap — treat all "no real
        # channel" cases uniformly so Group A/B policies stay aligned.
        return None
    if isinstance(ch, discord.Thread):
        return ch.parent_id or interaction.channel_id
    return ch.id


async def reject_if_unbound(interaction: discord.Interaction, bot) -> bool:
    """Refuse Group A commands on unbound channels. Returns True iff refusal sent."""
    channel_id = resolve_channel_id(interaction)

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
