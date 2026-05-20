"""#211 — `/mode` slash command group for Claude permission mode.

Surfaces the SDK's existing ``ClaudeSDKClient.set_permission_mode()``
control-plane runtime switch as user-facing slash commands so a Discord
user can see / change / cycle the active permission mode without having
to recreate the session. Mirrors the Claude CLI's terminal UI which
displays the mode below the cost row and supports ``shift+tab`` to
cycle. See ``docs/prd/v1.18-permission-mode-cmd.md``.

Per PRD user decision #5 only 4 of the SDK's 6 ``PermissionMode``
literals are exposed (``dontAsk`` / ``auto`` are deliberately hidden).
``test_permission_mode_literals_match_sdk_contract`` pins this list as
a subset of the SDK's ``PermissionMode`` so a future SDK rename of any
of our 4 modes fails loudly rather than silently.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ._unbound import resolve_session_id
from ..discord_renderer import (
    COLOR_INFO,
    COLOR_TOOL_FAILURE,
    COLOR_TOOL_SUCCESS,
    MODE_EMOJI,
)


# #250: unified message for the 5 sibling sites where a per-thread
# session lookup must reject channel/DM invocation rather than silently
# returning "no active session". Mirrors the existing NO_CHANNEL_MESSAGE
# in _unbound.py but is thread-specific.
_USE_IN_THREAD_MESSAGE = "Use this command inside a thread."

log = logging.getLogger("clauded.bot")


# Cycle order surfaced by ``/mode cycle`` (per PRD user decision #6).
# Order is fixed: default → acceptEdits → plan → bypassPermissions → default.
_CYCLE_ORDER: list[str] = [
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
]


# Emoji mapping for the footer + display surfaces. ``default`` is
# MODE_EMOJI is imported from `..discord_renderer` (#211 R1 architect:
# relocated there to break a cyclic import — the renderer's cost-footer
# code is the primary consumer; this cog imports it back for
# /mode current + /health + /session info displays).


def _next_mode(current: str) -> str:
    """Return the next mode in ``_CYCLE_ORDER`` after ``current``.

    Loops back to the first element after the last. If ``current`` is
    not in the cycle list (e.g., it's ``dontAsk`` / ``auto`` / some
    future SDK literal we don't surface), snap to the first element so
    the user can recover into the known cycle.
    """
    try:
        idx = _CYCLE_ORDER.index(current)
    except ValueError:
        return _CYCLE_ORDER[0]
    return _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]


def _mode_source_for_bridge(bridge) -> tuple[str, str]:
    """Return ``(source, value)`` describing where the bridge's mode came from.

    Mirror of ``cogs/model.py:_model_source_for_bridge`` but with only 3
    tiers (no SDK-observed tier — the SDK doesn't report ``permission_mode``
    on ``ResultMessage``, so the override / env / default split is the
    full picture).

    Returns one of:
    - ``("override", "<mode>")``  — user ran ``/mode set`` or ``/mode cycle``
    - ``("env", "<mode>")``       — ``CLAUDE_PERMISSION_MODE`` env var set
    - ``("default", "default")``  — fallback (env unset → ``"default"``)
    """
    override = getattr(bridge, "_permission_mode_override", None)
    if override:
        return ("override", override)
    config = getattr(bridge, "_config", None)
    env_mode = getattr(config, "claude_permission_mode", None) if config else None
    if env_mode and env_mode != "default":
        # env explicitly pinned to something non-default
        return ("env", env_mode)
    return ("default", "default")


def _format_mode_display(mode: str, source: str) -> str:
    """Render ``{emoji} {mode} (source: ...) — persisted/...`` for embed bodies.

    Used by ``/mode current``, ``/health``, and ``/session info`` so
    every surface labels the active mode identically. ``default`` shows
    no emoji prefix; the other three modes get their ``MODE_EMOJI``
    glyph.

    Source → lifetime label (#211 R1 architect):
      - ``override`` → “persisted” (survives bot restart, PRD §Decision #4)
      - ``env``      → “env-pinned” (CLAUDED_PERMISSION_MODE env)
      - ``default``  → “CLI default”
    Surfacing the lifetime is the user-visible fix for the
    /mode-persistent vs /model-ephemeral dichotomy.
    """
    emoji = MODE_EMOJI.get(mode, "")
    label = f"{emoji} `{mode}`".strip() if emoji else f"`{mode}`"
    lifetime = {
        "override": "persisted",
        "env": "env-pinned",
        "default": "CLI default",
    }.get(source, source)
    return f"{label} (source: {source} — {lifetime})"


mode_group = app_commands.Group(
    name="mode",
    description="View / switch Claude permission mode for this thread",
    # #211 R1 security HIGH (PR #221): /mode set bypassPermissions auto-
    # approves all SDK tool calls + persists across restart. Gate write
    # subcommands to admin-default; `/mode current` is read-only and
    # stays open. Defense-in-depth re-check at each callback (Discord
    # default_permissions is admin-reassignable in the server UI; the
    # callback check defends against role overrides). Pattern matches
    # `ops.unbound_fallback_toggle` (the closest precedent).
)


def _require_admin(interaction: discord.Interaction) -> bool:
    """Defense-in-depth admin check for write-side /mode subcommands.

    Returns True if caller is administrator; otherwise emits an
    ephemeral refusal and returns False (caller should return early).
    Mirror of the per-callback re-check in ``ops.unbound_fallback_toggle``
    (#211 R1 security #1).
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    return False


@mode_group.command(name="set", description="Set Claude permission mode for this thread")
@app_commands.describe(mode="Mode: default / acceptEdits / plan / bypassPermissions")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.choices(mode=[
    app_commands.Choice(name="default", value="default"),
    app_commands.Choice(name="acceptEdits ✏️", value="acceptEdits"),
    app_commands.Choice(name="plan 🔒", value="plan"),
    app_commands.Choice(name="bypassPermissions ⚡", value="bypassPermissions"),
])
async def mode_set(
    interaction: discord.Interaction, mode: app_commands.Choice[str]
) -> None:
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #211 R1 security HIGH: defense-in-depth admin re-check (Discord
    # default_permissions can be UI-overridden per role; the callback
    # check guarantees a privilege-elevating action requires admin
    # regardless of UI policy).
    if not _require_admin(interaction):
        await interaction.response.send_message(
            "❌ Administrator permission required to change permission mode.",
            ephemeral=True,
        )
        return
    thread_id = resolve_session_id(interaction)
    if thread_id is None:
        await interaction.response.send_message(
            _USE_IN_THREAD_MESSAGE, ephemeral=True
        )
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message(
            "ℹ️ No active session in this channel. Send a message to start one, "
            "then `/mode set` will take effect on the next turn.",
            ephemeral=True,
        )
        return

    # #211 R1 security HIGH: privilege-elevation audit trail.
    # Every permission_mode write is forensic-worthy; log WARNING with
    # WHO/WHERE/WHAT-CHANGED so operators can reconstruct who relaxed
    # tool permissions when. Matches `ops.unbound_fallback_toggle`
    # precedent.
    member = interaction.user
    previous_mode = getattr(bridge, "effective_permission_mode", "default")
    try:
        await bridge.set_permission_mode(mode.value)
    except Exception as exc:
        log.exception("/mode set failed: %s", exc)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Failed to set mode",
                description=f"```\n{str(exc)[:500]}\n```",
                color=COLOR_TOOL_FAILURE,
            ),
            ephemeral=True,
        )
        return
    log.warning(
        "SECURITY: /mode set permission_mode %s -> %s by user=%s(id=%s) guild=%s channel=%s thread=%s",
        previous_mode, mode.value,
        getattr(member, "name", "?"), getattr(member, "id", "?"),
        interaction.guild_id, interaction.channel_id, thread_id,
    )

    # Persist immediately so a bot restart between this turn and the
    # next one preserves the user's choice (PRD user decision #4).
    try:
        bot.session_manager.save_session_state(thread_id)
    except Exception:  # pragma: no cover - persistence is best-effort
        log.exception("/mode set: failed to persist session state")

    emoji = MODE_EMOJI.get(mode.value, "")
    title = f"{emoji} Permission mode: `{mode.value}`".strip()
    embed = discord.Embed(
        title=title,
        description=(
            f"Mode set to **{mode.value}**. The next tool call will respect it.\n\n"
            "-# 💾 **Persisted** — survives bot restart. (Contrast with "
            "`/model switch` which is per-session.) Use `/mode set default` to clear."
        ),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mode_group.command(
    name="cycle",
    description="Advance to the next permission mode in the fixed cycle order",
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def mode_cycle(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #211 R1 security HIGH: defense-in-depth admin re-check.
    if not _require_admin(interaction):
        await interaction.response.send_message(
            "❌ Administrator permission required to cycle permission mode.",
            ephemeral=True,
        )
        return
    thread_id = resolve_session_id(interaction)
    if thread_id is None:
        await interaction.response.send_message(
            _USE_IN_THREAD_MESSAGE, ephemeral=True
        )
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message(
            "ℹ️ No active session in this channel. Send a message to start one.",
            ephemeral=True,
        )
        return

    # Cycle reads the EFFECTIVE mode (override > env > default), not just
    # the override field. That way the first cycle from an env-pinned
    # ``plan`` advances to ``bypassPermissions`` rather than snapping
    # back to ``acceptEdits`` (the would-be successor of ``default``).
    current = getattr(bridge, "effective_permission_mode", "default") or "default"
    new_mode = _next_mode(current)
    member = interaction.user
    try:
        await bridge.set_permission_mode(new_mode)
    except Exception as exc:
        log.exception("/mode cycle failed: %s", exc)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Failed to cycle mode",
                description=f"```\n{str(exc)[:500]}\n```",
                color=COLOR_TOOL_FAILURE,
            ),
            ephemeral=True,
        )
        return
    log.warning(
        "SECURITY: /mode cycle permission_mode %s -> %s by user=%s(id=%s) guild=%s channel=%s thread=%s",
        current, new_mode,
        getattr(member, "name", "?"), getattr(member, "id", "?"),
        interaction.guild_id, interaction.channel_id, thread_id,
    )

    try:
        bot.session_manager.save_session_state(thread_id)
    except Exception:  # pragma: no cover - persistence is best-effort
        log.exception("/mode cycle: failed to persist session state")

    emoji = MODE_EMOJI.get(new_mode, "")
    title = f"{emoji} Permission mode: `{new_mode}`".strip()
    embed = discord.Embed(
        title=title,
        description=(
            f"Cycled `{current}` → **{new_mode}**.\n\n"
            "-# 💾 **Persisted** — survives bot restart."
        ),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mode_group.command(
    name="current",
    description="Show the current permission mode + which tier it came from",
)
async def mode_current(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = resolve_session_id(interaction)
    if thread_id is None:
        await interaction.response.send_message(
            _USE_IN_THREAD_MESSAGE, ephemeral=True
        )
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None:
        await interaction.response.send_message(
            "ℹ️ No active session in this channel. Send a message to start one.",
            ephemeral=True,
        )
        return

    source, value = _mode_source_for_bridge(bridge)
    embed = discord.Embed(
        title="🛡️ Current Permission Mode",
        description=_format_mode_display(value, source),
        color=COLOR_TOOL_SUCCESS,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


__all__ = [
    "mode_group",
    "mode_set",
    "mode_cycle",
    "mode_current",
    "_CYCLE_ORDER",
    "MODE_EMOJI",
    "_next_mode",
    "_mode_source_for_bridge",
    "_format_mode_display",
]
