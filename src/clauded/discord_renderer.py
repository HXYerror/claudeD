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
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
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


class DiscordRenderer:
    """Render a Claude streaming response into a Discord channel/thread."""

    def __init__(self, target: discord.abc.Messageable) -> None:
        self.target = target
        self._last_msg: discord.Message | None = None

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
        # Stats populated from ResultMessage
        stats: dict | None = None

        try:
            async for event in bridge.send_message(user_text):
                # Tool results can arrive on UserMessage objects too — handle any
                # message that exposes a ``content`` list of blocks.
                content = getattr(event, "content", None)
                if isinstance(event, ResultMessage):
                    # Capture stats from the result
                    stats = {
                        'cost': float(getattr(event, 'total_cost_usd', 0) or 0),
                        'input_tokens': int(getattr(event, 'input_tokens', 0) or 0),
                        'output_tokens': int(getattr(event, 'output_tokens', 0) or 0),
                        'duration_ms': (time.time() - stream_start) * 1000,
                        'num_turns': int(getattr(event, 'num_turns', 0) or 0),
                        'model': getattr(event, 'model', '') or '',
                    }
                    break

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

                            # Build a colored embed for the tool execution
                            tool_embed = discord.Embed(
                                title=f"🔄 {name}",
                                color=COLOR_TOOL_RUNNING,
                            )
                            if name == "Bash":
                                cmd = block.input.get("command", "")[:500]
                                tool_embed.description = f"```bash\n{cmd}\n```"
                            elif name in ("Write", "Edit", "Read"):
                                path = block.input.get("file_path", block.input.get("file", ""))
                                tool_embed.description = f"📄 `{path}`"
                            else:
                                tool_embed.description = "Executing..."

                            tmsg = await self._safe_send(embed=tool_embed)
                            if tmsg is not None and tool_id:
                                tool_msgs[tool_id] = tmsg

                            # Show file content preview for Write/Edit tools
                            if block.name == "Write":
                                file_path = block.input.get("file_path", "unknown")
                                file_content = block.input.get("content", "")
                                ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
                                lang = ext if ext in ("py", "js", "ts", "go", "rs", "java", "c", "cpp", "h", "md", "yaml", "yml", "json", "toml", "sh", "bash", "sql", "html", "css") else ""
                                preview = file_content[:1500].replace("```", "` ` `")  # SEC2: break triple backtick
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
                                diff_str = "\n".join(diff_lines)[:1500].replace("```", "` ` `")  # SEC2: break triple backtick
                                if len("\n".join(diff_lines)) > 1500:
                                    diff_str += "\n... (truncated)"
                                try:
                                    await self.target.send(f"✏️ `{file_path}`\n```diff\n{diff_str}\n```")
                                except discord.HTTPException:
                                    pass

                        elif isinstance(block, ToolResultBlock):
                            tool_id = getattr(block, "tool_use_id", None)
                            if tool_id and tool_id in tool_msgs:
                                orig_msg = tool_msgs[tool_id]
                                name = tool_names.get(tool_id, "tool")
                                is_err = bool(getattr(block, "is_error", False))
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
                    f" │ 📥 {stats['input_tokens']}"
                    f" │ 📤 {stats['output_tokens']}"
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
    ) -> discord.Message | None:
        """Send a message, swallowing transient HTTP errors with a short backoff."""
        if not content and embed is None and file is None:
            return None

        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file

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
                # Rebuild file if needed (stream may have been consumed)
                msg = await self.target.send(**kwargs)
                if content:
                    self._last_msg = msg
                return msg
            except discord.HTTPException:
                log.exception("Discord send failed after rate-limit backoff; dropping")
                return None
        except discord.HTTPException:
            log.warning("Discord send failed; backing off and retrying once")
            await asyncio.sleep(HTTP_BACKOFF_SECONDS)
            try:
                msg = await self.target.send(**kwargs)
                if content:
                    self._last_msg = msg
                return msg
            except discord.HTTPException:
                log.exception("Discord send failed after retry; dropping content")
                return None

    async def _safe_edit(
        self,
        msg: discord.Message,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> None:
        """Edit a message, swallowing transient HTTP errors with a short backoff."""
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed

        if not kwargs:
            return

        try:
            await msg.edit(**kwargs)
            return
        except discord.RateLimited as exc:
            retry = max(HTTP_BACKOFF_SECONDS, float(getattr(exc, "retry_after", 1.0)))
            log.debug("Discord edit rate-limited; sleeping %.2fs", retry)
            await asyncio.sleep(retry)
            try:
                await msg.edit(**kwargs)
            except discord.HTTPException:
                log.warning("Discord edit failed after rate-limit backoff; dropping")
        except discord.HTTPException:
            log.debug("Discord edit failed; backing off and retrying once")
            await asyncio.sleep(HTTP_BACKOFF_SECONDS)
            try:
                await msg.edit(**kwargs)
            except discord.HTTPException:
                log.warning("Discord edit failed after retry; dropping update")

    # ------------------------------------------------------------------
    # Smart splitting
    # ------------------------------------------------------------------

    def _smart_split(self, text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
        """Split ``text`` into <= ``limit``-char chunks.

        Preference order for split points: paragraph (``\\n\\n``) > line
        (``\\n``) > space > hard cut. If a chunk would leave an unclosed
        triple-backtick code fence, we close it with ``\\n```\\n`` and
        reopen the next chunk with ``\\n```\\n`` so each rendered Discord
        message is syntactically self-contained.
        """
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        remaining = text
        # Reserve a few chars for a possible "\n```" close fence.
        fence_reserve = len("\n```")

        while len(remaining) > limit:
            cut = self._find_cut(remaining, limit - fence_reserve)
            chunk = remaining[:cut]
            tail = remaining[cut:]
            # Strip a leading newline that we just split on.
            if tail.startswith("\n"):
                tail = tail.lstrip("\n")

            # If the chunk leaves an unclosed code fence, close it here and
            # reopen at the start of the next chunk so syntax highlighting
            # doesn't bleed across messages.
            if chunk.count("```") % 2 == 1:
                # Try to detect the language tag of the open fence so we can
                # reopen with the same one.
                lang = self._detect_open_fence_lang(chunk)
                chunk = chunk.rstrip() + "\n```"
                tail = f"```{lang}\n" + tail

            chunks.append(chunk)
            remaining = tail

        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _find_cut(text: str, limit: int) -> int:
        """Return a "good" cut index <= ``limit``.

        Tries paragraph break, then line, then space, then a hard cut.
        Cuts are required to be at least ~half the limit so we don't
        produce tiny chunks just because the first paragraph is short.
        """
        if len(text) <= limit:
            return len(text)

        # Discord splits the text immediately after the cut, so
        # ``rfind`` returns the index of the separator; we cut *after* it
        # for "\n\n"/"\n" so the newline ends the previous chunk.
        floor = max(1, limit // 2)

        para = text.rfind("\n\n", 0, limit)
        if para >= floor:
            return para + 2

        line = text.rfind("\n", 0, limit)
        if line >= floor:
            return line + 1

        space = text.rfind(" ", 0, limit)
        if space >= floor:
            return space + 1

        return limit

    @staticmethod
    def _detect_open_fence_lang(chunk: str) -> str:
        """Return the language tag of the *open* fence in ``chunk`` (or "")."""
        # Find the last ``` that opens a fence (i.e. an odd-indexed one).
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
