"""Discord bot entrypoint for claudeD.

Wires up event handlers (on_ready, on_message) and registers slash command
groups (`/project`, `/session`). The on_message handler bridges Discord
messages to a per-thread :class:`ClaudeBridge` session.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, load_config
from .discord_renderer import DiscordRenderer
from .interaction_handler import InteractionHandler
from .project_manager import ProjectManager
from .session_manager import SessionManager

log = logging.getLogger("clauded.bot")


def _cleanup_tmp_dir(tmp_dir: Path | None) -> None:
    """Best-effort cleanup of an attachment temp directory.

    Called after the renderer finishes (success or failure) to avoid
    leaking on-disk attachments for the lifetime of the process. We
    swallow ``OSError`` because the worst case is a stale temp dir that
    the OS will eventually clean up.
    """
    if tmp_dir is None:
        return
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:  # pragma: no cover - rmtree(ignore_errors=True) shouldn't raise
        log.debug("Failed to clean up attachment tempdir %s", tmp_dir)


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
        self.config = config
        self.session_manager = SessionManager()
        self.project_manager = ProjectManager(projects_root=config.projects_root)

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

        thread_name = (message.content or "claude session")[:100] or "claude session"
        try:
            thread = await message.create_thread(name=thread_name)
        except discord.Forbidden:
            log.exception("Missing permission to create threads in channel=%s", channel.id)
            try:
                await channel.send(
                    "❌ I don't have permission to create threads in this channel."
                )
            except discord.HTTPException:
                log.debug("Could not surface thread-permission error to channel")
            return
        except discord.HTTPException:
            log.exception("Failed to create thread for channel=%s", channel.id)
            try:
                await channel.send("❌ Failed to create a thread for this message.")
            except discord.HTTPException:
                log.debug("Could not surface thread-creation error to channel")
            return

        # Acquire the per-thread lock *before* creating the session so a
        # concurrent thread message that Discord delivers out of order can't
        # race in and replace+disconnect the bridge we're about to build.
        async with self.session_manager.get_lock(thread.id):
            try:
                handler = InteractionHandler(thread)
                bridge = await self.session_manager.create_session(
                    thread.id,
                    project_path,
                    self.config,
                    on_ask_user=handler.handle_ask_user_question,
                )
            except Exception as exc:
                log.exception("Failed to start ClaudeBridge")
                try:
                    await thread.send(f"❌ Failed to start Claude session: `{exc}`")
                except discord.HTTPException:
                    log.debug("Could not post session-start error to thread")
                return

            user_text, tmp_dir = await self._compose_user_text(message)
            renderer = DiscordRenderer(thread)
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=thread,
                    project_path=project_path,
                )
            finally:
                _cleanup_tmp_dir(tmp_dir)

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
        # Acquire the per-thread lock for the entire send/render cycle so
        # concurrent messages in the same thread are processed in order
        # rather than racing each other into the SDK.
        async with self.session_manager.get_lock(thread_id):
            bridge = self.session_manager.get_session(thread_id)
            if bridge is None or not bridge.is_active:
                try:
                    handler = InteractionHandler(message.channel)
                    bridge = await self.session_manager.create_session(
                        thread_id,
                        project_path,
                        self.config,
                        on_ask_user=handler.handle_ask_user_question,
                    )
                except Exception as exc:
                    log.exception("Failed to start ClaudeBridge for thread=%s", thread_id)
                    try:
                        await message.channel.send(
                            f"❌ Failed to start Claude session: `{exc}`"
                        )
                    except discord.HTTPException:
                        log.debug("Could not post session-start error to thread")
                    return

            user_text, tmp_dir = await self._compose_user_text(message)
            renderer = DiscordRenderer(message.channel)
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=message.channel,
                    project_path=project_path,
                )
            finally:
                _cleanup_tmp_dir(tmp_dir)

    # ------------------------------------------------------------------
    # Helpers used by both channel- and thread-message handlers
    # ------------------------------------------------------------------

    async def _compose_user_text(
        self, message: discord.Message
    ) -> tuple[str, Path | None]:
        """Build the text prompt sent to Claude, including any attachments.

        For each attachment we download it to a per-message temp directory
        and prepend a line announcing the filename and on-disk path so
        Claude can choose to ``Read`` it. The temp directory is returned
        alongside the prompt so the caller can clean it up after Claude
        finishes processing the message. Discord caps attachment size at
        25MB on free guilds, so the on-disk footprint is bounded.
        """
        text = message.content or ""
        attachments = list(message.attachments or [])
        if not attachments:
            return text, None

        tmp_dir = Path(tempfile.mkdtemp(prefix="clauded_att_"))
        notes: list[str] = []
        for att in attachments:
            # Sanitize the filename: take the basename and drop anything that
            # looks like path traversal. Discord already restricts these but
            # better safe.
            safe_name = os.path.basename(att.filename or "attachment")
            if not safe_name or safe_name in ("", ".", ".."):
                safe_name = f"attachment-{att.id}"
            target = tmp_dir / safe_name
            try:
                await att.save(target)
            except (discord.HTTPException, OSError):
                log.exception("Failed to save attachment %s", safe_name)
                continue
            notes.append(f"User attached file: {safe_name} at {target}")

        if not notes:
            # No attachments actually saved — drop the empty tmp dir now.
            _cleanup_tmp_dir(tmp_dir)
            return text, None
        # Prepend so Claude sees the file references before the user's prose.
        prefix = "\n".join(notes)
        composed = f"{prefix}\n\n{text}" if text else prefix
        return composed, tmp_dir

    async def _render_with_retry(
        self,
        *,
        renderer: DiscordRenderer,
        bridge,  # ClaudeBridge — typed loosely to avoid an extra import
        user_text: str,
        thread: discord.abc.Messageable,
        project_path: str,
    ) -> None:
        """Run ``renderer.render_response`` and surface a retry button on crash.

        On exception we drop the (now-dead) bridge so the next message —
        either via the retry button or a fresh user message — recreates a
        clean session.
        """
        try:
            await renderer.render_response(bridge, user_text)
        except Exception as exc:
            log.exception("Renderer failed; offering retry button")
            thread_id = getattr(thread, "id", None)
            if thread_id is not None:
                await self.session_manager.stop_session(thread_id)

            async def _on_retry() -> None:
                # Re-acquire the lock so a manual click can't race with a
                # follow-up message the user just typed.
                if thread_id is None:
                    return
                async with self.session_manager.get_lock(thread_id):
                    try:
                        new_handler = InteractionHandler(thread)
                        new_bridge = await self.session_manager.create_session(
                            thread_id,
                            project_path,
                            self.config,
                            on_ask_user=new_handler.handle_ask_user_question,
                        )
                    except Exception as start_exc:
                        log.exception("Retry: failed to restart ClaudeBridge")
                        try:
                            await thread.send(
                                f"❌ Retry failed to start session: `{start_exc}`"
                            )
                        except discord.HTTPException:
                            log.debug("Retry: could not surface restart error")
                        return
                    new_renderer = DiscordRenderer(thread)
                    await self._render_with_retry(
                        renderer=new_renderer,
                        bridge=new_bridge,
                        user_text=user_text,
                        thread=thread,
                        project_path=project_path,
                    )

            await renderer.send_error_with_retry(exc, _on_retry)


# ---------------------------------------------------------------------------
# Slash command groups.
# ---------------------------------------------------------------------------

project_group = app_commands.Group(
    name="project",
    description="Manage channel ↔ project-directory bindings.",
    # Restrict the entire /project group to guild administrators. Binding a
    # channel to a directory effectively grants every poster shell access to
    # that path (Claude can run tools); this is not a power we want to give
    # to ordinary members. ``default_permissions`` is the slash-command
    # equivalent of a permission gate — non-admins won't even see the
    # commands in their picker.
    default_permissions=discord.Permissions(administrator=True),
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
        # Pull the running totals the bridge has been collecting from
        # ResultMessage events. Defaults are sensible for a session that
        # hasn't completed a turn yet.
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _ensure_cli_path() -> None:
    """Make sure common ``claude`` CLI install locations are on ``$PATH``.

    The ``claude-code-sdk`` resolves the ``claude`` binary via
    ``shutil.which`` at session-start time. Inside a Python venv on macOS
    the activated ``$PATH`` often omits ``/opt/homebrew/bin`` (and on Linux
    setups, ``/usr/local/bin`` or ``~/.local/bin``), which makes the SDK
    fail to start with a confusing "claude not found" error even though
    the CLI is installed. We prepend known-good locations once at process
    startup so the lookup succeeds regardless of how the bot was launched.
    """
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    prepend = [p for p in extra if p and p not in parts]
    if prepend:
        os.environ["PATH"] = (
            os.pathsep.join(prepend + parts) if parts else os.pathsep.join(prepend)
        )


def main() -> None:
    """Console-script entry point: load config and run the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Ensure the Claude CLI is discoverable before we touch the SDK.
    _ensure_cli_path()

    config = load_config()
    bot = ClaudedBot(config)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
