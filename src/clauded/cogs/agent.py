"""Agent management commands: /agent group."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ._unbound import reject_if_unbound
from ..discord_renderer import COLOR_INFO

log = logging.getLogger("clauded.bot")


agent_group = app_commands.Group(
    name="agent",
    description="Manage custom Claude agents.",
)


@agent_group.command(name="create", description="Create a custom agent")
@app_commands.describe(name="Agent name", prompt="Agent system prompt", description="Optional description")
async def agent_create(
    interaction: discord.Interaction, name: str, prompt: str, description: str = ""
) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    try:
        bot.agent_manager.create(name, prompt, description)
    except ValueError as exc:
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"\u2705 Agent `{name}` created",
        description=f"Prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="list", description="List available agents")
async def agent_list(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    agents = bot.agent_manager.list_all()
    if not agents:
        await interaction.response.send_message("No custom agents defined. Use `/agent create`.", ephemeral=True)
        return
    embed = discord.Embed(title="\U0001f916 Custom Agents", color=COLOR_INFO)
    for aname, ainfo in agents.items():
        desc = ainfo.get("description", "")
        prompt_preview = ainfo.get("prompt", "")[:100]
        embed.add_field(
            name=aname,
            value=f"{desc}\n`{prompt_preview}{'…' if len(ainfo.get('prompt', '')) > 100 else ''}`",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="use", description="Use a custom agent in this thread")
@app_commands.describe(name="Agent name")
async def agent_use(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    agent = bot.agent_manager.get(name)
    if not agent:
        await interaction.response.send_message(f"\u274c Agent `{name}` not found.", ephemeral=True)
        return
    agents_json = {name: {"description": agent["description"], "prompt": agent["prompt"]}}
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    current = bot.session_manager.get_session(thread_id) if thread_id else None
    sid = getattr(current, "session_id", None) if current and getattr(current, "is_active", False) else None
    bridge = await bot._recreate_session(
        interaction, agent_name=name, custom_agents=agents_json, resume_session_id=sid,
    )
    if bridge:
        embed = discord.Embed(
            title=f"\U0001f916 Agent `{name}` activated",
            description=f"{agent['description']}\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@agent_group.command(name="delete", description="Delete a custom agent")
@app_commands.describe(name="Agent name")
async def agent_delete(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    if bot.agent_manager.delete(name):
        embed = discord.Embed(
            title=f"\U0001f5d1\ufe0f Agent `{name}` deleted",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            f"\u274c Agent `{name}` not found.", ephemeral=True
        )


async def agent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        return []
    agents = bot.agent_manager.list_all()
    return [app_commands.Choice(name=n, value=n) for n in agents if current.lower() in n.lower()][:25]

agent_use.autocomplete("name")(agent_autocomplete)
