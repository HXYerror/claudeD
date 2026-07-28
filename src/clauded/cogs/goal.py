"""/goal — session-level completion condition (mirrors Claude Code's /goal).

Set a measurable condition and let Claude keep working across turns until an
evaluator (the CLI's small fast model, default Haiku) judges it met. Claude
Code's native ``/goal`` (>= v2.1.139) is a session-scoped feature; the CLI
processes ``/goal <condition>`` as a slash command even through the SDK, so we
forward it through the bridge as a turn.

Verified against the real CLI/SDK (not just unit tests): the whole goal loop
runs INSIDE one ``send_message``/``receive_response`` — the evaluator injects
its "continue" turns into the stream as UserMessages and there is ONE terminal
ResultMessage (a forced 2-turn goal reports ``turns=2`` in a single call;
``receive_pending`` sees nothing). So the normal render path shows every loop
turn live and nothing is handed to the post-turn background reader. claudeD's
own Stop hook coexists with the evaluator on the streaming path (both fire, no
stop-hook error — unlike a raw ``claude -p`` invocation).

``bot._active_goals[thread_id]`` therefore marks a goal as running only for the
DURATION of the set turn (backing a concurrent ``/goal status``) and is cleared
once the loop resolves. To stop a running goal use ``/session interrupt`` — the
loop holds the per-thread lock, so ``/goal clear`` can't preempt it (clear only
drops the status marker).

Commands: ``/goal set``, ``/goal status``, ``/goal clear``.
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
            "met. It runs to completion in this turn — stop it early with "
            "`/session interrupt`.\n"
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
    log.info("/goal set thread=%s len=%d", thread_id, len(condition))
    # Verified against the real CLI/SDK: the /goal loop runs to completion
    # (met or stopped) WITHIN this single turn — the evaluator injects its
    # "continue" turns INTO the stream as UserMessages, and receive_response()
    # returns only at the ONE terminal ResultMessage (a 2-turn goal yields
    # turns=2 in a single send_message; nothing lands on the bg reader). So we
    # mark the goal active only for the DURATION of the turn (backs a concurrent
    # `/goal status`) and clear it once the loop resolves. The bridge renders
    # every loop turn live via the normal path — no bg-reader handoff involved.
    bot._active_goals[thread_id] = condition
    await interaction.followup.send(embed=_goal_panel(condition))
    lock = bot.session_manager.get_lock(thread_id)
    try:
        async with lock:
            renderer = DiscordRenderer(
                interaction.channel, bot=bot,
                project_path=Path(project_path) if project_path else None,
            )
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
        try:
            await interaction.followup.send("❌ The goal turn failed.", ephemeral=True)
        except Exception:
            pass
    finally:
        # The loop is done (met / stopped / failed); drop the active marker so
        # `/goal status` doesn't lie and the post-turn bg reader winds down.
        bot._active_goals.pop(thread_id, None)


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


@goal_group.command(name="clear", description="Clear the active-goal marker (see notes for stopping a running goal).")
async def goal_clear(interaction: discord.Interaction) -> None:
    bot, thread_id, _ = _resolve(interaction)
    if bot is None or thread_id is None:
        await interaction.response.send_message("Use this inside a thread.", ephemeral=True)
        return
    # A /goal loop runs synchronously in the turn that set it (holding the
    # per-thread lock), so we deliberately do NOT inject a `/goal clear` turn
    # here — it would just queue behind the running goal, useless for aborting.
    # This only drops the status marker; to actually stop a running goal use
    # `/session interrupt`.
    had = bot._active_goals.pop(thread_id, None) is not None
    if had:
        msg = (
            "🧹 Cleared the goal marker. If a goal is **currently running**, stop "
            "it with `/session interrupt` — this only clears the status."
        )
    else:
        msg = "No active goal marker here. (Running goal? Use `/session interrupt`.)"
    await interaction.response.send_message(msg, ephemeral=True)


__all__ = ["goal_group"]
