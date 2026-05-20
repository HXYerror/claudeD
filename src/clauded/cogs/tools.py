"""Tool and budget management commands: /tools and /budget groups."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO
from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id

log = logging.getLogger("clauded.bot")


tools_group = app_commands.Group(
    name="tools",
    description="Control Claude's available tools.",
)


@tools_group.command(name="allow", description="Only allow specific tools")
@app_commands.describe(tools="Space-separated tool names: Bash Edit Read Write")
async def tools_allow(interaction: discord.Interaction, tools: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    tool_list = tools.split()
    if not tool_list:
        await interaction.response.send_message("Provide at least one tool name.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, allowed_tools=tool_list)
    if bridge:
        embed = discord.Embed(
            title="🔧 Allowed Tools Set",
            description=f"Only these tools are allowed: `{' '.join(tool_list)}`\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@tools_group.command(name="deny", description="Deny specific tools")
@app_commands.describe(tools="Space-separated tool names: WebSearch Bash")
async def tools_deny(interaction: discord.Interaction, tools: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    tool_list = tools.split()
    if not tool_list:
        await interaction.response.send_message("Provide at least one tool name.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, disallowed_tools=tool_list)
    if bridge:
        embed = discord.Embed(
            title="🚫 Denied Tools Set",
            description=f"These tools are denied: `{' '.join(tool_list)}`\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@tools_group.command(name="reset", description="Reset to default tools")
async def tools_reset(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction)
    if bridge:
        embed = discord.Embed(
            title="🔧 Tools Reset",
            description="All tools restored to defaults.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /budget group
# ---------------------------------------------------------------------------

budget_group = app_commands.Group(
    name="budget",
    description="Control session spending.",
)


@budget_group.command(name="set", description="Set max budget per session (USD)")
@app_commands.describe(amount="Maximum USD to spend per session")
async def budget_set(interaction: discord.Interaction, amount: float) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("Budget must be positive.", ephemeral=True)
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is not None:
        bot.project_manager.set_budget(binding_id, amount)
    bridge = await bot._recreate_session(interaction, max_budget_usd=amount)
    if bridge:
        embed = discord.Embed(
            title="💵 Budget Set",
            description=f"Max session budget: **${amount:.2f}**.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@budget_group.command(name="show", description="Show current budget setting")
async def budget_show(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    budget = bot.project_manager.get_budget(binding_id)
    if budget is not None:
        embed = discord.Embed(
            title="💵 Current Budget",
            description=f"Max session budget: **${budget:.2f}**",
            color=COLOR_INFO,
        )
    else:
        embed = discord.Embed(
            title="💵 Current Budget",
            description="No budget limit set.",
            color=COLOR_INFO,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@budget_group.command(name="clear", description="Remove budget limit")
async def budget_clear(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    bot.project_manager.clear_budget(binding_id)
    embed = discord.Embed(
        title="💵 Budget Cleared",
        description="Budget limit removed. Sessions are now unlimited.",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
