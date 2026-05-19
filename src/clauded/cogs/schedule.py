"""#241 — ``/schedule`` slash command group (5 subcommands).

PRD: ``docs/prd/v1.18-scheduler.md`` §3.2 / §6 / §8 Subtask 4.

The five subcommands split by who actually creates the schedule:

* ``/schedule message <text>``   — injects PRD §6.1 system-reminder + the
  user's text as a normal claude turn; claude then calls the
  ``schedule_message`` MCP tool to actually create the schedule.
* ``/schedule new_task <text>``  — same flow for kind=new_task (PRD §6.2).
* ``/schedule list``             — cog calls :class:`SchedulerStore` directly
  (no claude turn needed).
* ``/schedule delete <id>``      — cog calls
  :meth:`SchedulerManager.delete` directly.
* ``/schedule toggle <id> <enabled>`` — direct toggle, mirror of delete.

The natural-language → tool-args translation happens *inside the claude
turn* — the cog just provides the system-reminder template that instructs
claude to call the right tool. This keeps the cog dumb (no datetime
parsing here) while letting users say "明天 9 点提醒我开会" / "every
Monday 9am".
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from ._unbound import NO_CHANNEL_MESSAGE
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE, DiscordRenderer
from ..session_config import SessionConfig

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# System reminder templates — verbatim from PRD §6.1 / §6.2. These are the
# exact strings approved in the PRD; do not edit without re-getting user
# sign-off (the wording was chosen to force claude into the tool-call path
# rather than responding in plain text).
# --------------------------------------------------------------------------

_REMINDER_MESSAGE = """\
<system-reminder>
The user just invoked `/schedule message`. You MUST call the
`schedule_message` tool to fulfill this request. Do not respond
with text explanation alone — call `schedule_message` as your first
action.

`schedule_message` creates a timer that, when it fires, will inject
a user message into a Discord thread's existing session. The target
session will continue its conversation as if the user typed that
message themselves.

When parsing the user's text:
- Extract `when` as either `"cron: <5-field>"` (recurring) or
  `"iso: <ISO 8601 with tz>"` (one-shot). Convert natural language
  like "明天 9 点" / "每周一 9am" using the channel's timezone
  (default Asia/Shanghai).
- Extract `what` — the message text that will be injected when the
  timer fires. Phrase it from the user's first-person point of view
  (e.g. "提醒我开会" not "remind the user about meeting"), since the
  session will see it as a user message.
- If the user said the schedule should only last for a certain
  duration (e.g. "持续一个月" / "for the next week"), set
  `max_lifetime` to a duration string like `"30d"` / `"7d"`
  (max 365d). Only valid with recurring=true.
- `target_thread_id` defaults to the current thread; only set it if
  the user explicitly named a different thread.
- Pick a short `name` (≤50 chars) if not obvious from the text.

After calling the tool, respond briefly to the user confirming what
was scheduled and the next_fire_at.
</system-reminder>

<user-text>{user_text}</user-text>
"""

_REMINDER_NEW_TASK = """\
<system-reminder>
The user just invoked `/schedule new_task`. You MUST call the
`schedule_new_task` tool. Do not respond with text explanation alone.

`schedule_new_task` creates a timer that, when it fires, will spawn a
BRAND NEW Discord thread + a FRESH claude session, then submit `what`
as that session's first user prompt. Use this when the user wants an
independent task started at a scheduled time — not a reminder injected
into an existing conversation.

When parsing the user's text:
- Extract `when` (cron / iso). For recurring tasks, every fire creates
  a new thread + new session (52 threads/year for weekly cron — that
  is intentional). Honor user tz (default Asia/Shanghai).
- Extract `what` — the task brief that becomes the new session's
  first user prompt. Phrase it as an actionable task (e.g. "整理本周
  的 PR 状态报告" not "remind me to do reports"), not a reminder.
- If the user said the schedule should only last for a certain
  duration, set `max_lifetime` (e.g. `"30d"`, max 365d). Only valid
  with recurring=true.
- `target_channel_id` defaults to the current channel; only set it if
  the user explicitly named a different channel.
- Pick a `thread_name` (≤50 chars) for the new thread, and a `name`
  for the schedule display label.

Reminder: schedule_new_task vs schedule_message
- schedule_new_task → fresh thread + fresh session every fire
- schedule_message  → inject into existing thread's existing session

After calling the tool, respond briefly confirming the schedule.
</system-reminder>

<user-text>{user_text}</user-text>
"""


schedule_group = app_commands.Group(
    name="schedule",
    description="Manage scheduled timers (message/new_task/list/delete/toggle)",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _ensure_bridge(bot, thread_id: int, binding_id: int, user_name: str):
    """Return the live bridge for ``thread_id``, resurrecting from the
    persisted ``resume_session_id`` if needed.

    Mirrors the get-or-resurrect dance used by the natural-message handler
    in :meth:`ClaudedBot._handle_thread_message` so a slash-injected turn
    behaves identically to a typed ``@bot`` message. Inherits the channel
    binding's system-prompt / extra-dirs / mcp-servers / env exactly like
    a normal turn.
    """
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is not None and getattr(bridge, "is_active", False):
        return bridge
    stored = bot.session_manager.get_stored_session(thread_id)
    resume_id = stored.get("session_id") if stored else None
    project_path = bot.project_manager.get_path(binding_id)
    sc = SessionConfig(
        system_prompt=bot.project_manager.get_system_prompt(binding_id),
        resume_session_id=resume_id,
        add_dirs=bot.project_manager.get_extra_dirs(binding_id) or None,
        mcp_servers=bot.project_manager.get_mcp_servers(binding_id) or None,
        env=bot.project_manager.get_env(binding_id) or None,
        user=user_name,
    )
    return await bot.session_manager.create_session(
        thread_id, project_path, bot.config, sc,
    )


def _resolve_full_schedule_id(bot, partial: str) -> tuple[str | None, str | None]:
    """Resolve a possibly-truncated schedule id to its full 16-char form.

    Returns ``(full_id, None)`` on unique match, ``(None, reason)`` otherwise.
    Accepts the full 16-char hex unchanged (no prefix scan needed in that
    case). For shorter inputs, scans the store for prefix matches; rejects
    ambiguous and unknown prefixes with an explanatory ``reason``.
    """
    if len(partial) >= 16:
        return partial, None
    matches = [
        sid for sid in bot.scheduler.store.list_all()
        if sid.startswith(partial)
    ]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"Unknown id prefix {partial!r}"
    return None, f"Ambiguous id prefix {partial!r}: matched {len(matches)}"


# --------------------------------------------------------------------------
# /schedule message <text>
# --------------------------------------------------------------------------

@schedule_group.command(
    name="message",
    description="Schedule a message to be injected into this thread",
)
@app_commands.describe(text="What to schedule (e.g. '明天 9 点提醒我开会')")
async def schedule_message_cmd(
    interaction: discord.Interaction,
    text: str,
) -> None:
    """Inject PRD §6.1 reminder + ``text`` so claude calls ``schedule_message``.

    Restricted to thread context because claude needs a live session
    (which lives at thread granularity) to call the MCP tool. The actual
    schedule creation happens inside the claude turn, not in this cog.
    """
    bot = interaction.client
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "/schedule message must be used inside a thread.",
            ephemeral=True,
        )
        return
    parent_id = channel.parent_id
    if parent_id is None:
        await interaction.response.send_message(
            NO_CHANNEL_MESSAGE, ephemeral=True,
        )
        return
    if not bot.project_manager.is_bound(parent_id):
        await interaction.response.send_message(
            "❌ This channel isn't bound to a project. "
            "Run `/project bind <path>` first.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "⏰ Scheduling message… (claude will create the schedule)",
        ephemeral=False,
    )

    try:
        bridge = await _ensure_bridge(
            bot, channel.id, parent_id, interaction.user.name,
        )
    except Exception as exc:
        log.exception("/schedule message: failed to start bridge")
        await channel.send(embed=discord.Embed(
            title="❌ Failed to open session",
            description=f"```\n{str(exc)[:500]}\n```",
            color=COLOR_TOOL_FAILURE,
        ))
        return

    bot._register_scheduler_ctx(
        thread_id=channel.id,
        channel_id=parent_id,
        guild_id=getattr(channel.guild, "id", None),
    )
    full = _REMINDER_MESSAGE.format(user_text=text)
    project_path = bot.project_manager.get_path(parent_id)
    renderer = DiscordRenderer(
        channel,
        bot=bot,
        project_path=Path(project_path) if project_path else None,
    )
    try:
        await renderer.render_response(
            bridge, full, author_id=interaction.user.id,
        )
    except Exception:
        log.exception("/schedule message render failed")
        try:
            await channel.send(embed=discord.Embed(
                title="❌ Schedule injection failed",
                description="See bot logs.",
                color=COLOR_TOOL_FAILURE,
            ))
        except Exception:
            pass


# --------------------------------------------------------------------------
# /schedule new_task <text>
# --------------------------------------------------------------------------

@schedule_group.command(
    name="new_task",
    description="Schedule a new independent task in a fresh thread",
)
@app_commands.describe(text="What to schedule (e.g. '每周一 9am 整理周报')")
async def schedule_new_task_cmd(
    interaction: discord.Interaction,
    text: str,
) -> None:
    """Inject PRD §6.2 reminder + ``text`` so claude calls ``schedule_new_task``.

    Same thread-context restriction as ``/schedule message``: claude needs
    a live session to call the tool. The actual fresh-thread creation at
    fire time is handled by :meth:`ClaudedBot._fire_schedule_new_task`,
    not here.
    """
    bot = interaction.client
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            "/schedule new_task must be used inside a thread "
            "(claude needs a session to call the tool).",
            ephemeral=True,
        )
        return
    binding_id = channel.parent_id
    if binding_id is None:
        await interaction.response.send_message(
            NO_CHANNEL_MESSAGE, ephemeral=True,
        )
        return
    if not bot.project_manager.is_bound(binding_id):
        await interaction.response.send_message(
            "❌ This channel isn't bound to a project. "
            "Run `/project bind <path>` first.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "⏰ Scheduling new_task… (claude will create the schedule)",
        ephemeral=False,
    )

    try:
        bridge = await _ensure_bridge(
            bot, channel.id, binding_id, interaction.user.name,
        )
    except Exception as exc:
        log.exception("/schedule new_task: failed to start bridge")
        await channel.send(embed=discord.Embed(
            title="❌ Failed to open session",
            description=f"```\n{str(exc)[:500]}\n```",
            color=COLOR_TOOL_FAILURE,
        ))
        return

    bot._register_scheduler_ctx(
        thread_id=channel.id,
        channel_id=binding_id,
        guild_id=getattr(channel.guild, "id", None),
    )
    full = _REMINDER_NEW_TASK.format(user_text=text)
    project_path = bot.project_manager.get_path(binding_id)
    renderer = DiscordRenderer(
        channel,
        bot=bot,
        project_path=Path(project_path) if project_path else None,
    )
    try:
        await renderer.render_response(
            bridge, full, author_id=interaction.user.id,
        )
    except Exception:
        log.exception("/schedule new_task render failed")


# --------------------------------------------------------------------------
# /schedule list
# --------------------------------------------------------------------------

@schedule_group.command(name="list", description="List schedules in this thread")
@app_commands.describe(scope="thread (default) | channel | all")
async def schedule_list_cmd(
    interaction: discord.Interaction,
    scope: str = "thread",
) -> None:
    """List schedules in the requested scope.

    Calls :class:`SchedulerStore` directly — no claude turn involved.
    Markers follow PRD §3.8 / §6:

    * ``📨`` = kind=message     /    ``🧵`` = kind=new_task
    * ``🤖`` = created_by claude /  ``👤`` = created_by user (slash)
    """
    bot = interaction.client
    if scope == "thread":
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "scope=thread requires a thread context.",
                ephemeral=True,
            )
            return
        items = bot.scheduler.store.list_for_thread(interaction.channel.id)
    elif scope == "channel":
        ch = interaction.channel
        if isinstance(ch, discord.Thread):
            cid = ch.parent_id
        else:
            cid = getattr(ch, "id", None)
        if cid is None:
            await interaction.response.send_message(
                "scope=channel requires a channel context.",
                ephemeral=True,
            )
            return
        items = bot.scheduler.store.list_for_channel(cid)
    elif scope == "all":
        items = list(bot.scheduler.store.list_all().values())
    else:
        await interaction.response.send_message(
            f"Invalid scope: {scope!r}. Use thread|channel|all.",
            ephemeral=True,
        )
        return

    if not items:
        await interaction.response.send_message(
            "(no schedules)", ephemeral=True,
        )
        return

    lines: list[str] = []
    # Discord embed practical safety cap: 25 lines. We don't try to be
    # clever with pagination — the rare user with >25 schedules can run
    # ``schedule_list`` via claude (no cap) or filter scope.
    for s in items[:25]:
        kind_marker = "📨" if s.get("kind") == "message" else "🧵"
        by_marker = "🤖" if s.get("created_by") == "claude" else "👤"
        sid = (s.get("schedule_id", "") or "")[:8]
        name = s.get("name", "") or ""
        state = s.get("state", {}) or {}
        nfa = state.get("next_fire_at", "") or "?"
        cnt = state.get("fire_count", 0)
        en_suffix = "" if state.get("enabled") else " (disabled)"
        lines.append(
            f"{kind_marker} {by_marker} `{sid}` {name} — next={nfa} "
            f"fires={cnt}{en_suffix}"
        )

    embed = discord.Embed(
        title=f"📅 Schedules ({scope})",
        description="\n".join(lines),
        color=COLOR_INFO,
    )
    if len(items) > 25:
        embed.set_footer(text=f"showing 25 of {len(items)}")
    await interaction.response.send_message(embed=embed, ephemeral=False)


# --------------------------------------------------------------------------
# /schedule delete <id>
# --------------------------------------------------------------------------

@schedule_group.command(
    name="delete",
    description="Delete a schedule (creator/admin only)",
)
@app_commands.describe(
    schedule_id="16-char hex schedule id (or first 8 chars)",
)
async def schedule_delete_cmd(
    interaction: discord.Interaction,
    schedule_id: str,
) -> None:
    """Delete a schedule. Permission per PRD §4.4: creator or admin.

    Short id prefixes are resolved server-side to the unique match (or
    rejected as ambiguous) so users don't have to copy/paste 16 hex chars.
    """
    bot = interaction.client
    target_id, err = _resolve_full_schedule_id(bot, schedule_id)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return

    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )
    ok, reason = bot.scheduler.delete(
        target_id,
        requester=str(interaction.user.id),
        is_admin=is_admin,
    )
    if ok:
        await interaction.response.send_message(
            f"✅ Deleted `{target_id[:8]}`",
            ephemeral=False,
        )
    else:
        await interaction.response.send_message(
            f"❌ {reason}", ephemeral=True,
        )


# --------------------------------------------------------------------------
# /schedule toggle <id> <enabled>
# --------------------------------------------------------------------------

@schedule_group.command(name="toggle", description="Enable or disable a schedule")
@app_commands.describe(
    schedule_id="16-char hex schedule id (or first 8 chars)",
    enabled="True to enable, False to disable",
)
async def schedule_toggle_cmd(
    interaction: discord.Interaction,
    schedule_id: str,
    enabled: bool,
) -> None:
    """Enable/disable a schedule. Same permission model as ``delete``."""
    bot = interaction.client
    target_id, err = _resolve_full_schedule_id(bot, schedule_id)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return

    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )
    ok, reason = bot.scheduler.toggle(
        target_id, enabled,
        requester=str(interaction.user.id),
        is_admin=is_admin,
    )
    if ok:
        await interaction.response.send_message(
            f"✅ `{target_id[:8]}` enabled={enabled}",
            ephemeral=False,
        )
    else:
        await interaction.response.send_message(
            f"❌ {reason}", ephemeral=True,
        )
