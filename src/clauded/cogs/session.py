"""Session management commands: /session group."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ._unbound import reject_if_unbound
from ._sessions_disk import (
    _resolve_project_dir, _list_sessions, _get_info, _rename, _tag, _delete,
    _resolve_session_id, _fmt_session_label, _session_id_autocomplete,
)
from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ..interaction_handler import InteractionHandler
from ..session_config import SessionConfig

log = logging.getLogger("clauded.bot")


session_group = app_commands.Group(
    name="session",
    description="Manage Claude sessions inside threads.",
)


@session_group.command(name="stop", description="Stop the Claude session in this thread.")
async def session_stop(interaction: discord.Interaction) -> None:
    log.info("/session stop channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
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

    # #audit(#4): interrupt any in-flight turn, THEN hold the per-thread lock
    # across the stop. A bare stop_session → bridge.stop() → client.disconnect()
    # closes the anyio TaskGroup out from under a concurrent receive_response()
    # and crashes the turn (the bridge documents this hazard at
    # claude_bridge.py:794). Defer first because acquiring the lock may wait for
    # the in-flight turn to end. Mirrors /session clear's locked teardown.
    await interaction.response.defer(ephemeral=True)
    bridge = bot.session_manager.get_session(thread_id)
    if bridge is not None and bridge.is_active:
        try:
            await bridge.interrupt()
        except Exception:
            log.debug("session_stop: interrupt before stop failed; continuing", exc_info=True)
    async with bot.session_manager.get_lock(thread_id):
        stopped = await bot.session_manager.stop_session(thread_id)
    if stopped:
        await interaction.followup.send("Claude session stopped.", ephemeral=True)
    else:
        await interaction.followup.send(
            "No active Claude session in this thread.", ephemeral=True
        )


@session_group.command(
    name="clear",
    description="Drop context and start a fresh session in this thread (#163 sub-task 2).",
)
async def session_clear(interaction: discord.Interaction) -> None:
    """Tear down the current bridge AND remove the persisted resume entry.

    The CLI's native ``/clear`` semantics: start a new session with empty
    context; the previous session stays on disk (Claude's own jsonl history)
    but won't be resumed by this thread.

    Inverse of ``/session resume``. Long sessions warrant a clean restart
    without leaving the thread — this gives users that control without
    requiring them to ``/session stop`` + leave + rebind.

    Implementation:
      - Stop the live bridge (if any) via ``stop_session``
      - Remove the entry from ``data/sessions.json`` via
        ``session_store.remove_session`` so the next user message starts
        a fresh session (no ``resume_session_id`` in SessionConfig)
    """
    log.info("/session clear channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
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

    # Atomic clear: holds the per-thread lock around stop + remove so a
    # concurrent /session resume (which also takes the lock) can't race in.
    had_active, had_stored = await bot.session_manager.clear_session(thread_id)

    if had_active or had_stored:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ Session cleared",
                description=(
                    "Dropped this thread's Claude session context. The next "
                    "message will start a fresh session (no resume)."
                ),
                color=COLOR_INFO,
            ),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "No session to clear in this thread.", ephemeral=True
        )


@session_group.command(name="info", description="Show the current session's status.")
async def session_info(interaction: discord.Interaction) -> None:
    log.info("/session info channel=%s", interaction.channel_id)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    bridge = (
        bot.session_manager.get_session(thread_id) if thread_id is not None else None
    )
    if bridge is not None and bridge.is_active:
        # #210: 4-case dispatch mirroring ``/model current`` (cogs/model.py).
        # Reuse ``_model_source_for_bridge`` so the rendered model display
        # accurately reflects which tier the value came from. Sibling-cog
        # import is safe (no circular: cogs/model.py only imports from
        # ..discord_renderer).
        from .model import _model_source_for_bridge

        source, value = _model_source_for_bridge(bridge)
        _placeholder = "(unknown — send a message to discover)"
        if source == "override":
            # User ran /model switch X — show X with no suffix
            model_display = f"`{value}`"
        elif source == "env":
            model_display = f"`{value}` (CLAUDE_MODEL env)"
        elif source == "sdk":
            # #210 R1 syntax: label matches ``/model current`` ("CLI default")
            # so the same logical value renders identically across both
            # surfaces. Internals call this tier `sdk_model` because the
            # value flows from ``ResultMessage.model``, but user-facing
            # copy says "CLI default" (the value originally came from
            # ~/.claude/settings.json via the SDK).
            #
            # #210 R1 security: ``value`` originates in
            # ``ResultMessage.model``, which is attacker-influenceable
            # (a malicious proxy could return backticks or pathological
            # strings). Strip backticks + cap length before embedding
            # in the inline code fence to prevent the fence from being
            # broken open. Risk class is Discord rendering only (no
            # XSS surface), so this is defense-in-depth.
            safe_value = str(value).replace("`", "'")[:120]
            model_display = f"`{safe_value}` (CLI default)"
        else:
            # source == "unset": bridge active but no _sdk_model yet
            model_display = _placeholder
        cost_str = f"${bridge.total_cost:.4f}" if bridge.total_cost else "$0.0000"
        # #211: surface the current permission mode + source-tier (parity
        # with ``/mode current`` and ``/health``). Centralized formatter
        # lives in cogs.mode so all three surfaces share one label format.
        from .mode import _mode_source_for_bridge, _format_mode_display

        mode_source, mode_value = _mode_source_for_bridge(bridge)
        mode_line = _format_mode_display(mode_value, mode_source)
        lines = [
            f"📡 **Session active** — cwd `{bridge.project_path}`",
            f"• Session ID: `{bridge.session_id or '(pending — send a message first)'}`",
            f"• Model: {model_display}",
            f"• Mode: {mode_line}",
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
    from ..bot import ClaudedBot
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
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    thread_id = interaction.channel_id
    if thread_id is None:
        await interaction.response.send_message("No thread context.", ephemeral=True)
        return
    stored = bot.session_manager.get_stored_session(thread_id)
    if not stored:
        await interaction.response.send_message("No saved session to resume.", ephemeral=True)
        return
    # #295: system_prompt and project_path are no longer shadowed in
    # sessions.json — read them from ``ProjectManager`` (canonical source)
    # keyed off the parent channel so bindings still resolve inside threads.
    parent_id = getattr(interaction.channel, "parent_id", None) or thread_id
    project_path = bot.project_manager.get_path(parent_id)
    if not project_path:
        await interaction.response.send_message(
            "Cannot resume: this channel is no longer bound to a project. "
            "Use `/project bind` first.",
            ephemeral=True,
        )
        return
    system_prompt = bot.project_manager.get_system_prompt(parent_id)
    await interaction.response.defer()
    lock = bot.session_manager.get_lock(thread_id)
    async with lock:
        await bot.session_manager.stop_session(thread_id)
        handler = InteractionHandler(interaction.channel)
        # #210: deliberately do NOT read stored.get("model"). Legacy entries
        # may carry "sonnet" pollution from pre-#199 builds; reinjecting it
        # would re-force the sonnet override that #198 set out to fix.
        # Cross-restart ``model_override`` is intentionally ephemeral per
        # user intent ("没设置就是 claude code 默认的"). The SDK falls back
        # to ~/.claude/settings.json (CLI default) when model_override is None.
        sc = SessionConfig(
            system_prompt=system_prompt,
            model_override=None,  # #210: ephemeral; see note above
            # #211: opposite of model_override — permission_mode_override
            # IS persistent (PRD user decision #4 contrast with #210). Read
            # the stored value if present; legacy rows without the field
            # safely return None and fall through to env / "default".
            permission_mode_override=stored.get("permission_mode_override"),
            on_ask_user=handler.handle_ask_user_question,
            resume_session_id=stored["session_id"],
        )
        try:
            await bot.session_manager.create_session(
                thread_id, project_path, bot.config, sc,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to resume: `{exc}`", ephemeral=True)
            return
    await interaction.followup.send("🔄 Session resumed with previous context.")


@session_group.command(name="list", description="List all active Claude sessions")
async def session_list(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
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
    from ..bot import ClaudedBot
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
        # #audit(#2): hold the per-thread lock so /compact serializes with any
        # in-flight user turn — otherwise both consume the single shared SDK
        # receive stream concurrently and silently split messages (lost
        # ResultMessage / interleaved text). Mirrors /session resume.
        async with bot.session_manager.get_lock(thread_id):
            async for _ in bridge.send_message("/compact"):
                pass
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
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    thread_id = interaction.channel_id
    if thread_id is None:
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
    new_bridge = await bot._recreate_session(
        interaction,
        resume_session_id=old_session_id,
        fork_session=True,
    )
    if new_bridge:
        embed = discord.Embed(
            title="🍴 Session Forked",
            description=f"New session branched from `{old_session_id[:12]}…`\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="worktree", description="Create a git worktree for isolated work")
@app_commands.describe(name="Worktree name (branch name)")
async def session_worktree(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, worktree=name, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="🌲 Worktree Created",
            description=f"Session started with worktree **{name}**.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="pin", description="Pin the last Claude reply")
async def session_pin(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    try:
        async for msg in channel.history(limit=10):
            if msg.author.bot and msg.content:
                await msg.pin()
                await interaction.response.send_message("📌 Pinned.", ephemeral=True)
                return
    except discord.HTTPException:
        pass
    await interaction.response.send_message("No reply to pin.", ephemeral=True)


@session_group.command(name="name", description="Set session display name")
@app_commands.describe(name="Display name for the session")
async def session_name(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, session_name=name, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title=f"📛 Session named: {name}",
            description="✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="security-review", description="Run a security review on the current project")
async def session_security_review(interaction: discord.Interaction) -> None:
    """Send /security-review to the Claude session."""
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
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
        # #audit(#3): same single-consumer invariant as /session compact —
        # serialize against any in-flight turn via the per-thread lock so the
        # two turns never split the shared SDK receive stream.
        async with bot.session_manager.get_lock(thread_id):
            async for _ in bridge.send_message("/security-review"):
                pass
        embed = discord.Embed(
            title="🔒 Security Review",
            description="Security review completed.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"❌ Failed to run security review: `{exc}`", ephemeral=True)


@session_group.command(name="settings", description="Apply custom settings JSON to session")
@app_commands.describe(json_str="Settings JSON string")
async def session_settings(interaction: discord.Interaction, json_str: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, settings=json_str, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="⚙️ Settings Applied",
            description="Custom settings applied.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@session_group.command(name="export", description="Export conversation history as markdown")
async def session_export(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.followup.send("Bot not ready.", ephemeral=True)
        return

    thread_id = interaction.channel_id
    channel = interaction.channel

    if thread_id is None or not hasattr(channel, 'history'):
        await interaction.followup.send("Use this in a thread.", ephemeral=True)
        return

    messages = []
    async for msg in channel.history(limit=500, oldest_first=True):
        role = "🤖 Claude" if msg.author.bot else f"👤 {msg.author.display_name}"
        content = msg.content or ""
        for embed in msg.embeds:
            if embed.description:
                content += "\n" + embed.description
            if embed.title:
                content = "**" + embed.title + "**\n" + content
        if content.strip():
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            messages.append(f"### {role} — {timestamp}\n\n{content}\n")

    if not messages:
        await interaction.followup.send("No messages to export.", ephemeral=True)
        return

    thread_name = getattr(channel, 'name', 'session')
    md = f"# {thread_name}\n\nExported {len(messages)} messages.\n\n---\n\n"
    md += "\n---\n\n".join(messages)

    import io
    file = discord.File(io.BytesIO(md.encode()), filename=f"{thread_name[:50]}.md")
    await interaction.followup.send("📄 Session exported:", file=file, ephemeral=True)


# ---------------------------------------------------------------------------
# #audit(#15): browse / open / rename / tag / delete PAST on-disk sessions.
# /session list shows only LIVE in-memory bridges; these surface the SDK's
# on-disk session store (keyed by project directory). Every SDK call routes
# through cogs/_sessions_disk (asyncio.to_thread) so a directory scan / jsonl
# edit / unlink never blocks the live bot's event loop.
# ---------------------------------------------------------------------------


class DeleteConfirmView(discord.ui.View):
    """Author-gated confirm for the irreversible ``/session delete``."""

    def __init__(self, directory: str, sid: str, author_id: int) -> None:
        super().__init__(timeout=30)
        self._dir = directory
        self._sid = sid
        self._author = author_id

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        # #audit(#15) BLOCKER-2: a bare `return False` leaves the other user's
        # client showing "This interaction failed" with no reason — send an
        # explicit ephemeral rejection instead.
        if itx.user.id != self._author:
            await itx.response.send_message(
                "Only the person who ran `/session delete` can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def _confirm(self, itx: discord.Interaction, _btn: discord.ui.Button) -> None:
        try:
            await _delete(self._dir, self._sid)
        except Exception as exc:
            await itx.response.edit_message(
                embed=discord.Embed(
                    title="❌ Delete failed", description=f"`{exc}`",
                    color=COLOR_TOOL_FAILURE,
                ),
                view=None,
            )
            return
        await itx.response.edit_message(
            embed=discord.Embed(
                title="🗑️ Deleted", description=f"`{self._sid[:8]}` removed.",
                color=COLOR_INFO,
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(self, itx: discord.Interaction, _btn: discord.ui.Button) -> None:
        await itx.response.edit_message(
            embed=discord.Embed(title="Cancelled", color=COLOR_INFO), view=None
        )


@session_group.command(name="history", description="Browse past on-disk Claude sessions for this project.")
@app_commands.describe(limit="How many to show (1-25, default 10)")
async def session_history(interaction: discord.Interaction, limit: int = 10) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    directory = _resolve_project_dir(interaction, bot)
    if not directory:
        # #audit(#15) polish: don't tell a DM user to "run /project bind" — just
        # state the requirement neutrally.
        await interaction.response.send_message(
            "This channel isn't bound to a project — `/session history` needs one.",
            ephemeral=True,
        )
        return
    limit = max(1, min(25, limit))
    await interaction.response.defer(ephemeral=True)
    try:
        sessions = await _list_sessions(directory, limit=limit)
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Could not read session history: `{exc}`", ephemeral=True
        )
        return
    if not sessions:
        await interaction.followup.send(
            "No past sessions on disk for this project.", ephemeral=True
        )
        return
    cur_id = bot._get_resume_session_id(interaction.channel_id)
    embed = discord.Embed(title="🗂️ Past sessions", description=f"`{directory}`", color=COLOR_INFO)
    for x in sessions:
        mark = "▶ " if x.session_id == cur_id else ""
        meta = [f"`{x.session_id[:8]}`", f"<t:{x.last_modified}:R>"]
        if getattr(x, "tag", None):
            meta.append(f"#{x.tag}")
        embed.add_field(
            name=f"{mark}{_fmt_session_label(x)}"[:256],
            value=" • ".join(meta)[:1024],
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@session_group.command(name="open", description="Resume a specific past session into this thread.")
@app_commands.describe(session_id="Pick from history (autocomplete)")
@app_commands.autocomplete(session_id=_session_id_autocomplete)
async def session_open(interaction: discord.Interaction, session_id: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    directory = _resolve_project_dir(interaction, bot)
    # #audit(#15) BLOCKER-1: validate with a single cheap get_session_info (NOT a
    # full list_sessions scan) so we don't blow Discord's 3s ack window before
    # _recreate_session (which defers first). Autocomplete delivers the full UUID.
    try:
        info = await _get_info(directory, session_id)
    except Exception as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    if info is None:
        await interaction.response.send_message(
            "❌ No such session in this project — pick one from the autocomplete.",
            ephemeral=True,
        )
        return
    bridge = await bot._recreate_session(interaction, resume_session_id=session_id)
    if bridge:
        await interaction.followup.send(embed=discord.Embed(
            title="📂 Session opened",
            description=f"Resumed `{session_id[:12]}…` — context restored.",
            color=COLOR_INFO,
        ))


@session_group.command(name="rename", description="Set a past session's custom title (distinct from /session name).")
@app_commands.describe(session_id="Pick from history (autocomplete)", title="New custom title for the session")
@app_commands.autocomplete(session_id=_session_id_autocomplete)
async def session_rename(interaction: discord.Interaction, session_id: str, title: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    directory = _resolve_project_dir(interaction, bot)
    await interaction.response.defer(ephemeral=True)
    try:
        sessions = await _list_sessions(directory, limit=50)
        full = _resolve_session_id(sessions, session_id)
        await _rename(directory, full, title)
    except (ValueError, FileNotFoundError) as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    await interaction.followup.send(
        embed=discord.Embed(
            title="✏️ Renamed", description=f"`{full[:8]}` → **{title[:80]}**",
            color=COLOR_INFO,
        ),
        ephemeral=True,
    )


@session_group.command(name="tag", description="Set or clear a past session's tag (empty or '-' clears).")
@app_commands.describe(session_id="Pick from history (autocomplete)", tag="Tag text; empty or '-' clears it")
@app_commands.autocomplete(session_id=_session_id_autocomplete)
async def session_tag(interaction: discord.Interaction, session_id: str, tag: str = "") -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    directory = _resolve_project_dir(interaction, bot)
    tag_val = None if tag.strip() in ("", "-") else tag.strip()
    await interaction.response.defer(ephemeral=True)
    try:
        sessions = await _list_sessions(directory, limit=50)
        full = _resolve_session_id(sessions, session_id)
        await _tag(directory, full, tag_val)
    except (ValueError, FileNotFoundError) as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    msg = f"`{full[:8]}` tagged **#{tag_val}**" if tag_val else f"`{full[:8]}` tag cleared"
    await interaction.followup.send(
        embed=discord.Embed(title="🏷️ Tag updated", description=msg, color=COLOR_INFO),
        ephemeral=True,
    )


@session_group.command(name="delete", description="Permanently delete a past session (jsonl + subagent transcripts).")
@app_commands.describe(session_id="Pick from history (autocomplete)")
@app_commands.autocomplete(session_id=_session_id_autocomplete)
async def session_delete(interaction: discord.Interaction, session_id: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    directory = _resolve_project_dir(interaction, bot)
    await interaction.response.defer(ephemeral=True)
    try:
        sessions = await _list_sessions(directory, limit=50)
        full = _resolve_session_id(sessions, session_id)
    except ValueError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    if full == bot._get_resume_session_id(interaction.channel_id):
        await interaction.followup.send(
            "❌ That's this thread's active/stored session — `/session clear` "
            "or `/session stop` first.",
            ephemeral=True,
        )
        return
    label = next((_fmt_session_label(x) for x in sessions if x.session_id == full), full[:12])
    view = DeleteConfirmView(directory, full, interaction.user.id)
    await interaction.followup.send(
        embed=discord.Embed(
            title="⚠️ Confirm delete",
            description=(
                f"Permanently delete **{label}** (`{full[:8]}`)?\n"
                "Also removes subagent transcripts. This cannot be undone."
            ),
            color=COLOR_TOOL_FAILURE,
        ),
        view=view,
        ephemeral=True,
    )



