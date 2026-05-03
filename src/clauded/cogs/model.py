"""Model / effort / max-turns / fallback / bare commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO

log = logging.getLogger("clauded.bot")


@app_commands.command(name="model", description="Switch Claude model for this thread")
@app_commands.describe(name="Model: sonnet, opus, haiku, or full model ID")
async def switch_model(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, model_override=name)
    if bridge:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"🔄 Switched to `{name}`",
                description="⚠️ Previous conversation context was reset.",
                color=COLOR_INFO,
            )
        )


async def model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    models = ["sonnet", "opus", "haiku", "claude-sonnet-4-20250514", "claude-opus-4-20250514"]
    return [app_commands.Choice(name=m, value=m) for m in models if current.lower() in m.lower()][:25]

switch_model.autocomplete("name")(model_autocomplete)


@app_commands.command(name="effort", description="Set Claude's thinking effort level")
@app_commands.describe(level="Effort: low, medium, high, xhigh, max")
@app_commands.choices(level=[
    app_commands.Choice(name="low", value="low"),
    app_commands.Choice(name="medium", value="medium"),
    app_commands.Choice(name="high", value="high"),
    app_commands.Choice(name="xhigh", value="xhigh"),
    app_commands.Choice(name="max", value="max"),
])
async def set_effort(interaction: discord.Interaction, level: app_commands.Choice[str]) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, effort=level.value)
    if bridge:
        embed = discord.Embed(
            title="🧠 Effort Level Set",
            description=f"Thinking effort set to **{level.value}**.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="max-turns", description="Set maximum turns for Claude session")
@app_commands.describe(number="Maximum number of turns")
async def max_turns_cmd(interaction: discord.Interaction, number: int) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if number < 1:
        await interaction.response.send_message("Number must be at least 1.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, max_turns=number)
    if bridge:
        embed = discord.Embed(
            title="🔄 Max Turns Set",
            description=f"Max turns set to **{number}**.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="fallback-model", description="Set fallback model for Claude session")
@app_commands.describe(model="Fallback model name or ID")
async def fallback_model_cmd(interaction: discord.Interaction, model: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, fallback_model=model)
    if bridge:
        embed = discord.Embed(
            title="🔄 Fallback Model Set",
            description=f"Fallback model set to **{model}**.\n⚠️ Previous conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="bare", description="Toggle bare/minimal Claude mode")
async def toggle_bare(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bridge = await bot._recreate_session(interaction, bare=True)
    if bridge:
        embed = discord.Embed(
            title="🔧 Bare Mode Enabled",
            description="Session restarted in bare/minimal mode.\n⚠️ Conversation context was reset.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
