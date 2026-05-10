"""Unified "channel not bound" refusal for Group A (project-mutating) commands.

See ``docs/prd/v1.11-unbound-fallback.md`` (R2) for the classification of
commands and rationale. Group A commands write project-scoped state and
MUST refuse on unbound channels rather than silently scattering files into
``~``.

This module exists so the message text and the parent-thread fallback logic
live in exactly one place, and so callers across cogs can add a single guard:

    from clauded.cogs._unbound import reject_if_unbound

    async def my_command(interaction):
        if await reject_if_unbound(interaction, bot):
            return
        ...
"""

from __future__ import annotations

import discord


UNBOUND_REFUSE_MESSAGE = (
    "❌ This channel isn't bound to a project. "
    "Run `/project bind <path>` first, then retry."
)


async def reject_if_unbound(interaction: discord.Interaction, bot) -> bool:
    """Return ``True`` if the interaction's channel is unbound and a refusal
    reply has been sent. Caller MUST ``return`` immediately after a ``True``
    result — the response has already been sent to Discord.

    Threads inherit their parent channel's bound state, so we resolve the
    parent ``channel_id`` for ``discord.Thread`` instances before checking
    ``ProjectManager.is_bound``.
    """
    ch = interaction.channel
    channel_id = ch.parent_id if isinstance(ch, discord.Thread) else ch.id
    if bot.project_manager.is_bound(channel_id):
        return False

    if not interaction.response.is_done():
        await interaction.response.send_message(
            UNBOUND_REFUSE_MESSAGE, ephemeral=True
        )
    else:
        await interaction.followup.send(
            UNBOUND_REFUSE_MESSAGE, ephemeral=True
        )
    return True
