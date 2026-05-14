"""MCP server management commands: /mcp group."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id
from ..discord_renderer import COLOR_INFO

log = logging.getLogger("clauded.bot")


mcp_group = app_commands.Group(
    name="mcp",
    description="Manage MCP servers for Claude.",
    default_permissions=discord.Permissions(administrator=True),
)


@mcp_group.command(name="add", description="Add a stdio MCP server")
@app_commands.describe(name="Server name", command="Command to run", args="Space-separated arguments")
async def mcp_add(
    interaction: discord.Interaction, name: str, command: str, args: str = ""
) -> None:
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
    config: dict = {"type": "stdio", "command": command}
    if args:
        config["args"] = args.split()
    bot.project_manager.add_mcp_server(binding_id, name, config)
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: stdio\nCommand: `{command}`" + (f"\nArgs: `{args}`" if args else ""),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="add-url", description="Add an HTTP MCP server")
@app_commands.describe(name="Server name", url="Server URL")
async def mcp_add_url(interaction: discord.Interaction, name: str, url: str) -> None:
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
    config: dict = {"type": "http", "url": url}
    bot.project_manager.add_mcp_server(binding_id, name, config)
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: http\nURL: `{url}`",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="list", description="List configured MCP servers")
async def mcp_list(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    servers = bot.project_manager.get_mcp_servers(binding_id)
    if not servers:
        await interaction.response.send_message("No MCP servers configured.", ephemeral=True)
        return
    embed = discord.Embed(title="\U0001f50c MCP Servers", color=COLOR_INFO)
    for sname, sconfig in servers.items():
        stype = sconfig.get("type", "stdio")
        if stype == "http":
            detail = f"URL: `{sconfig.get('url', 'N/A')}`"
        else:
            cmd = sconfig.get("command", "?")
            sargs = " ".join(sconfig.get("args", []))
            detail = f"Command: `{cmd}`" + (f"\nArgs: `{sargs}`" if sargs else "")
        embed.add_field(name=f"{sname} ({stype})", value=detail, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="remove", description="Remove an MCP server")
@app_commands.describe(name="Server name")
async def mcp_remove(interaction: discord.Interaction, name: str) -> None:
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
    if bot.project_manager.remove_mcp_server(binding_id, name):
        embed = discord.Embed(
            title=f"\u2705 MCP server `{name}` removed",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            f"\u274c MCP server `{name}` not found.", ephemeral=True
        )
