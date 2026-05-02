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
import time
import sys
import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config, load_config
from .discord_renderer import DiscordRenderer, COLOR_INFO, COLOR_TOOL_FAILURE
from .interaction_handler import InteractionHandler
from .project_manager import ProjectManager
from .session_manager import SessionManager
from .session_store import SessionStore
from .cost_tracker import CostTracker
from .agent_manager import AgentManager

log = logging.getLogger("clauded.bot")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


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
    intents.message_content = True  # Requires Portal toggle; bot degrades if unavailable
    intents.messages = True
    intents.guilds = True
    return intents


def _build_intents_safe() -> discord.Intents:
    """Fallback intents without privileged message_content."""
    intents = discord.Intents.default()
    intents.messages = True
    intents.guilds = True
    return intents


class ClaudedBot(commands.Bot):
    """Discord bot for the claudeD bridge."""

    def __init__(self, config: Config) -> None:
        super().__init__(command_prefix="!", intents=_build_intents())
        self.config = config
        self.session_manager = SessionManager(session_store=SessionStore())
        self.project_manager = ProjectManager(projects_root=config.projects_root)
        self._start_time = time.time()
        self.cost_tracker = CostTracker()
        self.agent_manager = AgentManager()

    async def setup_hook(self) -> None:
        """Register slash command groups and sync to Discord."""
        self.tree.add_command(project_group)
        self.tree.add_command(session_group)
        self.tree.add_command(cost_group)
        self.tree.add_command(switch_model)
        self.tree.add_command(set_effort)
        self.tree.add_command(tools_group)
        self.tree.add_command(budget_group)
        self.tree.add_command(health_check)
        self.tree.add_command(review_pr)
        self.tree.add_command(agent_group)
        self.tree.add_command(mcp_group)
        self.tree.add_command(max_turns_cmd)
        self.tree.add_command(fallback_model_cmd)
        self.tree.add_command(plugin_group)
        self.tree.add_command(send_to_claude)
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
        # Only trigger if bot is mentioned
        if self.user and self.user.id not in [m.id for m in message.mentions]:
            return

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

        # Strip the bot mention from the message content
        content = message.content
        if self.user:
            content = content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
        if not content:
            content = "Hello"  # fallback if user only typed @bot with no message

        thread_name = (content or "claude session")[:100] or "claude session"
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
                system_prompt = self.project_manager.get_system_prompt(channel.id)
                extra_dirs = self.project_manager.get_extra_dirs(channel.id)
                mcp_servers = self.project_manager.get_mcp_servers(channel.id)
                async def _pre_tool_notify(tool_name: str, input_data: dict) -> None:
                    try:
                        await thread.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                    except Exception:
                        pass  # best-effort; don't break the stream

                bridge = await self.session_manager.create_session(
                    thread.id,
                    project_path,
                    self.config,
                    on_ask_user=handler.handle_ask_user_question,
                    on_pre_tool_use=_pre_tool_notify,
                    system_prompt=system_prompt,
                    add_dirs=extra_dirs or None,
                    mcp_servers=mcp_servers or None,
                )
            except Exception as exc:
                log.exception("Failed to start ClaudeBridge")
                try:
                    err_embed = discord.Embed(
                        title="❌ Error",
                        description=f"```\n{str(exc)[:500]}\n```",
                        color=COLOR_TOOL_FAILURE,
                    )
                    await thread.send(embed=err_embed)
                except discord.HTTPException:
                    log.debug("Could not post session-start error to thread")
                return

            # Feature #66: Add hourglass reaction
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass

            user_text, tmp_dir = await self._compose_user_text(message)
            # Use mention-stripped content instead of raw message content
            if tmp_dir is not None:
                # Attachments present: replace raw content portion with stripped content
                user_text = user_text.replace(message.content, content) if message.content else user_text
            else:
                user_text = content
            renderer = DiscordRenderer(thread)
            cost_before = bridge.total_cost if bridge else 0.0
            _render_ok = False
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=thread,
                    project_path=project_path,
                )
                _render_ok = True
            except Exception:
                try:
                    await message.remove_reaction("⏳", self.user)
                    await message.add_reaction("❌")
                except discord.HTTPException:
                    pass
                raise
            finally:
                _cleanup_tmp_dir(tmp_dir)
                cost_after = bridge.total_cost if bridge else 0.0
                response_cost = cost_after - cost_before
                if response_cost > 0:
                    self.cost_tracker.record(channel.id, response_cost)
                self.session_manager.save_session_state(thread.id)
                if _render_ok:
                    try:
                        await message.remove_reaction("⏳", self.user)
                        await message.add_reaction("✅")
                    except discord.HTTPException:
                        pass

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
                    system_prompt = self.project_manager.get_system_prompt(parent_id)
                    # Check for stored session to resume
                    stored = self.session_manager.get_stored_session(thread_id)
                    resume_id = stored.get("session_id") if stored else None
                    stored_model = stored.get("model") if stored else None
                    stored_prompt = stored.get("system_prompt") if stored else None
                    extra_dirs = self.project_manager.get_extra_dirs(parent_id)
                    mcp_servers = self.project_manager.get_mcp_servers(parent_id)
                    _thread_target = message.channel

                    async def _pre_tool_notify_thread(tool_name: str, input_data: dict) -> None:
                        try:
                            await _thread_target.send(f"-# 🔮 Preparing: {tool_name}...", silent=True)
                        except Exception:
                            pass  # best-effort; don't break the stream

                    bridge = await self.session_manager.create_session(
                        thread_id,
                        project_path,
                        self.config,
                        on_ask_user=handler.handle_ask_user_question,
                        on_pre_tool_use=_pre_tool_notify_thread,
                        system_prompt=stored_prompt or system_prompt,
                        model_override=stored_model,
                        resume_session_id=resume_id,
                        add_dirs=extra_dirs or None,
                        mcp_servers=mcp_servers or None,
                    )
                except Exception as exc:
                    log.exception("Failed to start ClaudeBridge for thread=%s", thread_id)
                    try:
                        err_embed = discord.Embed(
                            title="❌ Error",
                            description=f"```\n{str(exc)[:500]}\n```",
                            color=COLOR_TOOL_FAILURE,
                        )
                        await message.channel.send(embed=err_embed)
                    except discord.HTTPException:
                        log.debug("Could not post session-start error to thread")
                    return

            # Feature #66: Add hourglass reaction
            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass

            user_text, tmp_dir = await self._compose_user_text(message)
            renderer = DiscordRenderer(message.channel)
            cost_before = bridge.total_cost if bridge else 0.0
            _render_ok = False
            try:
                await self._render_with_retry(
                    renderer=renderer,
                    bridge=bridge,
                    user_text=user_text,
                    thread=message.channel,
                    project_path=project_path,
                )
                _render_ok = True
            except Exception:
                try:
                    await message.remove_reaction("⏳", self.user)
                    await message.add_reaction("❌")
                except discord.HTTPException:
                    pass
                raise
            finally:
                _cleanup_tmp_dir(tmp_dir)
                cost_after = bridge.total_cost if bridge else 0.0
                response_cost = cost_after - cost_before
                if response_cost > 0:
                    self.cost_tracker.record(parent_id, response_cost)
                self.session_manager.save_session_state(thread_id)
                if _render_ok:
                    try:
                        await message.remove_reaction("⏳", self.user)
                        await message.add_reaction("✅")
                    except discord.HTTPException:
                        pass

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
            ext = os.path.splitext(safe_name)[1].lower()
            if ext in _IMAGE_EXTENSIONS:
                notes.append(f"[User attached image: {safe_name}]\nImage file saved at: {target}")
            else:
                notes.append(f"[User attached file: {safe_name}]\nFile saved at: {target}")

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
                            err_embed = discord.Embed(
                                title="❌ Error",
                                description=f"```\n{str(start_exc)[:500]}\n```",
                                color=COLOR_TOOL_FAILURE,
                            )
                            await thread.send(embed=err_embed)
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
# Cost tracking slash commands.
# ---------------------------------------------------------------------------

cost_group = app_commands.Group(
    name="cost",
    description="Track API costs.",
    default_permissions=discord.Permissions(administrator=True),
)


@cost_group.command(name="show", description="Show cost for this channel")
async def cost_show(interaction: discord.Interaction) -> None:
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    bot.cost_tracker.reset_channel(parent_id)
    await interaction.response.send_message("\u2705 Channel cost reset.", ephemeral=True)


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

    lines = [f"📁 This channel is bound to `{bound_path}`"]
    sp = bot.project_manager.get_system_prompt(channel_id)
    if sp:
        lines.append(f"📝 System prompt: {sp}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


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


@project_group.command(name="system-prompt", description="Set a system prompt for this project")
@app_commands.describe(text="System prompt text, or 'clear' to remove")
async def project_system_prompt(interaction: discord.Interaction, text: str) -> None:
    log.info("/project system-prompt channel=%s", interaction.channel_id)
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return

    if not bot.project_manager.is_bound(channel_id):
        await interaction.response.send_message("Channel not bound. Use /project bind first.", ephemeral=True)
        return

    if text.lower() == "clear":
        bot.project_manager.clear_system_prompt(channel_id)
        await interaction.response.send_message("✅ System prompt cleared", ephemeral=True)
    else:
        bot.project_manager.set_system_prompt(channel_id, text)
        await interaction.response.send_message("✅ System prompt set", ephemeral=True)


@project_group.command(name="add-dir", description="Add extra directory access for Claude")
@app_commands.describe(path="Path to directory")
async def project_add_dir(interaction: discord.Interaction, path: str) -> None:
    log.info("/project add-dir path=%s channel=%s", path, interaction.channel_id)
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return
    if not bot.project_manager.is_bound(channel_id):
        await interaction.response.send_message("Channel not bound. Use /project bind first.", ephemeral=True)
        return
    try:
        resolved = bot.project_manager.add_extra_dir(channel_id, path)
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
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return
    dirs = bot.project_manager.get_extra_dirs(channel_id)
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
    bot: ClaudedBot = interaction.client  # type: ignore[assignment]
    channel_id = interaction.channel_id
    if channel_id is None:
        await interaction.response.send_message("No channel context.", ephemeral=True)
        return
    removed = bot.project_manager.remove_extra_dir(channel_id, path)
    if removed:
        embed = discord.Embed(
            title="📂 Extra Directory Removed",
            description=f"Removed `{path}`",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("Directory not found in extra dirs.", ephemeral=True)


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



@session_group.command(name="interrupt", description="Interrupt the current Claude operation in this thread")
async def session_interrupt(interaction: discord.Interaction) -> None:
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
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
    # Stop any existing session and create new one under lock
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        handler = InteractionHandler(interaction.channel)
        try:
            await bot.session_manager.create_session(
                thread_id, stored["project_path"], bot.config,
                system_prompt=stored.get("system_prompt"),
                model_override=stored.get("model"),
                on_ask_user=handler.handle_ask_user_question,
                resume_session_id=stored["session_id"],
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to resume: `{exc}`", ephemeral=True)
            return
    await interaction.followup.send("🔄 Session resumed with previous context.")


@session_group.command(name="list", description="List all active Claude sessions")
async def session_list(interaction: discord.Interaction) -> None:
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
            pass  # consume the response stream
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if thread_id is None or parent_id is None:
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
    await interaction.response.defer()
    project_path = bridge.project_path
    system_prompt = bridge.system_prompt
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                resume_session_id=old_session_id,
                fork_session=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to fork: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🍴 Session Forked",
        description=f"New session branched from `{old_session_id[:12]}…`",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


@session_group.command(name="worktree", description="Create a git worktree for isolated work")
@app_commands.describe(name="Worktree name (branch name)")
async def session_worktree(interaction: discord.Interaction, name: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    extra_dirs = bot.project_manager.get_extra_dirs(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                worktree=name,
                add_dirs=extra_dirs or None,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to create worktree: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🌲 Worktree Created",
        description=f"Session started with worktree **{name}**.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Top-level slash commands
# ---------------------------------------------------------------------------

@app_commands.command(name="model", description="Switch Claude model for this thread")
@app_commands.describe(name="Model: sonnet, opus, haiku, or full model ID")
async def switch_model(interaction: discord.Interaction, name: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    # Get project path and system prompt
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    # Find the thread/channel for InteractionHandler
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        await bot.session_manager.create_session(
            thread_id, project_path, bot.config,
            system_prompt=system_prompt,
            model_override=name,
            on_ask_user=handler.handle_ask_user_question,
        )
    await interaction.followup.send(f"🔄 Switched to `{name}`. New session started.")


# ---------------------------------------------------------------------------
# Autocomplete handlers (#64)
# ---------------------------------------------------------------------------

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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                effort=level.value,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to set effort: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🧠 Effort Level Set",
        description=f"Thinking effort set to **{level.value}**. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /tools command group
# ---------------------------------------------------------------------------

tools_group = app_commands.Group(
    name="tools",
    description="Control Claude's available tools.",
)


@tools_group.command(name="allow", description="Only allow specific tools")
@app_commands.describe(tools="Space-separated tool names: Bash Edit Read Write")
async def tools_allow(interaction: discord.Interaction, tools: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    tool_list = tools.split()
    if not tool_list:
        await interaction.response.send_message("Provide at least one tool name.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                allowed_tools=tool_list,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to set tools: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🔧 Allowed Tools Set",
        description=f"Only these tools are allowed: `{' '.join(tool_list)}`",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


@tools_group.command(name="deny", description="Deny specific tools")
@app_commands.describe(tools="Space-separated tool names: WebSearch Bash")
async def tools_deny(interaction: discord.Interaction, tools: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    tool_list = tools.split()
    if not tool_list:
        await interaction.response.send_message("Provide at least one tool name.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                disallowed_tools=tool_list,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to set tools: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🚫 Denied Tools Set",
        description=f"These tools are denied: `{' '.join(tool_list)}`",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


@tools_group.command(name="reset", description="Reset to default tools")
async def tools_reset(interaction: discord.Interaction) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to reset tools: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🔧 Tools Reset",
        description="All tools restored to defaults.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /budget command group
# ---------------------------------------------------------------------------

budget_group = app_commands.Group(
    name="budget",
    description="Control session spending.",
)


@budget_group.command(name="set", description="Set max budget per session (USD)")
@app_commands.describe(amount="Maximum USD to spend per session")
async def budget_set(interaction: discord.Interaction, amount: float) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("Budget must be positive.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    # Store the budget in the project binding
    bot.project_manager.set_budget(parent_id, amount)
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                max_budget_usd=amount,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to set budget: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="💵 Budget Set",
        description=f"Max session budget: **${amount:.2f}**. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


@budget_group.command(name="show", description="Show current budget setting")
async def budget_show(interaction: discord.Interaction) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    parent_id = getattr(interaction.channel, "parent_id", None) or interaction.channel_id
    budget = bot.project_manager.get_budget(parent_id)
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    parent_id = getattr(interaction.channel, "parent_id", None) or interaction.channel_id
    bot.project_manager.clear_budget(parent_id)
    embed = discord.Embed(
        title="💵 Budget Cleared",
        description="Budget limit removed. Sessions are now unlimited.",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app_commands.command(name="health", description="Show bot health and status")
async def health_check(interaction: discord.Interaction) -> None:
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

    # Get claude version
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        claude_version = stdout.decode().strip() or "unknown"
    except Exception:
        claude_version = "unavailable"

    embed = discord.Embed(title="🏥 Bot Health", color=COLOR_INFO)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Sessions", value=str(active_sessions), inline=True)
    embed.add_field(name="Bound Projects", value=str(bound_projects), inline=True)
    embed.add_field(name="Claude CLI", value=claude_version, inline=True)
    embed.add_field(name="Python", value=sys.version.split()[0], inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------------------------------------------------------------------
# /review command
# ---------------------------------------------------------------------------

@app_commands.command(name="review", description="Start a PR review session")
@app_commands.describe(pr="PR number or URL")
async def review_pr(interaction: discord.Interaction, pr: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    await interaction.response.defer()
    channel = interaction.channel
    # Must be in a bound channel (or its thread)
    parent_id = getattr(channel, "parent_id", None) or channel.id
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.followup.send(
            embed=discord.Embed(title="❌ Channel not bound", color=COLOR_TOOL_FAILURE)
        )
        return
    # Create thread for the review
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
    # Create session with from-pr flag
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    extra_dirs = bot.project_manager.get_extra_dirs(parent_id)
    handler = InteractionHandler(thread)
    lock = bot.session_manager.get_lock(thread.id)
    async with lock:
        try:
            bridge = await bot.session_manager.create_session(
                thread.id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                from_pr=pr,
                add_dirs=extra_dirs or None,
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
# /agent command group
# ---------------------------------------------------------------------------

agent_group = app_commands.Group(
    name="agent",
    description="Manage custom Claude agents.",
)


@agent_group.command(name="create", description="Create a custom agent")
@app_commands.describe(name="Agent name", prompt="Agent system prompt", description="Optional description")
async def agent_create(
    interaction: discord.Interaction, name: str, prompt: str, description: str = ""
) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    bot.agent_manager.create(name, prompt, description)
    embed = discord.Embed(
        title=f"\u2705 Agent `{name}` created",
        description=f"Prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="list", description="List available agents")
async def agent_list(interaction: discord.Interaction) -> None:
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    agent = bot.agent_manager.get(name)
    if not agent:
        await interaction.response.send_message(f"\u274c Agent `{name}` not found.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    agents_json = {name: {"description": agent["description"], "prompt": agent["prompt"]}}
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    extra_dirs = bot.project_manager.get_extra_dirs(parent_id)
    mcp_servers = bot.project_manager.get_mcp_servers(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                agent_name=name,
                custom_agents=agents_json,
                add_dirs=extra_dirs or None,
                mcp_servers=mcp_servers or None,
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Failed to use agent: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title=f"\U0001f916 Agent `{name}` activated",
        description=f"{agent['description']}\nNew session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


@agent_group.command(name="delete", description="Delete a custom agent")
@app_commands.describe(name="Agent name")
async def agent_delete(interaction: discord.Interaction, name: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
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


# ---------------------------------------------------------------------------
# Autocomplete: agent_use (#64)
# ---------------------------------------------------------------------------

async def agent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        return []
    agents = bot.agent_manager.list_all()
    return [app_commands.Choice(name=n, value=n) for n in agents if current.lower() in n.lower()][:25]

agent_use.autocomplete("name")(agent_autocomplete)


# ---------------------------------------------------------------------------
# /mcp command group
# ---------------------------------------------------------------------------

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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    config: dict = {"type": "stdio", "command": command}
    if args:
        config["args"] = args.split()
    bot.project_manager.add_mcp_server(parent_id, name, config)
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: stdio\nCommand: `{command}`" + (f"\nArgs: `{args}`" if args else ""),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="add-url", description="Add an HTTP MCP server")
@app_commands.describe(name="Server name", url="Server URL")
async def mcp_add_url(interaction: discord.Interaction, name: str, url: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    config: dict = {"type": "http", "url": url}
    bot.project_manager.add_mcp_server(parent_id, name, config)
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: http\nURL: `{url}`",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="list", description="List configured MCP servers")
async def mcp_list(interaction: discord.Interaction) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    servers = bot.project_manager.get_mcp_servers(parent_id)
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
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    channel_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None) or channel_id
    if bot.project_manager.remove_mcp_server(parent_id, name):
        embed = discord.Embed(
            title=f"\u2705 MCP server `{name}` removed",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            f"\u274c MCP server `{name}` not found.", ephemeral=True
        )



# ---------------------------------------------------------------------------
# /max-turns command (#52)
# ---------------------------------------------------------------------------

@app_commands.command(name="max-turns", description="Set maximum turns for Claude session")
@app_commands.describe(number="Maximum number of turns")
async def max_turns_cmd(interaction: discord.Interaction, number: int) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    if number < 1:
        await interaction.response.send_message("Number must be at least 1.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                max_turns=number,
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Failed to set max turns: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🔄 Max Turns Set",
        description=f"Max turns set to **{number}**. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /fallback-model command (#53)
# ---------------------------------------------------------------------------

@app_commands.command(name="fallback-model", description="Set fallback model for Claude session")
@app_commands.describe(model="Fallback model name or ID")
async def fallback_model_cmd(interaction: discord.Interaction, model: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                fallback_model=model,
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Failed to set fallback model: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🔄 Fallback Model Set",
        description=f"Fallback model set to **{model}**. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /session security-review command (#57)
# ---------------------------------------------------------------------------

@session_group.command(name="security-review", description="Run a security review on the current project")
async def session_security_review(interaction: discord.Interaction) -> None:
    """Send /security-review to the Claude session."""
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
        async for _ in bridge.send_message("/security-review"):
            pass  # consume the response stream
        embed = discord.Embed(
            title="🔒 Security Review",
            description="Security review completed.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"\u274c Failed to run security review: `{exc}`", ephemeral=True)


# ---------------------------------------------------------------------------
# /plugin command group (#58)
# ---------------------------------------------------------------------------

plugin_group = app_commands.Group(
    name="plugin",
    description="Manage Claude plugins",
    default_permissions=discord.Permissions(administrator=True),
)


@plugin_group.command(name="add", description="Add plugin directory and restart session")
@app_commands.describe(path="Path to plugin directory")
async def plugin_add(interaction: discord.Interaction, path: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()

    # Validate plugin path against projects_root
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        await interaction.followup.send(embed=discord.Embed(title="❌ Not a directory", color=COLOR_TOOL_FAILURE), ephemeral=True)
        return

    try:
        resolved.relative_to(Path(bot.config.projects_root).resolve())
    except ValueError:
        await interaction.followup.send(embed=discord.Embed(
            title="❌ Path outside allowed root",
            description=f"Plugin path must be under `{bot.config.projects_root}`",
            color=COLOR_TOOL_FAILURE
        ), ephemeral=True)
        return

    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                plugin_dirs=[path],
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Failed to add plugin: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="🔌 Plugin Added",
        description=f"Plugin directory `{path}` added. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /session settings command (#59)
# ---------------------------------------------------------------------------

@session_group.command(name="settings", description="Apply custom settings JSON to session")
@app_commands.describe(json_str="Settings JSON string")
async def session_settings(interaction: discord.Interaction, json_str: str) -> None:
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = interaction.channel_id
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id is None:
        await interaction.response.send_message("Use this command inside a thread.", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message("Parent channel not bound.", ephemeral=True)
        return
    await interaction.response.defer()
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    channel = interaction.channel
    handler = InteractionHandler(channel)
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question,
                settings=json_str,
            )
        except Exception as exc:
            await interaction.followup.send(f"\u274c Failed to apply settings: `{exc}`", ephemeral=True)
            return
    embed = discord.Embed(
        title="⚙️ Settings Applied",
        description="Custom settings applied. New session started.",
        color=COLOR_INFO,
    )
    await interaction.followup.send(embed=embed)




# ---------------------------------------------------------------------------
# Context menu: Send to Claude (#65)
# ---------------------------------------------------------------------------

@app_commands.context_menu(name="Send to Claude")
async def send_to_claude(interaction: discord.Interaction, message: discord.Message):
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
    # Create thread from the target message
    thread_name = f"Claude: {message.content[:80]}" if message.content else "Claude session"
    try:
        thread = await message.create_thread(name=thread_name)
    except discord.HTTPException:
        await interaction.followup.send("❌ Failed to create thread.", ephemeral=True)
        return
    # Create session and send
    system_prompt = bot.project_manager.get_system_prompt(channel_id)
    handler = InteractionHandler(thread)
    lock = bot.session_manager.get_lock(thread.id)
    async with lock:
        try:
            bridge = await bot.session_manager.create_session(
                thread.id, project_path, bot.config,
                system_prompt=system_prompt,
                on_ask_user=handler.handle_ask_user_question)
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to start session: `{exc}`", ephemeral=True)
            return
    renderer = DiscordRenderer(thread)
    user_text = message.content or "Hello"
    await renderer.render_response(bridge, user_text)
    await interaction.followup.send(f"✅ Sent to Claude in {thread.mention}", ephemeral=True)


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
    try:
        bot.run(config.discord_bot_token, log_handler=None)
    except discord.errors.PrivilegedIntentsRequired:
        log.warning(
            "Message Content Intent not enabled in Discord Developer Portal. "
            "Retrying without message_content intent — @mention triggers will "
            "not work, but slash commands will. Enable the intent at "
            "https://discord.com/developers/applications/ for full functionality."
        )
        # Retry with safe intents
        bot2 = ClaudedBot.__new__(ClaudedBot)
        commands.Bot.__init__(bot2, command_prefix="!", intents=_build_intents_safe())
        bot2.config = config
        bot2.project_manager = bot.project_manager
        bot2.session_manager = bot.session_manager
        bot2.cost_tracker = bot.cost_tracker
        bot2.agent_manager = bot.agent_manager
        bot2._start_time = bot._start_time
        bot2.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
