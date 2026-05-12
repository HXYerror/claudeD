"""Session management commands: /session group."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ._unbound import reject_if_unbound
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ..interaction_handler import InteractionHandler
from ..session_config import SessionConfig

log = logging.getLogger("clauded.bot")


session_group = app_commands.Group(
    name="session",
    description="Manage Claude sessions inside threads.",
)


@session_group.command(name="stop", description="Stop the Claude session in this thread.")
async def session_stop(interaction: discord.Interaction) -> None:
    log.info("/session stop channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message(
            "No thread context for this command.", ephemeral=True
        )
        return

    stopped = await bot.session_manager.stop_session(thread_id)
    if stopped:
        await interaction.response.send_message(
            "Claude session stopped.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "No active Claude session in this thread.", ephemeral=True
        )


@session_group.command(
    name="clear",
    description="Drop context and start a fresh session in this thread (#163 sub-task 2).",
)
async def session_clear(interaction: discord.Interaction) -> None:
    """Tear down the current bridge AND remove the persisted resume entry.

    The CLI's native ``/clear`` semantics: start a new session with empty
    context; the previous session stays on disk (Claude's own jsonl history)
    but won't be resumed by this thread.

    Inverse of ``/session resume``. Long sessions warrant a clean restart
    without leaving the thread — this gives users that control without
    requiring them to ``/session stop`` + leave + rebind.

    Implementation:
      - Stop the live bridge (if any) via ``stop_session``
      - Remove the entry from ``data/sessions.json`` via
        ``session_store.remove_session`` so the next user message starts
        a fresh session (no ``resume_session_id`` in SessionConfig)
    """
    log.info("/session clear channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message(
            "No thread context for this command.", ephemeral=True
        )
        return

    # Atomic clear: holds the per-thread lock around stop + remove so a
    # concurrent /session resume (which also takes the lock) can't race in.
    had_active, had_stored = await bot.session_manager.clear_session(thread_id)

    if had_active or had_stored:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ Session cleared",
                description=(
                    "Dropped this thread's Claude session context. The next "
                    "message will start a fresh session (no resume)."
                ),
                color=COLOR_INFO,
            ),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "No session to clear in this thread.", ephemeral=True
        )


@session_group.command(name="info", description="Show the current session's status.")
async def session_info(interaction: discord.Interaction) -> None:
    log.info("/session info channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
    if bridge is not None and bridge.is_active:
        model = bridge.model or bot.config.claude_model
        cost_str = f"${bridge.total_cost:.4f}" if bridge.total_cost else "$0.0000"
        lines = [
            f"📡 **Session active** — cwd `{bridge.project_path}`",
            f"• Model: `{model}`",
            f"• Turns: `{bridge.num_turns}`",
            f"• Total cost: `{cost_str}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
    else:
        await interaction.response.send_message(
            "No active Claude session in this thread.", ephemeral=True
        )


@session_group.command(name="interrupt", description="Interrupt the current Claude operation in this thread")
async def session_interrupt(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("Use this in a thread.", ephemeral=True)
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message("No active session in this thread.", ephemeral=True)
        return
    interrupted = await bridge.interrupt()
    if interrupted:
        await interaction.response.send_message("⚠️ Claude interrupted by user.")
    else:
        await interaction.response.send_message("Failed to interrupt.", ephemeral=True)


@session_group.command(name="resume", description="Resume the previous Claude session in this thread")
async def session_resume(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("No thread context.", ephemeral=True)
        return
    stored = bot.session_manager.get_stored_session(thread_id)
    if not stored:
        await interaction.response.send_message("No saved session to resume.", ephemeral=True)
        return
    await interaction.response.defer()
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        handler = InteractionHandler(interaction.channel)
        sc = SessionConfig(
            system_prompt=stored.get("system_prompt"),
            model_override=stored.get("model"),
            on_ask_user=handler.handle_ask_user_question,
            resume_session_id=stored["session_id"],
        )
        try:
            await bot.session_manager.create_session(
                thread_id, stored["project_path"], bot.config, sc,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to resume: `{exc}`", ephemeral=True)
            return
    await interaction.followup.send("🔄 Session resumed with previous context.")


@session_group.command(name="list", description="List all active Claude sessions")
async def session_list(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    sessions = bot.session_manager.list_sessions()
    if not sessions:
        await interaction.response.send_message("No active sessions.", ephemeral=True)
        return
    embed = discord.Embed(title="📋 Active Sessions", color=discord.Color.blue())
    for thread_id, bridge in sessions.items():
        model = getattr(bridge, 'model', 'unknown')
        cost = f"${bridge.total_cost:.4f}" if hasattr(bridge, 'total_cost') else "N/A"
        turns = getattr(bridge, 'num_turns', 0)
        embed.add_field(
            name=f"Thread {thread_id}",
            value=f"📁 `{bridge.project_path}`\n🤖 {model} | 💰 {cost} | 🔄 {turns} turns",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@session_group.command(name="compact", description="Compact the current session to save tokens")
async def session_compact(interaction: discord.Interaction) -> None:
    """Send /compact to the Claude session to compress context."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("Use this in a thread.", ephemeral=True)
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message("No active session in this thread.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        async for _ in bridge.send_message("/compact"):
            pass
        embed = discord.Embed(
            title="🗜️ Context Compacted",
            description="Session context has been compressed to save tokens.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"❌ Failed to compact: `{exc}`", ephemeral=True)


@session_group.command(name="fork", description="Fork the current session (new branch from same context)")
async def session_fork(interaction: discord.Interaction) -> None:
    """Fork the current session — creates a new session branching from the same conversation."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message("No active session to fork.", ephemeral=True)
        return
    old_session_id = bridge.session_id
    if not old_session_id:
        await interaction.response.send_message("Session has no ID yet (send a message first).", ephemeral=True)
        return
    new_bridge = await bot._recreate_session(
        interaction,
        resume_session_id=old_session_id,
        fork_session=True,
    )
    if new_bridge:
        embed = discord.Embed(
            title="🍴 Session Forked",
            description=f"New session branched from `{old_session_id[:12]}…`\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="worktree", description="Create a git worktree for isolated work")
@app_commands.describe(name="Worktree name (branch name)")
async def session_worktree(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    bridge = await bot._recreate_session(interaction, worktree=name)
    if bridge:
        embed = discord.Embed(
            title="🌲 Worktree Created",
            description=f"Session started with worktree **{name}**.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="pin", description="Pin the last Claude reply")
async def session_pin(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    try:
        async for msg in channel.history(limit=10):
            if msg.author.bot and msg.content:
                await msg.pin()
                await interaction.response.send_message("📌 Pinned.", ephemeral=True)
                return
    except discord.HTTPException:
        pass
    await interaction.response.send_message("No reply to pin.", ephemeral=True)


@session_group.command(name="name", description="Set session display name")
@app_commands.describe(name="Display name for the session")
async def session_name(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, session_name=name)
    if bridge:
        embed = discord.Embed(
            title=f"📛 Session named: {name}",
            description="⚠️ Conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="security-review", description="Run a security review on the current project")
async def session_security_review(interaction: discord.Interaction) -> None:
    """Send /security-review to the Claude session."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("Use this in a thread.", ephemeral=True)
        return
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None or not bridge.is_active:
        await interaction.response.send_message("No active session in this thread.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        async for _ in bridge.send_message("/security-review"):
            pass
        embed = discord.Embed(
            title="🔒 Security Review",
            description="Security review completed.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"❌ Failed to run security review: `{exc}`", ephemeral=True)


@session_group.command(name="settings", description="Apply custom settings JSON to session")
@app_commands.describe(json_str="Settings JSON string")
async def session_settings(interaction: discord.Interaction, json_str: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    bridge = await bot._recreate_session(interaction, settings=json_str)
    if bridge:
        embed = discord.Embed(
            title="⚙️ Settings Applied",
            description="Custom settings applied.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="export", description="Export conversation history as markdown")
async def session_export(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.followup.send("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    channel = interaction.channel

    if thread_id is None or not hasattr(channel, 'history'):
        await interaction.followup.send("Use this in a thread.", ephemeral=True)
        return

    messages = []
    async for msg in channel.history(limit=500, oldest_first=True):
        role = "🤖 Claude" if msg.author.bot else f"👤 {msg.author.display_name}"
        content = msg.content or ""
        for embed in msg.embeds:
            if embed.description:
                content += "\n" + embed.description
            if embed.title:
                content = "**" + embed.title + "**\n" + content
        if content.strip():
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            messages.append(f"### {role} — {timestamp}\n\n{content}\n")

    if not messages:
        await interaction.followup.send("No messages to export.", ephemeral=True)
        return

    thread_name = getattr(channel, 'name', 'session')
    md = f"# {thread_name}\n\nExported {len(messages)} messages.\n\n---\n\n"
    md += "\n---\n\n".join(messages)

    import io
    file = discord.File(io.BytesIO(md.encode()), filename=f"{thread_name[:50]}.md")
    await interaction.followup.send("📄 Session exported:", file=file, ephemeral=True)



