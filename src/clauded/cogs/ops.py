"""Operational commands: /cost, /health, /ratelimit, /review, /plugin, context menus."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import discord
from discord import app_commands

from ._unbound import reject_if_unbound
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ..interaction_handler import InteractionHandler
from ..session_config import SessionConfig

log = logging.getLogger("clauded.bot")


# ---------------------------------------------------------------------------
# /cost group
# ---------------------------------------------------------------------------

cost_group = app_commands.Group(
    name="cost",
    description="Track API costs.",
    default_permissions=discord.Permissions(administrator=True),
)


@cost_group.command(name="show", description="Show cost for this channel")
async def cost_show(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    total, calls = bot.cost_tracker.get_channel_cost(parent_id)
    embed = discord.Embed(
        title="💰 Channel Cost",
        description=f"**${total:.4f}** across {calls} API call(s)",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@cost_group.command(name="total", description="Show total cost across all channels")
async def cost_total(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    total = bot.cost_tracker.get_total_cost()
    embed = discord.Embed(
        title="💰 Total Cost",
        description=f"**${total:.4f}**",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@cost_group.command(name="reset", description="Reset cost for this channel")
async def cost_reset(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    bot.cost_tracker.reset_channel(parent_id)
    await interaction.response.send_message("\u2705 Channel cost reset.", ephemeral=True)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app_commands.command(name="health", description="Show bot health and status")
async def health_check(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    uptime_s = int(time.time() - bot._start_time)
    hours, remainder = divmod(uptime_s, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    active_sessions = len(bot.session_manager.list_sessions())
    bound_projects = len(bot.project_manager._projects)

    claude_version = bot._claude_version

    embed = discord.Embed(title="🏥 Bot Health", color=COLOR_INFO)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Sessions", value=str(active_sessions), inline=True)
    embed.add_field(name="Bound Projects", value=str(bound_projects), inline=True)
    embed.add_field(name="Claude CLI", value=claude_version, inline=True)
    embed.add_field(name="Python", value=sys.version.split()[0], inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /ratelimit
# ---------------------------------------------------------------------------

@app_commands.command(name="ratelimit", description="Show API usage stats")
async def ratelimit_info(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    total = bot.cost_tracker.get_total_cost()
    embed = discord.Embed(title="📊 API Usage", color=COLOR_INFO)
    embed.add_field(name="Total Spent", value=f"${total:.4f}")
    embed.add_field(name="Active Sessions", value=str(len(bot.session_manager.list_sessions())))
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /review
# ---------------------------------------------------------------------------

@app_commands.command(name="review", description="Start a PR review session")
@app_commands.describe(pr="PR number or URL")
async def review_pr(interaction: discord.Interaction, pr: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    await interaction.response.defer()
    channel = interaction.channel
    parent_id = getattr(channel, "parent_id", None) or channel.id
    project_path = bot.project_manager.get_path(parent_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Use this in a text channel", color=COLOR_TOOL_FAILURE),
            ephemeral=True
        )
        return
    try:
        thread = await channel.create_thread(
            name=f"PR Review: {pr}"[:100], type=discord.ChannelType.public_thread
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Missing permission to create threads", color=COLOR_TOOL_FAILURE),
            ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Failed to create thread", description=str(exc)[:500], color=COLOR_TOOL_FAILURE),
            ephemeral=True
        )
        return
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    extra_dirs = bot.project_manager.get_extra_dirs(parent_id)
    handler = InteractionHandler(thread)
    sc = SessionConfig(
        system_prompt=system_prompt,
        on_ask_user=handler.handle_ask_user_question,
        from_pr=pr,
        add_dirs=extra_dirs or None,
    )
    lock = bot.session_manager.get_lock(thread.id)
    async with lock:
        try:
            bridge = await bot.session_manager.create_session(
                thread.id, project_path, bot.config, sc,
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error",
                    description=f"```\n{str(exc)[:500]}\n```",
                    color=COLOR_TOOL_FAILURE,
                )
            )
            return
    embed = discord.Embed(
        title="📋 PR Review started",
        description=f"See thread: {thread.mention}",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /plugin group
# ---------------------------------------------------------------------------

plugin_group = app_commands.Group(
    name="plugin",
    description="Manage Claude plugins",
    default_permissions=discord.Permissions(administrator=True),
)


@plugin_group.command(name="add", description="Add plugin directory and restart session")
@app_commands.describe(path="Path to plugin directory")
async def plugin_add(interaction: discord.Interaction, path: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        await interaction.response.send_message(
            embed=discord.Embed(title="❌ Not a directory", color=COLOR_TOOL_FAILURE),
            ephemeral=True,
        )
        return

    try:
        resolved.relative_to(Path(bot.config.projects_root).resolve())
    except ValueError:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Path outside allowed root",
                description=f"Plugin path must be under `{bot.config.projects_root}`",
                color=COLOR_TOOL_FAILURE,
            ),
            ephemeral=True,
        )
        return

    bridge = await bot._recreate_session(interaction, plugin_dirs=[path])
    if bridge:
        embed = discord.Embed(
            title="🔌 Plugin Added",
            description=f"Plugin directory `{path}` added.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Context menus
# ---------------------------------------------------------------------------

@app_commands.context_menu(name="Send to Claude")
async def send_to_claude(interaction: discord.Interaction, message: discord.Message):
    from ..bot import ClaudedBot
    from ..discord_renderer import DiscordRenderer
    await interaction.response.defer()
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.followup.send("❌ Bot not ready.", ephemeral=True)
        return
    channel = message.channel
    channel_id = getattr(channel, "parent_id", channel.id) or channel.id
    project_path = bot.project_manager.get_path(channel_id)
    if not project_path:
        await interaction.followup.send("❌ Channel not bound.", ephemeral=True)
        return
    thread_name = f"Claude: {message.content[:80]}" if message.content else "Claude session"
    try:
        thread = await message.create_thread(name=thread_name)
    except discord.HTTPException:
        await interaction.followup.send("❌ Failed to create thread.", ephemeral=True)
        return
    system_prompt = bot.project_manager.get_system_prompt(channel_id)
    extra_dirs = bot.project_manager.get_extra_dirs(channel_id)
    mcp_servers = bot.project_manager.get_mcp_servers(channel_id)
    env_vars = bot.project_manager.get_env(channel_id)
    handler = InteractionHandler(thread)
    sc = SessionConfig(
        system_prompt=system_prompt,
        on_ask_user=handler.handle_ask_user_question,
        add_dirs=extra_dirs or None,
        mcp_servers=mcp_servers or None,
        env=env_vars or None,
        user=str(message.author),
    )
    lock = bot.session_manager.get_lock(thread.id)
    async with lock:
        try:
            bridge = await bot.session_manager.create_session(
                thread.id, project_path, bot.config, sc)
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to start session: `{exc}`", ephemeral=True)
            return
    try:
        renderer = DiscordRenderer(thread)
        user_text = message.content or "Hello"
        await renderer.render_response(bridge, user_text)
    except Exception:
        log.exception("send_to_claude render failed")
        await bot.session_manager.stop_session(thread.id)
        try:
            await thread.send(embed=discord.Embed(title="❌ Error", description="Claude session failed", color=0xEF4444))
        except Exception:
            pass
    await interaction.followup.send(f"✅ Sent to Claude in {thread.mention}", ephemeral=True)


@app_commands.context_menu(name="Pin Message")
async def pin_message(interaction: discord.Interaction, message: discord.Message):
    try:
        await message.pin()
        await interaction.response.send_message("📌 Message pinned.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to pin.", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"❌ Failed to pin: `{exc}`", ephemeral=True)


# ---------------------------------------------------------------------------
# /debug command
# ---------------------------------------------------------------------------

@app_commands.command(name="debug", description="Toggle debug logging")
async def debug_toggle(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bot._debug_logging = not bot._debug_logging
    level = logging.DEBUG if bot._debug_logging else logging.INFO
    logging.getLogger("clauded").setLevel(level)
    state = "ON" if bot._debug_logging else "OFF"
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"🔧 Debug logging: {state}",
            color=COLOR_INFO,
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /notify command
# ---------------------------------------------------------------------------

@app_commands.command(name="notify", description="Toggle pre-tool notifications on/off")
async def notify_toggle(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    tid = interaction.channel_id
    if tid is not None:
        current = bot._notify_enabled.get(tid, True)
        bot._notify_enabled[tid] = not current
        state = "ON" if not current else "OFF"
    else:
        bot._pre_tool_notifications = not bot._pre_tool_notifications
        state = "ON" if bot._pre_tool_notifications else "OFF"
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"🔔 Pre-tool notifications: {state}",
            color=COLOR_INFO,
        ),
        ephemeral=True,
    )
