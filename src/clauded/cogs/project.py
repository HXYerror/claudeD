"""Project management commands: /project and /env groups."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO
from ..project_manager import ProjectManager
from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id

log = logging.getLogger("clauded.bot")


# ---------------------------------------------------------------------------
# /project group
# ---------------------------------------------------------------------------

project_group = app_commands.Group(
    name="project",
    description="Manage channel ↔ project-directory bindings.",
    default_permissions=discord.Permissions(administrator=True),
)


@project_group.command(name="bind", description="Bind this channel to a local directory.")
@app_commands.describe(path="Absolute path to the project directory")
async def project_bind(interaction: discord.Interaction, path: str) -> None:
    log.info("/project bind path=%s channel=%s", path, interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(
            NO_CHANNEL_MESSAGE, ephemeral=True
        )
        return

    # #audit(live-log): ACK within the 3s interaction window BEFORE the bind
    # disk write, so a busy event loop can't let the token expire (10062)
    # between here and the reply.
    await interaction.response.defer(ephemeral=True)
    try:
        stored = bot.project_manager.bind(binding_id, path, guild_id=interaction.guild_id)
    except ValueError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ Bound this channel to `{stored}`", ephemeral=True
    )


@project_group.command(name="info", description="Show this channel's current binding.")
async def project_info(interaction: discord.Interaction) -> None:
    log.info("/project info channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return

    bound_path = bot.project_manager.get_project(binding_id)
    if bound_path is None:
        await interaction.response.send_message(
            "This channel is not bound to a project. Use `/project bind` to set one.",
            ephemeral=True,
        )
        return

    lines = [f"📁 This channel is bound to `{bound_path}`"]
    sp = bot.project_manager.get_system_prompt(binding_id)
    if sp:
        lines.append(f"📝 System prompt: {sp}")
    mode = bot.project_manager.get_channel_mode(binding_id)
    if mode != "thread":
        lines.append(f"🔀 Channel mode: `{mode}`")
    mention_required = bot.project_manager.get_mention_required(binding_id)
    if mention_required:
        lines.append("💬 Mention: required (default — use `/project set-mention-required false` to opt out)")
    else:
        lines.append("💬 Mention: not required (responds to all messages)")
    guild_root = bot.project_manager.get_guild_root(interaction.guild_id)
    if interaction.guild_id and str(interaction.guild_id) in bot.project_manager._guild_roots:
        lines.append(f"🏠 Guild root: `{guild_root}`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@project_group.command(name="unbind", description="Remove this channel's binding.")
async def project_unbind(interaction: discord.Interaction) -> None:
    log.info("/project unbind channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return

    if bot.project_manager.unbind(binding_id):
        # v1.18 product carry: surface that mention preference survives unbind
        # so users aren't surprised when a rebind silently restores the
        # "responds to all messages" mode (#153 R1 product §4).
        mention_required = bot.project_manager.get_mention_required(binding_id)
        message_parts = ["✅ Removed this channel's project binding."]
        if not mention_required:
            message_parts.append(
                "\n🔔 Note: `mention not required` preference is preserved "
                "across rebind. Use `/project set-mention-required true` to reset."
            )
        await interaction.response.send_message(
            "".join(message_parts), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "This channel had no binding to remove.", ephemeral=True
        )


class SystemPromptModal(discord.ui.Modal, title="Set System Prompt"):
    """Modal dialog for editing the channel's system prompt."""

    prompt_input = discord.ui.TextInput(
        label="System Prompt",
        style=discord.TextStyle.paragraph,
        placeholder="Describe the project context, coding style, etc.",
        max_length=4000,
        required=False,
    )

    def __init__(self, binding_id: int, project_manager: ProjectManager) -> None:
        super().__init__()
        self._channel_id = binding_id
        self._pm = project_manager
        existing = project_manager.get_system_prompt(binding_id)
        if existing:
            self.prompt_input.default = existing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.prompt_input.value.strip()
        if text:
            self._pm.set_system_prompt(self._channel_id, text)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="✅ System prompt updated",
                    description=f"```\n{text[:200]}\n```",
                    color=COLOR_INFO,
                ),
                ephemeral=True,
            )
        else:
            self._pm.clear_system_prompt(self._channel_id)
            await interaction.response.send_message("✅ System prompt cleared.", ephemeral=True)


@project_group.command(name="system-prompt", description="Set system prompt for this project")
async def project_system_prompt(interaction: discord.Interaction) -> None:
    log.info("/project system-prompt channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return

    if await reject_if_unbound(interaction, bot):
        return

    # Threads inherit the parent channel's binding; resolve_binding_id walks
    # thread → parent so the prompt attaches to the bound row, not the
    # (unbound) thread id. See #209 for the helper rationale.
    modal = SystemPromptModal(binding_id, bot.project_manager)
    await interaction.response.send_modal(modal)


@project_group.command(name="add-dir", description="Add extra directory access for Claude")
@app_commands.describe(path="Path to directory")
async def project_add_dir(interaction: discord.Interaction, path: str) -> None:
    log.info("/project add-dir path=%s channel=%s", path, interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    # Threads inherit the parent channel's binding; resolve_binding_id walks
    # thread → parent so the extra dir attaches to the bound row, not the
    # (unbound) thread id. See #209.
    try:
        # review E4: pass guild_id so the extra dir is confined to the guild's
        # root (mirror of /project bind), not the global projects_root.
        resolved = bot.project_manager.add_extra_dir(
            binding_id, path, guild_id=interaction.guild_id
        )
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    embed = discord.Embed(
        title="📂 Extra Directory Added",
        description=f"Added `{resolved}`",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@project_group.command(name="dirs", description="List extra directories")
async def project_dirs(interaction: discord.Interaction) -> None:
    log.info("/project dirs channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    dirs = bot.project_manager.get_extra_dirs(binding_id)
    if not dirs:
        await interaction.response.send_message("No extra directories configured.", ephemeral=True)
        return
    listing = "\n".join(f"• `{d}`" for d in dirs)
    embed = discord.Embed(
        title="📂 Extra Directories",
        description=listing,
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@project_group.command(name="remove-dir", description="Remove extra directory")
@app_commands.describe(path="Path to remove")
async def project_remove_dir(interaction: discord.Interaction, path: str) -> None:
    log.info("/project remove-dir path=%s channel=%s", path, interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    removed = bot.project_manager.remove_extra_dir(binding_id, path)
    if removed:
        embed = discord.Embed(
            title="📂 Extra Directory Removed",
            description=f"Removed `{path}`",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("Directory not found in extra dirs.", ephemeral=True)


@project_group.command(name="set-mode", description="Set channel mode (thread or forum)")
@app_commands.describe(mode="thread (default) or forum")
@app_commands.choices(mode=[
    app_commands.Choice(name="thread", value="thread"),
    app_commands.Choice(name="forum", value="forum"),
])
async def project_set_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    log.info("/project set-mode mode=%s channel=%s", mode.value, interaction.channel_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    bot.project_manager.set_channel_mode(binding_id, mode.value)
    await interaction.response.send_message(
        f"✅ Channel mode set to `{mode.value}`", ephemeral=True
    )


@project_group.command(
    name="set-mention-required",
    description="Toggle whether @ClaudeBot mention is required in this channel.",
)
@app_commands.describe(required="True (default) requires @bot. False responds to all messages.")
async def project_set_mention_required(
    interaction: discord.Interaction, required: bool
) -> None:
    """v1.17 #138 — per-channel mention-required toggle.

    Thread messages are NEVER affected by this setting (matches v1.1 PRD F1).
    Setting persists across unbind/rebind via the separate
    ``_channel_settings`` registry in ``ProjectManager``.
    """
    log.info(
        "/project set-mention-required required=%s channel=%s",
        required, interaction.channel_id,
    )
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    # Thread invocation → settings live on the parent channel (matches
    # /project bind, system_prompt, env, etc. sibling patterns). Use the
    # shared resolve_binding_id helper for consistency (#209).
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    try:
        bot.project_manager.set_mention_required(binding_id, required)
    except ValueError as exc:
        # _assert_bound rejects unbound channels with ValueError.
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    description = (
        "Bot will respond only when @-mentioned (default)."
        if required
        else "Bot will respond to every non-bot message in this channel."
    )
    embed = discord.Embed(
        title=f"✅ Mention required: {required}",
        description=description,
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@project_group.command(name="set-root", description="Set per-guild projects root directory")
@app_commands.describe(path="Absolute path to the guild's projects root directory")
async def project_set_root(interaction: discord.Interaction, path: str) -> None:
    log.info("/project set-root path=%s guild=%s", path, interaction.guild_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message(
            "This command can only be used in a guild.", ephemeral=True
        )
        return
    try:
        resolved = bot.project_manager.set_guild_root(guild_id, path)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ Guild projects root set to `{resolved}`", ephemeral=True
    )


@project_group.command(name="clear-root", description="Remove per-guild projects root override")
async def project_clear_root(interaction: discord.Interaction) -> None:
    log.info("/project clear-root guild=%s", interaction.guild_id)
    from ..bot import ClaudedBot
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message(
            "This command can only be used in a guild.", ephemeral=True
        )
        return
    if bot.project_manager.clear_guild_root(guild_id):
        await interaction.response.send_message(
            "✅ Guild projects root override removed. Using default.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "No guild-specific root was set.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# /env group
# ---------------------------------------------------------------------------

env_group = app_commands.Group(
    name="env",
    description="Manage environment variables for Claude sessions.",
    default_permissions=discord.Permissions(administrator=True),
)


@env_group.command(name="set", description="Set environment variable")
@app_commands.describe(key="Variable name", value="Variable value")
async def env_set(interaction: discord.Interaction, key: str, value: str) -> None:
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
    try:
        bot.project_manager.set_env(binding_id, key, value)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    embed = discord.Embed(
        title="✅ Environment Variable Set",
        description=f"`{key}` = `{value[:100]}{'…' if len(value) > 100 else ''}`",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@env_group.command(name="list", description="List environment variables")
async def env_list(interaction: discord.Interaction) -> None:
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
    env = bot.project_manager.get_env(binding_id)
    if not env:
        await interaction.response.send_message("No environment variables configured.", ephemeral=True)
        return
    def _mask(v: str) -> str:
        return v[:2] + "****" if len(v) > 4 else "****"
    listing = "\n".join(f"• `{k}` = `{_mask(v)}`" for k, v in env.items())
    embed = discord.Embed(
        title="🔐 Environment Variables",
        description=listing,
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@env_group.command(name="remove", description="Remove environment variable")
@app_commands.describe(key="Variable name")
async def env_remove(interaction: discord.Interaction, key: str) -> None:
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
    if bot.project_manager.remove_env(binding_id, key):
        embed = discord.Embed(
            title="✅ Environment Variable Removed",
            description=f"Removed `{key}`",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(f"Variable `{key}` not found.", ephemeral=True)
