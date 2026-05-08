"""Streaming renderer that bridges Claude SDK output to Discord.

The renderer consumes the message stream from a :class:`ClaudeBridge` and
writes it to a Discord channel/thread with three behaviors:

1. **Fast-path**: if Claude finishes within :data:`FAST_PATH_SECONDS`, the
   full reply is sent in one (smart-split) batch.
2. **Typewriter mode**: if the response takes longer, an initial message is
   sent with a trailing cursor and then edited in place every
   :data:`EDIT_INTERVAL_SECONDS` until the buffer exceeds Discord's per-
   message limit, at which point the current message is finalized and a
   new one is started.
3. **Tool status**: ``ToolUseBlock`` events render a colored embed status
   message which is updated to ``✅`` / ``❌`` when the matching
   ``ToolResultBlock`` arrives.

The renderer also smart-splits text on paragraph → line → space boundaries
and protects unclosed ``` code fences by closing-and-reopening them across
chunk boundaries.

Sub-agent display: when Claude spawns a sub-agent via the ``Task`` tool,
a new thread is created in the parent channel. The thread name includes
the main thread name so users can identify which conversation it belongs
to. Sub-agent content is routed to the sub-thread; a compact summary
embed with a link is posted in the main thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

import re

from .claude_bridge import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_code_sdk.types import StreamEvent

if TYPE_CHECKING:
    from .claude_bridge import ClaudeBridge

log = logging.getLogger("clauded.discord_renderer")

# ---------------------------------------------------------------------------
# Color scheme for embeds
# ---------------------------------------------------------------------------
COLOR_CLAUDE = 0x7C3AED        # Purple — Claude replies
COLOR_TOOL_RUNNING = 0xF59E0B  # Yellow — tool executing
COLOR_TOOL_SUCCESS = 0x10B981  # Green — tool completed
COLOR_TOOL_FAILURE = 0xEF4444  # Red — tool failed / error
COLOR_INFO = 0x3B82F6          # Blue — info / commands
COLOR_THINKING = 0x6B7280      # Gray — thinking

# Discord caps message content at 2000 characters; we leave a small margin so
# we can append a cursor or close-and-reopen a code fence safely.
DISCORD_MAX_LEN = 1900

# Single-character cursor appended to the in-flight typewriter message.
CURSOR = "▌"

# Below this, the response is considered "fast" and is sent as a single
# (smart-split) batch of messages once the stream completes.
FAST_PATH_SECONDS = 3.0

# Minimum delay between successive edits of the live typewriter message.
# Discord rate-limits edits aggressively; ~1.2s keeps us well under the cap.
EDIT_INTERVAL_SECONDS = 1.2

# Sleep used when an edit/send fails (likely rate-limited) before continuing.
HTTP_BACKOFF_SECONDS = 0.5

# Threshold above which a code block is uploaded as a file attachment.
CODE_FILE_UPLOAD_THRESHOLD = 3000

# Regex patterns for Claude channel/thread management markers.
_THREAD_PATTERN = re.compile(r'\[CREATE_THREAD:\s*(.+?)\]')
_CHANNEL_PATTERN = re.compile(r'\[CREATE_CHANNEL:\s*(.+?)\]')




def _fmt_tokens(n: int) -> str:
    """Format token count: 2235 -> 2.2k, 523 -> 523."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


class DiscordRenderer:
    """Render a Claude streaming response into a Discord channel/thread."""

    def __init__(self, target: discord.abc.Messageable) -> None:
        self.target = target
        self._last_msg: discord.Message | None = None

    # ------------------------------------------------------------------
    # Helper: build a tool embed from a ToolUseBlock
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_embed(block: ToolUseBlock) -> discord.Embed:
        """Build a colored embed summarizing a tool invocation."""
        name = getattr(block, "name", "tool") or "tool"
        if name == "Bash":
            cmd = block.input.get("command", "")[:500]
            return discord.Embed(
                title=f"🔄 {name}",
                description=f"```bash\n{cmd}\n```",
                color=COLOR_TOOL_RUNNING,
            )
        elif name == "Write":
            path = block.input.get("file_path", block.input.get("file", ""))
            return discord.Embed(
                title="🔄 Write",
                description=f"📄 `{path}`",
                color=COLOR_TOOL_RUNNING,
            )
        elif name == "Edit":
            path = block.input.get("file_path", block.input.get("file", ""))
            return discord.Embed(
                title="🔄 Edit",
                description=f"📄 `{path}`",
                color=COLOR_TOOL_RUNNING,
            )
        elif name == "Read":
            path = block.input.get("file_path", block.input.get("file", ""))
            return discord.Embed(
                title="🔄 Read",
                description=f"📄 `{path}`",
                color=COLOR_TOOL_RUNNING,
            )
        else:
            return discord.Embed(
                title=f"🔄 {name}",
                description="Executing...",
                color=COLOR_TOOL_RUNNING,
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def render_response(self, bridge: "ClaudeBridge", user_text: str) -> None:
        """Stream Claude's response for ``user_text`` to :attr:`target`."""
        buffer = ""                               # text not yet finalized into a sent msg
        live_msg: discord.Message | None = None   # the in-flight typewriter message
        start_time: float | None = None
        stream_start: float = time.time()
        last_edit = 0.0
        typewriter = False                        # have we entered typewriter mode?
        saw_text = False                          # any TextBlock seen?
        # tool_use_id -> discord.Message
        tool_msgs: dict[str, discord.Message] = {}
        tool_names: dict[str, str] = {}
        task_depth = 0                            # subtask nesting depth (#74)
        # Stats populated from ResultMessage
        stats: dict | None = None
        # Rolling tool log (Issue #1: merge consecutive tool embeds)
        tool_log_msg: discord.Message | None = None
        tool_log_lines: list[str] = []
        # Sub-agent threads: tool_use_id -> discord.Thread / DiscordRenderer
        subagent_threads: dict[str, discord.Thread] = {}
        subagent_renderers: dict[str, "DiscordRenderer"] = {}

        try:
            async for event in bridge.send_message(user_text):
                # Tool results can arrive on UserMessage objects too — handle any
                # message that exposes a ``content`` list of blocks.
                content = getattr(event, "content", None)

                # -------------------------------------------------------
                # Sub-agent routing: redirect to sub-thread renderer
                # -------------------------------------------------------
                ptid = getattr(event, 'parent_tool_use_id', None)
                if ptid and ptid in subagent_renderers:
                    sub_renderer = subagent_renderers[ptid]
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, TextBlock):
                                # Buffer text for sub-renderer
                                if not hasattr(sub_renderer, '_sub_buffer'):
                                    sub_renderer._sub_buffer = ""
                                    sub_renderer._sub_msg = None
                                    sub_renderer._sub_last_edit = 0.0
                                sub_renderer._sub_buffer += block.text
                                now = time.time()
                                if now - sub_renderer._sub_last_edit >= EDIT_INTERVAL_SECONDS:
                                    display = sub_renderer._sub_buffer[:DISCORD_MAX_LEN]
                                    if sub_renderer._sub_msg is None:
                                        sub_renderer._sub_msg = await sub_renderer._safe_send(content=display + CURSOR)
                                    else:
                                        await sub_renderer._safe_edit(sub_renderer._sub_msg, content=display + CURSOR)
                                    sub_renderer._sub_last_edit = now
                            elif isinstance(block, ThinkingBlock):
                                thinking_text = block.thinking[:3900].replace("||", "\\|\\|")
                                embed = discord.Embed(
                                    title="💭 Thinking...",
                                    description=f"||{thinking_text}||",
                                    color=COLOR_THINKING,
                                )
                                await sub_renderer._safe_send(embed=embed)
                            elif isinstance(block, ToolUseBlock):
                                # Flush text buffer first
                                if hasattr(sub_renderer, '_sub_buffer') and sub_renderer._sub_buffer:
                                    if sub_renderer._sub_msg:
                                        await sub_renderer._safe_edit(sub_renderer._sub_msg, content=sub_renderer._sub_buffer[:DISCORD_MAX_LEN])
                                    else:
                                        await sub_renderer._safe_send(content=sub_renderer._sub_buffer[:DISCORD_MAX_LEN])
                                    sub_renderer._sub_buffer = ""
                                    sub_renderer._sub_msg = None
                                # Use rolling tool log in sub-thread
                                if not hasattr(sub_renderer, '_tool_log_lines'):
                                    sub_renderer._tool_log_lines = []
                                    sub_renderer._tool_log_msg = None
                                bname = block.name
                                if bname == "Bash":
                                    cmd = block.input.get("command", "")[:80]
                                    sub_renderer._tool_log_lines.append(f"🔄 Bash: `{cmd}`")
                                elif bname in ("Write", "Edit", "Read"):
                                    path = block.input.get("file_path", block.input.get("file", ""))[:60]
                                    sub_renderer._tool_log_lines.append(f"🔄 {bname}: `{path}`")
                                elif bname == "WebSearch":
                                    query = block.input.get("query", "")[:60]
                                    sub_renderer._tool_log_lines.append(f"🔄 🔍 {query}")
                                elif bname == "WebFetch":
                                    url = block.input.get("url", "")[:60]
                                    sub_renderer._tool_log_lines.append(f"🔄 🌐 {url}")
                                else:
                                    sub_renderer._tool_log_lines.append(f"🔄 {bname}...")
                                tl_embed = discord.Embed(title="🔧 Tool Activity", description="\n".join(sub_renderer._tool_log_lines[-15:]), color=COLOR_TOOL_RUNNING)
                                if sub_renderer._tool_log_msg is None:
                                    sub_renderer._tool_log_msg = await sub_renderer._safe_send(embed=tl_embed)
                                else:
                                    await sub_renderer._safe_edit(sub_renderer._tool_log_msg, embed=tl_embed)
                            elif isinstance(block, ToolResultBlock):
                                # Update tool log in sub-thread
                                if hasattr(sub_renderer, '_tool_log_lines') and sub_renderer._tool_log_lines:
                                    is_err = bool(getattr(block, "is_error", False))
                                    status = "✅" if not is_err else "❌"
                                    # Update last matching entry
                                    for i in range(len(sub_renderer._tool_log_lines) - 1, -1, -1):
                                        if sub_renderer._tool_log_lines[i].startswith("🔄"):
                                            sub_renderer._tool_log_lines[i] = f"{status} {sub_renderer._tool_log_lines[i][2:]}"
                                            break
                                    has_errors = any("❌" in l for l in sub_renderer._tool_log_lines)
                                    tl_embed = discord.Embed(title="🔧 Tool Activity", description="\n".join(sub_renderer._tool_log_lines[-15:]), color=COLOR_TOOL_FAILURE if has_errors else COLOR_TOOL_SUCCESS)
                                    if sub_renderer._tool_log_msg:
                                        await sub_renderer._safe_edit(sub_renderer._tool_log_msg, embed=tl_embed)
                    # Handle StreamEvent for sub-agent
                    if isinstance(event, StreamEvent):
                        ev = event.event
                        if ev.get("type") == "content_block_delta":
                            delta = ev.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    if not hasattr(sub_renderer, '_sub_buffer'):
                                        sub_renderer._sub_buffer = ""
                                        sub_renderer._sub_msg = None
                                        sub_renderer._sub_last_edit = 0.0
                                    sub_renderer._sub_buffer += text
                                    now = time.time()
                                    if now - sub_renderer._sub_last_edit >= EDIT_INTERVAL_SECONDS:
                                        display = sub_renderer._sub_buffer[:DISCORD_MAX_LEN]
                                        if sub_renderer._sub_msg is None:
                                            sub_renderer._sub_msg = await sub_renderer._safe_send(content=display + CURSOR)
                                        else:
                                            await sub_renderer._safe_edit(sub_renderer._sub_msg, content=display + CURSOR)
                                        sub_renderer._sub_last_edit = now
                    continue

                if isinstance(event, ResultMessage):
                    # Capture stats from the result
                    stats = {
                        'cost': float(getattr(event, 'total_cost_usd', 0) or 0),
                        'input_tokens': int((getattr(event, 'usage', None) or {}).get('input_tokens', 0) or 0),
                        'output_tokens': int((getattr(event, 'usage', None) or {}).get('output_tokens', 0) or 0),
                        'duration_ms': (time.time() - stream_start) * 1000,
                        'num_turns': int(getattr(event, 'num_turns', 0) or 0),
                        'model': getattr(event, 'model', '') or '',
                    }
                    break


                # -------------------------------------------------------
                # Feature #61: handle StreamEvent for partial messages
                # -------------------------------------------------------
                if isinstance(event, StreamEvent):
                    ev = event.event
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                saw_text = True
                                buffer += text

                                now = time.time()
                                if start_time is None:
                                    start_time = now

                                # Enter typewriter mode once streaming long enough.
                                if not typewriter and (now - start_time) > FAST_PATH_SECONDS:
                                    typewriter = True
                                    live_msg, buffer = await self._typewriter_tick(
                                        live_msg, buffer
                                    )
                                    last_edit = now
                                elif typewriter and (now - last_edit) >= EDIT_INTERVAL_SECONDS:
                                    live_msg, buffer = await self._typewriter_tick(
                                        live_msg, buffer
                                    )
                                    last_edit = now
                    # StreamEvent handled — skip the content-list branch
                    continue

                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ThinkingBlock):
                            thinking_text = block.thinking[:3900].replace("||", "\\|\\|")
                            embed = discord.Embed(
                                title="💭 Thinking...",
                                description=f"||{thinking_text}||",
                                color=COLOR_THINKING,
                            )
                            await self._safe_send(embed=embed)
                            continue

                        if isinstance(block, TextBlock):
                            text = getattr(block, "text", "") or ""
                            if not text:
                                continue
                            saw_text = True
                            buffer += text

                            now = time.time()
                            if start_time is None:
                                start_time = now

                            # Enter typewriter mode once we've been streaming long enough.
                            if not typewriter and (now - start_time) > FAST_PATH_SECONDS:
                                typewriter = True
                                live_msg, buffer = await self._typewriter_tick(
                                    live_msg, buffer
                                )
                                last_edit = now
                            elif typewriter and (now - last_edit) >= EDIT_INTERVAL_SECONDS:
                                live_msg, buffer = await self._typewriter_tick(
                                    live_msg, buffer
                                )
                                last_edit = now

                        elif isinstance(block, ToolUseBlock):
                            # Finalize any pending typewriter message before
                            # interleaving a tool-status message, so the order
                            # in the channel matches the order of events.
                            if live_msg is not None:
                                await self._finalize_typewriter(live_msg, buffer)
                                live_msg = None
                                buffer = ""
                                typewriter = False
                                start_time = None

                            name = getattr(block, "name", "tool") or "tool"
                            tool_id = getattr(block, "id", None)
                            if tool_id:
                                tool_names[tool_id] = name

                            # --- Special tool display: Plan Mode (#54) ---
                            if name == "EnterPlanMode":
                                plan_embed = discord.Embed(
                                    title="📋 Entered plan mode",
                                    color=COLOR_INFO,
                                )
                                await self._safe_send(embed=plan_embed)
                                continue
                            if name == "ExitPlanMode":
                                plan_embed = discord.Embed(
                                    title="✅ Exited plan mode",
                                    color=COLOR_INFO,
                                )
                                await self._safe_send(embed=plan_embed)
                                continue

                            # --- Special tool display: Task subtask (#55, #74) ---
                            # Create a sub-thread for each sub-agent
                            if name in ("Task", "Agent"):
                                task_depth += 1
                                desc = block.input.get("description", "")[:200]

                                try:
                                    # Get parent channel
                                    parent_channel = self.target
                                    if hasattr(parent_channel, 'parent') and parent_channel.parent:
                                        parent_channel = parent_channel.parent

                                    # Thread name: [main_thread_name] > subtask description
                                    main_name = getattr(self.target, 'name', 'session')[:30]
                                    sub_name = f"[{main_name}] 🔀 {desc[:60]}" if desc else f"[{main_name}] 🔀 Subtask #{task_depth}"

                                    anchor = await parent_channel.send(
                                        embed=discord.Embed(title=sub_name[:100], description=f"Sub-agent for: {self.target.mention}", color=COLOR_INFO)
                                    )
                                    sub_thread = await anchor.create_thread(name=sub_name[:100], auto_archive_duration=60)

                                    if tool_id:
                                        subagent_threads[tool_id] = sub_thread
                                        subagent_renderers[tool_id] = DiscordRenderer(sub_thread)

                                    # Compact summary in main thread
                                    summary_embed = discord.Embed(
                                        title=f"🔀 Subtask #{task_depth}",
                                        description=f"{desc}\n📎 {sub_thread.mention}",
                                        color=COLOR_INFO,
                                    )
                                    tmsg = await self._safe_send(embed=summary_embed)
                                    if tmsg and tool_id:
                                        tool_msgs[tool_id] = tmsg
                                except discord.HTTPException:
                                    # Fallback: inline
                                    sep = discord.Embed(title=f"🔀 Subtask #{task_depth}", description=desc, color=COLOR_INFO)
                                    tmsg = await self._safe_send(embed=sep)
                                    if tmsg and tool_id:
                                        tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: TaskOutput (#74) ---
                            if name in ("TaskOutput", "AgentOutput"):
                                output = block.input.get("output", "")[:500]
                                task_out_embed = discord.Embed(
                                    title="📤 Subtask Output",
                                    description=output,
                                    color=COLOR_INFO,
                                )
                                tmsg = await self._safe_send(embed=task_out_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: TaskStop (#74) ---
                            if name in ("TaskStop", "AgentStop"):
                                task_depth = max(0, task_depth - 1)
                                task_stop_embed = discord.Embed(
                                    title="⏹️ Subtask Stopped",
                                    color=COLOR_TOOL_FAILURE,
                                )
                                tmsg = await self._safe_send(embed=task_stop_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: TodoWrite (#56) ---
                            if name == "TodoWrite":
                                todos = block.input.get("todos", [])
                                lines = []
                                for item in todos[:20]:
                                    if isinstance(item, dict):
                                        status = item.get("status", "")
                                        label = item.get("content", item.get("label", item.get("text", "")))
                                        if status in ("completed", "done"):
                                            lines.append(f"☑ {label}")
                                        else:
                                            lines.append(f"☐ {label}")
                                    else:
                                        lines.append(f"☐ {item}")
                                todo_text = "\n".join(lines) if lines else "No items"
                                todo_embed = discord.Embed(
                                    title="📝 Todo List",
                                    description=todo_text[:4000],
                                    color=COLOR_INFO,
                                )
                                tmsg = await self._safe_send(embed=todo_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: WebSearch (#67) ---
                            if name == "WebSearch":
                                query = block.input.get("query", "")[:200]
                                tool_embed = discord.Embed(title=f"🔍 Searching: {query}", color=COLOR_TOOL_RUNNING)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: WebFetch (#67) ---
                            if name == "WebFetch":
                                url = block.input.get("url", "")[:200]
                                tool_embed = discord.Embed(title="🌐 Fetching", description=f"`{url}`", color=COLOR_TOOL_RUNNING)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: Glob (#67) ---
                            if name == "Glob":
                                pattern = block.input.get("pattern", "")[:100]
                                tool_embed = discord.Embed(title=f"📂 Glob: {pattern}", color=COLOR_TOOL_RUNNING)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: Grep (#67) ---
                            if name == "Grep":
                                pattern = block.input.get("pattern", "")[:100]
                                grep_path = block.input.get("path", ".")[:100]
                                tool_embed = discord.Embed(title=f"🔎 Grep: {pattern}", description=f"in `{grep_path}`", color=COLOR_TOOL_RUNNING)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: Worktree (#73) ---
                            if name == "EnterWorktree":
                                wt_name = block.input.get("name", "")[:100]
                                tool_embed = discord.Embed(title=f"🌳 Entered worktree: {wt_name}", color=COLOR_INFO)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            if name == "ExitWorktree":
                                tool_embed = discord.Embed(title="🌳 Exited worktree", color=COLOR_INFO)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: Cron (#73) ---
                            if name == "CronCreate":
                                schedule = block.input.get("schedule", "")[:100]
                                cmd = block.input.get("command", "")[:200]
                                tool_embed = discord.Embed(title="⏰ Cron Created", description=f"`{schedule}`\n```\n{cmd}\n```", color=COLOR_INFO)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            if name == "CronDelete":
                                tool_embed = discord.Embed(title="⏰ Cron Deleted", color=COLOR_TOOL_FAILURE)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            if name == "CronList":
                                tool_embed = discord.Embed(title="⏰ Listing Crons", color=COLOR_INFO)
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue


                            # --- Special tool display: NotebookEdit ---
                            if name == "NotebookEdit":
                                cell_type = block.input.get("cell_type", "code")
                                cell_idx = block.input.get("cell_index", "?")
                                content_preview = str(block.input.get("new_source", block.input.get("source", "")))[:500]
                                lang = "python" if cell_type == "code" else ""
                                tool_embed = discord.Embed(
                                    title=f"📓 Notebook Cell [{cell_idx}] ({cell_type})",
                                    description=f"```{lang}\n{content_preview.replace(chr(96)*3, chr(96)+' '+chr(96)+' '+chr(96))}\n```" if content_preview else "Empty cell",
                                    color=COLOR_TOOL_RUNNING,
                                )
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # --- Special tool display: ScheduleWakeup ---
                            if name == "ScheduleWakeup":
                                delay = block.input.get("delay_seconds", block.input.get("seconds", "?"))
                                reason = block.input.get("message", block.input.get("reason", ""))[:200]
                                tool_embed = discord.Embed(
                                    title=f"⏰ Scheduled Wakeup: {delay}s",
                                    description=reason or "Waiting...",
                                    color=COLOR_TOOL_RUNNING,
                                )
                                tmsg = await self._safe_send(embed=tool_embed)
                                if tmsg is not None and tool_id:
                                    tool_msgs[tool_id] = tmsg
                                continue

                            # Skill: show name in log, suppress content
                            if name == "Skill":
                                skill_name = block.input.get("name", block.input.get("skill", ""))[:100]
                                tool_log_lines.append(f"🔄 Skill: {skill_name}")
                            else:
                                # Rolling tool log: merge consecutive tool embeds
                                # Add key info for each tool type
                                if name == "Bash":
                                    cmd = block.input.get("command", "")[:80]
                                    tool_log_lines.append(f"🔄 Bash: `{cmd}`")
                                elif name in ("Write", "Edit", "Read"):
                                    path = block.input.get("file_path", block.input.get("file", ""))[:60]
                                    tool_log_lines.append(f"🔄 {name}: `{path}`")
                                elif name == "WebSearch":
                                    query = block.input.get("query", "")[:60]
                                    tool_log_lines.append(f"🔄 🔍 {query}")
                                elif name == "WebFetch":
                                    url = block.input.get("url", "")[:60]
                                    tool_log_lines.append(f"🔄 🌐 {url}")
                                elif name in ("Glob", "Grep"):
                                    pattern = block.input.get("pattern", "")[:40]
                                    tool_log_lines.append(f"🔄 {name}: `{pattern}`")
                                else:
                                    tool_log_lines.append(f"🔄 {name}...")
                            tool_embed = discord.Embed(
                                title="🔧 Tool Activity",
                                description="\n".join(tool_log_lines[-15:]),
                                color=COLOR_TOOL_RUNNING,
                            )
                            if tool_log_msg is None:
                                tool_log_msg = await self._safe_send(embed=tool_embed)
                            else:
                                await self._safe_edit(tool_log_msg, embed=tool_embed)

                            # Show file content preview for Write/Edit tools
                            if block.name == "Write":
                                file_path = block.input.get("file_path", "unknown")
                                file_content = block.input.get("content", "")
                                ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
                                lang = ext if ext in ("py", "js", "ts", "go", "rs", "java", "c", "cpp", "h", "md", "yaml", "yml", "json", "toml", "sh", "bash", "sql", "html", "css") else ""
                                preview = file_content.replace("```", "` ` `")[:1500]  # SEC2: escape before truncate
                                if len(file_content) > 1500:
                                    preview += "\n... (truncated)"
                                try:
                                    await self.target.send(f"📝 `{file_path}`\n```{lang}\n{preview}\n```")
                                except discord.HTTPException:
                                    pass

                            if block.name == "Edit":
                                file_path = block.input.get("file_path", "unknown")
                                old_text = block.input.get("old_text", "")
                                new_text = block.input.get("new_text", "")
                                diff_lines = []
                                for line in old_text.splitlines():
                                    diff_lines.append(f"- {line}")
                                for line in new_text.splitlines():
                                    diff_lines.append(f"+ {line}")
                                diff_str = "\n".join(diff_lines).replace("```", "` ` `")[:1500]  # SEC2: escape before truncate
                                if len("\n".join(diff_lines)) > 1500:
                                    diff_str += "\n... (truncated)"
                                try:
                                    await self.target.send(f"✏️ `{file_path}`\n```diff\n{diff_str}\n```")
                                except discord.HTTPException:
                                    pass

                        elif isinstance(block, ToolResultBlock):
                            tool_id = getattr(block, "tool_use_id", None)
                            # --- Task result display (#55, #74) ---
                            result_name = tool_names.get(tool_id, "") if tool_id else ""
                            if result_name in ("Task", "Agent"):
                                task_depth = max(0, task_depth - 1)
                                is_err = bool(getattr(block, "is_error", False))

                                if tool_id in subagent_threads:
                                    sub_thread = subagent_threads[tool_id]

                                    # Flush remaining buffer in sub-renderer
                                    if tool_id in subagent_renderers:
                                        sr = subagent_renderers[tool_id]
                                        if hasattr(sr, '_sub_buffer') and sr._sub_buffer:
                                            if sr._sub_msg:
                                                await sr._safe_edit(sr._sub_msg, content=sr._sub_buffer[:DISCORD_MAX_LEN])
                                            else:
                                                await sr._safe_send(content=sr._sub_buffer[:DISCORD_MAX_LEN])

                                    # Completion embed in sub-thread
                                    done = discord.Embed(
                                        title="✅ Subtask Complete" if not is_err else "❌ Subtask Failed",
                                        description=str(block.content)[:300] if block.content else "Done",
                                        color=COLOR_TOOL_SUCCESS if not is_err else COLOR_TOOL_FAILURE,
                                    )
                                    await subagent_renderers[tool_id]._safe_send(embed=done)

                                    # Update main thread summary
                                    if tool_id in tool_msgs:
                                        summary = discord.Embed(
                                            title="✅ Subtask Complete" if not is_err else "❌ Subtask Failed",
                                            description=f"📎 {sub_thread.mention}",
                                            color=COLOR_TOOL_SUCCESS if not is_err else COLOR_TOOL_FAILURE,
                                        )
                                        await self._safe_edit(tool_msgs[tool_id], embed=summary)
                                elif tool_id in tool_msgs:
                                    # Fallback: inline completion (no sub-thread was created)
                                    done_embed = discord.Embed(
                                        title=f"{'✅' if not is_err else '❌'} Subtask Complete",
                                        description=str(block.content)[:300] if block.content else "Done",
                                        color=COLOR_TOOL_SUCCESS if not is_err else COLOR_TOOL_FAILURE,
                                    )
                                    await self._safe_edit(tool_msgs[tool_id], embed=done_embed)
                                continue

                            if result_name == "TaskStop":
                                continue

                            is_err = bool(getattr(block, "is_error", False))
                            name = tool_names.get(tool_id, "tool") if tool_id else "tool"
                            if tool_log_msg is not None:
                                # Update rolling tool log
                                status = "✅" if not is_err else "❌"
                                for i in range(len(tool_log_lines) - 1, -1, -1):
                                    if tool_log_lines[i].startswith("🔄 " + name):
                                        if is_err:
                                            error_text = str(block.content)[:100] if block.content else "Failed"
                                            tool_log_lines[i] = f"{status} {name}: {error_text}"
                                        else:
                                            tool_log_lines[i] = f"{status} {name}"
                                        break
                                has_errors = any("❌" in l for l in tool_log_lines)
                                tool_embed = discord.Embed(
                                    title="🔧 Tool Activity",
                                    description="\n".join(tool_log_lines[-15:]),
                                    color=COLOR_TOOL_FAILURE if has_errors else COLOR_TOOL_SUCCESS,
                                )
                                await self._safe_edit(tool_log_msg, embed=tool_embed)
                            elif tool_id and tool_id in tool_msgs:
                                orig_msg = tool_msgs[tool_id]
                                if is_err:
                                    result_embed = discord.Embed(
                                        title=f"❌ {name}",
                                        color=COLOR_TOOL_FAILURE,
                                    )
                                    error_text = str(block.content)[:500] if block.content else "Failed"
                                    result_embed.description = f"```\n{error_text}\n```"
                                else:
                                    result_embed = discord.Embed(
                                        title=f"✅ {name}",
                                        color=COLOR_TOOL_SUCCESS,
                                    )
                                await self._safe_edit(orig_msg, embed=result_embed)
        except Exception:
            log.exception("ClaudeBridge stream failed")
            # Best-effort: clean up the live cursor. Don't try to surface a
            # plain-text error here — callers (bot.py) wrap render_response
            # in the crash-notification flow which posts a richer embed
            # with a retry button. Re-raise so they can do so.
            if live_msg is not None:
                await self._safe_edit(live_msg, content=buffer[:DISCORD_MAX_LEN] or "…")
            raise

        # Stream finished cleanly. Flush whatever is left.
        await self._flush(live_msg, buffer, typewriter, saw_text, tool_msgs)

        # Append cost/stats footer to the last sent message
        if self._last_msg and stats and stats.get('cost', 0) > 0:
            try:
                current = self._last_msg.content or ""
                duration_s = stats['duration_ms'] / 1000
                footer = (
                    f"\n\n-# 💰 ${stats['cost']:.4f}"
                    f" │ 📥 {_fmt_tokens(stats['input_tokens'])}"
                    f" │ 📤 {_fmt_tokens(stats['output_tokens'])}"
                    f" │ ⏱️ {duration_s:.1f}s"
                )
                await self._safe_edit(self._last_msg, content=current + footer)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    async def _process_markers(self, text: str) -> str:
        """Replace [CREATE_THREAD: x] and [CREATE_CHANNEL: x] markers with results."""

        # SEC1: Permission pre-checks — bail early if the bot lacks rights.
        guild = getattr(self.target, 'guild', None)
        if guild is None:
            text = _THREAD_PATTERN.sub("❌ Cannot manage channels: no server context", text)
            text = _CHANNEL_PATTERN.sub("❌ Cannot manage channels: no server context", text)
            return text

        bot_member = guild.me
        if bot_member is None:
            return text

        # For channel creation, check bot permission
        if not bot_member.guild_permissions.manage_channels:
            text = _CHANNEL_PATTERN.sub("❌ Bot lacks manage_channels permission", text)

        # For thread creation, check bot permission
        if not bot_member.guild_permissions.create_public_threads:
            text = _THREAD_PATTERN.sub("❌ Bot lacks create_threads permission", text)

        # If either permission was missing the markers are already replaced;
        # only proceed if markers survive the checks above.

        # E2: Process thread markers in reverse order so offset shifts don't
        # corrupt subsequent replacements (fixes duplicate-marker bug).
        matches = list(_THREAD_PATTERN.finditer(text))
        for match in reversed(matches):
            thread_name = match.group(1).strip()[:100]
            try:
                channel = self.target
                if hasattr(channel, 'parent') and channel.parent:
                    channel = channel.parent  # if we're in a thread, create in parent

                msg = await channel.send(f"📌 Creating thread: {thread_name}")
                thread = await msg.create_thread(name=thread_name)
                replacement = f"✅ Created thread: {thread.mention}"
            except Exception as e:
                replacement = f"❌ Failed to create thread: {e}"
            text = text[:match.start()] + replacement + text[match.end():]

        # E2: Process channel markers in reverse order.
        matches = list(_CHANNEL_PATTERN.finditer(text))
        for match in reversed(matches):
            channel_name = match.group(1).strip()[:100]
            try:
                new_channel = await guild.create_text_channel(name=channel_name)
                replacement = f"✅ Created channel: {new_channel.mention}"
            except Exception as e:
                replacement = f"❌ Failed to create channel: {e}"
            text = text[:match.start()] + replacement + text[match.end():]

        return text

    async def _typewriter_tick(
        self, live_msg: discord.Message | None, buffer: str
    ) -> tuple[discord.Message | None, str]:
        """Update (or create) the live typewriter message.

        If ``buffer`` still fits in one Discord message we just edit/send it
        with a trailing cursor. If it doesn't, we finalize the current
        message with the first smart-split chunk and start a new live
        message containing the remainder + cursor. Returns the (possibly
        new) live message and the (possibly truncated) live buffer.
        """
        # Reserve room for the cursor.
        soft_limit = DISCORD_MAX_LEN - len(CURSOR)

        if len(buffer) <= soft_limit:
            content = buffer + CURSOR
            if live_msg is None:
                live_msg = await self._safe_send(content=content)
            else:
                await self._safe_edit(live_msg, content=content)
            return live_msg, buffer

        # Buffer too big for one message — split it.
        chunks = self._smart_split(buffer, limit=soft_limit)
        if not chunks:  # pragma: no cover - defensive
            return live_msg, buffer

        first, *middle_and_last = chunks
        # Finalize the current live message with the first chunk (no cursor).
        if live_msg is None:
            live_msg = await self._safe_send(content=first)
        else:
            await self._safe_edit(live_msg, content=first)

        # Any middle chunks are sent as their own messages with no cursor.
        for mid in middle_and_last[:-1]:
            await self._safe_send(content=mid)

        # The last chunk becomes the new live buffer with a fresh cursor message.
        tail = middle_and_last[-1] if middle_and_last else ""
        new_live = await self._safe_send(content=tail + CURSOR) if tail else None
        return new_live, tail

    async def _finalize_typewriter(
        self, live_msg: discord.Message, buffer: str
    ) -> None:
        """Replace the cursor in ``live_msg`` and emit any overflow as new messages."""
        # E1: Process channel/thread markers before finalizing.
        buffer = await self._process_markers(buffer)
        buffer = self._format_tables(buffer)
        if len(buffer) <= DISCORD_MAX_LEN:
            await self._safe_edit(live_msg, content=buffer)
            self._last_msg = live_msg
            return

        chunks = self._smart_split(buffer, limit=DISCORD_MAX_LEN)
        if not chunks:  # pragma: no cover - defensive
            await self._safe_edit(live_msg, content=buffer[:DISCORD_MAX_LEN])
            self._last_msg = live_msg
            return

        await self._safe_edit(live_msg, content=chunks[0])
        for chunk in chunks[1:]:
            sent = await self._safe_send(content=chunk)
            if sent is not None:
                self._last_msg = sent

    async def _flush(
        self,
        live_msg: discord.Message | None,
        buffer: str,
        typewriter: bool,
        saw_text: bool,
        tool_msgs: dict[str, discord.Message],
    ) -> None:
        """Send any remaining text once the stream completes."""
        # Process channel/thread management markers before sending.
        if buffer:
            buffer = await self._process_markers(buffer)
        buffer = self._format_tables(buffer)

        if typewriter and live_msg is not None:
            await self._finalize_typewriter(live_msg, buffer)
            return

        if buffer:
            for chunk in self._smart_split(buffer, limit=DISCORD_MAX_LEN):
                if self._should_upload_as_file(chunk):
                    ext, code = self._extract_code_info(chunk)
                    f = discord.File(io.BytesIO(code.encode()), filename=f"output.{ext}")
                    sent = await self._safe_send(file=f)
                else:
                    sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
            return

        # No text buffered. If we never showed *anything* (no text, no tools),
        # leave a placeholder so the user knows the round-trip finished.
        if not saw_text and not tool_msgs:
            sent = await self._safe_send(content="(Claude returned no text response)")
            if sent is not None:
                self._last_msg = sent

    # ------------------------------------------------------------------
    # Long code block → file upload helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _should_upload_as_file(text: str) -> bool:
        """Check if text is a long code block that should be uploaded as a file."""
        stripped = text.strip()
        if not stripped.startswith("```"):
            return False
        return len(stripped) > CODE_FILE_UPLOAD_THRESHOLD

    @staticmethod
    def _extract_code_info(text: str) -> tuple[str, str]:
        """Extract language extension and code body from a fenced code block."""
        stripped = text.strip()
        first_line = stripped.split("\n", 1)[0]
        lang = first_line.replace("```", "").strip() or "txt"
        # Remove opening ``` line
        code = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        # Remove closing ```
        if code.endswith("```"):
            code = code[:-3]
        ext_map = {
            "python": "py", "javascript": "js", "typescript": "ts",
            "bash": "sh", "shell": "sh",
        }
        ext = ext_map.get(lang, lang)
        return ext, code

    # ------------------------------------------------------------------
    # Discord I/O wrappers
    # ------------------------------------------------------------------

    async def _safe_send(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        """Send a message, swallowing transient HTTP errors with a short backoff."""
        if not content and embed is None and file is None and view is None:
            return None

        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view

        try:
            msg = await self.target.send(**kwargs)
            if content:
                self._last_msg = msg
            return msg
        except discord.RateLimited as exc:
            retry = max(HTTP_BACKOFF_SECONDS, float(getattr(exc, "retry_after", 1.0)))
            log.warning("Discord send rate-limited; sleeping %.2fs", retry)
            await asyncio.sleep(retry)
            try:
                # Reset file stream if it was consumed by the first attempt
                if "file" in kwargs:
                    f = kwargs["file"]
                    if hasattr(f, 'fp') and hasattr(f.fp, 'seek'):
                        f.fp.seek(0)
                msg = await self.target.send(**kwargs)
                if content:
                    self._last_msg = msg
                return msg
            except discord.HTTPException:
                log.warning("Discord send failed after backoff", exc_info=True)
                return None
        except discord.HTTPException:
            log.warning("Discord send failed", exc_info=True)
            return None

    async def _safe_edit(
        self,
        msg: discord.Message,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        """Edit a message, swallowing transient HTTP errors."""
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if not kwargs:
            return

        try:
            await msg.edit(**kwargs)
        except discord.RateLimited as exc:
            retry = max(HTTP_BACKOFF_SECONDS, float(getattr(exc, "retry_after", 1.0)))
            log.warning("Discord edit rate-limited; sleeping %.2fs", retry)
            await asyncio.sleep(retry)
            try:
                await msg.edit(**kwargs)
            except discord.HTTPException:
                log.warning("Discord edit failed after backoff", exc_info=True)
        except discord.HTTPException:
            log.warning("Discord edit failed", exc_info=True)

    # ------------------------------------------------------------------
    # Markdown table → code block conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tables(text: str) -> str:
        """Convert markdown tables to code blocks for Discord.

        Discord does not render markdown tables, so we wrap them in
        code blocks where at least the column alignment is preserved.
        Tables already inside code fences are left untouched.
        """
        lines_in = text.split("\n")
        result: list[str] = []
        in_table = False
        in_code_fence = False
        table_lines: list[str] = []

        for line in lines_in:
            stripped = line.strip()

            # Track existing code fences
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                if in_table and table_lines:
                    result.append("```")
                    result.extend(table_lines)
                    result.append("```")
                    in_table = False
                    table_lines = []
                result.append(line)
                continue

            if in_code_fence:
                result.append(line)
                continue

            if stripped.startswith("|") and stripped.endswith("|"):
                if not in_table:
                    in_table = True
                    table_lines = []
                if not all(c in "|-: " for c in stripped):
                    table_lines.append(stripped)
            else:
                if in_table:
                    if table_lines:
                        result.append("```")
                        result.extend(table_lines)
                        result.append("```")
                    in_table = False
                    table_lines = []
                result.append(line)

        if in_table and table_lines:
            result.append("```")
            result.extend(table_lines)
            result.append("```")

        return "\n".join(result)


    # ------------------------------------------------------------------
    # Smart text splitting
    # ------------------------------------------------------------------

    @staticmethod
    def _smart_split(text: str, *, limit: int = DISCORD_MAX_LEN) -> list[str]:
        """Split ``text`` into chunks that fit within ``limit``.

        Break preference: paragraph (``\\n\\n``) → line (``\\n``) → space →
        hard cut. Code fences are closed at the cut and re-opened in the
        next chunk to keep every chunk self-contained.
        """
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            # Leave space for a potential close-fence if the chunk has an
            # unclosed code block.
            fence_reserve = len("\n```")
            cut = limit - fence_reserve

            # Look for a good break point in the back half of the chunk.
            half = cut // 2

            # 1) Paragraph boundary (\n\n)
            idx = remaining.rfind("\n\n", half, cut)
            if idx >= 0:
                idx += 2  # include the double newline in the first chunk
            else:
                # 2) Line boundary (\n)
                idx = remaining.rfind("\n", half, cut)
                if idx >= 0:
                    idx += 1
                else:
                    # 3) Space
                    idx = remaining.rfind(" ", half, cut)
                    if idx >= 0:
                        idx += 1
                    else:
                        # 4) Hard cut
                        idx = cut

            chunk = remaining[:idx]
            remaining = remaining[idx:]

            # Fix unclosed code fences.
            if chunk.count("```") % 2 == 1:
                lang = DiscordRenderer._detect_open_fence_lang(chunk)
                chunk += "\n```"
                remaining = f"```{lang}\n" + remaining

            # Strip leading blank lines from the next chunk (avoids a visual
            # gap when the split happened at a paragraph boundary).
            remaining = remaining.lstrip("\n")

            chunks.append(chunk)

        return chunks

    @staticmethod
    def _detect_open_fence_lang(chunk: str) -> str:
        """Return the language tag of the last unclosed ``\u0060\u0060\u0060`` fence in *chunk*."""
        # Walk through all ``` occurrences; the last odd-numbered one is the
        # currently open fence.
        idx = -1
        cursor = 0
        opens = 0
        while True:
            pos = chunk.find("```", cursor)
            if pos < 0:
                break
            opens += 1
            if opens % 2 == 1:
                idx = pos
            cursor = pos + 3
        if idx < 0:
            return ""
        # Read up to the next newline as the language tag.
        nl = chunk.find("\n", idx + 3)
        if nl < 0:
            return chunk[idx + 3 :].strip()
        return chunk[idx + 3 : nl].strip()

    # ------------------------------------------------------------------
    # Crash notification with retry
    # ------------------------------------------------------------------

    async def send_error_with_retry(
        self,
        exc: BaseException,
        on_retry: Callable[[], Awaitable[None]],
    ) -> None:
        """Post a crash embed with a 🔄 Retry button.

        ``on_retry`` is invoked when the user clicks the button. It should
        re-send the last user message through a fresh bridge; this class
        does not know about sessions, so the wiring lives in the bot.
        """
        embed = discord.Embed(
            title="❌ Claude session crashed",
            description=(
                "Something went wrong while talking to Claude. You can retry the "
                "last message — a fresh session will be started for it.\n\n"
                f"Error: `{exc}`"
            )[:_RETRY_EMBED_DESC_MAX],
            color=COLOR_TOOL_FAILURE,
        )
        view = RetryView(on_retry=on_retry)
        try:
            await self.target.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Failed to post crash-with-retry embed")


# ---------------------------------------------------------------------------
# Retry view
# ---------------------------------------------------------------------------


_RETRY_EMBED_DESC_MAX = 4000
_RETRY_TIMEOUT_SECONDS = 600.0


class RetryView(discord.ui.View):
    """View attached to a crash embed that re-runs the last user message."""

    def __init__(
        self,
        *,
        on_retry: Callable[[], Awaitable[None]],
        timeout: float = _RETRY_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._on_retry = on_retry
        self._fired = False
        # Override the button's custom_id to be unique per instance
        for item in self.children:
            if hasattr(item, 'custom_id') and item.custom_id == 'clauded_retry_btn':
                item.custom_id = f"clauded_retry_{uuid.uuid4().hex[:8]}"

    @discord.ui.button(
        label="Retry",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="clauded_retry_btn",
    )
    async def retry_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        # Single-shot: disable the button immediately so a double-click
        # doesn't queue two retries against the same dead session.
        if self._fired:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        self._fired = True
        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                log.debug("Retry: failed to defer interaction")
        try:
            await self._on_retry()
        except Exception:
            log.exception("Retry callback raised")
        finally:
            self.stop()


__all__ = [
    "DiscordRenderer",
    "RetryView",
    "COLOR_CLAUDE",
    "COLOR_TOOL_RUNNING",
    "COLOR_TOOL_SUCCESS",
    "COLOR_TOOL_FAILURE",
    "COLOR_INFO",
    "COLOR_THINKING",
]
