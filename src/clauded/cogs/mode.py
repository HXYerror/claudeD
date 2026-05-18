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

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE, COLOR_TOOL_SUCCESS

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
# deliberately absent — when the effective mode is ``default`` the
# footer second line is omitted entirely (PRD §Design "Footer"). Keep
# this dict centralized so the renderer + /health + /session info all
# share the same source of truth.
MODE_EMOJI: dict[str, str] = {
    "acceptEdits": "✏️",
    "plan": "🔒",
    "bypassPermissions": "⚡",
}


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
    """Render ``{emoji} {mode} (source: ...)`` for embed bodies.

    Used by ``/mode current``, ``/health``, and ``/session info`` so
    every surface labels the active mode identically. ``default`` shows
    no emoji prefix; the other three modes get their ``MODE_EMOJI``
    glyph.
    """
    emoji = MODE_EMOJI.get(mode, "")
    label = f"{emoji} `{mode}`".strip() if emoji else f"`{mode}`"
    return f"{label} (source: {source})"


mode_group = app_commands.Group(
    name="mode",
    description="View / switch Claude permission mode for this thread",
)


@mode_group.command(name="set", description="Set Claude permission mode for this thread")
@app_commands.describe(mode="Mode: default / acceptEdits / plan / bypassPermissions")
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
    thread_id = getattr(interaction.channel, "id", None)
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message(
            "ℹ️ No active session in this channel. Send a message to start one, "
            "then `/mode set` will take effect on the next turn.",
            ephemeral=True,
        )
        return

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
        description=f"Mode set to **{mode.value}**. The next tool call will respect it.",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mode_group.command(
    name="cycle",
    description="Advance to the next permission mode in the fixed cycle order",
)
async def mode_cycle(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = getattr(interaction.channel, "id", None)
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
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

    try:
        bot.session_manager.save_session_state(thread_id)
    except Exception:  # pragma: no cover - persistence is best-effort
        log.exception("/mode cycle: failed to persist session state")

    emoji = MODE_EMOJI.get(new_mode, "")
    title = f"{emoji} Permission mode: `{new_mode}`".strip()
    embed = discord.Embed(
        title=title,
        description=f"Cycled `{current}` → **{new_mode}**.",
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
    thread_id = getattr(interaction.channel, "id", None)
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
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
