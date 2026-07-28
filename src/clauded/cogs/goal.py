"""/goal — session-level completion condition (mirrors Claude Code's /goal).

Set a measurable condition and let Claude keep working across turns until an
evaluator (the CLI's small fast model, default Haiku) judges it met. Claude
Code's native ``/goal`` (>= v2.1.139) is a session-scoped feature; the CLI
processes ``/goal <condition>`` as a slash command even in headless/``-p`` mode,
so we forward it through the bridge as a turn. Between goal-loop turns the bot's
post-turn background reader stays warm (``bot._active_goals`` keeps it from
winding down) so the loop's later turns render.

Commands: ``/goal set``, ``/goal status``, ``/goal clear``.

Limitation (pending live verification): the CLI clears the goal internally when
it's met, but the exact SDK stream signal for "goal met" is unconfirmed, so the
bot-side ``_active_goals`` entry is cleared by ``/goal clear`` or the reader's
max-lifetime backstop — not yet auto-detected on completion.
"""
from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from ._unbound import reject_if_unbound
from ..discord_renderer import COLOR_INFO, DiscordRenderer

log = logging.getLogger("clauded.bot")


goal_group = app_commands.Group(
    name="goal",
    description="Set a completion condition and let Claude work until it's met.",
)

_MAX_CONDITION = 4000  # matches the CLI's documented condition cap


def _goal_panel(condition: str) -> discord.Embed:
    """The ◎ goal-active panel shown on set / status."""
    return discord.Embed(
        title="◎ Goal active",
        description=(
            "Claude keeps working across turns until this holds:\n\n"
            f"> {condition[:1500]}\n\n"
            "A fast model checks after each turn; it clears automatically once "
            "met. Clear early with `/goal clear`.\n"
            "-# Tip: pair with auto-mode so goal turns run unattended, and write "
            "a *measurable* condition (e.g. `npm test exits 0`)."
        ),
        color=COLOR_INFO,
    )


def _resolve(interaction: discord.Interaction):
    """Return ``(bot, thread_id, parent_id)`` or ``(None, None, None)``."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        return None, None, None
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or thread_id
    return bot, thread_id, parent_id


@goal_group.command(
    name="set",
    description="Set a goal; Claude works across turns until an evaluator says it's met.",
)
@app_commands.describe(
    condition="A measurable completion condition, e.g. 'all tests in test/auth pass and lint is clean'"
)
async def goal_set(interaction: discord.Interaction, condition: str) -> None:
    bot, thread_id, parent_id = _resolve(interaction)
    if bot is None or thread_id is None:
        await interaction.response.send_message("Use this inside a bound thread.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    condition = (condition or "").strip()
    if not condition:
        await interaction.response.send_message("Provide a completion condition.", ephemeral=True)
        return
    if len(condition) > _MAX_CONDITION:
        await interaction.response.send_message(
            f"Condition too long (max {_MAX_CONDITION} chars).", ephemeral=True
        )
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not getattr(bridge, "is_active", False):
        await interaction.response.send_message(
            "No active session here — send a message to Claude first, then set a goal.",
            ephemeral=True,
        )
        return
    project_path = bot.project_manager.get_path(parent_id)
    await interaction.response.defer()
    # Register BEFORE the turn so the bg reader (started at the end of
    # _render_with_retry) already sees the goal as active and stays warm.
    bot._active_goals[thread_id] = condition
    log.info("/goal set thread=%s len=%d", thread_id, len(condition))
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        renderer = DiscordRenderer(
            interaction.channel, bot=bot,
            project_path=Path(project_path) if project_path else None,
        )
        try:
            await bot._render_with_retry(
                renderer=renderer,
                bridge=bridge,
                user_text=f"/goal {condition}",
                thread=interaction.channel,
                project_path=project_path,
                author_id=interaction.user.id,
            )
        except Exception:
            log.exception("/goal set render failed thread=%s", thread_id)
            bot._active_goals.pop(thread_id, None)
            await interaction.followup.send("❌ Failed to set the goal.", ephemeral=True)
            return
    await interaction.followup.send(embed=_goal_panel(condition))


@goal_group.command(name="status", description="Show the active goal for this thread.")
async def goal_status(interaction: discord.Interaction) -> None:
    bot, thread_id, _ = _resolve(interaction)
    if bot is None or thread_id is None:
        await interaction.response.send_message("Use this inside a thread.", ephemeral=True)
        return
    condition = bot._active_goals.get(thread_id)
    if not condition:
        await interaction.response.send_message("No active goal in this thread.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_goal_panel(condition), ephemeral=True)


@goal_group.command(name="clear", description="Clear the active goal.")
async def goal_clear(interaction: discord.Interaction) -> None:
    bot, thread_id, parent_id = _resolve(interaction)
    if bot is None or thread_id is None:
        await interaction.response.send_message("Use this inside a thread.", ephemeral=True)
        return
    had = bot._active_goals.pop(thread_id, None) is not None
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is not None and getattr(bridge, "is_active", False):
        # Best-effort: tell the CLI to drop its own session goal too.
        await interaction.response.defer()
        project_path = bot.project_manager.get_path(parent_id)
        lock = bot.session_manager.get_lock(thread_id)
        async with lock:
            renderer = DiscordRenderer(
                interaction.channel, bot=bot,
                project_path=Path(project_path) if project_path else None,
            )
            try:
                await bot._render_with_retry(
                    renderer=renderer, bridge=bridge, user_text="/goal clear",
                    thread=interaction.channel, project_path=project_path,
                    author_id=interaction.user.id,
                )
            except Exception:
                log.debug("/goal clear render failed thread=%s", thread_id, exc_info=True)
        await interaction.followup.send("🧹 Goal cleared." if had else "No active goal (CLI cleared anyway).")
        return
    await interaction.response.send_message(
        "🧹 Goal cleared." if had else "No active goal to clear.", ephemeral=True
    )


__all__ = ["goal_group"]
