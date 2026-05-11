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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

import re

from .claude_bridge import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent
from .stream_logger import log_event as _log_stream
from .cogs._table_view import CopyTableTextView

if TYPE_CHECKING:
    from .claude_bridge import ClaudeBridge

log = logging.getLogger("clauded.discord_renderer")

# Pillow defensive fallback (#135 / PRD R6 + acceptance E).
# If Pillow (or .table_png's other deps) cannot be imported, fall back to the
# legacy code-fence wrap in ``DiscordRenderer._format_tables`` rather than
# crashing the renderer. ``PILLOW_AVAILABLE`` is consulted at the top of
# :meth:`DiscordRenderer._extract_and_render_tables` for a fast-fail.
try:
    from .table_png import render_table_png, MAX_COLS, MAX_ROWS, MAX_TABLE_PIXELS
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    log.warning("Pillow not installed; tables fall back to code-fence rendering")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TableRender:
    """A markdown table extracted from a renderer buffer for PNG attachment.

    Produced by :meth:`DiscordRenderer._extract_and_render_tables`. Each
    instance carries the parsed structure plus the rendered PNG bytes and the
    original markdown source (kept verbatim for the Copy-as-text button).

    Fields
    ------
    headers
        Cell text of the table's header row, one per column.
    rows
        Body rows (each a list of cell strings, length matching ``headers``).
    png_bytes
        Result of :func:`table_png.render_table_png` — ready to attach to a
        Discord message via :class:`discord.File`.
    markdown_source
        Original markdown table text exactly as it appeared in the input
        buffer (verbatim, including any leading/trailing whitespace per line).
        This is the source-of-truth surfaced by the Copy button.
    placeholder
        Token inserted into the surrounding text where the table used to be,
        e.g. ``"\\n[TABLE_PNG_0]\\n"``. Callers split on these to interleave
        text segments with PNG follow-ups (PRD R2.2 ordering).
    """

    headers: list[str]
    rows: list[list[str]]
    png_bytes: bytes
    markdown_source: str
    placeholder: str

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

# Up to 5 retries with 0.5/1/2/4/8s backoff covers ~15s of transient HTTP badness.
MAX_HTTP_RETRIES = 5
_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0)  # seconds, indexed by attempt

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
        # Shadow of the text we last wrote to ``_last_msg``. discord.py 2.0+
        # Message.edit() is not in-place; reading msg.content post-edit returns
        # stale text. See docs/investigations/stale-message-content.md (#113).
        self._last_msg_text: str = ""

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
        _last_stop_reason: str | None = None
        # Rolling tool log (Issue #1: merge consecutive tool embeds)
        tool_log_msg: discord.Message | None = None
        tool_log_lines: list[str] = []
        # Sub-agent threads: tool_use_id -> discord.Thread / DiscordRenderer
        subagent_threads: dict[str, discord.Thread] = {}
        subagent_renderers: dict[str, "DiscordRenderer"] = {}

        try:
            async for event in bridge.send_message(user_text):
                _log_stream(event, buffer_len=len(buffer))

                # Tool results can arrive on UserMessage objects too — handle any
                # message that exposes a ``content`` list of blocks.
                content = getattr(event, "content", None)

                # -------------------------------------------------------
                # Sub-agent routing: redirect to sub-thread renderer
                # -------------------------------------------------------
                ptid = getattr(event, 'parent_tool_use_id', None)
                if ptid and ptid in subagent_renderers:
                    sub_renderer = subagent_renderers[ptid]
                    # Only render AssistantMessage content — skip UserMessage
                # (UserMessage contains tool results and injected context like skill files)
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
                                    sub_renderer._sub_msg = await sub_renderer._typewriter_apply(
                                        sub_renderer._sub_msg, display + CURSOR,
                                    )
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
                                    await sub_renderer._typewriter_apply(
                                        sub_renderer._sub_msg,
                                        sub_renderer._sub_buffer[:DISCORD_MAX_LEN],
                                    )
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
                                        sub_renderer._sub_msg = await sub_renderer._typewriter_apply(
                                            sub_renderer._sub_msg, display + CURSOR,
                                        )
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
                        'stop_reason': _last_stop_reason,
                    }
                    break


                # -------------------------------------------------------
                # Feature #61: handle StreamEvent for partial messages
                # -------------------------------------------------------
                if isinstance(event, StreamEvent):
                    ev = event.event
                    if ev.get("type") == "message_delta":
                        delta = ev.get("delta", {})
                        if "stop_reason" in delta:
                            _last_stop_reason = delta["stop_reason"]
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

                # Only render AssistantMessage content — skip UserMessage
                # (UserMessage contains injected context like skill files)
                if isinstance(content, list) and isinstance(event, AssistantMessage):
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
                            # Skip: with include_partial_messages=True, all text
                            # arrives via StreamEvent first. TextBlock is a duplicate.
                            continue

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

                            # AskUserQuestion is handled via can_use_tool
                            # callback + InteractionHandler (Discord buttons/modals).
                            # The tool embed is displayed by the normal tool-use path
                            # below.

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
                                    main_name = getattr(self.target, 'name', 'session') or 'session'
                                    # Strip Discord mention markers from name
                                    main_name = re.sub(r'<@[!&]?\d+>', '', main_name).strip()[:30] or 'session'
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
                                # Write preview disabled — path shown in Tool Activity log
                                # # try:
                                # #     await self.target.send(f"📝 `{file_path}`\n```{lang}\n{preview}\n```")
                                # except discord.HTTPException:
                                # pass

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
                                # Edit diff disabled — path shown in Tool Activity log
                                # # try:
                                # #     await self.target.send(f"✏️ `{file_path}`\n```diff\n{diff_str}\n```")
                                # except discord.HTTPException:
                                # pass

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
                                            await sr._typewriter_apply(
                                                sr._sub_msg, sr._sub_buffer[:DISCORD_MAX_LEN],
                                            )

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
                # Read the shadow, not _last_msg.content (stale; see #113).
                current = self._last_msg_text.rstrip(CURSOR)
                duration_s = stats['duration_ms'] / 1000
                footer = (
                    f"\n\n-# 💰 ${stats['cost']:.4f}"
                    f" │ 📥 {_fmt_tokens(stats['input_tokens'])}"
                    f" │ 📤 {_fmt_tokens(stats['output_tokens'])}"
                    f" │ ⏱️ {duration_s:.1f}s"
                )
                if _last_stop_reason and _last_stop_reason != "end_turn":
                    footer += f" │ ⚠️ {_last_stop_reason}"
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

    async def _typewriter_apply(
        self, live_msg: discord.Message | None, content: str
    ) -> discord.Message | None:
        """Edit-or-send a typewriter-style update. Returns the message
        callers should use as the new ``live_msg``.

        - If ``live_msg`` is ``None``: send a fresh message.
        - Otherwise edit it; on permanent edit failure, send a fresh
          message so the buffer doesn't get stuck on a dead msg.
        - If the fallback send also fails, return the old ``live_msg``
          unchanged (the next tick will retry against it).

        Trade-off: when the fallback send succeeds, the old ``live_msg``
        stays in the channel as a stale ghost. We accept that ghost in
        exchange for forward progress; do NOT attempt msg.delete() — that
        call itself can fail and we'd be back to the silent-loss bug.
        """
        if live_msg is None:
            return await self._safe_send(content=content)
        ok = await self._safe_edit(live_msg, content=content)
        if ok:
            return live_msg
        fresh = await self._safe_send(content=content)
        return fresh if fresh is not None else live_msg

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
            return await self._typewriter_apply(live_msg, buffer + CURSOR), buffer

        # Buffer too big for one message — split it.
        chunks = self._smart_split(buffer, limit=soft_limit)
        if not chunks:  # pragma: no cover - defensive
            return live_msg, buffer

        first, *middle_and_last = chunks
        # Finalize the current live message with the first chunk (no cursor).
        live_msg = await self._typewriter_apply(live_msg, first)

        # Middle chunks: failures are logged inside _safe_send; no in-band recovery.
        n_middle_dropped = 0
        for mid in middle_and_last[:-1]:
            sent = await self._safe_send(content=mid)
            if sent is None:
                n_middle_dropped += 1
        if n_middle_dropped:
            log.warning(
                "Typewriter tick lost %d middle chunk(s); subsequent text may "
                "reference dropped content",
                n_middle_dropped,
            )

        # The last chunk becomes the new live buffer with a fresh cursor message.
        tail = middle_and_last[-1] if middle_and_last else ""
        new_live = await self._safe_send(content=tail + CURSOR) if tail else None
        return new_live, tail

    async def _send_table_renders(
        self, renders: list[TableRender]
    ) -> None:
        """Send each :class:`TableRender` as a follow-up PNG message.

        Each render becomes one Discord message carrying two attachments
        (``table_N.png`` and ``table_N.md`` sidecar with the verbatim
        markdown source) plus a :class:`CopyTableTextView`. The view is
        persistent (``custom_id="copy_table_text"``) — see
        :mod:`clauded.cogs._table_view` (PRD R3.3).

        After each successful send we point ``_last_msg`` at the new
        message but reset ``_last_msg_text`` to ``""``. The shadow reset is
        what prevents the cost-footer splicer from re-writing the previous
        text body onto this attachment-only message (`current + footer`
        would otherwise carry the prior chunk's text onto a PNG-only msg).
        It is NOT about losing the attachment — ``Message.edit(content=…)``
        does not strip attachments. See #113.

        If a PNG send permanently fails (``_safe_send`` returned ``None``
        after MAX_HTTP_RETRIES), log an error so the silent drop becomes
        visible in bot.log (review I2).
        """
        for i, render in enumerate(renders):
            png_file = discord.File(
                io.BytesIO(render.png_bytes),
                filename=f"table_{i}.png",
            )
            md_file = discord.File(
                io.BytesIO(render.markdown_source.encode()),
                filename=f"table_{i}.md",
            )
            sent = await self._safe_send(
                files=[png_file, md_file],
                view=CopyTableTextView(),
            )
            if sent is not None:
                self._last_msg = sent
                # Block cost-footer splice of the prior text body onto
                # this PNG message — see docstring + #113.
                self._last_msg_text = ""
            else:
                log.error(
                    "Table %d PNG send permanently failed (%d bytes)",
                    i, len(render.png_bytes),
                )

    async def _send_text_and_tables(
        self, text: str, renders: list[TableRender]
    ) -> None:
        """Interleave text segments and PNG attachments per PRD R2.2.

        Splits ``text`` on each ``render.placeholder`` token (in order) and
        alternates ``_safe_send(content=segment)`` with the matching PNG
        follow-up. Segments are passed through ``_smart_split`` so they
        still respect ``DISCORD_MAX_LEN``. Empty segments (table at start
        or back-to-back tables) are skipped — never send a blank message.

        This is the fix for review C1: previously the caller sent ``text``
        whole then PNGs after, leaking ``[TABLE_PNG_N]`` placeholders into
        the user-visible message body.
        """
        segments = text
        for render in renders:
            before, _, segments = segments.partition(render.placeholder)
            if before.strip():
                for chunk in self._smart_split(before, limit=DISCORD_MAX_LEN):
                    sent = await self._safe_send(content=chunk)
                    if sent is not None:
                        self._last_msg = sent
                        self._last_msg_text = chunk
            # Now the PNG (with its own _last_msg / shadow reset).
            await self._send_table_renders([render])
        # Tail after the last placeholder (or all of ``text`` if no renders).
        if segments.strip():
            for chunk in self._smart_split(segments, limit=DISCORD_MAX_LEN):
                sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
                    self._last_msg_text = chunk

    async def _finalize_typewriter(
        self, live_msg: discord.Message, buffer: str
    ) -> None:
        """Replace the cursor in ``live_msg`` and emit any overflow as new messages.

        If the in-place edit fails permanently we fall back to sending a
        fresh message; otherwise the entire ``buffer`` content would be
        silently lost (see truncation root-cause analysis 5/9).
        """
        # E1: Process channel/thread markers before finalizing.
        buffer = await self._process_markers(buffer)
        # v1.12 — extract tables → PNG follow-ups (PRD R2). On the first
        # text-bearing chunk we still drive the typewriter via in-place
        # edit; if tables exist the remaining text is interleaved with
        # PNG follow-ups via ``_send_text_and_tables`` (review C1).
        stripped_text, table_renders = await self._extract_and_render_tables(buffer)

        if not table_renders:
            # Fast path — no tables, pre-table behaviour preserved verbatim.
            buffer = stripped_text
            if len(buffer) <= DISCORD_MAX_LEN:
                chunks = [buffer]
            else:
                chunks = self._smart_split(buffer, limit=DISCORD_MAX_LEN) or [buffer[:DISCORD_MAX_LEN]]

            first = await self._typewriter_apply(live_msg, chunks[0])
            if first is not None:
                self._last_msg = first
            for chunk in chunks[1:]:
                sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
            return

        # Interleaved path (PRD R2.2): split ``stripped_text`` on
        # placeholders. The first text segment (before the first table)
        # replaces the cursor in ``live_msg``; subsequent segments and PNG
        # follow-ups are sent fresh. If the first segment is empty (table
        # at the very start of the buffer), edit the cursor away with the
        # empty string so the cursor is gone before the PNG arrives.
        first_seg, _, rest_text = stripped_text.partition(table_renders[0].placeholder)
        if first_seg.strip():
            first_chunks = self._smart_split(first_seg, limit=DISCORD_MAX_LEN) or [first_seg[:DISCORD_MAX_LEN]]
        else:
            first_chunks = [""]
        first = await self._typewriter_apply(live_msg, first_chunks[0])
        if first is not None:
            self._last_msg = first
            self._last_msg_text = first_chunks[0]
        for chunk in first_chunks[1:]:
            sent = await self._safe_send(content=chunk)
            if sent is not None:
                self._last_msg = sent
                self._last_msg_text = chunk

        # First PNG, then the rest of the interleave (text-then-PNG pairs +
        # final tail). ``_send_text_and_tables`` handles the residual.
        await self._send_table_renders([table_renders[0]])
        await self._send_text_and_tables(rest_text, table_renders[1:])

    async def _flush(
        self,
        live_msg: discord.Message | None,
        buffer: str,
        typewriter: bool,
        saw_text: bool,
        tool_msgs: dict[str, discord.Message],
    ) -> None:
        """Send any remaining text once the stream completes."""
        _log_stream(None, buffer_len=len(buffer), extra={"action": "flush", "typewriter": typewriter})
        # Process channel/thread management markers before sending.
        if buffer:
            buffer = await self._process_markers(buffer)

        if typewriter and live_msg is not None:
            # ``_finalize_typewriter`` runs its own table extraction so it
            # can interleave text segments with PNG follow-ups (PRD R2.2).
            await self._finalize_typewriter(live_msg, buffer)
            return

        # Fast-path: split text from tables before send. If buffer is empty
        # the extractor is a no-op (returns ``("", [])``).
        stripped_text, table_renders = await self._extract_and_render_tables(buffer)

        if buffer:
            # If text after table extraction is still too big for inline
            # chunks, upload as file — re-splice the markdown sources back
            # into the upload buffer so the `.md` is self-contained (C3).
            chunks = self._smart_split(stripped_text, limit=DISCORD_MAX_LEN) if stripped_text else []
            if len(chunks) > 4:
                import io as _io
                # Re-splice markdown_source back into the upload .md so the
                # user-downloadable file doesn't contain ``[TABLE_PNG_N]``
                # placeholders (review C3).
                upload_buffer = stripped_text
                for render in table_renders:
                    upload_buffer = upload_buffer.replace(
                        render.placeholder,
                        "\n" + render.markdown_source + "\n",
                        1,
                    )
                summary = upload_buffer[:200] + "..." if len(upload_buffer) > 200 else upload_buffer
                f = discord.File(_io.BytesIO(upload_buffer.encode()), filename="claude-response.md")
                await self._safe_send(content=summary, file=f)
                # Still send the PNG follow-ups so chat carries the visuals.
                if table_renders:
                    await self._send_table_renders(table_renders)
                return

            # Standard inline path. If tables exist, use the interleaving
            # helper so placeholders never leak into the user-visible text
            # (review C1). Otherwise fall through to the legacy chunked send
            # which knows how to do the long-code-block file upload.
            if table_renders:
                await self._send_text_and_tables(stripped_text, table_renders)
                return

            for chunk in chunks:
                is_file = self._should_upload_as_file(chunk)
                if is_file:
                    ext, code = self._extract_code_info(chunk)
                    f = discord.File(io.BytesIO(code.encode()), filename=f"output.{ext}")
                    sent = await self._safe_send(file=f)
                else:
                    sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
                    if is_file:
                        # File-only — no text body for the cost footer to splice.
                        self._last_msg_text = ""
            return

        # No surrounding text but a table can still exist (e.g., Claude
        # replied with a bare table) — emit the PNG(s) directly.
        if table_renders:
            await self._send_table_renders(table_renders)
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

    async def _retry_http(
        self,
        op: Callable[[], Awaitable[discord.Message | None]],
        *,
        label: str,
        content_len: int,
        between_attempts: Callable[[], None] | None = None,
    ) -> discord.Message | None:
        """Run ``op()`` with retry/backoff.

        Returns op's result on success; ``None`` on permanent failure.
        ``label`` is "send" or "edit" — used in log messages.
        ``content_len`` is the number of characters that will be lost on
        permanent failure; if non-zero we emit an ``ERROR`` log on giveup.
        ``between_attempts`` is invoked just before each retry sleep
        (used by ``_safe_send`` to ``seek(0)`` a file stream between tries).
        """
        for attempt in range(MAX_HTTP_RETRIES + 1):
            try:
                return await op()
            except discord.RateLimited as exc:
                try:
                    retry_after = float(getattr(exc, "retry_after", 1.0) or 1.0)
                except (TypeError, ValueError):
                    retry_after = 1.0
                if attempt == MAX_HTTP_RETRIES:
                    log.warning(
                        "Discord %s rate-limited (final attempt %d/%d); giving up",
                        label, attempt + 1, MAX_HTTP_RETRIES + 1,
                    )
                    break
                wait = max(_BACKOFF[attempt], retry_after)
                log.warning(
                    "Discord %s rate-limited (attempt %d/%d); sleeping %.2fs",
                    label, attempt + 1, MAX_HTTP_RETRIES + 1, wait,
                )
                if between_attempts is not None:
                    between_attempts()
                await asyncio.sleep(wait)
            except discord.HTTPException as exc:
                try:
                    status = int(getattr(exc, "status", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                retriable = status >= 500 or status == 0
                if not retriable or attempt == MAX_HTTP_RETRIES:
                    if content_len:
                        verb = "DROPPED" if label == "send" else "UNDELIVERED"
                        log.error(
                            "Discord %s permanently failed after %d attempts "
                            "(status=%s); %s %d chars",
                            label, attempt + 1, status, verb, content_len,
                            exc_info=True,
                        )
                    else:
                        log.warning(
                            "Discord %s failed after %d attempts (status=%s)",
                            label, attempt + 1, status, exc_info=True,
                        )
                    return None
                log.warning(
                    "Discord %s transient failure (attempt %d/%d, status=%s); "
                    "sleeping %.2fs",
                    label, attempt + 1, MAX_HTTP_RETRIES + 1, status,
                    _BACKOFF[attempt],
                )
                if between_attempts is not None:
                    between_attempts()
                await asyncio.sleep(_BACKOFF[attempt])

        # Loop fell through — every attempt was a RateLimited (HTTPException
        # would have returned via the giveup branch above). Without this
        # log we'd silently lose `content_len` chars.
        if content_len:
            verb = "DROPPED" if label == "send" else "UNDELIVERED"
            log.error(
                "Discord %s rate-limited %d times in a row; %s %d chars of content",
                label, MAX_HTTP_RETRIES + 1, verb, content_len,
            )
        return None

    async def _safe_send(
        self,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        """Send a message with retry/backoff on transient HTTP errors.

        Discord's REST occasionally returns 5xx or rate-limits us. Silently
        dropping a chunk in the middle of a long typewriter session was the
        root cause of the truncation users reported. We retry up to
        ``MAX_HTTP_RETRIES`` times with the ``_BACKOFF`` schedule before
        giving up. On giveup with a non-empty ``content``, we log at error
        level so the drop shows up in bot.log.

        ``file`` (single) and ``files`` (list) are mutually exclusive — both
        map onto ``discord.abc.Messageable.send``'s twin kwargs. The list
        form is used by #134 to attach a PNG table + ``.md`` sidecar in one
        message together with a :class:`CopyTableTextView` persistent view.
        """
        if (
            not content
            and embed is None
            and file is None
            and files is None
            and view is None
        ):
            return None

        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        if files is not None:
            kwargs["files"] = files
        if view is not None:
            kwargs["view"] = view

        def _reset_file() -> None:
            # Reset file stream(s) between attempts so the second send
            # doesn't see an empty buffer.
            f = kwargs.get("file")
            if f is not None and hasattr(f, "fp") and hasattr(f.fp, "seek"):
                try:
                    f.fp.seek(0)
                except Exception:
                    pass
            fs = kwargs.get("files")
            if fs:
                for entry in fs:
                    if hasattr(entry, "fp") and hasattr(entry.fp, "seek"):
                        try:
                            entry.fp.seek(0)
                        except Exception:
                            pass

        async def _op() -> discord.Message | None:
            return await self.target.send(**kwargs)

        msg = await self._retry_http(
            _op,
            label="send",
            content_len=len(content or ""),
            between_attempts=(
                _reset_file if ("file" in kwargs or "files" in kwargs) else None
            ),
        )
        # Only advance the shadow on content-bearing sends. Embed-only and
        # file-only sends do NOT touch _last_msg here — callers that want
        # _last_msg to track them must reassign it (and the shadow)
        # explicitly. See #113 round-2: widening this to "any successful
        # send" lets an interleaved tool-embed clobber the shadow to "" and
        # the cost-footer then rewrites the long text down to just footer.
        if msg is not None and content:
            self._last_msg = msg
            self._last_msg_text = content
        return msg

    async def _safe_edit(
        self,
        msg: discord.Message,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> bool:
        """Edit ``msg`` with retry/backoff. Returns ``True`` iff the edit
        eventually succeeded.

        The shadow ``self._last_msg_text`` is updated only when ``msg is
        self._last_msg``. Callers that mutate ``self._last_msg`` directly
        (e.g., ``_finalize_typewriter``, ``_flush``) are responsible for
        keeping the shadow consistent — see #113.

        We expose the success bool so callers (most importantly
        ``_typewriter_tick``) can fall back to a fresh ``send`` if an edit
        on a stale message fails permanently — otherwise the live cursor
        message is forever stuck at its previous content while the buffer
        keeps growing in memory.
        """
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if not kwargs:
            return True

        async def _op() -> discord.Message:
            await msg.edit(**kwargs)
            return msg

        result = await self._retry_http(
            _op,
            label="edit",
            content_len=len(content) if content else 0,
        )
        # Sync shadow on success — see #113 / docs/investigations/stale-message-content.md.
        if result is not None and content is not None and msg is self._last_msg:
            self._last_msg_text = content
        return result is not None

    # ------------------------------------------------------------------
    # Markdown table → code block conversion (legacy) / PNG extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _format_tables(text: str) -> str:
        """Legacy: wrap markdown tables in code-fence blocks for Discord.

        Kept as the fallback path for #135 (no-Pillow / missing-font
        environments) — do not remove. Live callers use
        :meth:`_extract_and_render_tables` for the PNG path; this method
        remains the documented escape hatch when PNG rendering is
        unavailable.

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

    @staticmethod
    async def _extract_and_render_tables(
        text: str,
    ) -> tuple[str, list[TableRender]]:
        """Extract markdown tables, render to PNG, return placeholders.

        Walks ``text`` line-by-line. Each *well-formed* markdown table is
        replaced in the returned text with a unique placeholder
        ``\\n[TABLE_PNG_N]\\n`` and yields a :class:`TableRender` carrying
        headers/rows/png_bytes/markdown_source.

        Two table shapes are accepted (≥2 columns, not inside a ``` code
        fence, ≥1 data row in both cases):

        1. **Strict GFM**: header + ``|---|---|`` separator + data rows.
        2. **Relaxed (no separator)**: header followed directly by data
           rows where the first data row has the same cell count as the
           header. Claude's SDK frequently emits tables without a
           separator row, so the relaxed shape catches production output
           that the strict matcher would otherwise drop into the legacy
           code-fence fallback path. On the relaxed path a synthetic
           ``|---|...|`` line is spliced into ``markdown_source`` only
           (so the ``.md`` sidecar stays GFM-valid); the PNG renderer
           and the verbatim-fallback paths see exactly the original
           input lines.

        - ``headers`` / ``rows`` — parsed cell strings (pipe-stripped, trimmed)
        - ``png_bytes`` — output of :func:`table_png.render_table_png`
        - ``markdown_source`` — original verbatim block of lines that formed
          the table (for the Copy-as-text button)
        - ``placeholder`` — the same token inserted into the returned text

        Returns ``(text_with_placeholders, [TableRender, ...])``. If no
        tables are found, returns ``(text, [])`` unchanged.

        PNG generation is dispatched through :func:`asyncio.to_thread` so
        the renderer's heartbeat-bound event loop is never blocked on the
        Pillow draw + PNG-encode CPU burst (review I3).

        Edge cases (per PRD R1.4 / R6):

        - Tables inside ``` code fences → left verbatim, no TableRender.
        - Single-column tables (``| Header |``) → re-emitted verbatim.
        - Header-only tables (separator present but no data rows) → verbatim.
        - Header line with no following ``|...|`` line → verbatim.
        - Header + non-separator next row with mismatched cell count → verbatim.
        - PNG render failure (Pillow error, oversize → ``ValueError``) →
          original lines re-emitted verbatim, error logged (review C2).

        TODO(v1.13): support border-less GFM tables (no leading/trailing
        pipe). Currently the line must satisfy ``startswith("|") and
        endswith("|")``; Claude sometimes emits ``Name | Score`` form
        (review I5).
        """
        # Pillow defensive fallback (#135 / PRD R6 + acceptance E).
        # If the import at module load failed, we never had a working PNG
        # path — return the legacy code-fence formatting and an empty
        # render list. Caller branches on ``not renders`` and continues
        # down its no-table path (no PNG follow-ups will be attempted).
        if not PILLOW_AVAILABLE:
            return DiscordRenderer._format_tables(text), []

        lines_in = text.split("\n")
        result: list[str] = []
        renders: list[TableRender] = []
        in_code_fence = False
        i = 0
        n = len(lines_in)

        def _is_table_line(s: str) -> bool:
            return s.startswith("|") and s.endswith("|")

        def _is_separator_line(s: str) -> bool:
            # e.g. ``|---|---|`` or ``| :--- | ---: |`` — only ``|``, ``-``,
            # ``:`` and spaces, and at least one ``-``.
            inner = s.strip("|").strip()
            if not inner or "-" not in inner:
                return False
            return all(c in "|-: " for c in s)

        def _split_cells(s: str) -> list[str]:
            # Strip leading/trailing pipes, then split. Empty cells preserved.
            body = s
            if body.startswith("|"):
                body = body[1:]
            if body.endswith("|"):
                body = body[:-1]
            return [c.strip() for c in body.split("|")]

        while i < n:
            line = lines_in[i]
            stripped = line.strip()

            # Track code fences first — anything inside is opaque.
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                result.append(line)
                i += 1
                continue

            if in_code_fence or not _is_table_line(stripped):
                result.append(line)
                i += 1
                continue

            # Candidate header. Two accepted shapes:
            #   1. Strict GFM: header + ``|---|---|`` separator + ≥1 row.
            #   2. Relaxed: header followed directly by another ``|...|``
            #      row with matching cell count (Claude SDK frequently emits
            #      tables without a separator row). On the relaxed path we
            #      synthesize a ``|---|...|`` separator solely for
            #      ``markdown_source`` so the ``.md`` sidecar stays
            #      GFM-valid; the PNG renderer only ever sees headers+rows.
            header_line = line
            header_stripped = stripped
            j = i + 1

            if j >= n or not _is_table_line(lines_in[j].strip()):
                # No next table-shaped line — not a table.
                result.append(header_line)
                i += 1
                continue

            next_stripped = lines_in[j].strip()
            sep_synthesized = False
            if _is_separator_line(next_stripped):
                # Strict path — separator row consumes lines_in[j].
                sep_line = lines_in[j]
                k = j + 1
            else:
                # Relaxed path — no separator row. Require matching cell
                # count between header and the candidate first data row;
                # otherwise this is not a table and we fall back verbatim.
                header_cells = len(_split_cells(header_stripped))
                next_cells = len(_split_cells(next_stripped))
                if header_cells != next_cells:
                    result.append(header_line)
                    i += 1
                    continue
                # Synthesize a GFM separator for ``markdown_source`` only.
                sep_line = "|" + "|".join(["---"] * header_cells) + "|"
                sep_synthesized = True
                # First data row is lines_in[j] — do NOT skip it.
                k = j

            # Collect contiguous data rows.
            data_lines: list[str] = []
            while k < n:
                ds = lines_in[k].strip()
                if not _is_table_line(ds) or _is_separator_line(ds):
                    break
                data_lines.append(lines_in[k])
                k += 1

            headers = _split_cells(header_stripped)
            rows = [_split_cells(dl.strip()) for dl in data_lines]

            # PRD R1.4 — single-column / header-only → emit verbatim.
            # Only emit ``sep_line`` if it actually came from the input
            # (strict path); the relaxed path synthesizes it for the
            # markdown_source sidecar only and must not leak into output.
            if len(headers) < 2 or not rows:
                result.append(header_line)
                if not sep_synthesized:
                    result.append(sep_line)
                for dl in data_lines:
                    result.append(dl)
                i = k
                continue

            # Well-formed table — render PNG (off the event loop), then
            # replace with placeholder. On any render failure (Pillow OOM,
            # oversize guard, font corruption), emit the original lines
            # verbatim so the user never silently loses a table block.
            try:
                png = await asyncio.to_thread(render_table_png, headers, rows)
            except Exception:
                log.exception(
                    "PNG render failed for table %d; emitting verbatim",
                    len(renders),
                )
                result.append(header_line)
                if not sep_synthesized:
                    result.append(sep_line)
                result.extend(data_lines)
                i = k
                continue

            markdown_source = "\n".join(
                [header_line, sep_line, *data_lines]
            )
            placeholder = f"\n[TABLE_PNG_{len(renders)}]\n"
            renders.append(
                TableRender(
                    headers=headers,
                    rows=rows,
                    png_bytes=png,
                    markdown_source=markdown_source,
                    placeholder=placeholder,
                )
            )
            result.append(placeholder)
            i = k

        return "\n".join(result), renders


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
