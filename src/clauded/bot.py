"""Discord bot entrypoint for claudeD.

Wires up event handlers (on_ready, on_message) and registers slash command
groups (`/project`, `/session`). The on_message handler bridges Discord
messages to a per-thread :class:`ClaudeBridge` session.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, load_config
from .discord_renderer import DiscordRenderer
from .project_manager import ProjectManager
from .session_manager import SessionManager

log = logging.getLogger("clauded.bot")


def _build_intents() -> discord.Intents:
    """Intents needed: messages + content for bridging, guilds for commands."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.messages = True
    intents.guilds = True
    return intents


class ClaudedBot(commands.Bot):
    """Discord bot for the claudeD bridge."""

    def __init__(self, config: Config) -> None:
        super().__init__(command_prefix="!", intents=_build_intents())
        self.session_manager = SessionManager()
        self.project_manager = ProjectManager()

    async def setup_hook(self) -> None:
        """Register slash command groups and sync to Discord."""
        self.tree.add_command(project_group)
        self.tree.add_command(session_group)
        synced = await self.tree.sync()
        log.info("Synced %d application command(s)", len(synced))

    async def on_ready(self) -> None:  # type: ignore[override]
        user = self.user
        log.info("Bot online as %s (id=%s)", user, getattr(user, "id", "?"))

    async def on_message(self, message: discord.Message) -> None:  # type: ignore[override]
        # Ignore self / other bots.
        if message.author.bot:
            return

        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)

        log.info(
            "on_message channel=%s thread=%s author=%s len=%d",
            channel.id,
            parent_id,
            message.author,
            len(message.content),
        )

        try:
            if parent_id is None:
                await self._handle_channel_message(message)
            else:
                await self._handle_thread_message(message, parent_id)
        except Exception:
            log.exception("on_message handling failed")

        await self.process_commands(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _handle_channel_message(self, message: discord.Message) -> None:
        """Channel (non-thread) message: open a new thread + session."""
        channel = message.channel
        if not self.project_manager.is_bound(channel.id):
            return  # Channel isn't wired up; ignore.

        project_path = self.project_manager.get_path(channel.id)
        if project_path is None:
            log.warning("Channel %s reports bound but has no path", channel.id)
            return

        if not isinstance(channel, discord.TextChannel):
            log.warning("Bound channel %s is not a TextChannel; skipping", channel.id)
            return

        thread_name = (message.content or "claude session")[:80] or "claude session"
        try:
            thread = await message.create_thread(name=thread_name)
        except Exception:
            log.exception("Failed to create thread for channel=%s", channel.id)
            return

        try:
            bridge = await self.session_manager.create_session(
                thread.id, project_path, self.config
            )
        except Exception as exc:
            log.exception("Failed to start ClaudeBridge")
            await thread.send(f"Failed to start Claude session: `{exc}`")
            return

        renderer = DiscordRenderer(thread)
        await renderer.render_response(bridge, message.content)

    async def _handle_thread_message(
        self, message: discord.Message, parent_id: int
    ) -> None:
        """Thread message: route to the existing/new session for that thread."""
        if not self.project_manager.is_bound(parent_id):
            return  # Parent channel isn't bound; ignore.

        project_path = self.project_manager.get_path(parent_id)
        if project_path is None:
            log.warning("Parent channel %s bound but has no path", parent_id)
            return

        thread_id = message.channel.id
        bridge = self.session_manager.get_session(thread_id)
        if bridge is None or not bridge.is_active:
            try:
                bridge = await self.session_manager.create_session(
                    thread_id, project_path, self.config
                )
            except Exception as exc:
                log.exception("Failed to start ClaudeBridge for thread=%s", thread_id)
                await message.channel.send(f"Failed to start Claude session: `{exc}`")
                return

        renderer = DiscordRenderer(message.channel)
        await renderer.render_response(bridge, message.content)


# ---------------------------------------------------------------------------
# Slash command groups.
# ---------------------------------------------------------------------------

project_group = app_commands.Group(
    name="project",
    description="Manage channel ↔ project-directory bindings.",
)


@project_group.command(name="bind", description="Bind this channel to a local directory.")
@app_commands.describe(path="Absolute path to the project directory")
async def project_bind(interaction: discord.Interaction, path: str) -> None:
    log.info("/project bind path=%s channel=%s", path, interaction.channel_id)
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message(
            "Cannot bind: no channel context.", ephemeral=True
        )
        return

    try:
        stored = bot.project_manager.bind(channel_id, path)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ Bound this channel to `{stored}`", ephemeral=True
    )


@project_group.command(name="info", description="Show this channel's current binding.")
async def project_info(interaction: discord.Interaction) -> None:
    log.info("/project info channel=%s", interaction.channel_id)
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return

    bound_path = bot.project_manager.get_project(channel_id)
    if bound_path is None:
        await interaction.response.send_message(
            "This channel is not bound to a project. Use `/project bind` to set one.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"📁 This channel is bound to `{bound_path}`", ephemeral=True
    )


@project_group.command(name="unbind", description="Remove this channel's binding.")
async def project_unbind(interaction: discord.Interaction) -> None:
    log.info("/project unbind channel=%s", interaction.channel_id)
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return

    if bot.project_manager.unbind(channel_id):
        await interaction.response.send_message(
            "✅ Removed this channel's project binding.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "This channel had no binding to remove.", ephemeral=True
        )


session_group = app_commands.Group(
    name="session",
    description="Manage Claude sessions inside threads.",
)


@session_group.command(name="stop", description="Stop the Claude session in this thread.")
async def session_stop(interaction: discord.Interaction) -> None:
    log.info("/session stop channel=%s", interaction.channel_id)
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


@session_group.command(name="info", description="Show the current session's status.")
async def session_info(interaction: discord.Interaction) -> None:
    log.info("/session info channel=%s", interaction.channel_id)
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
    if bridge is not None and bridge.is_active:
        await interaction.response.send_message(
            f"Session active — cwd `{bridge.project_path}`.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "No active Claude session in this thread.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Console-script entry point: load config and run the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    bot = ClaudedBot(config)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
