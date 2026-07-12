"""#292 S3 — ``/workflow`` slash command group for Dynamic Workflow management.

Provides three subcommands:

* ``/workflow list`` — list all running workflow tasks
* ``/workflow kill <task_id>`` — stop a running task by prefix match
* ``/workflow detail <task_id>`` — show detailed task info

Task state is read from ``bot._workflow_tasks`` (populated by
:class:`~clauded.discord_renderer.DiscordRenderer` via ``_sync_task_to_bot``).
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands

from ._unbound import USE_IN_THREAD_MESSAGE, resolve_session_id
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE, COLOR_TOOL_SUCCESS

log = logging.getLogger("clauded.bot")


workflow_group = app_commands.Group(
    name="workflow",
    description="Manage dynamic workflows",
)


def _resolve_task_id(workflow_tasks: dict, prefix: str) -> str | None:
    """Find a full task_id by 8-char prefix match.

    Returns the full task_id if exactly one match is found, ``None`` otherwise.
    """
    matches = [tid for tid in workflow_tasks if tid.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


@workflow_group.command(name="list")
async def workflow_list(interaction: discord.Interaction) -> None:
    """List running dynamic workflow tasks."""
    bot = interaction.client
    workflow_tasks: dict = getattr(bot, "_workflow_tasks", {})

    if not workflow_tasks:
        embed = discord.Embed(
            title="⚡ Dynamic Workflows",
            description="No running workflow tasks.",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    now = time.time()
    lines: list[str] = []
    for task_id, state in workflow_tasks.items():
        desc = getattr(state, "description", "—")[:60]
        started = getattr(state, "started_at", 0.0)
        duration = _format_duration(now - started) if started else "—"
        usage = getattr(state, "last_usage", None) or {}
        tokens = int(usage.get("total_tokens", 0) or 0)
        token_str = f"{tokens:,}" if tokens else "—"
        lines.append(
            f"**`{task_id[:8]}`** · {desc}\n"
            f"  ⏱️ {duration} · 🪙 {token_str}"
        )

    embed = discord.Embed(
        title=f"⚡ Dynamic Workflows ({len(workflow_tasks)})",
        description="\n\n".join(lines),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@workflow_group.command(name="kill")
@app_commands.describe(task_id="Task ID (first 8 chars)")
async def workflow_kill(interaction: discord.Interaction, task_id: str) -> None:
    """Stop a running dynamic workflow task."""
    bot = interaction.client
    workflow_tasks: dict = getattr(bot, "_workflow_tasks", {})

    full_id = _resolve_task_id(workflow_tasks, task_id)
    if full_id is None:
        matches = [tid for tid in workflow_tasks if tid.startswith(task_id)]
        if len(matches) > 1:
            msg = f"❌ Ambiguous prefix `{task_id}` — matches {len(matches)} tasks. Use more characters."
        else:
            msg = f"❌ No running task found with prefix `{task_id}`."
        await interaction.response.send_message(msg, ephemeral=True)
        return

    # Find the bridge for the active session
    session_id = resolve_session_id(interaction)
    if session_id is None:
        await interaction.response.send_message(USE_IN_THREAD_MESSAGE, ephemeral=True)
        return

    session_manager = getattr(bot, "session_manager", None)
    if session_manager is None:
        await interaction.response.send_message(
            "❌ Session manager not available.", ephemeral=True,
        )
        return

    bridge = session_manager.get_session(session_id)
    if bridge is None:
        await interaction.response.send_message(
            "❌ No active session in this thread.", ephemeral=True,
        )
        return

    try:
        await bridge.stop_task(full_id)
        await interaction.response.send_message(
            f"⏹️ Stopping task `{full_id[:8]}`…",
        )
    except Exception as exc:
        log.warning("workflow kill failed for %s: %s", full_id, exc)
        await interaction.response.send_message(
            f"❌ Failed to stop task `{full_id[:8]}`: {exc}",
            ephemeral=True,
        )


@workflow_group.command(name="detail")
@app_commands.describe(task_id="Task ID (first 8 chars)")
async def workflow_detail(interaction: discord.Interaction, task_id: str) -> None:
    """Show detailed info for a running dynamic workflow task."""
    bot = interaction.client
    workflow_tasks: dict = getattr(bot, "_workflow_tasks", {})

    full_id = _resolve_task_id(workflow_tasks, task_id)
    if full_id is None:
        await interaction.response.send_message(
            f"❌ No running task found with prefix `{task_id}`.",
            ephemeral=True,
        )
        return

    state = workflow_tasks[full_id]
    desc = getattr(state, "description", "—")
    task_type = getattr(state, "task_type", None) or "—"
    started = getattr(state, "started_at", 0.0)
    now = time.time()
    duration = _format_duration(now - started) if started else "—"
    usage = getattr(state, "last_usage", None) or {}
    tokens = int(usage.get("total_tokens", 0) or 0)
    tool_uses = int(usage.get("tool_uses", 0) or 0)
    duration_ms = int(usage.get("duration_ms", 0) or 0)

    embed = discord.Embed(
        title="⚡ Workflow Task Detail",
        color=COLOR_INFO,
    )
    embed.add_field(name="📋 ID", value=f"`{full_id[:8]}`", inline=True)
    embed.add_field(name="🤖 Type", value=task_type, inline=True)
    embed.add_field(name="⏱️ Duration", value=duration, inline=True)
    embed.add_field(name="🔮 Description", value=desc[:200] or "—", inline=False)
    usage_parts: list[str] = []
    if tokens:
        usage_parts.append(f"🪙 {tokens:,} tokens")
    if tool_uses:
        usage_parts.append(f"🔧 {tool_uses} tool uses")
    if duration_ms:
        usage_parts.append(f"⏱️ {duration_ms / 1000:.1f}s SDK time")
    if usage_parts:
        embed.add_field(name="Usage", value=" · ".join(usage_parts), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)
