"""Model / effort / max-turns / fallback / bare commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_SUCCESS

log = logging.getLogger("clauded.bot")


# #186: hybrid hardcoded table of known model aliases + metadata.
# Maintained here; reviewer is responsible for refreshing when Anthropic
# releases new SKUs. Order is intentional — user-facing list preserves
# this ordering (most-common balanced first, then deep, then fast,
# then context-window-extended variants).
KNOWN_MODELS: dict[str, dict[str, str | int]] = {
    "sonnet":   {"id": "claude-sonnet-4-5",     "context": 200_000, "tier": "balanced"},
    "opus":     {"id": "claude-opus-4-1",       "context": 200_000, "tier": "deep"},
    "haiku":    {"id": "claude-haiku-3-5",      "context": 200_000, "tier": "fast"},
    "sonnet-1m":{"id": "claude-sonnet-4-5-1m",  "context": 1_000_000, "tier": "balanced"},
}


def _fmt_context(n: int) -> str:
    """`200000` -> `200k`; `1000000` -> `1M`. Defensive on non-ints."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _current_model_for_thread(bot, thread_id: int | None) -> str | None:
    """Return the active model name for ``thread_id`` if a session exists,
    else None. Resolves through ``bridge.model`` which already follows
    the override > sdk-reported > config-default chain."""
    if thread_id is None:
        return None
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is None:
        return None
    return getattr(bridge, "model", None)


model_group = app_commands.Group(
    name="model",
    description="View / switch Claude model for this thread",
)


@model_group.command(name="switch", description="Switch Claude model for this thread")
@app_commands.describe(name="Model: sonnet, opus, haiku, or full model ID")
async def model_switch(interaction: discord.Interaction, name: str) -> None:
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


@model_group.command(name="list", description="List available models + show current")
async def model_list(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = getattr(interaction.channel, "id", None)
    current = _current_model_for_thread(bot, thread_id)
    # Build the rendered list
    lines = []
    for alias, info in KNOWN_MODELS.items():
        ctx = _fmt_context(info["context"])
        tier = info["tier"]
        model_id = info["id"]
        # Mark currently-active model (match alias OR id)
        marker = "🟢 " if current and (current == alias or current == model_id) else "• "
        lines.append(f"{marker}**{alias}** (`{model_id}`) \u2014 {tier}, {ctx} context")
    desc = "\n".join(lines)
    if current:
        header = f"**Current**: `{current}`\n\n**Available models**:\n"
    else:
        header = "_No active session in this channel; current model unknown._\n\n**Available models**:\n"
    embed = discord.Embed(
        title="🤖 Model Selection",
        description=header + desc + "\n\nUse `/model switch <name>` to switch.\n-# Switching resets the conversation context.",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed)


@model_group.command(name="current", description="Show current Claude model for this thread")
async def model_current(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = getattr(interaction.channel, "id", None)
    current = _current_model_for_thread(bot, thread_id)
    if current is None:
        await interaction.response.send_message(
            "ℹ️ No active session in this channel. Send a message to start one.",
            ephemeral=True,
        )
        return
    # Try to find the matching KNOWN_MODELS entry for metadata
    matched = None
    for alias, info in KNOWN_MODELS.items():
        if current == alias or current == info["id"]:
            matched = (alias, info)
            break
    if matched:
        alias, info = matched
        ctx = _fmt_context(info["context"])
        desc = (
            f"• **alias**: `{alias}`\n"
            f"• **id**: `{info['id']}`\n"
            f"• **tier**: {info['tier']}\n"
            f"• **context**: {ctx}"
        )
    else:
        # Unknown model (full id / experimental)
        desc = f"• **id**: `{current}`\n• _(not in known-models table)_"
    embed = discord.Embed(
        title="🤖 Current Model",
        description=desc,
        color=COLOR_TOOL_SUCCESS,
    )
    await interaction.response.send_message(embed=embed)


async def model_switch_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """#186 enhanced: include metadata in the choice display so users see
    context / tier / id before committing."""
    out: list[app_commands.Choice[str]] = []
    cur_low = (current or "").lower()
    for alias, info in KNOWN_MODELS.items():
        if cur_low and cur_low not in alias.lower():
            continue
        display = f"{alias} \u2014 {info['tier']}, {_fmt_context(info['context'])} ({info['id']})"
        # Discord choice name cap = 100 chars
        out.append(app_commands.Choice(name=display[:100], value=alias))
        if len(out) >= 25:
            break
    return out

model_switch.autocomplete("name")(model_switch_autocomplete)


# ----- backward-compat top-level alias (#186 migration safety) -----
# Keep ``switch_model`` symbol exported so bot.py's existing add_command
# call continues to work BUT route to a thin shim that just calls the
# group's switch. We do NOT re-register it under the top-level ``model``
# name (that would conflict with the group). Instead we shadow the
# import-time variable name only — bot.py's import is updated below.
switch_model = model_switch  # alias for any external lookup


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
