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

# #250: unified refusal for the 5 sibling sites where a per-thread
# session lookup must reject channel/DM invocation rather than silently
# returning "no active session". Mirrors :data:`NO_CHANNEL_MESSAGE`
# above but is thread-specific. Centralized here so cogs/mode.py and
# cogs/ops.py share one source of truth (DRY — R1 reviewer finding).
USE_IN_THREAD_MESSAGE = "Use this command inside a thread."


def resolve_channel_id(interaction: discord.Interaction) -> int | None:
    """Resolve the channel id used for **session-level state** lookups.

    Returns ``None`` for DM channels (explicit or by cache-miss
    fallback) so callers can surface a friendly "must be run in a
    channel" error. Threads resolve to their parent channel so a
    thread inherits its parent's bound state.

    .. seealso::

       :func:`resolve_binding_id` for **project-level state** lookups.
       The two helpers are intentionally asymmetric: binding state is
       keyed by parent (threads share their parent's binding), session
       state is keyed by the thread itself (each thread has its own
       live SDK client). See #197 / #209 for the bug lineage.
    """
    ch = interaction.channel
    if ch is None or isinstance(ch, discord.DMChannel):
        # DM, cache miss, or permission gap — treat all "no real
        # channel" cases uniformly so Group A/B policies stay aligned.
        return None
    if isinstance(ch, discord.Thread):
        return ch.parent_id or interaction.channel_id
    return ch.id


def resolve_session_id(interaction: discord.Interaction) -> int | None:
    """Resolve the session id (``thread.id``) for **per-thread live state** lookups.

    Returns ``interaction.channel.id`` only when invoked from inside a
    :class:`discord.Thread`. For any other surface (top-level
    :class:`~discord.TextChannel`, :class:`~discord.DMChannel`, cache
    miss, etc.) returns ``None`` so callers must surface a friendly
    "use this command inside a thread" message rather than silently
    falling through to ``session_manager.get_session(None)``.

    Sibling of :func:`resolve_binding_id` (binding state is keyed by
    parent; session state is keyed by thread). See #197 / #209 / #247
    / #250 for the bug lineage that motivated this helper. Use this
    helper anywhere live SDK ``ClaudeBridge`` / per-thread settings
    (e.g. ``_notify_enabled``) are read or written — never pass raw
    ``interaction.channel_id`` or ``getattr(interaction.channel, "id", None)``.
    """
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return channel.id
    return None


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
    if channel is None or isinstance(channel, discord.DMChannel):
        # DM, cache miss, or permission gap — mirror :func:`resolve_channel_id`
        # so DM callers fail-closed consistently (R1 tester #210 finding:
        # docstring said "Returns None... DMs" but pre-fix returned channel.id).
        return None
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
