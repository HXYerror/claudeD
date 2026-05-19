"""#241 — /schedule slash command cog.

Four commands:

* ``/schedule <text>`` — main entry. Injects a system reminder + the user's
  prose as a user message into the current thread's active session, so
  Claude sees ``<system-reminder>...</system-reminder><user-text>...``
  and is steered toward calling ``schedule_create`` tool. We deliberately
  do NOT parse the timing here — Claude's natural-language understanding
  is the parser.

* ``/schedule list`` — cog reads SchedulerStore directly (no Claude); shows
  schedules for current thread.

* ``/schedule delete <id>`` — cog calls SchedulerManager.delete directly;
  enforces \"only own + admin\" rule.

* ``/schedule toggle <id> <enabled>`` — cog calls SchedulerManager.toggle.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands

from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE

log = logging.getLogger("clauded.cogs.schedule")


schedule_group = app_commands.Group(
    name="schedule",
    description="Create / list / manage scheduled tasks",
)


_SYSTEM_REMINDER = (
    "<system-reminder>The user just invoked /schedule. Use the "
    "schedule_create tool to fulfill this request. Do NOT respond with "
    "text explanation alone — you MUST call schedule_create. The "
    "scheduler tool is available via the in-process MCP server "
    "`clauded-scheduler`. Schedules trigger automatically at the "
    "specified time and inject the `what` payload as a user prompt "
    "into this thread's session, just as if the user had typed it.\n"
    "</system-reminder>\n\n"
    "<user-text>{text}</user-text>"
)


@schedule_group.command(
    name="create",
    description="Tell Claude to create a scheduled task (Claude parses the timing)",
)
@app_commands.describe(text="Natural-language description of the schedule")
async def schedule_create_cmd(
    interaction: discord.Interaction, text: str,
) -> None:
    """Inject the prompt-with-system-reminder into the current thread's active session."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "❌ `/schedule create` must be used inside a thread. Start (or "
            "resume) a Claude thread first.",
            ephemeral=True,
        )
        return

    if await reject_if_unbound(interaction, bot):
        return

    thread_id = channel.id
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message(
            "ℹ️ No active session in this thread. Send a message to start "
            "one, then re-run `/schedule create`.",
            ephemeral=True,
        )
        return

    # Defer because send_message + render can take a while
    await interaction.response.defer(thinking=True, ephemeral=True)

    composed = _SYSTEM_REMINDER.format(text=text)
    log.info(
        "#241 /schedule injecting reminder thread=%s text_len=%d",
        thread_id, len(text),
    )

    # Reuse the bot's render pipeline so output streams to the thread
    # the same way a normal user message would.
    from ..discord_renderer import DiscordRenderer
    renderer = DiscordRenderer(channel, bot=bot)
    try:
        await renderer.render_response(
            bridge, composed, author_id=interaction.user.id,
        )
        await interaction.followup.send(
            "✅ Sent your /schedule request to Claude — see the thread above for the response.",
            ephemeral=True,
        )
    except Exception as exc:
        log.exception("#241 /schedule injection failed")
        await interaction.followup.send(
            f"❌ Failed to inject schedule request: `{type(exc).__name__}: {exc}`",
            ephemeral=True,
        )


@schedule_group.command(
    name="list",
    description="List scheduled tasks in this thread",
)
async def schedule_list_cmd(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "❌ `/schedule list` must be used inside a thread.",
            ephemeral=True,
        )
        return

    items = bot.scheduler.store.list_for_thread(channel.id)
    if not items:
        await interaction.response.send_message(
            "ℹ️ No schedules in this thread. Use `/schedule create <text>` "
            "to add one.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title="⏰ Scheduled tasks", color=COLOR_INFO)
    for s in items:
        state = s.get("state", {})
        trig = s.get("trigger", {})
        when_human = trig.get("cron") or trig.get("iso", "?")
        what = s.get("payload", {}).get("what", "")
        what_preview = (what[:80] + "…") if len(what) > 80 else what
        marker = "🤖 claude" if s.get("created_by") == "claude" else f"👤 <@{s.get('created_by')}>"
        emoji_state = "✅" if state.get("enabled", True) else "⏸"
        name = s.get("name", s["schedule_id"][:8])
        embed.add_field(
            name=f"{emoji_state} {name} (`{s['schedule_id'][:8]}`)",
            value=(
                f"**When**: {when_human}\n"
                f"**What**: {what_preview}\n"
                f"**Next**: {state.get('next_fire_at', '?')}\n"
                f"**Fires**: {state.get('fire_count', 0)} · {marker}"
            ),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@schedule_group.command(
    name="delete",
    description="Delete a schedule (by id; can use first 8 chars)",
)
@app_commands.describe(schedule_id="Schedule id (full uuid or first 8 chars)")
async def schedule_delete_cmd(
    interaction: discord.Interaction, schedule_id: str,
) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    sid = schedule_id.strip()
    if len(sid) < 32:
        matches = [
            full for full in bot.scheduler.store.list_all().keys()
            if full.startswith(sid)
        ]
        if not matches:
            await interaction.response.send_message(
                f"❌ No schedule matches `{sid}`",
                ephemeral=True,
            )
            return
        if len(matches) > 1:
            await interaction.response.send_message(
                f"❌ Prefix `{sid}` is ambiguous: {len(matches)} matches",
                ephemeral=True,
            )
            return
        sid = matches[0]

    is_admin = bool(
        interaction.user.guild_permissions.administrator
        if hasattr(interaction.user, "guild_permissions")
        else False
    )
    ok, reason = bot.scheduler.delete(
        sid, requester=interaction.user.id, is_admin=is_admin,
    )
    if ok:
        await interaction.response.send_message(
            f"✅ Deleted schedule `{sid}`", ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"❌ {reason}", ephemeral=True,
        )


@schedule_group.command(
    name="toggle",
    description="Enable / disable a schedule",
)
@app_commands.describe(
    schedule_id="Schedule id (full uuid or first 8 chars)",
    enabled="True to enable, False to pause",
)
async def schedule_toggle_cmd(
    interaction: discord.Interaction, schedule_id: str, enabled: bool,
) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    sid = schedule_id.strip()
    if len(sid) < 32:
        matches = [
            full for full in bot.scheduler.store.list_all().keys()
            if full.startswith(sid)
        ]
        if not matches:
            await interaction.response.send_message(
                f"❌ No schedule matches `{sid}`", ephemeral=True,
            )
            return
        if len(matches) > 1:
            await interaction.response.send_message(
                f"❌ Prefix `{sid}` is ambiguous", ephemeral=True,
            )
            return
        sid = matches[0]

    is_admin = bool(
        interaction.user.guild_permissions.administrator
        if hasattr(interaction.user, "guild_permissions")
        else False
    )
    ok, reason = bot.scheduler.toggle(
        sid, enabled, requester=interaction.user.id, is_admin=is_admin,
    )
    if ok:
        state_word = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"✅ Schedule `{sid[:8]}` {state_word}", ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"❌ {reason}", ephemeral=True,
        )
