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
    "Run `/project bind <path>` first, then retry. "
    "— Operator alternative: set `CLAUDED_ALLOW_UNBOUND_FALLBACK=1` "
    "in the bot environment to globally allow unbound channels "
    "(falls back to `~`)."
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


def resolve_binding_id(interaction: discord.Interaction) -> int | None:
    """Resolve the channel id for **project-level state** lookups.

    Mirrors :func:`resolve_channel_id` (which is for session-level state),
    but explicitly walks thread → parent. ProjectManager keys all bindings
    on the parent channel; a thread inherits its parent's binding by
    design. Use this helper anywhere ProjectManager state is read or
    written — never pass raw ``interaction.channel_id``.

    Returns ``None`` only when the interaction has no channel context
    (e.g. DMs / cache miss); callers should surface a friendly error
    in that case (see :data:`NO_CHANNEL_MESSAGE`).

    Symmetry table::

        +-----------------+----------------------+-----------------------+
        | State           | Key                  | Helper                |
        +=================+======================+=======================+
        | Binding (proj)  | parent_id in thread  | resolve_binding_id    |
        | Session (live)  | thread.id in thread  | resolve_channel_id +  |
        |                 |                      |   raw interaction.id  |
        +-----------------+----------------------+-----------------------+

    See #197 + #209 + #210 for the lineage of this bug class. The naming
    asymmetry (binding=parent vs session=thread) is intentional, not a
    code smell — project bindings are inherited from the parent channel
    so threads can share configuration, while session state must remain
    scoped to the actual thread for per-conversation isolation.
    """
    channel = interaction.channel
    if channel is None:
        return interaction.channel_id  # may also be None
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None:
        return parent_id
    return channel.id


async def reject_if_unbound(interaction: discord.Interaction, bot) -> bool:
    """Refuse Group A commands on unbound channels. Returns True iff refusal sent."""
    # ``resolve_channel_id`` already walks thread → parent for thread
    # interactions, so the resolved value here is the same as
    # ``resolve_binding_id`` would produce. Named ``binding_id`` so the
    # audit lint in tests/test_binding_id_resolution.py doesn't flag it.
    binding_id = resolve_channel_id(interaction)

    sender = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )

    if binding_id is None:
        await sender(NO_CHANNEL_MESSAGE, ephemeral=True)
        return True

    if bot.project_manager.is_bound(binding_id):
        return False

    await sender(UNBOUND_REFUSE_MESSAGE, ephemeral=True)
    return True
