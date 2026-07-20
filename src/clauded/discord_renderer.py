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
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp
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
from claude_agent_sdk.types import AssistantMessageError, StreamEvent, UserMessage  # noqa: F401 — AssistantMessageError documents the contract pinned by tests/test_api_error_render.py::test_assistant_message_error_field_exists_on_sdk; runtime detection uses getattr() for duck-typing flexibility.

# #292 Dynamic Workflow events. SDK 0.2.110 ships all four Task* message
# types. Wrapped in a try for forward-compatibility with older SDKs that
# lack these entirely — the renderer falls back to silent-ignore when None.
try:
    from claude_agent_sdk.types import (  # noqa: F401
        TaskStartedMessage as _SdkTaskStartedMessage,
        TaskProgressMessage as _SdkTaskProgressMessage,
        TaskNotificationMessage as _SdkTaskNotificationMessage,
        TaskUpdatedMessage as _SdkTaskUpdatedMessage,
    )
except ImportError:  # pragma: no cover — legacy SDK
    _SdkTaskStartedMessage = None  # type: ignore[assignment,misc]
    _SdkTaskProgressMessage = None  # type: ignore[assignment,misc]
    _SdkTaskNotificationMessage = None  # type: ignore[assignment,misc]
    _SdkTaskUpdatedMessage = None  # type: ignore[assignment,misc]
from .stream_logger import log_event as _log_stream
from .cogs._table_view import CopyTableTextView
from ._errors import is_transient_discord_error
from ._http_retry import safe_send_message

if TYPE_CHECKING:
    from .claude_bridge import ClaudeBridge

log = logging.getLogger("clauded.discord_renderer")

# Pillow defensive fallback (#135 / PRD R6 + acceptance E).
# If Pillow (or .table_png's other deps) cannot be imported, fall back to the
# legacy code-fence wrap in ``DiscordRenderer._format_tables`` rather than
# crashing the renderer. ``PILLOW_AVAILABLE`` is consulted at the top of
# :meth:`DiscordRenderer._extract_and_render_tables` for a fast-fail.
try:
    from .table_png import render_table_png
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    log.warning("Pillow not installed; tables fall back to code-fence rendering")


# ---------------------------------------------------------------------------
# CommonMark §4.5 fenced code-block state machine (v1.16 #142 §A2).
# ---------------------------------------------------------------------------


def _advance_fence_state(line: str, fence_count: int) -> int:
    """Return the new fence depth after consuming ``line``.

    Implements the CommonMark §4.5 fence rule: a fence opens with a run of
    N≥3 backticks at the start of an otherwise unindented line; it only
    closes on a line whose leading backtick run is ≥N. Inner runs shorter
    than the opener do NOT close the fence (this is the v1.12 bug-D
    regression case — an inner ```` ``` ```` inside a ```` ```` ```` outer
    fence must be preserved verbatim).

    Args:
        line: the input line (not yet stripped — leading whitespace ignored).
        fence_count: ``0`` if we are not currently inside a fence, otherwise
            the opener's backtick run length (so a ≥N closer is recognised).

    Returns:
        The new fence_count. ``0`` means we are not in a fence after this
        line; any positive value means we are. The caller decides how to
        emit ``line`` (typically: if either the new or old count is >0,
        the line is "fence-owned" — emit verbatim and skip parsing).

    The helper is intentionally pure / module-level so both the legacy
    :meth:`DiscordRenderer._format_tables` path (no-Pillow fallback) and
    the production :meth:`DiscordRenderer._extract_and_render_tables`
    path share one state machine. Before the v1.16 extraction these
    paths used *different* trackers (legacy used a bool toggle that was
    wrong for quad-backtick fences); routing both through this helper
    closes that latent gap now that the fallback is reachable via the
    Pillow-missing branch (#135 / PRD R6).
    """
    stripped = line.lstrip()
    if not stripped.startswith("`"):
        return fence_count
    j = 0
    while j < len(stripped) and stripped[j] == "`":
        j += 1
    ticks = j
    if ticks < 3:
        return fence_count
    if fence_count == 0:
        # Opens a fence whose closer must be ≥``ticks`` backticks.
        return ticks
    # Already inside a fence — only a ≥fence_count run closes it.
    if ticks >= fence_count:
        return 0
    # Inner shorter run — fence remains open (CommonMark §4.5).
    return fence_count


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


@dataclass
class _TaskState:
    """Per-task tracking for #292 Dynamic Workflow rendering.

    A single Dynamic Workflow subtask has one Discord message that gets
    edited in-place through its lifetime (TaskStarted → N × TaskProgress →
    TaskNotification / TaskUpdated terminal). Storing the ``discord.Message``
    reference here is what enables the in-place edit — dropping it (or
    losing this dict entry) forces the next event to send a fresh embed,
    which was the pre-#292 spam behavior.

    Fields
    ------
    description
        Human-readable subtask description from ``TaskStartedMessage``.
    task_type
        SDK-provided classifier (``"local_workflow"`` / ``"remote-agent"`` /
        etc.); ``None`` when the SDK omits it.
    tool_use_id
        Parent ``ToolUseBlock`` id, when the subtask was spawned via the
        Task tool. Used to route follow-up events into an existing
        sub-agent thread (see ``subagent_renderers``).
    message
        The Discord message we send on TaskStarted and edit on
        TaskProgress / TaskNotification. ``None`` iff the send failed
        (transient HTTP, permission drop); handlers must degrade gracefully.
    started_at
        Wall-clock ``time.time()`` when the TaskStarted arrived — used to
        compute an elapsed footer and to bound the throttle window.
    last_edit_at
        Last successful edit timestamp; the TaskProgress throttle checks
        this against :data:`EDIT_INTERVAL_SECONDS`.
    last_usage
        Snapshot of the most recent ``TaskUsage`` payload — kept so the
        terminal embed (TaskNotification without ``usage``, or TaskUpdated
        killed) can still render a footer.
    thread_id
        #322: Discord channel/thread id this task is rendered into (the
        renderer's ``target.id``). ``/workflow detail`` uses it to look up the
        live per-agent roster (``bot._agent_roster[thread_id]``) that the
        SubagentStart / PreToolUse / SubagentStop hooks maintain. ``None``
        when the target exposes no ``id``.
    last_tool_name
        #322: most recent ``last_tool_name`` seen on a ``TaskProgress`` event,
        surfaced by ``/workflow detail`` when the roster is empty.
    """

    description: str
    task_type: str | None = None
    tool_use_id: str | None = None
    message: "discord.Message | None" = None
    started_at: float = 0.0
    last_edit_at: float = 0.0
    last_usage: dict[str, Any] | None = None
    thread_id: int | None = None
    last_tool_name: str | None = None

# ---------------------------------------------------------------------------
# Color scheme for embeds
# ---------------------------------------------------------------------------
COLOR_CLAUDE = 0x7C3AED        # Purple — Claude replies
COLOR_TOOL_RUNNING = 0xF59E0B  # Yellow — tool executing
COLOR_TOOL_SUCCESS = 0x10B981  # Green — tool completed
COLOR_TOOL_FAILURE = 0xEF4444  # Red — tool failed / error

# WebSearch title prefix (shared between launch + completion embeds)
_WEBSEARCH_TITLE_PREFIX = "🔍 Searching: "
COLOR_INFO = 0x3B82F6          # Blue — info / commands
COLOR_THINKING = 0x6B7280      # Gray — thinking
COLOR_WORKFLOW = 0x8B5CF6      # Purple — Dynamic Workflow banner

# Discord caps message content at 2000 characters; we leave a small margin so
# we can append a cursor or close-and-reopen a code fence safely.
DISCORD_MAX_LEN = 1900

# #211 R1 architect: relocated from cogs/mode.py to break a real cyclic
# import (cogs/mode.py imports COLOR_INFO from this module; the renderer
# previously did a lazy ``from .cogs.mode import MODE_EMOJI`` to paper
# over the cycle). MODE_EMOJI is a rendering concern — it lives here.
# cogs/mode.py imports it back for /mode current + /health + /session info.
MODE_EMOJI: dict[str, str] = {
    "acceptEdits": "✏️",
    "plan": "🔒",
    "bypassPermissions": "⚡",
}

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

# review A1: total wall-clock budget for the post-ResultMessage drain of
# trailing workflow Task* messages. ``receive_response()`` returns at the
# first ResultMessage, so a workflow subtask whose terminal
# TaskNotification/TaskUpdated lands just after the result would otherwise
# never be finalized (its ``⚡ Running`` embed stays forever + the
# ``_task_states`` entry leaks into the next turn). We only enter this drain
# when tasks are still pending, so normal turns pay ZERO latency. Env
# ``CLAUDED_DRAIN_TOTAL_TIMEOUT`` overrides (default 8s). Per-message gap
# timeout lives in ``ClaudeBridge.receive_pending`` (``CLAUDED_DRAIN_MSG_TIMEOUT``).
DRAIN_TOTAL_SECONDS = float(os.environ.get("CLAUDED_DRAIN_TOTAL_TIMEOUT", "8.0"))

# Threshold above which a code block is uploaded as a file attachment.
CODE_FILE_UPLOAD_THRESHOLD = 3000

# Regex patterns for Claude channel/thread management markers.
_THREAD_PATTERN = re.compile(r'\[CREATE_THREAD:\s*(.+?)\]')
_CHANNEL_PATTERN = re.compile(r'\[CREATE_CHANNEL:\s*(.+?)\]')

# #222: markdown image syntax pointing at a local file.
#   ![alt-text](/tmp/foo.png)   — stripped + uploaded as attachment
#   ![alt-text](http://...)     — left untouched (Discord previews http
#                                  directly; only LOCAL paths need lifting)
# Captures: 1 = alt-text (discarded), 2 = path.
_IMG_PATTERN = re.compile(
    r'!\[([^\]]*)\]\(([^)\s]+\.(?:png|jpg|jpeg|gif|webp))\)',
    re.IGNORECASE,
)

# #222: cap per-attachment size at the conservative Discord non-boost
# limit. Larger files → reject + leave the markdown in place + log.
_IMG_MAX_BYTES = 8 * 1024 * 1024

# #222: Discord hard limit per message.
_IMG_MAX_PER_MESSAGE = 10


def _is_path_allowed(path: Path, project_path: Path | None) -> bool:
    """True iff ``path.resolve()`` lives under an allowed root (#222).

    Roots (default):
    - ``/tmp`` (resolved — macOS ``/tmp→/private/tmp``)
    - ``$TMPDIR`` (macOS per-user system temp, e.g.
      ``/var/folders/<hash>/T/``)
    - ``project_path`` (the bot-bound project subtree, if any)

    NOT ``$HOME`` — too wide. claude could inline ``~/.ssh/id_rsa``
    or ``~/.aws/credentials`` and we MUST NOT lift those.

    The check uses ``Path.resolve(strict=True)`` so:
    1. Symlinks are resolved before the under-root test (defeats
       ``/tmp/link-to-etc-passwd`` escape).
    2. Missing files reject immediately (avoid leaking "file existed"
       side-channels via WARN log).
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False

    allowed_roots: list[Path] = []
    # Cross-platform temp directory (#289)
    try:
        import tempfile
        allowed_roots.append(Path(tempfile.gettempdir()).resolve())
    except OSError:
        pass
    try:
        allowed_roots.append(Path("/tmp").resolve())
    except OSError:
        pass
    sys_tmp = os.environ.get("TMPDIR")
    if sys_tmp:
        try:
            allowed_roots.append(Path(sys_tmp).resolve())
        except OSError:
            pass
    if project_path is not None:
        try:
            allowed_roots.append(project_path.resolve())
        except OSError:
            pass

    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False




def _fmt_tokens(n: int) -> str:
    """Format token count: 2235 -> 2.2k, 523 -> 523."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


async def _fetch_context_pct_settled(
    bridge: Any,
    *,
    settle_delay: float = 0.5,
    log_label: str = "footer",
) -> float | None:
    """Fetch context-window usage percentage with settle delay + retry-on-0 (#220).

    The CLI control plane doesn't immediately reflect the just-finished
    turn's token state when ``ResultMessage`` is emitted. Calling
    ``get_context_usage()`` in that window returns ``percentage == 0``.
    By the time the user runs ``/context`` manually, state has settled —
    confirms it's a timing window, not a data-path bug.

    Strategy:
      1. Sleep ``settle_delay`` (default 0.5s) — lets the CLI commit.
      2. Call ``bridge.get_context_usage()``. Compute precise percentage.
      3. If precise percentage == 0, sleep ``settle_delay`` again and call
         once more. Accept whatever the second call returns (including 0 —
         first message in a fresh session is legitimately 0%).

    **#v1.18 precision fix**: the SDK's ``ContextUsageResponse.percentage``
    is documented as float but the live wire format returns an INT —
    anything ``< 0.5%`` rounds to ``0`` and the user sees ``🧠 0%``
    forever on small conversations under wide context windows (Opus-4
    at 1M tokens means a 28k-token turn rounds to ``0``). Compute the
    percentage ourselves from ``totalTokens / maxTokens`` so a 2.8%
    conversation renders as ``🧠 2.8%`` not ``🧠 0%``.

    Falls back to the SDK-supplied ``percentage`` int when ``totalTokens``
    or ``maxTokens`` is missing (old/different SDK versions).

    ``settle_delay`` is a single knob (R1 simplicity #225: pre-PR had
    separate ``initial_delay``/``retry_delay`` that were always set
    together). Tests pass ``settle_delay=0.0`` to skip actual sleep.

    Returns the percentage float, or ``None`` on any error / missing field.
    Caller's :func:`_format_context_segment` handles the
    ``None`` / ``0`` / ``0 < pct < 1`` cases.
    """
    def _compute(cu: dict[str, Any] | None) -> float | None:
        if cu is None:
            return None
        from ._context_usage import compute_global_context_pct
        # #280: pass the bridge's cached context window so a just-switched
        # model (e.g. /model switch opus) renders against the *new*
        # denominator instead of the SDK's stale ``maxTokens``.
        override = getattr(bridge, "_context_window_override", None)
        result = compute_global_context_pct(cu, max_tokens_override=override)
        return result[2] if result is not None else None

    try:
        await asyncio.sleep(settle_delay)
        cu = await bridge.get_context_usage()
        pct = _compute(cu)
        if pct is None:
            log.debug("%s: get_context_usage returned no usable percentage", log_label)
            return None
        if pct > 0:
            return pct
        # Retry once — CLI likely hasn't fully committed turn state yet.
        await asyncio.sleep(settle_delay)
        cu = await bridge.get_context_usage()
        return _compute(cu)
    except Exception:
        log.debug("%s: get_context_usage failed", log_label, exc_info=True)
        return None


def _format_context_segment(stats: dict[str, Any] | None) -> str | None:
    """Return ``│ 🧠 N%`` (or thresholded ⚠️ / 🔥) for the cost footer, or
    ``None`` when no usable percentage is available.

    #182: stats may have ``context_percentage`` populated from
    ``bridge.get_context_usage()`` (#163 sub-task 3 reuse). The renderer
    silently omits the segment on missing / invalid data so we never
    break the existing footer for legacy bridges or transient errors.

    Thresholds (boundary inclusive on the lower bound):
      - < 75%   → 🧠 (calm)
      - 75-89%  → ⚠️ (warning)
      - >= 90%  → 🔥 (critical)

    Floor: percentages > 0 but < 1% render as ``<1%`` (otherwise
    ``int(0.4) == 0`` would mislead the user into thinking they've
    used nothing).
    """
    if not stats:
        return None
    pct = stats.get("context_percentage")
    if pct is None:
        return None
    try:
        pct_f = float(pct)
    except (TypeError, ValueError):
        return None
    if pct_f < 0 or pct_f > 100:
        return None  # invalid range, omit silently
    # Threshold-colored emoji
    if pct_f >= 90:
        emoji = "🔥"
    elif pct_f >= 75:
        emoji = "⚠️"
    else:
        emoji = "🧠"
    # Floor sub-1% to ``<1%`` so a 0.4% turn doesn't render as ``0%``.
    if 0 < pct_f < 1:
        return f" │ {emoji} <1%"
    # #v1.18 precision tier: SDK's ``percentage`` field is int-rounded,
    # so a 2.8% conversation showed as ``🧠 0%`` (or ``3%`` if SDK gave
    # us the rounded value directly). We now compute pct ourselves from
    # ``totalTokens / maxTokens`` for precision; honor that precision by
    # showing 1 decimal in the 1–10% range where the difference matters
    # most. ``0`` exact stays ``0%`` (no decimal noise on truly empty);
    # values >= 10% stay int (🔥 92.4% reads no better than 🔥 92%).
    if 0 < pct_f < 10:
        return f" │ {emoji} {pct_f:.1f}%"
    return f" │ {emoji} {int(pct_f)}%"


# ---------------------------------------------------------------------------
# #192 Fix A: normalize SDK ToolResultBlock.content shapes to plain text.
# ---------------------------------------------------------------------------
# Claude SDK 0.1.80 returns ``ToolResultBlock.content`` in 3 shapes depending
# on the tool:
#   - ``str``                            — most Bash/Read/Grep results
#   - ``list[dict]`` with TextBlock dicts — Task/Agent tool family, async
#     sub-agents, multi-modal results: ``[{"type": "text", "text": "..."}]``
#   - ``dict``                            — rare; some structured responses
#
# Before #192, the renderer did ``str(block.content)``, which produced the
# Python repr (``[{'type': 'text', 'text': '...'}]``) on shape #2 — user saw
# raw brackets + quotes + ``\n`` literals. Prod-impact concentrated on the
# Subtask Complete embed (#161 D.1 latent bug, escalated to P0 via #192).

# Pre-compiled patterns for CLI-internal meta-instructions that leak into
# user-facing text. Claude CLI scaffolds async-agent / Task tool results
# with hints intended for the LLM's reasoning ("do not mention to user"),
# never for display. Strip best-effort; the pattern list is extensible.
_INTERNAL_META_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ``(internal ID - do not mention to user. Use SendMessage with to:
    # 'xxx' to continue this agent.)`` — wrapped in parens
    re.compile(r"\((?:internal ID|internal id) - do not mention to user[^)]*\)", re.IGNORECASE),
    # Standalone ``do not mention ... to user ...`` sentence (no paren
    # wrapping). Bounded by sentence end so we don't eat following content.
    re.compile(r"\bdo not mention[^.]*?to user[^.]*?\.", re.IGNORECASE),
)


def _extract_block_content_text(content: Any) -> str:
    """Normalize a ``ToolResultBlock.content`` payload to a plain text string.

    Handles SDK's documented shapes (#192 Fix A):
    - ``None``                                 → ``""`` (no-op)
    - ``str``                                  → the string itself
    - ``list[dict]`` with ``{"type":"text","text":"..."}``  → join ``text``s
    - ``list[str]``                            → join with ``"\\n"``
    - ``list[mixed]``                          → best-effort element repr
    - ``dict`` with ``"text"`` key             → return that
    - anything else                            → ``str(content)`` fallback

    No exception escapes; the worst-case fallback (``str``) preserves the
    pre-#192 behavior for unrecognized shapes.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    return str(content)


def _strip_internal_metadata(text: str) -> str:
    """Remove known CLI-internal meta-instruction phrases from user-facing
    text (#192 Fix B). Best-effort — pattern list lives at module scope
    (``_INTERNAL_META_PATTERNS``) for easy extension as new CLI quirks
    surface. Idempotent on already-clean text.
    """
    if not text:
        return text
    for pat in _INTERNAL_META_PATTERNS:
        text = pat.sub("", text)
    # Collapse any double-space artifacts left behind
    text = re.sub(r"  +", " ", text)
    return text.strip()


def _is_async_agent_dispatch(text: str) -> bool:
    """Detect async-agent launch acknowledgments (#192 Fix C).

    Claude CLI's async-agent tool emits a ``Async agent launched
    successfully.`` text payload as a tool RESULT (not a completion).
    The actual sub-agent work continues in the background. Marking this
    as "✅ Subtask Complete" was misleading; the renderer instead shows
    "🚀 Sub-agent dispatched" so users know the work is still running.

    Uses word-boundary anchoring on "async agent" (case-insensitive)
    so a stray substring elsewhere doesn't false-trigger.
    """
    if not text:
        return False
    return bool(_ASYNC_DISPATCH_PATTERN.search(text))


_ASYNC_DISPATCH_PATTERN = re.compile(r"\basync agent launched\b", re.IGNORECASE)


def _extract_subagent_stats(input_value: Any) -> dict[str, Any] | None:
    """Extract stats from a sub-agent ``UserMessage.tool_use_result`` payload.

    The Claude SDK 0.1.80 attaches sub-agent (Task/Agent tool) cost / token /
    duration stats to the ``UserMessage.tool_use_result`` attribute as a
    ``dict``, NOT to ``ToolResultBlock.content`` (which is a ``list[dict]``
    of text blocks — the human-readable report, not the metrics).

    #172 fix: prior call sites passed ``block.content`` here, so this
    helper saw lists of TextBlock dicts and always returned ``None`` —
    Fix C's sub-thread mini-footer never rendered in production. Tests
    used synthetic JSON strings that never matched real SDK shape and so
    falsely passed. Callers now pass ``getattr(event, 'tool_use_result',
    None)`` from the parent ``UserMessage``.

    Real SDK shape (verified against jsonl):

    .. code-block:: python

        {
            "status": "completed",
            "totalDurationMs": 340388,
            "totalTokens": 2094,
            "totalToolUseCount": 17,
            "usage": {"input_tokens": 735, "output_tokens": 1359},
        }

    Fails-soft: ANY exception during parsing (malformed dict, deep
    nesting RecursionError, non-numeric values) yields ``None``. This
    is the boundary between SDK output (attacker-controllable via sub-
    agent stdout) and the renderer's fatal-error path; we MUST NOT let
    a malformed stats payload tear down the whole turn.

    #160 Fix C: sub-agent threads previously had NO footer at all. The
    user runs ``/crew`` workflows that spawn 5-10 sub-threads per turn;
    surfacing per-sub-thread cost is the highest-value half of this fix.
    """
    if input_value is None:
        return None
    try:
        # Accept dict directly (real SDK shape) or JSON-encoded string
        # (legacy v1.18 R1 contract; preserved for callers that already
        # serialized the dict).
        if isinstance(input_value, dict):
            parsed: dict = input_value
        elif isinstance(input_value, str):
            parsed = json.loads(input_value)
            if not isinstance(parsed, dict):
                return None
        else:
            return None
        out: dict[str, Any] = {}
        if "totalDurationMs" in parsed:
            out["duration_s"] = float(parsed["totalDurationMs"]) / 1000
        if "totalTokens" in parsed:
            out["total_tokens"] = int(parsed["totalTokens"])
        # SDK 0.1.80 nests input/output under ``usage`` dict; older shapes
        # had flat ``inputTokens`` / ``outputTokens`` at the top level.
        # Accept both.
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                out["input_tokens"] = int(usage["input_tokens"])
            if "output_tokens" in usage:
                out["output_tokens"] = int(usage["output_tokens"])
        if "inputTokens" in parsed and "input_tokens" not in out:
            out["input_tokens"] = int(parsed["inputTokens"])
        if "outputTokens" in parsed and "output_tokens" not in out:
            out["output_tokens"] = int(parsed["outputTokens"])
        if "totalCostUsd" in parsed:
            out["cost"] = float(parsed["totalCostUsd"])
        if "totalToolUseCount" in parsed:
            out["tool_count"] = int(parsed["totalToolUseCount"])
        return out if out else None
    except Exception as exc:
        # Malformed input / unexpected types / recursion / encoding errors:
        # treat as "no stats" and let the caller skip the mini-footer.
        #
        # #223 PR-B: previously this swallowed ALL renderer bugs in
        # _compute_stats silently — if the parser ever broke, no log
        # line surfaced. Now record at WARNING with exc_info; behavior
        # (return None) unchanged.
        log.warning(
            "_compute_stats failed; mini-footer suppressed: %s",
            exc,
            exc_info=True,
        )
        return None


def _format_subagent_footer(stats: dict[str, Any] | None) -> str | None:
    """Render a sub-agent stats dict into a one-line Discord small-text footer.

    Returns None if stats is None or has no renderable fields, so the caller
    can simply ``if footer := _format_subagent_footer(stats):`` without an
    extra emptiness check.

    Token display: prefer direction-explicit ``input_tokens`` + ``output_tokens``
    over aggregate ``total_tokens``. Only fall back to total when neither
    direction is available (R1 engineer #2: prior asymmetric rendering could
    show both 📥 and 📊 on a stats dict that had input+total).
    """
    if not stats:
        return None
    parts: list[str] = []
    if "cost" in stats:
        parts.append(f"💰 ${stats['cost']:.4f}")
    has_direction = "input_tokens" in stats or "output_tokens" in stats
    if "input_tokens" in stats:
        parts.append(f"📥 {_fmt_tokens(stats['input_tokens'])}")
    if "output_tokens" in stats:
        parts.append(f"📤 {_fmt_tokens(stats['output_tokens'])}")
    if not has_direction and "total_tokens" in stats:
        parts.append(f"📊 {_fmt_tokens(stats['total_tokens'])}")
    if "duration_s" in stats:
        parts.append(f"⏱️ {stats['duration_s']:.1f}s")
    if "tool_count" in stats:
        parts.append(f"🔧 {stats['tool_count']}")
    if not parts:
        return None
    return "-# " + " │ ".join(parts)


class DiscordRenderer:
    """Render a Claude streaming response into a Discord channel/thread."""

    def __init__(
        self,
        target: discord.abc.Messageable,
        *,
        bot: discord.Client | None = None,
        project_path: "Path | None" = None,
    ) -> None:
        self.target = target
        # Optional bot reference — when provided, persistent views (e.g.
        # :class:`ToolResultsView` from #161) are registered via
        # ``bot.add_view(view, message_id=...)`` so button-click routing
        # works even after the in-memory View instance is gc'd or the bot
        # restarts (discord.py only re-dispatches custom_id interactions
        # to views that are in the client's persistent view store).
        self._bot = bot
        # #222: bound project path is part of the markdown-image allowlist;
        # claude can inline ``![alt](./project/img.png)`` and we'll lift it
        # to a Discord attachment iff it resolves under here. ``None`` =
        # only the temp dirs are allowed.
        self._project_path = project_path
        self._last_msg: discord.Message | None = None
        # Shadow of the text we last wrote to ``_last_msg``. discord.py 2.0+
        # Message.edit() is not in-place; reading msg.content post-edit returns
        # stale text. See docs/investigations/stale-message-content.md (#113).
        self._last_msg_text: str = ""
        # #292 Dynamic Workflow: per-task lifecycle state keyed by task_id.
        # Instance-level so /workflow commands can inspect across turns.
        self._task_states: dict[str, _TaskState] = {}

    # ------------------------------------------------------------------
    # #292 S3: sync task lifecycle state to bot-level registry
    # ------------------------------------------------------------------

    def _sync_task_to_bot(self, task_id: str, state: _TaskState | None) -> None:
        """Write/remove a task state entry in ``bot._workflow_tasks``.

        Called from TaskStarted (write), TaskNotification/TaskUpdated (remove).
        The bot-level dict is what ``/workflow list|detail|kill`` reads.
        """
        bot = self._bot
        if bot is None:
            return
        wt = getattr(bot, "_workflow_tasks", None)
        if wt is None:
            return
        if state is None:
            wt.pop(task_id, None)
        else:
            wt[task_id] = state

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
    # Dynamic Workflow (#292): task lifecycle handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_task_usage(usage: dict[str, Any] | None) -> str:
        """Format a ``TaskUsage`` dict into a footer-friendly segment.

        The SDK ships :class:`claude_agent_sdk.types.TaskUsage` as a TypedDict
        (``total_tokens``/``tool_uses``/``duration_ms``). ``None`` and empty
        dicts collapse to an empty string so the caller can safely
        concatenate. Duration flips to seconds with one decimal once it
        crosses 999 ms so the footer stays readable in long-running tasks.
        """
        if not usage:
            return ""
        tokens = int(usage.get("total_tokens", 0) or 0)
        tool_uses = int(usage.get("tool_uses", 0) or 0)
        duration_ms = int(usage.get("duration_ms", 0) or 0)
        if duration_ms >= 1000:
            dur_seg = f"{duration_ms / 1000:.1f}s"
        else:
            dur_seg = f"{duration_ms}ms"
        return f"🪙 {_fmt_tokens(tokens)} · 🔧 {tool_uses} · ⏱️ {dur_seg}"

    @staticmethod
    def _task_type_label(task_type: str | None) -> str:
        """#321: human label for a Task* event keyed on the real task_type.

        Real background tasks use task_type ``local_bash`` / ``local_agent`` /
        ``local_workflow`` (only the last contains the substring "workflow").
        Map each to a distinct banner so the user can tell a background bash
        task from a background agent from a dynamic workflow. Unknown /
        missing types fall back to a generic label.
        """
        t = (task_type or "").lower()
        if "bash" in t:
            return "🖥️ Background Task"
        if "agent" in t:
            return "🤖 Background Agent"
        if "workflow" in t:
            return "⚡ Dynamic Workflow"
        return "📦 Background Task"

    async def _handle_task_started(
        self,
        event: Any,
        subagent_renderers: dict[str, "DiscordRenderer"],
    ) -> None:
        """Render a 🚀 embed for a newly-launched background subtask.

        #321: tracks EVERY task_type (local_bash / local_agent /
        local_workflow), labelling the embed by the real type so background
        bash tasks and background agents get progress too — not just workflows.

        Routes the embed to the sub-agent thread when the parent tool_use_id
        matches one we've already spun a thread for (keeps the workflow's
        chatter grouped with its owning Task tool call). Otherwise the embed
        lands in the main channel. The sent ``discord.Message`` is stashed in
        ``_TaskState`` so subsequent Progress/Notification events can edit
        in-place instead of spamming.
        """
        task_id = getattr(event, "task_id", None)
        if not task_id:
            return
        description = (getattr(event, "description", "") or "")[:60] or "Task"
        task_type = getattr(event, "task_type", None)
        tool_use_id = getattr(event, "tool_use_id", None)

        # #321: label by real task_type instead of always "Dynamic Workflow".
        label = self._task_type_label(task_type)
        embed = discord.Embed(
            title=f"{label} Started",
            description=(
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔮 Task: {description[:60]}\n"
                f"🤖 Type: {task_type or 'workflow'}\n"
                f"📋 ID: `{task_id[:8]}`"
            ),
            color=COLOR_WORKFLOW,
        )

        # Route to sub-agent thread if this task belongs to one; otherwise
        # main target. Preserves the existing #172 grouping discipline.
        target_renderer: "DiscordRenderer" = self
        if tool_use_id and tool_use_id in subagent_renderers:
            target_renderer = subagent_renderers[tool_use_id]

        msg = await target_renderer._safe_send(embed=embed)
        state = _TaskState(
            description=description,
            task_type=task_type,
            tool_use_id=tool_use_id,
            message=msg,
            started_at=time.time(),
            last_edit_at=0.0,
            last_usage=None,
            # #322 (review fix): key by the MAIN renderer's target id, NOT
            # target_renderer.target (which can be a sub-thread) — bot._agent_roster
            # is keyed by the main thread id, so /workflow detail's roster lookup
            # must use the same key or it silently finds nothing.
            thread_id=getattr(self.target, "id", None),
        )
        self._task_states[task_id] = state
        self._sync_task_to_bot(task_id, state)

    async def _handle_task_progress(
        self,
        event: Any,
    ) -> None:
        """Edit the task's message with fresh usage stats, throttled.

        Discord edits are rate-limited (see :data:`EDIT_INTERVAL_SECONDS`);
        Dynamic Workflow can emit progress on every tool call so unbounded
        edits would burn the bot's rate budget. We always update
        ``last_usage`` in state so the terminal embed can render whatever
        the throttler dropped, but only push a Discord edit when the
        interval has elapsed.
        """
        task_id = getattr(event, "task_id", None)
        if not task_id:
            return
        state = self._task_states.get(task_id)
        if state is None:
            # Progress before Started — SDK ordering violation. Log and
            # skip; we don't have a message to edit.
            log.debug("TaskProgress for unknown task_id=%s; ignoring", task_id)
            return

        usage = getattr(event, "usage", None)
        if isinstance(usage, dict):
            state.last_usage = usage
        last_tool = getattr(event, "last_tool_name", None)
        # #322: persist the latest tool so /workflow detail can show it even
        # when the live per-agent roster is empty.
        if last_tool:
            state.last_tool_name = last_tool

        now = time.time()
        if now - state.last_edit_at < EDIT_INTERVAL_SECONDS:
            return
        if state.message is None:
            return

        # Build usage stats for inline display
        usage = state.last_usage or {}
        tokens = int(usage.get("total_tokens", 0) or 0)
        tool_uses = int(usage.get("tool_uses", 0) or 0)
        duration_ms = int(usage.get("duration_ms", 0) or 0)
        duration = f"{duration_ms / 1000:.1f}"

        # --- Parse workflowProgress from event data ---
        data = getattr(event, "data", None) or {}
        progress = data.get("workflowProgress", [])

        if progress:
            embed = self._build_workflow_progress_embed(
                progress, tokens, tool_uses, duration, last_tool,
            )
        else:
            # T1-B/T1-E: the CLI's ``workflowProgress`` payload never arrives
            # in practice (investigation: 0 occurrences in any log), so the old
            # fallback was a flat one-liner. Build a real per-agent panel from
            # the hook-fed roster (bot._agent_roster — populated by
            # SubagentStart/PreToolUse/SubagentStop, which DO fire) plus elapsed
            # wall-clock, so the user sees live agent status regardless.
            elapsed = int(time.time() - state.started_at) if state.started_at else 0
            roster: dict = {}
            try:
                tid = getattr(self.target, "id", None)
                if tid is not None and self._bot is not None:
                    roster = getattr(self._bot, "_agent_roster", {}).get(tid, {}) or {}
            except Exception:
                roster = {}
            lines = [
                "━━━━━━━━━━━━━━━━━━━━━━━",
                f"🔮 Task: {state.description[:60]}",
                f"⏱️ {elapsed}s elapsed",
            ]
            agent_lines = self._format_roster_lines(roster)
            if agent_lines:
                lines.append(f"🤖 Agents: {len(roster)} running")
                lines.extend(agent_lines)
            else:
                lines.append(f"💭 Last tool: {last_tool or '—'}")
            lines.append(
                f"🪙 {tokens} tokens · 🔧 {tool_uses} tools · ⏱️ {duration}s"
            )
            embed = discord.Embed(
                title=f"🔄 {self._task_type_label(state.task_type).split(' ', 1)[-1]} Running",
                description="\n".join(lines),
                color=COLOR_TOOL_RUNNING,
            )

        ok = await self._safe_edit(state.message, embed=embed)
        if ok:
            state.last_edit_at = now

    def _build_workflow_progress_embed(
        self,
        progress: list[dict],
        tokens: int,
        tool_uses: int,
        duration: str,
        last_tool: str | None,
    ) -> "discord.Embed":
        """Build a rich embed from workflowProgress data."""
        phases = [p for p in progress if p.get("type") == "workflow_phase"]
        agents = [a for a in progress if a.get("type") == "workflow_agent"]

        lines: list[str] = ["━━━━━━━━━━━━━━━━━━━━━━━"]

        # Phase line
        if phases:
            current_phase = next(
                (p for p in phases if p.get("status") == "running"), None,
            )
            total_phases = len(phases)
            if current_phase:
                idx = current_phase.get("index", 0) + 1
                title = current_phase.get("title", "…")
                lines.append(f'📋 Phase {idx}/{total_phases}: "{title}"')
            else:
                # No running phase — show total
                completed = sum(1 for p in phases if p.get("status") == "completed")
                lines.append(f"📋 Phases: {completed}/{total_phases} completed")

        # Agent summary
        if agents:
            running = sum(
                1 for a in agents if a.get("state") in ("start", "progress")
            )
            done = sum(1 for a in agents if a.get("state") == "done")
            failed = sum(1 for a in agents if a.get("state") == "error")
            parts = []
            if running:
                parts.append(f"{running} running")
            if done:
                parts.append(f"{done} done")
            if failed:
                parts.append(f"{failed} failed")
            if parts:
                lines.append(f"🤖 Agents: {' · '.join(parts)}")

            # Per-agent lines (fold if >10)
            agent_lines = self._format_agent_lines(agents, last_tool)
            if len(agent_lines) > 10:
                # Show first 5 + "... N more" + last 2
                folded = agent_lines[:5]
                folded.append(f"  … {len(agent_lines) - 7} more …")
                folded.extend(agent_lines[-2:])
                lines.extend(folded)
            else:
                lines.extend(agent_lines)

        # Usage footer
        tok_display = f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)
        lines.append(f"🪙 {tok_display} tokens · 🔧 {tool_uses} tools · ⏱️ {duration}s")

        description = "\n".join(lines)
        # Cap at 4000 chars (Discord embed limit)
        if len(description) > 4000:
            description = description[:3997] + "…"

        return discord.Embed(
            title="⚡ Dynamic Workflow Running",
            description=description,
            color=COLOR_TOOL_RUNNING,
        )

    @staticmethod
    def _format_roster_lines(roster: dict) -> list[str]:
        """T1-B: format the hook-fed per-agent roster into embed lines.

        ``roster`` is ``bot._agent_roster[thread_id]`` — a mapping of
        ``agent_id -> {type, tool, started}`` maintained by the
        SubagentStart / PreToolUse / SubagentStop hooks. Unlike
        :meth:`_format_agent_lines` (which needs the CLI's ``workflowProgress``
        payload that never arrives), this derives entirely from hook data that
        reliably fires. Shows type + current tool + elapsed, folded past 10.
        """
        lines: list[str] = []
        now = time.time()
        items = sorted(roster.items())
        for i, (_aid, info) in enumerate(items, 1):
            if i > 10:
                remaining = len(items) - 10
                if remaining > 0:
                    lines.append(f"  … {remaining} more …")
                break
            if not isinstance(info, dict):
                continue
            atype = (info.get("type") or "subagent")[:24]
            tool = info.get("tool")
            started = info.get("started") or now
            elapsed = int(now - started)
            tool_seg = f"🔧 {tool}" if tool else "💭 …"
            lines.append(f"  🔄 #{i} {atype} · {tool_seg} · ⏱️ {elapsed}s")
        return lines

    @staticmethod
    def _format_agent_lines(
        agents: list[dict], last_tool: str | None,
    ) -> list[str]:
        """Format per-agent status lines."""
        lines: list[str] = []
        for a in agents:
            idx = a.get("index", 0) + 1
            label = a.get("label", f"Agent {idx}")
            state = a.get("state", "")
            agent_tokens = a.get("tokens", 0) or 0

            if state == "done":
                tok_k = f"{agent_tokens / 1000:.0f}k tok" if agent_tokens else ""
                suffix = f" ({tok_k})" if tok_k else ""
                lines.append(f"  ✅ #{idx} {label}{suffix}")
            elif state in ("start", "progress"):
                tool_hint = a.get("promptPreview") or last_tool or "…"
                lines.append(f"  🔄 #{idx} {label}... (💭 {tool_hint})")
            elif state == "error":
                lines.append(f"  ❌ #{idx} {label}")
            elif state == "skipped":
                lines.append(f"  ⏭️ #{idx} {label}")
            else:
                # pending or unknown
                lines.append(f"  ⏳ #{idx} {label}")
        return lines

    async def _render_terminal_task(
        self,
        task_id: str,
        status: str,
        description: str,
        usage: dict | None,
    ) -> None:
        """Render a terminal (✅/❌/⏹️) task embed and drop the task from state.

        Shared body for :meth:`_handle_task_notification` (source:
        ``TaskNotificationMessage`` with statuses ``completed`` / ``failed``
        / ``stopped``) and :meth:`_handle_task_updated` (source:
        ``TaskUpdatedMessage`` with terminal statuses ``completed`` /
        ``failed`` / ``killed``). Callers are responsible for extracting
        ``status`` / ``description`` / ``usage`` from their event and for
        any pre-flight guards (e.g. the updated handler's E3 duplicate-skip
        and non-terminal-status filter).

        Prefers ``_safe_edit`` on the existing task-state message when we
        have one (keeps the whole subtask on a single Discord slot); falls
        back to ``_safe_send`` if state was lost or the initial send never
        succeeded. Always pops the task from ``self._task_states`` so
        subsequent events for the same ``task_id`` are ignored.
        """
        if status == "completed":
            title = "✅ Task Complete"
            color = COLOR_TOOL_SUCCESS
        elif status == "failed":
            title = "❌ Task Failed"
            color = COLOR_TOOL_FAILURE
        elif status == "stopped":
            title = "⏹️ Task Stopped"
            color = COLOR_THINKING
        elif status == "killed":
            title = "⏹️ Task Killed"
            color = COLOR_THINKING
        else:
            title = f"ℹ️ Task ({status or 'unknown'})"
            color = COLOR_INFO

        embed = discord.Embed(title=title, description=description, color=color)
        state = self._task_states.get(task_id)
        # T1-D: fold total wall-clock duration into the terminal footer so the
        # completed/failed/stopped card summarizes how long the workflow ran,
        # alongside the SDK usage (tokens/tool-uses/SDK-time).
        usage_seg = self._fmt_task_usage(usage)
        if state is not None and getattr(state, "started_at", 0):
            elapsed = int(time.time() - state.started_at)
            dur_seg = f"⏱️ {elapsed}s total"
            usage_seg = f"{usage_seg} · {dur_seg}" if usage_seg else dur_seg
        if usage_seg:
            embed.set_footer(text=usage_seg)

        edited = False
        if state and state.message is not None:
            edited = await self._safe_edit(state.message, embed=embed)
        if not edited:
            await self._safe_send(embed=embed)

        self._task_states.pop(task_id, None)
        self._sync_task_to_bot(task_id, None)

    async def _handle_task_notification(
        self,
        event: Any,
    ) -> None:
        """Render the terminal (✅/❌/⏹️) embed and drop the task from state.

        ``TaskNotificationMessage.status`` is one of ``completed`` / ``failed``
        / ``stopped``. Summary is the pre-formatted human message from the
        subagent; we cap it at 3800 chars to leave headroom under Discord's
        4096 description limit for the title + emoji.

        Delegates the embed/edit/cleanup dance to
        :meth:`_render_terminal_task` after extracting summary/usage from
        the event.
        """
        task_id = getattr(event, "task_id", None)
        if not task_id:
            return
        status = str(getattr(event, "status", "") or "").lower()
        summary = (getattr(event, "summary", "") or "")[:3800]
        usage = getattr(event, "usage", None)

        state = self._task_states.get(task_id)
        description = summary or (state.description if state else "")
        # Prefer the terminal usage payload; fall back to the last known
        # progress snapshot so we never show an empty footer when the SDK
        # skips ``usage`` on the notification.
        effective_usage = usage if isinstance(usage, dict) else (state.last_usage if state else None)

        await self._render_terminal_task(
            task_id, status, description, effective_usage,
        )

    async def _handle_task_updated(
        self,
        event: Any,
    ) -> None:
        """Handle ``TaskUpdatedMessage`` — the ``killed`` fallback for
        stopped-in-background tasks that never emit a Notification.

        SDK 0.2.110 now exports ``TaskUpdatedMessage`` so callers use
        ``isinstance`` dispatch. ``event.status`` is one of ``pending``/
        ``running``/
        ``paused``/``completed``/``failed``/``killed``; we only act on
        terminal states — non-terminal patches are logged and left to the
        Progress stream. Rendering itself is delegated to
        :meth:`_render_terminal_task`.
        """
        task_id = getattr(event, "task_id", None)
        if not task_id:
            return
        # E3: guard against duplicate terminal when Notification already handled
        if task_id not in self._task_states:
            log.debug("TaskUpdated task_id=%s already cleaned; skip", task_id)
            return
        status = str(getattr(event, "status", "") or "").lower()
        terminal = {"completed", "failed", "killed"}
        if status not in terminal:
            log.debug("TaskUpdated non-terminal status=%s task_id=%s; skip", status, task_id)
            return

        state = self._task_states.get(task_id)
        description = state.description if state else "Task terminated"
        usage = state.last_usage if state else None

        await self._render_terminal_task(
            task_id, status, description, usage,
        )

    # ------------------------------------------------------------------
    # review A1: unified Task* dispatch (shared by main loop + drain)
    # ------------------------------------------------------------------

    async def _dispatch_task_event(
        self,
        event: Any,
        subagent_renderers: dict[str, "DiscordRenderer"],
    ) -> bool:
        """Route a #292 Dynamic Workflow ``Task*`` message to its handler.

        Returns ``True`` iff ``event`` was one of the four SDK ``Task*``
        message types AND we handled it (i.e. the caller should treat it as
        consumed and ``continue``). Returns ``False`` for any non-Task* event,
        and also for Task* events we aren't tracking (Progress / Notification /
        Updated for a ``task_id`` not in ``_task_states``).

        #321: ``TaskStarted`` is now tracked for EVERY ``task_type`` (local_bash
        / local_agent / local_workflow), not only workflows — the old
        ``"workflow" in task_type`` gate dropped background bash / agent
        progress. Follow-up events still gate on ``task_id in _task_states``.

        review A1: extracted verbatim from the inline block that used to live
        in :meth:`render_response`'s main loop (the four ``isinstance(event,
        _SdkTask*Message)`` checks plus their gates). Sharing one code path
        means the post-ResultMessage drain (which runs
        :meth:`ClaudeBridge.receive_pending`) finalizes trailing workflow
        terminals with byte-for-byte the same behavior as the in-stream case.
        The ordering matters: ``TaskUpdated`` must be checked last so a future
        SDK subclass overlap doesn't shadow the isinstance-matched types above
        it (same rationale as the original inline block).
        """
        if _SdkTaskStartedMessage is not None and isinstance(event, _SdkTaskStartedMessage):
            # #321: track EVERY task_type, not just workflows. Real background
            # tasks use task_type local_bash / local_agent / local_workflow;
            # only local_workflow contains the substring "workflow", so the old
            # `"workflow" in task_type` gate silently dropped background bash
            # tasks and background agents — the user saw no progress at all.
            # Started now tracks all types into _task_states, so the
            # Progress/Notification/Updated handlers (which gate on
            # `task_id in self._task_states`) render for all of them too.
            await self._handle_task_started(event, subagent_renderers)
            return True

        if _SdkTaskProgressMessage is not None and isinstance(event, _SdkTaskProgressMessage):
            task_id = getattr(event, "task_id", "")
            if task_id in self._task_states:  # Only handle if we started tracking it (workflow)
                await self._handle_task_progress(event)
                return True
            return False

        if _SdkTaskNotificationMessage is not None and isinstance(event, _SdkTaskNotificationMessage):
            task_id = getattr(event, "task_id", "")
            if task_id in self._task_states:
                await self._handle_task_notification(event)
                return True
            return False

        if _SdkTaskUpdatedMessage is not None and isinstance(event, _SdkTaskUpdatedMessage):
            task_id = getattr(event, "task_id", "")
            if task_id in self._task_states:
                await self._handle_task_updated(event)
                return True
            return False

        return False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def render_response(
        self,
        bridge: "ClaudeBridge",
        user_text: str | list[dict],
        *,
        author_id: int | None = None,
    ) -> None:
        """Stream Claude's response for ``user_text`` to :attr:`target`.

        ``user_text`` is either a plain ``str`` (text-only turn) or a
        ``list[dict]`` of Anthropic Messages-API content blocks (text +
        inline image blocks). Forwarded as-is to ``bridge.send_message``;
        see #242 round 2 for the spike-verified rationale.

        ``author_id`` (optional) is the Discord user id that triggered the
        turn — used by :class:`ToolResultsView` to restrict ephemeral
        followups (clicking a tool-result button) to the original author,
        so other channel members can't read another user's tool output.
        When omitted, the View accepts clicks from anyone (legacy /
        single-user channel behavior)."""
        buffer = ""                               # text not yet finalized into a sent msg
        live_msg: discord.Message | None = None   # the in-flight typewriter message
        start_time: float | None = None
        stream_start: float = time.time()
        last_edit = 0.0
        typewriter = False                        # have we entered typewriter mode?
        saw_text = False                          # any TextBlock seen?
        # review C1: did streaming deltas deliver text for the CURRENT
        # assistant message? Set in the ``content_block_delta`` text handler,
        # consulted + reset when the matching AssistantMessage content list is
        # processed. When a provider/proxy doesn't emit deltas (non-streaming
        # gateway / LiteLLM / some Copilot bridges), this stays False and the
        # AssistantMessage TextBlock branch renders the text as a fallback
        # instead of dropping it to the "(no text response)" placeholder.
        stream_text_seen = False
        # tool_use_id -> discord.Message
        tool_msgs: dict[str, discord.Message] = {}
        tool_names: dict[str, str] = {}
        task_depth = 0                            # subtask nesting depth (#74)
        # Stats populated from ResultMessage
        stats: dict | None = None
        # #v1.18-footer: snapshot the bridge's cumulative ``total_cost``
        # BEFORE the turn starts so we can compute the per-turn delta when
        # ``ResultMessage`` arrives. SDK only ships cumulative cost; we
        # want the footer's 💰 to show the user what THIS turn cost (the
        # 💰 cumulative used to mislead users into thinking a 6.9k-token
        # turn cost $5.98 when really only a few cents of it was theirs).
        cost_before: float = float(getattr(bridge, "total_cost", 0.0) or 0.0)
        _last_stop_reason: str | None = None
        # Rolling tool log (Issue #1: merge consecutive tool embeds)
        tool_log_msg: discord.Message | None = None
        tool_log_lines: list[str] = []
        # Sub-agent threads: tool_use_id -> discord.Thread / DiscordRenderer
        subagent_threads: dict[str, discord.Thread] = {}
        subagent_renderers: dict[str, "DiscordRenderer"] = {}
        # #172: capture Task/Agent sub-agent stats from UserMessage's
        # ``tool_use_result`` attribute. Keyed by tool_use_id so the
        # downstream AssistantMessage ToolResultBlock handler (which is
        # what actually renders the Subtask Complete embed) can pull the
        # right stats. Populated in the UserMessage branch below; consumed
        # at line ~915 / ~944.
        tool_use_results: dict[str, dict[str, Any]] = {}
        # #161 medium tier (v1.18 R3): collected tool results that need a
        # "view" button on the rolling-log embed itself. Each entry:
        #   {tool_use_id: (tool_name, content_str)}
        # The accompanying ToolResultsView attaches one button per entry
        # (up to 25, Discord's per-view limit) to the rolling-log message.
        # Clicking sends an ephemeral message containing the .txt file
        # attachment — only the clicker sees it, zero channel real-estate.
        medium_results: dict[str, tuple[str, str]] = {}
        tool_results_view: ToolResultsView | None = None

        # review A1: set True when the main loop breaks on ResultMessage.
        # Gates the post-loop ``receive_pending()`` drain so it only runs on a
        # normally-completed turn that still has pending workflow tasks (never
        # on the transient/fatal-error paths, which ``return``/``raise`` before
        # reaching the drain).
        result_received = False

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
                                # #208: skip empty/whitespace-only payloads.
                                # SDK can emit placeholder ThinkingBlock with
                                # `thinking=""` (streaming intermediate, proxy
                                # strip, models that don't surface reasoning),
                                # which would render as literal `||||` in
                                # Discord (the spoiler parser requires non-
                                # empty content between the markers).
                                raw = block.thinking or ""
                                if not raw.strip():
                                    continue
                                thinking_text = raw[:3900].replace("||", "\\|\\|")
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
                                    # #204: extract content + apply short-tier
                                    # parity. Sub-agent rolling log previously
                                    # just sliced ``[2:]`` to swap 🔄 → ✅/❌
                                    # — losing the inline content surfaced on
                                    # the main thread by #161 / #165 / #180.
                                    # Medium-tier button is out-of-scope (per
                                    # #204): sub-renderer doesn't own a
                                    # persistent view store; per-sub-thread
                                    # button persistence is a separate piece
                                    # of work.
                                    content_str = _extract_block_content_text(block.content)
                                    is_short = (
                                        len(content_str) < 200
                                        and content_str.strip() != ""
                                    )
                                    # Update last matching entry
                                    for i in range(len(sub_renderer._tool_log_lines) - 1, -1, -1):
                                        line = sub_renderer._tool_log_lines[i]
                                        if not line.startswith("🔄"):
                                            continue
                                        # Existing line shape is ``🔄 Bash: `cmd` `` —
                                        # `[2:]` strips the running emoji + space
                                        # and we keep the rest as the "name fragment"
                                        # (full ``Bash: `cmd` `` rather than just
                                        # ``Bash`` to preserve sub-renderer's
                                        # current display). For #204 short tier we
                                        # need the bare tool name to format
                                        # ``✅ Bash → content``; pull it from the
                                        # fragment by splitting on the first ``:``
                                        # or space.
                                        body = line[2:].lstrip()
                                        # Tool-name heuristic: everything up to
                                        # the first ``:`` (Bash/Read/Edit/Write
                                        # all use ``Name: ...`` shape) OR everything
                                        # before the first space (WebSearch "🔍 ..."
                                        # / fallback "name...").
                                        if ":" in body:
                                            tool_label = body.split(":", 1)[0].strip()
                                        elif " " in body:
                                            tool_label = body.split(" ", 1)[0].strip()
                                        else:
                                            tool_label = body.strip()
                                        if is_err:
                                            error_text = content_str[:100] if content_str else "Failed"
                                            sub_renderer._tool_log_lines[i] = (
                                                f"{status} {tool_label}: {error_text}"
                                            )
                                        elif is_short:
                                            # Same backtick-strip + newline-collapse
                                            # contract as the main-thread short tier.
                                            safe = (
                                                content_str.strip()
                                                .replace("`", "'")
                                                .replace("\n", " │ ")
                                            )
                                            sub_renderer._tool_log_lines[i] = (
                                                f"{status} {tool_label} → {safe}"
                                            )
                                        else:
                                            # Fallback to current behavior:
                                            # keep the original line body, just
                                            # flip the leading emoji.
                                            sub_renderer._tool_log_lines[i] = (
                                                f"{status} {body}"
                                            )
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

                # -------------------------------------------------------
                # #292 Dynamic Workflow (SDK 0.2.110+): 4 Task* message
                # types emitted during ``effort=xhigh/max`` parallel
                # sub-agent execution. Handlers own the entire render for
                # these events — no ``content`` list to feed the main
                # AssistantMessage/UserMessage branches below.
                # review A1: dispatch is now delegated to the shared
                # :meth:`_dispatch_task_event` helper so the identical logic
                # can be re-run by the post-ResultMessage drain below. It
                # returns True iff it fully handled a Task* message (workflow
                # TaskStarted, or a Progress/Notification/Updated for a
                # tracked task); we ``continue`` then. A non-workflow
                # TaskStarted returns False so it falls through to the
                # sub-thread ``parent_tool_use_id`` routing that already ran
                # above for its content, matching pre-A1 behavior.
                if await self._dispatch_task_event(event, subagent_renderers):
                    continue

                if isinstance(event, ResultMessage):
                    # Capture stats from the result.
                    # #v1.18-footer: ``total_cost_usd`` on ResultMessage is
                    # the CUMULATIVE conversation cost (per SDK contract /
                    # claude_bridge._update_stats). For the footer's 💰
                    # we want the per-turn delta = cumulative_after -
                    # cost_before snapshot taken at render_response start.
                    cumulative_cost = float(getattr(event, 'total_cost_usd', 0) or 0)
                    stats = {
                        'cost': max(0.0, cumulative_cost - cost_before),
                        'cost_cumulative': cumulative_cost,
                        'input_tokens': int((getattr(event, 'usage', None) or {}).get('input_tokens', 0) or 0),
                        'output_tokens': int((getattr(event, 'usage', None) or {}).get('output_tokens', 0) or 0),
                        'duration_ms': (time.time() - stream_start) * 1000,
                        'num_turns': int(getattr(event, 'num_turns', 0) or 0),
                        'model': getattr(event, 'model', '') or '',
                        'stop_reason': _last_stop_reason,
                        # #audit(#13): end-of-turn error signals that were
                        # previously dropped, so a rate-limit / max-turns /
                        # API-error turn looked like a normal stop to the user.
                        'is_error': bool(getattr(event, 'is_error', False)),
                        'subtype': getattr(event, 'subtype', None),
                        'api_error_status': getattr(event, 'api_error_status', None),
                    }
                    # #182 + #220: pull context-window usage and fold
                    # percentage into stats so the footer can render
                    # `🧠 N%` segment. ResultMessage.usage carries per-turn
                    # API tokens only; the cumulative-vs-max ratio lives
                    # on a separate SDK control-plane call (already wired
                    # for /context cmd, #163 sub-task 3). #220 wraps the
                    # call in `_fetch_context_pct_settled` which adds a
                    # short settle delay + retry-on-0 to side-step a CLI
                    # control-plane race that previously made the footer
                    # always show `🧠 0%`. Mirrors #160's graceful-fallback
                    # discipline — helper returns None on any error, and
                    # `_format_context_segment` omits the segment then.
                    stats["context_percentage"] = await _fetch_context_pct_settled(
                        bridge, log_label="footer"
                    )
                    # review A1: ``receive_response()`` returns at the first
                    # ResultMessage, so there is nothing more to iterate here —
                    # the old ``result_received=True; continue`` path was dead
                    # code (the ``async for`` ended anyway). Break the main loop
                    # unconditionally; the bounded ``receive_pending()`` drain
                    # AFTER the loop is what now finalizes any trailing workflow
                    # Task* terminals still pending in ``_task_states``.
                    result_received = True
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
                                # review C1: a real streaming text delta arrived
                                # for the CURRENT assistant message, so the
                                # trailing AssistantMessage TextBlock replay is
                                # a duplicate and must be skipped. This flag is
                                # consumed + reset when we process that
                                # AssistantMessage's content list below.
                                stream_text_seen = True
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
                # #172: but BEFORE we skip, capture any
                # ``tool_use_result`` attribute the SDK attached to a
                # UserMessage. Task/Agent sub-agents emit their
                # cost/duration/token stats here (NOT inside
                # ToolResultBlock.content). We key by the matching
                # ToolResultBlock.tool_use_id so the downstream
                # AssistantMessage handler can pull the stats by id.
                if isinstance(event, UserMessage):
                    tur = getattr(event, "tool_use_result", None)
                    if isinstance(tur, dict) and isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                tid = getattr(block, "tool_use_id", None)
                                if tid:
                                    tool_use_results[tid] = tur
                # Only render AssistantMessage content for the regular
                # ToolUseBlock / TextBlock / ThinkingBlock path. ToolResultBlock
                # (#172 root-cause analysis verified via jsonl) ALWAYS arrives
                # on UserMessage from the SDK — it's how the agent passes the
                # sub-agent / tool output back into the conversation. We thus
                # process BOTH event classes here, but only walk the blocks
                # the respective event class actually carries:
                #   - AssistantMessage: TextBlock, ThinkingBlock, ToolUseBlock
                #     (ToolResultBlock would never appear here in real SDK
                #     output but the tests below historically synthesized it
                #     this way; keep the path for back-compat with those
                #     tests — the if isinstance(block, ToolResultBlock):
                #     branch is defensive)
                #   - UserMessage: ToolResultBlock (with stats on
                #     event.tool_use_result that we already captured above)
                if isinstance(content, list) and isinstance(event, (AssistantMessage, UserMessage)):
                    # #183: synthetic API-error messages from the SDK or
                    # upstream provider (LiteLLM, GitHub Copilot, etc.)
                    # arrive as ``AssistantMessage(error=<kind>, model=
                    # "<synthetic>", content=[TextBlock("API Error: ...")]
                    # )``. The TextBlock branch below skips these as
                    # "streaming duplicates" — but synthetic errors don't
                    # stream, so dropping them leaves the user with the
                    # final "(Claude returned no text response)"
                    # placeholder and no idea what went wrong. Render as
                    # a red error embed and mark ``saw_text`` so the
                    # placeholder catch-all doesn't fire on top of it.
                    api_error = getattr(event, "error", None) if isinstance(event, AssistantMessage) else None
                    if api_error is not None:
                        # Pull the human-readable text from the first
                        # TextBlock (synthetic errors always pack one).
                        err_text = ""
                        for blk in content:
                            if isinstance(blk, TextBlock):
                                err_text = blk.text
                                break
                        if not err_text:
                            err_text = f"(no error body; AssistantMessage.error={api_error})"
                        # R1 security: upstream API error bodies can
                        # contain literal ``` (e.g. an inner exception
                        # repr) which would close our outer 3-backtick
                        # fence early and visually mangle the embed.
                        # Replace with a zero-width-joiner separator so
                        # the text still reads correctly.
                        err_text = err_text.replace("```", "`\u200d`\u200d`")
                        # Discord embed description cap is 4096; trim with
                        # a tail marker for the rare giant traceback.
                        if len(err_text) > 3800:
                            err_text = err_text[:3800] + "\n… (truncated)"
                        embed = discord.Embed(
                            title=f"❌ Provider error: {api_error}",
                            description=(
                                f"```\n{err_text}\n```\n\n"
                                f"-# Try `/session clear` if the conversation has "
                                f"grown past the provider's context window, or "
                                f"switch providers (`/model` / env)."
                            ),
                            color=COLOR_TOOL_FAILURE,
                        )
                        await self._safe_send(embed=embed)
                        # Mark saw_text so the end-of-render catch-all
                        # "(no text response)" placeholder doesn't fire on
                        # top of the error embed.
                        saw_text = True
                        continue
                    for block in content:
                        if isinstance(block, ThinkingBlock):
                            # #208: skip empty/whitespace-only payloads — see
                            # sub-agent path above for full rationale.
                            raw = block.thinking or ""
                            if not raw.strip():
                                continue
                            thinking_text = raw[:3900].replace("||", "\\|\\|")
                            embed = discord.Embed(
                                title="💭 Thinking...",
                                description=f"||{thinking_text}||",
                                color=COLOR_THINKING,
                            )
                            await self._safe_send(embed=embed)
                            continue

                        if isinstance(block, TextBlock):
                            # review C1: normally a duplicate — with
                            # ``include_partial_messages=True`` the text already
                            # arrived via StreamEvent ``content_block_delta``
                            # (which set ``stream_text_seen`` + appended
                            # ``buffer``), so skip it to avoid double-rendering.
                            # BUT some providers/proxies (non-streaming gateway,
                            # LiteLLM, certain Copilot bridges) never emit those
                            # deltas — the real answer arrives ONLY here. When no
                            # stream text was seen for this assistant message and
                            # the block carries real text, render it as a
                            # fallback (append to ``buffer`` + set ``saw_text``)
                            # so the normal finalize path shows it instead of the
                            # "(Claude returned no text response)" placeholder.
                            # Precedent: the #183 synthetic-error branch above
                            # handles a sibling non-streaming AssistantMessage
                            # case. Idempotency: gating on ``not
                            # stream_text_seen`` is what prevents the streaming
                            # case from rendering text twice.
                            if not stream_text_seen and block.text.strip():
                                saw_text = True
                                buffer += block.text
                            continue

                        elif isinstance(block, ToolUseBlock):
                            # Finalize any pending typewriter message before
                            # interleaving a tool-status message, so the order
                            # in the channel matches the order of events.
                            # #141 — ``is_final=False`` skips table extraction
                            # on the partial buffer; see _finalize_typewriter.
                            if live_msg is not None:
                                await self._finalize_typewriter(live_msg, buffer, is_final=False)
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
                            # #212 forensics: log any sub-agent tool name +
                            # input keys to stream-debug.jsonl so we capture
                            # real schema for future re-introduction. #223
                            # PR-A added the stream_logger; we just emit
                            # one event per sub-agent-ish ToolUseBlock.
                            if name in (
                                "TaskOutput", "AgentOutput",
                                "TaskStop", "AgentStop",
                                "Task", "Agent",
                            ):
                                _log_stream({
                                    "type": "SubAgentToolUse",
                                    "name": name,
                                    "tool_id": tool_id,
                                    "input_keys": (
                                        list(block.input.keys())
                                        if isinstance(block.input, dict)
                                        else None
                                    ),
                                })

                            if name in ("Task", "Agent"):
                                # #311: flush pending text before subagent dispatch
                                # so user sees claude's message immediately.
                                if buffer.strip():
                                    live_msg, buffer = await self._typewriter_tick(
                                        live_msg, buffer
                                    )
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
                                        subagent_renderers[tool_id] = DiscordRenderer(sub_thread, bot=self._bot)

                                    # Compact summary in main thread
                                    summary_embed = discord.Embed(
                                        title=f"🔀 Subtask #{task_depth}",
                                        description=f"{desc}\n📎 {sub_thread.mention}",
                                        color=COLOR_INFO,
                                    )
                                    tmsg = await self._safe_send(embed=summary_embed)
                                    if tmsg and tool_id:
                                        tool_msgs[tool_id] = tmsg
                                except discord.HTTPException as exc:
                                    # #223 PR-B: HTTPException is expected
                                    # (e.g. cross-guild) and we already have
                                    # a fallback, so just record at WARNING
                                    # — not silent.
                                    log.warning(
                                        "sub-thread summary embed failed; falling back to inline: %s",
                                        exc,
                                    )
                                    # Fallback: inline
                                    sep = discord.Embed(title=f"🔀 Subtask #{task_depth}", description=desc, color=COLOR_INFO)
                                    tmsg = await self._safe_send(embed=sep)
                                    if tmsg and tool_id:
                                        tool_msgs[tool_id] = tmsg
                                continue

                            # --- #74 / #212: dead Subtask Output/Stop paths removed ---
                            #
                            # v1.6 PRD F4 speculatively wired ``TaskOutput`` /
                            # ``AgentOutput`` / ``TaskStop`` / ``AgentStop``
                            # tool names with guessed ``block.input`` schemas.
                            # SDK never documented these names; the field name
                            # (``output``) was never observed in prod stream
                            # data, so the resulting embeds rendered with empty
                            # descriptions and no anchor link to the spawning
                            # ``Task`` — orphans in the main thread.
                            #
                            # #212 decision (Approach C): delete the
                            # speculative paths. If the CLI does emit these
                            # tool names, they fall through to the generic
                            # tool-use embed below, which is already correct
                            # (#192 Fix C also handles async-agent dispatch).
                            # We can re-add targeted handling once stream-debug
                            # (#223 PR-A) captures real schema.
                            #
                            # ``task_depth`` is still incremented in the
                            # ``Task`` / ``Agent`` branch above and consumed
                            # by downstream indentation logic; the missing
                            # decrement on TaskStop is acceptable because
                            # ``task_depth`` is a sub-thread display hint
                            # only — spurious depth is bounded by the
                            # parent turn ending (depth resets per render).

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
                                tool_embed = discord.Embed(title=f"{_WEBSEARCH_TITLE_PREFIX}{query}", color=COLOR_TOOL_RUNNING)
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

                                    # #160 Fix C / #172: extract sub-agent
                                    # stats from the matching UserMessage's
                                    # ``tool_use_result`` (captured in the
                                    # UserMessage branch above, keyed by
                                    # tool_use_id). The SDK delivers Task
                                    # results in TWO events: UserMessage
                                    # carries the stats dict; AssistantMessage
                                    # carries the ToolResultBlock. We render
                                    # here (Assistant branch) but the data
                                    # source is the captured Assistant->User
                                    # mapping.
                                    sub_stats = _extract_subagent_stats(
                                        tool_use_results.get(tool_id)
                                    )
                                    sub_footer = _format_subagent_footer(sub_stats)

                                    # #192 Fix A: extract list-of-dict
                                    # content to plain text (was leaking
                                    # Python repr). Fix B: strip CLI's
                                    # "do not mention to user" meta
                                    # instructions. Fix C: detect async-
                                    # agent dispatch and label it as such
                                    # rather than "Complete".
                                    raw_text = _extract_block_content_text(block.content)
                                    cleaned = _strip_internal_metadata(raw_text)
                                    is_dispatch = (
                                        not is_err
                                        and _is_async_agent_dispatch(cleaned)
                                    )
                                    description = (cleaned[:300] if cleaned else "Done")
                                    if sub_footer:
                                        description = f"{description}\n\n{sub_footer}"
                                    if is_dispatch:
                                        embed_title = "🚀 Sub-agent dispatched (running in background)"
                                        embed_color = COLOR_INFO
                                    elif is_err:
                                        embed_title = "❌ Subtask Failed"
                                        embed_color = COLOR_TOOL_FAILURE
                                    else:
                                        embed_title = "✅ Subtask Complete"
                                        embed_color = COLOR_TOOL_SUCCESS
                                    done = discord.Embed(
                                        title=embed_title,
                                        description=description,
                                        color=embed_color,
                                    )
                                    await subagent_renderers[tool_id]._safe_send(embed=done)

                                    # Update main thread summary — keep it as a
                                    # pure mention-link per PRD invariant; the
                                    # mini-footer lives only in the sub-thread
                                    # so users browsing the main thread aren't
                                    # double-shown the cost data.
                                    if tool_id in tool_msgs:
                                        # Mirror the title computed above
                                        # so main-thread summary matches
                                        # the sub-thread completion state.
                                        summary = discord.Embed(
                                            title=embed_title,
                                            description=f"📎 {sub_thread.mention}",
                                            color=embed_color,
                                        )
                                        await self._safe_edit(tool_msgs[tool_id], embed=summary)
                                elif tool_id in tool_msgs:
                                    # Fallback: inline completion (no sub-thread was created).
                                    # #160 Fix C / #172: same UserMessage
                                    # tool_use_result source as the sub-
                                    # thread path above; just rendered
                                    # inline when no sub-thread exists.
                                    sub_stats = _extract_subagent_stats(
                                        tool_use_results.get(tool_id)
                                    )
                                    sub_footer = _format_subagent_footer(sub_stats)
                                    # #192 Fix A/B/C applied here too.
                                    raw_text = _extract_block_content_text(block.content)
                                    cleaned = _strip_internal_metadata(raw_text)
                                    is_dispatch = (
                                        not is_err
                                        and _is_async_agent_dispatch(cleaned)
                                    )
                                    description = (cleaned[:300] if cleaned else "Done")
                                    if sub_footer:
                                        description = f"{description}\n\n{sub_footer}"
                                    if is_dispatch:
                                        embed_title = "🚀 Sub-agent dispatched (running in background)"
                                        embed_color = COLOR_INFO
                                    elif is_err:
                                        embed_title = "❌ Subtask Failed"
                                        embed_color = COLOR_TOOL_FAILURE
                                    else:
                                        embed_title = "✅ Subtask Complete"
                                        embed_color = COLOR_TOOL_SUCCESS
                                    done_embed = discord.Embed(
                                        title=embed_title,
                                        description=description,
                                        color=embed_color,
                                    )
                                    await self._safe_edit(tool_msgs[tool_id], embed=done_embed)
                                continue

                            if result_name == "TaskStop":
                                # #212: the speculative TaskStop tool-use
                                # branch was removed above (Approach C),
                                # so we'd never have a ``tool_msgs[tool_id]``
                                # to update here. Keep the early-continue
                                # to skip generic tool-result rendering for
                                # any TaskStop tool_result that does slip
                                # through (it'd carry no useful info).
                                continue

                            is_err = bool(getattr(block, "is_error", False))
                            name = tool_names.get(tool_id, "tool") if tool_id else "tool"
                            if tool_log_msg is not None:
                                # Update rolling tool log
                                status = "✅" if not is_err else "❌"
                                # #161 bonus bug fix: WebSearch / WebFetch emit
                                # rolling-log lines that start with "🔄 🔍" /
                                # "🔄 🌐" (tool-specific emoji between marker and
                                # content). The original ``startswith("🔄 " + name)``
                                # never matched those, so the status emoji stuck
                                # at 🔄 forever. Use a tolerant match: any line
                                # starting with 🔄 (any line not yet completed)
                                # AND containing the name OR its tool-specific
                                # emoji.
                                #
                                # Audit of all rolling-log append sites at
                                # _tool_log_lines.append(...) confirms the
                                # following tools need alias entries (only those
                                # whose append format inserts a glyph between
                                # "🔄 " and the tool name):
                                #   - WebSearch → "🔄 🔍 {query}"
                                #   - WebFetch  → "🔄 🌐 {url}"
                                # All other tools (Bash, Read, Write, Edit, Grep,
                                # Glob, Skill, fallback) append as
                                # "🔄 {name}: …" so direct match already works.
                                # If a future tool adds emoji-prefix shape, add
                                # it here and to test_tool_result_shorttier.py.
                                tool_marker_aliases = {
                                    "WebSearch": "🔍",
                                    "WebFetch": "🌐",
                                }
                                alias = tool_marker_aliases.get(name, "")
                                # #226: pre-loop defaults so the no-match
                                # path (rolling-log eviction past the
                                # last-15 window / ToolResultBlock arrives
                                # before its ToolUseBlock) doesn't crash
                                # the renderer with UnboundLocalError.
                                # Downstream guards (`if is_medium and
                                # not is_err and tool_id:` etc.) all gate
                                # on `is_medium`; with the False default,
                                # those branches skip cleanly and the
                                # event is a safe no-op (correct since
                                # there's no rolling-log line to update).
                                is_medium = False
                                is_xlong = False  # #181 xlong tier marker
                                content_str = ""
                                medium_index = None
                                matched = False  # for the diagnostic log below
                                for i in range(len(tool_log_lines) - 1, -1, -1):
                                    line = tool_log_lines[i]
                                    if not line.startswith("🔄 "):
                                        continue
                                    matches_name = line.startswith("🔄 " + name)
                                    matches_alias = alias and line.startswith("🔄 " + alias)
                                    if not (matches_name or matches_alias):
                                        continue
                                    # #161 short tier: when result is short
                                    # (< 200 chars), surface it inline so
                                    # user sees the actual data instead of a
                                    # bare ``✅ Bash``. v1.18 R3 (user
                                    # feedback "1-3 lines should display
                                    # directly"): drop the single-line
                                    # constraint. Multiline short outputs
                                    # collapse to ``line1 │ line2 │ line3``
                                    # so they fit in the rolling-log embed
                                    # without ballooning vertical height.
                                    content_str = _extract_block_content_text(block.content)
                                    is_short = (
                                        len(content_str) < 200
                                        and content_str.strip() != ""
                                    )
                                    # #161 medium tier (v1.18 R3): 200 <=
                                    # len(content) < 8000. Instead of
                                    # sending a separate file message (R2)
                                    # or trying spoiler hacks (R1), attach
                                    # a *button* directly to the rolling-
                                    # log embed. Clicking the button sends
                                    # an ephemeral message with the .txt
                                    # file attachment — only the clicker
                                    # sees it, zero channel real-estate
                                    # when nobody clicks. Discord per-view
                                    # cap is 25 buttons / 5 rows; that
                                    # bounds tool calls per turn but is
                                    # rarely hit (typical turn < 10 tool
                                    # calls).
                                    is_medium = (
                                        not is_short
                                        and not is_err
                                        and 200 <= len(content_str) < 8000
                                        and content_str.strip() != ""
                                    )
                                    # #181 xlong tier: ``len >= 8000``. Button-
                                    # tier rate-limit + 25-component cap make
                                    # the medium-tier ephemeral button approach
                                    # impractical at this size. Send the result
                                    # as a standalone .txt file attachment
                                    # message immediately, and surface a summary
                                    # line on the rolling log. Errors stay
                                    # capped at 100 chars regardless of size.
                                    is_xlong = (
                                        not is_short
                                        and not is_err
                                        and len(content_str) >= 8000
                                        and content_str.strip() != ""
                                    )
                                    if is_err:
                                        error_text = content_str[:100] if content_str else "Failed"
                                        tool_log_lines[i] = f"{status} {name}: {error_text}"
                                    elif is_short:
                                        # Strip backticks to avoid breaking the
                                        # rolling-log embed's markdown. Collapse
                                        # newlines to ``│`` so multi-line short
                                        # outputs fit on one log line.
                                        safe = (
                                            content_str.strip()
                                            .replace("`", "'")
                                            .replace("\n", " │ ")
                                        )
                                        tool_log_lines[i] = f"{status} {name} → {safe}"
                                    elif is_medium:
                                        # Rolling log shows summary + the
                                        # same ``#N`` index that the button
                                        # below uses, so user can map
                                        # ``✅ Bash #3`` log line to
                                        # ``[📄 #3 Bash]`` button without guessing.
                                        # #187: index = len(medium_results)+1
                                        # (medium_results not yet appended;
                                        # see below). Use tool_id ordering
                                        # so re-entries (duplicate event) keep
                                        # the same number.
                                        line_count = content_str.count("\n") + 1
                                        if tool_id in medium_results:
                                            # idempotent re-render
                                            medium_index = list(medium_results.keys()).index(tool_id) + 1
                                        else:
                                            medium_index = len(medium_results) + 1
                                        # #187 invariant: must match ToolResultsView.add_result numbering at ~line 2918.
                                        # #207: bold `**#N name**` prefix so the
                                        # index pops out for visual scan-match
                                        # with the button label. Also flip
                                        # ordering to `#N name` to match the
                                        # button label exactly (was `name #N`).
                                        tool_log_lines[i] = (
                                            f"{status} **#{medium_index} {name}**: "
                                            f"{line_count} lines / "
                                            f"{len(content_str)} chars (⬇ click to view)"
                                        )
                                    elif is_xlong:
                                        # #181: summary line on rolling log;
                                        # file attachment is sent as a
                                        # separate message AFTER the rolling-
                                        # log embed update (so order is
                                        # "✅ log update + 📄 file").
                                        line_count = content_str.count("\n") + 1
                                        tool_log_lines[i] = (
                                            f"{status} {name}: "
                                            f"{line_count} lines / "
                                            f"{len(content_str)} chars (see attached file)"
                                        )
                                    else:
                                        tool_log_lines[i] = f"{status} {name}"
                                    # #226 R1 simplicity: hoist `matched =
                                    # True` out of the 4 branches —
                                    # duplicating the flag across branches
                                    # re-creates the bug class being fixed
                                    # ("branch forgot to set flag").
                                    matched = True
                                    break
                                if not matched and tool_id not in tool_msgs:
                                    # #226: rolling-log line for this tool
                                    # was evicted (kept only last 15) or
                                    # the event arrived out of order.
                                    # Logged for #223 / #224 diagnostic
                                    # epic; downstream guards on `is_medium
                                    # = False` make this a safe no-op.
                                    # #audit(live-log): gate on `tool_id not in
                                    # tool_msgs`. Tools rendered as their OWN
                                    # standalone embed (TodoWrite / WebSearch /
                                    # WebFetch / Grep / Glob / …, tracked in
                                    # tool_msgs) never append a 🔄 rolling-log
                                    # line by design, so their result no-matching
                                    # here is EXPECTED, not a diagnostic — this
                                    # was ~264/274 of these warnings (pure log
                                    # spam, no lost data). Only a GENUINE
                                    # eviction / out-of-order event still warns.
                                    log.warning(
                                        "ToolResultBlock no-matched rolling-log line; "
                                        "tool_id=%s name=%s alias=%r rolling_log_len=%d "
                                        "(likely rolling-log eviction or out-of-order event)",
                                        tool_id, name, alias, len(tool_log_lines),
                                    )
                                has_errors = any("❌" in l for l in tool_log_lines)
                                tool_embed = discord.Embed(
                                    title="🔧 Tool Activity",
                                    description="\n".join(tool_log_lines[-15:]),
                                    color=COLOR_TOOL_FAILURE if has_errors else COLOR_TOOL_SUCCESS,
                                )
                                # #161 medium tier (v1.18 R3): if this tool
                                # call hit medium tier, add it to the view
                                # before editing the rolling-log message,
                                # so the new button is part of the same
                                # edit (atomic).
                                if is_medium and not is_err and tool_id:
                                    medium_results[tool_id] = (name, content_str)
                                    if tool_results_view is None:
                                        # R2: timeout=None required by
                                        # ``bot.add_view`` for persistent
                                        # dispatch. Buttons carry stable
                                        # custom_ids derived from
                                        # tool_use_id so clicks route to
                                        # this view instance via the bot's
                                        # view store. author_id restricts
                                        # ephemeral followups to the turn's
                                        # original requester.
                                        tool_results_view = ToolResultsView(
                                            author_id=author_id,
                                            timeout=None,
                                        )
                                    # Idempotent: add_result skips if id
                                    # already present (defends against
                                    # duplicate ToolResultBlock events).
                                    tool_results_view.add_result(
                                        tool_use_id=tool_id,
                                        tool_name=name,
                                        content=content_str,
                                    )
                                # Edit the rolling-log message with the new
                                # embed AND attach (or refresh) the view.
                                edit_view_kwarg = tool_results_view if tool_results_view is not None else None
                                edit_ok = await self._safe_edit(
                                    tool_log_msg,
                                    embed=tool_embed,
                                    view=edit_view_kwarg,
                                )
                                # #161 R2 fix: register the view in the
                                # bot's persistent-view store so button
                                # clicks route to this View instance even
                                # after gc / bot restart. Without this,
                                # ``Message.edit(view=v)`` stores the view
                                # only weakly and clicks were getting
                                # dropped silently (user-reported). Idem-
                                # potent: discord.py de-dups by
                                # (custom_id, message_id).
                                if (
                                    edit_ok
                                    and tool_results_view is not None
                                    and self._bot is not None
                                    and tool_log_msg is not None
                                ):
                                    add_view_failed = False
                                    try:
                                        self._bot.add_view(
                                            tool_results_view,
                                            message_id=tool_log_msg.id,
                                        )
                                    except Exception:
                                        log.exception(
                                            "Failed to register ToolResultsView "
                                            "for persistence"
                                        )
                                        add_view_failed = True
                                    # R1 engineer: if add_view raised, the
                                    # rolling-log line still promises
                                    # ``click to view`` but Discord won't
                                    # route the click anywhere. Downgrade
                                    # the line + refresh embed so the user
                                    # isn't lied to.
                                    if add_view_failed and is_medium and tool_id:
                                        for j in range(len(tool_log_lines) - 1, -1, -1):
                                            line = tool_log_lines[j]
                                            # #187/#207: format now `{status} **#N {name}**:`; match on the
                                            # click-to-view marker instead of name-startswith.
                                            if f"#{medium_index} {name}" in line and "click to view" in line:
                                                tool_log_lines[j] = (
                                                    f"{status} **#{medium_index} {name}**: "
                                                    f"{len(content_str)} chars "
                                                    f"(view registration failed)"
                                                )
                                                break
                                        refresh_embed = discord.Embed(
                                            title="🔧 Tool Activity",
                                            description="\n".join(tool_log_lines[-15:]),
                                            color=COLOR_TOOL_SUCCESS,
                                        )
                                        await self._safe_edit(
                                            tool_log_msg, embed=refresh_embed
                                        )
                                if not edit_ok and is_medium and not is_err and tool_id:
                                    # Edit failed permanently (rate-limit /
                                    # message deleted). Downgrade the
                                    # rolling log line so the user doesn't
                                    # see a click-to-view promise that
                                    # can't be honored.
                                    for j in range(len(tool_log_lines) - 1, -1, -1):
                                        # #187/#207: match new `**#N {name}**:` format on the marker
                                        if f"#{medium_index} {name}" in tool_log_lines[j] and "click to view" in tool_log_lines[j]:
                                            tool_log_lines[j] = f"{status} **#{medium_index} {name}** ({len(content_str)} chars; view button unavailable)"
                                            break
                                # #181 xlong tier: send the .txt attachment
                                # as a follow-up message AFTER the rolling-
                                # log embed is updated. The summary line
                                # already promises "(see attached file)";
                                # honor that promise here.
                                if is_xlong and not is_err and tool_id:
                                    line_count = content_str.count("\n") + 1
                                    safe_name = name.lower().replace(" ", "_")
                                    file_bytes = content_str.encode(
                                        "utf-8", errors="replace"
                                    )
                                    await self._safe_send(
                                        content=(
                                            f"📄 **{name} result** — "
                                            f"{len(content_str)} chars / "
                                            f"{line_count} lines"
                                        ),
                                        file=discord.File(
                                            fp=io.BytesIO(file_bytes),
                                            filename=f"{safe_name}_result.txt",
                                        ),
                                    )
                            elif tool_id and tool_id in tool_msgs:
                                orig_msg = tool_msgs[tool_id]
                                if is_err:
                                    result_embed = discord.Embed(
                                        title=f"❌ {name}",
                                        color=COLOR_TOOL_FAILURE,
                                    )
                                    error_text = _extract_block_content_text(block.content)[:500] or "Failed"
                                    result_embed.description = f"```\n{error_text}\n```"
                                else:
                                    # #281: preserve URL/query from the launch
                                    # embed so the completion embed still tells
                                    # the user *what* was fetched/searched
                                    # (otherwise 5 ``✅ WebFetch`` rows are
                                    # indistinguishable). WebFetch keeps the
                                    # URL in description; WebSearch encodes
                                    # the query in the title (``🔍 Searching:
                                    # {query}``), so we lift it into the
                                    # completion title as ``✅ WebSearch:
                                    # {query}``.
                                    completion_title = f"✅ {name}"
                                    preserved_desc = None
                                    if name in ("WebFetch", "WebSearch"):
                                        try:
                                            orig_embed = orig_msg.embeds[0]
                                            orig_title = orig_embed.title or ""
                                            orig_desc = orig_embed.description
                                        except (AttributeError, IndexError):
                                            orig_title = ""
                                            orig_desc = None
                                        if name == "WebFetch":
                                            preserved_desc = orig_desc
                                        else:  # WebSearch
                                            prefix = _WEBSEARCH_TITLE_PREFIX
                                            if orig_title.startswith(prefix):
                                                query = orig_title[len(prefix):]
                                                completion_title = f"✅ WebSearch: {query}"
                                    result_embed = discord.Embed(
                                        title=completion_title,
                                        color=COLOR_TOOL_SUCCESS,
                                    )
                                    if preserved_desc:
                                        result_embed.description = preserved_desc
                                await self._safe_edit(orig_msg, embed=result_embed)
                    # review C1: reset the per-message streaming flag at the
                    # AssistantMessage boundary. Deltas for the NEXT assistant
                    # message arrive AFTER this event, so clearing here (not at
                    # the top of the branch) keeps the flag scoped to exactly
                    # one assistant message. Gated on AssistantMessage because
                    # UserMessage shares this content-list branch (it carries
                    # tool results) and must not clear a flag set by the
                    # assistant text that preceded it.
                    if isinstance(event, AssistantMessage):
                        stream_text_seen = False
        except Exception as exc:
            if is_transient_discord_error(exc):
                # Discord blip — bridge is fine, just couldn't render. The
                # individual edit/send wrappers already do their own
                # ``_retry_http`` so reaching here means the blip outlasted
                # one operation's retry budget. Don't tear down the bridge;
                # caller will keep streaming, eventually an edit will
                # succeed and the buffer catches up.
                log.warning(
                    "Discord-transient error during render; bridge stays alive",
                    extra={
                        "exc_class": type(exc).__name__,
                        "thread_id": getattr(self.target, "id", None),
                    },
                )
                # #147 R2 (C2): best-effort flush of whatever is buffered so
                # the user doesn't lose tail text on a blip. ``_flush`` wraps
                # Discord ops in their own ``_safe_*`` retry budget, so a
                # continuing blip won't crash this recovery path. Cost
                # footer is intentionally skipped on transient: the next
                # user turn naturally won't try to footer this dead turn,
                # and attempting it here doubles the failure surface.
                try:
                    await self._flush(live_msg, buffer, typewriter, saw_text, tool_msgs)
                except Exception:  # noqa: BLE001
                    log.debug("transient-recovery flush also failed; deferring", exc_info=True)
                # review C3: the blip outlasted our retry budget, so the tail of
                # the response (and the cost footer) may not have landed — yet
                # render_response returns normally, so the caller marks the turn
                # ✅. Post a best-effort notice so the user isn't left with a
                # misleading clean checkmark. Guarded: if the blip is ongoing
                # this send just no-ops (it has its own retry budget).
                try:
                    await self._safe_send(embed=discord.Embed(
                        description=(
                            "⚠️ A Discord connectivity blip interrupted delivery — "
                            "part of the response above may be missing. "
                            "Resend your message if it looks truncated."
                        ),
                        color=COLOR_TOOL_FAILURE,
                    ))
                except Exception:  # noqa: BLE001
                    log.debug("C3 partial-delivery notice failed", exc_info=True)
                # Don't re-raise — caller (_render_with_retry) won't stop_session.
                return
            if "buffer size" in str(exc) or "exceeded maximum buffer" in str(exc):
                # #audit(live-log): a single SDK stdout JSON frame exceeded
                # max_buffer_size. The bridge is already torn down by
                # send_message's except path, so log this distinctly (NOT a
                # generic "fatal") for diagnosability, then fall through to the
                # normal retry flow. Raising CLAUDED_MAX_BUFFER_SIZE (now 10MB)
                # prevents the vast majority of these.
                log.warning(
                    "Renderer: SDK buffer overflow on a single JSON frame (%s); "
                    "turn interrupted — bridge recreates on retry",
                    type(exc).__name__,
                )
            else:
                log.exception("Renderer fatal error; tearing down bridge")
            # Best-effort: clean up the live cursor. Don't try to surface a
            # plain-text error here — callers (bot.py) wrap render_response
            # in the crash-notification flow which posts a richer embed
            # with a retry button. Re-raise so they can do so.
            if live_msg is not None:
                await self._safe_edit(live_msg, content=buffer[:DISCORD_MAX_LEN] or "…")
            raise

        # review A1: bounded post-ResultMessage drain of trailing workflow
        # Task* terminals. ``bridge.send_message`` streams from
        # ``client.receive_response()``, which RETURNS at the first
        # ResultMessage — so a workflow subtask whose terminal
        # TaskNotification/TaskUpdated arrives just AFTER the main result never
        # gets finalized by the main loop (its ``⚡ Running`` embed would stay
        # forever + the ``_task_states`` entry would leak into the next turn).
        # ``receive_pending()`` opens a SECOND iterator on the SDK's shared
        # receive stream and drains whatever landed after
        # ``receive_response()`` stopped. We ONLY enter this when tasks are
        # still pending, so normal turns pay zero added latency and
        # ``receive_pending`` is never even consumed. We stop as soon as the
        # pending set empties (fast path) and hard-cap the total wall-clock at
        # ``DRAIN_TOTAL_SECONDS`` so a subtask that never terminates can't hang
        # the turn. Any drain error is swallowed inside ``receive_pending``
        # (the turn is already complete). Task embeds are their own Discord
        # messages, so finalizing them here — before the text flush + footer
        # below — does not disturb the assistant-text ordering.
        if result_received and self._task_states:
            drain_deadline = time.monotonic() + DRAIN_TOTAL_SECONDS
            async for event in bridge.receive_pending():
                await self._dispatch_task_event(event, subagent_renderers)
                if not self._task_states:
                    break
                if time.monotonic() > drain_deadline:
                    log.warning(
                        "review A1: drain timeout, %d task(s) still pending",
                        len(self._task_states),
                    )
                    break

        # Stream finished cleanly. Flush whatever is left.
        await self._flush(live_msg, buffer, typewriter, saw_text, tool_msgs)

        # Append cost/stats footer to the last sent message.
        # #160 Fix B: split the prior 3-way AND so each condition gets a sane
        # fallback. Old code dropped the entire footer when any of
        # _last_msg/stats/cost was missing AND swallowed every Discord error;
        # users observed "好多消息都没显示尾巴". Now:
        #   - stats missing → log.warning, skip (no data to render)
        #   - cost == 0 with tokens/duration available → render anyway
        #   - _last_msg missing → send footer as a separate thread message
        #   - edit/send fails → log.warning with reason (no silent swallow)
        if not stats:
            log.warning("Footer skipped: no stats (stream ended without ResultMessage)")
        else:
            try:
                duration_s = stats.get("duration_ms", 0) / 1000
                cost = stats.get("cost", 0.0)
                footer_text = (
                    f"-# 💰 ${cost:.4f}"
                    f" │ 📥 {_fmt_tokens(stats.get('input_tokens', 0))}"
                    f" │ 📤 {_fmt_tokens(stats.get('output_tokens', 0))}"
                    f" │ ⏱️ {duration_s:.1f}s"
                )
                # #182: append `│ 🧠 N%` context-window segment when
                # available. Graceful-omit when not (helper returns None).
                ctx_seg = _format_context_segment(stats)
                if ctx_seg:
                    footer_text += ctx_seg
                if _last_stop_reason and _last_stop_reason != "end_turn":
                    footer_text += f" │ ⚠️ {_last_stop_reason}"
                # #audit(#13): surface a ResultMessage error terminal so the
                # user learns WHY a turn ended abnormally instead of seeing "it
                # just stopped" (rate-limit, error_max_turns, API error, …).
                # Trigger ONLY on is_error or an ``error*`` subtype — a normal
                # turn's subtype ("result"/"success") must NOT show 🛑.
                _subtype = stats.get("subtype") or ""
                if stats.get("is_error") or _subtype.startswith("error"):
                    _err_seg = stats.get("api_error_status") or _subtype or "error"
                    footer_text += f" │ 🛑 {_err_seg}"

                # #211: append a second footer line showing the current
                # effective permission mode — but ONLY when it's not the
                # implicit ``"default"`` (PRD §Design "Footer"). Pulling
                # ``bridge.effective_permission_mode`` here collapses
                # override / env / default into one string we just compare.
                # MODE_EMOJI lives at module scope of this file (#211 R1
                # architect relocation — breaks the previous lazy import
                # cycle with cogs/mode.py).
                try:
                    effective_mode = getattr(
                        bridge, "effective_permission_mode", "default"
                    )
                    if effective_mode and effective_mode != "default":
                        mode_emoji = MODE_EMOJI.get(effective_mode, "")
                        # Discord ``-#`` small-text marker repeats per
                        # line: emoji on its own row keeps the mode
                        # visually separated from the cost stats above.
                        footer_text += f"\n-# {mode_emoji} {effective_mode}".rstrip()
                except Exception:  # pragma: no cover - never break the cost footer
                    log.debug("Mode footer append failed; continuing", exc_info=True)

                if self._last_msg is not None:
                    # Try to append to the last user-visible message.
                    current = self._last_msg_text.rstrip(CURSOR)
                    candidate = current + "\n\n" + footer_text
                    if len(candidate) <= DISCORD_MAX_LEN:
                        ok = await self._safe_edit(self._last_msg, content=candidate)
                        if not ok:
                            # Edit refused (4xx/5xx exhausted retry budget). Fall
                            # through to standalone-send so footer still reaches user.
                            log.warning(
                                "Footer edit failed; sending as standalone message"
                            )
                            await self._safe_send(content=footer_text)
                            # R1 engineer #4: standalone-send updates ``_last_msg``
                            # to the footer message itself, which would cause the
                            # NEXT turn (renderer instance is reused across turns
                            # per thread) to try editing/appending to the footer.
                            # Clear the shadow so the next turn re-attaches
                            # cleanly to a fresh cursor message.
                            self._last_msg = None
                            self._last_msg_text = ""
                    else:
                        # Edit would blow Discord's 2000-char hard cap; send footer
                        # standalone instead of silently dropping.
                        log.debug(
                            "Footer would exceed DISCORD_MAX_LEN; sending standalone"
                        )
                        await self._safe_send(content=footer_text)
                        self._last_msg = None  # R1 engineer #4 (see above)
                        self._last_msg_text = ""
                else:
                    # No live cursor message (e.g., the turn produced only an
                    # attachment / PNG-only path). Send footer as its own message
                    # so cost/duration still surface.
                    await self._safe_send(content=footer_text)
                    self._last_msg = None  # R1 engineer #4 (see above)
                    self._last_msg_text = ""
            except Exception:
                # #160 Fix B: never silently swallow. Log so future failures
                # are diagnosable from bot.log.
                log.warning(
                    "Footer render failed (continuing)",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    def _process_image_inlines(self, text: str) -> tuple[str, list[Path]]:
        """Strip markdown image refs to local files; queue paths for upload (#222).

        Returns ``(cleaned_text, planned_attachments)``. For each match:

        * Path under the allowlist + exists + readable + ≤ ``_IMG_MAX_BYTES``
          → strip from text, append Path to attachments list.
        * Otherwise → leave the original markdown in place + ``log.warning``
          the rejection reason.

        Does not perform IO except ``Path.resolve(strict=True)`` and a
        ``stat()`` for size; the actual file open happens inside
        ``discord.File`` on send.
        """
        attachments: list[Path] = []
        offset = 0
        out_parts: list[str] = []
        for m in _IMG_PATTERN.finditer(text):
            raw = m.group(2)
            # Always emit everything before this match unchanged.
            out_parts.append(text[offset:m.start()])
            offset = m.end()

            path = Path(raw)
            # Reject http(s) URLs (Discord previews those natively) — the
            # regex doesn't allow whitespace/colons in path so http:// fails
            # match anyway, but defensively: if path looks like URL keep it.
            if not path.is_absolute() and self._project_path is not None:
                # Resolve relative to bound project so claude can write
                # ``![x](./build/foo.png)``.
                path = (self._project_path / path)

            if not _is_path_allowed(path, self._project_path):
                log.warning(
                    "#222: rejected image attachment %r; not under allowlist or missing",
                    raw,
                )
                out_parts.append(m.group(0))  # keep original markdown
                continue

            try:
                size = path.stat().st_size
            except OSError as exc:
                log.warning(
                    "#222: rejected image attachment %r; stat() failed: %s",
                    raw, exc,
                )
                out_parts.append(m.group(0))
                continue
            if size > _IMG_MAX_BYTES:
                log.warning(
                    "#222: rejected image attachment %r; size %d > cap %d",
                    raw, size, _IMG_MAX_BYTES,
                )
                out_parts.append(m.group(0))
                continue

            attachments.append(path)

        out_parts.append(text[offset:])
        return ("".join(out_parts), attachments)

    async def _send_text_with_attachments(
        self,
        text: str,
        attachments: list[Path],
    ) -> None:
        """Send ``text`` then batch ``attachments`` 10-per-message (#222).

        First message carries the text + first up-to-10 files; subsequent
        messages carry the remaining files in batches. Mirrors the
        ``_send_text_and_tables`` interleave contract: empty text is
        OK (attachment-only); empty attachments degrades to plain text
        via ``_safe_send``.
        """
        first_batch = attachments[:_IMG_MAX_PER_MESSAGE]
        rest = attachments[_IMG_MAX_PER_MESSAGE:]

        files = [discord.File(p) for p in first_batch]
        text_or_none = text if text.strip() else None
        if text_or_none is None and not files:
            return

        # Smart-split text if oversized, send only first chunk with files.
        if text_or_none is not None and len(text_or_none) > DISCORD_MAX_LEN:
            chunks = self._smart_split(text_or_none, limit=DISCORD_MAX_LEN) or [
                text_or_none[:DISCORD_MAX_LEN]
            ]
            sent = await self._safe_send(content=chunks[0], files=files or None)
            if sent is not None:
                self._last_msg = sent
                self._last_msg_text = chunks[0]
            for chunk in chunks[1:]:
                sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
                    self._last_msg_text = chunk
        else:
            sent = await self._safe_send(
                content=text_or_none, files=files or None
            )
            if sent is not None:
                self._last_msg = sent
                self._last_msg_text = text_or_none or ""

        # Drain remaining files in 10-batches with no text.
        while rest:
            batch = rest[:_IMG_MAX_PER_MESSAGE]
            rest = rest[_IMG_MAX_PER_MESSAGE:]
            files = [discord.File(p) for p in batch]
            sent = await self._safe_send(files=files)
            if sent is not None:
                self._last_msg = sent
                self._last_msg_text = ""

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
                # #223 PR-B: previously the error string-only went into the
                # user message — if the user didn't paste a screenshot, the
                # exception was 100% lost. Now we record exc_info too.
                log.warning(
                    "Failed to create thread %r in channel=%s: %s",
                    thread_name,
                    getattr(channel, "id", "?"),
                    e,
                    exc_info=True,
                )
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
                log.warning(
                    "Failed to create text channel %r in guild=%s: %s",
                    channel_name,
                    getattr(guild, "id", "?"),
                    e,
                    exc_info=True,
                )
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

    @staticmethod
    def _find_table_boundary(buffer: str) -> int:
        """Return byte offset where a trailing table block starts, or -1.

        A "trailing table" is a contiguous run of lines at the end of
        *buffer* where each non-empty line starts with ``|``.  We require
        at least 2 such lines (header + separator or data row) to consider
        it a table in progress.
        """
        lines = buffer.split("\n")
        # Walk backwards finding consecutive table lines.
        table_line_count = 0
        first_table_line_idx = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("|"):
                table_line_count += 1
                first_table_line_idx = i
            elif stripped == "":
                # Allow trailing blank lines inside table block
                if table_line_count > 0:
                    first_table_line_idx = i
                    continue
                break
            else:
                break
        if table_line_count < 2:
            return -1
        # Compute byte offset of line at first_table_line_idx.
        offset = sum(len(lines[j]) + 1 for j in range(first_table_line_idx))
        return offset

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

        # #308: check if buffer tail has an in-progress table
        table_start = self._find_table_boundary(buffer)
        if table_start > 0:
            # Split at table boundary — send pre-table, keep table in buffer
            pre_table = buffer[:table_start]
            if pre_table.strip():
                chunks = self._smart_split(pre_table, limit=soft_limit)
                for chunk in chunks[:-1]:
                    await self._safe_send(content=chunk)
                live_msg = await self._typewriter_apply(live_msg, chunks[-1])
            return live_msg, buffer[table_start:]

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
        self,
        live_msg: discord.Message,
        buffer: str,
        *,
        is_final: bool = True,
    ) -> None:
        """Replace the cursor in ``live_msg`` and emit any overflow as new messages.

        Pre-tool-use interleave (``is_final=False``): raw dump without
        extraction — caller resets ``buffer`` so any table content here
        renders as markdown, not PNG. Final flush (``is_final=True``):
        run v1.12 extract + PNG interleave. See #141.

        #222: BEFORE marker / table processing, lift any markdown image
        refs to local files into a queue of attachments. Attachments
        are sent at the END (after typewriter + tables), as one or more
        attachment-only follow-up messages (Discord cap 10/msg).

        #222 R1 simplicity: image-attachment drain happens in ONE place
        — a ``try/finally`` wrapper — so a future return-branch that
        forgets to drain can't silently drop attachments.
        """
        # #222: image inlining — strip text + queue attachments. Done
        # BEFORE marker processing because ``_THREAD_PATTERN`` doesn't
        # touch ``![...](...)`` and we want symmetry of order even if
        # claude's output mixes both. Done BEFORE table extraction so
        # ``![alt|foo](path)`` doesn't get misread as a table row.
        buffer, _image_attachments = self._process_image_inlines(buffer)
        try:
            await self._finalize_typewriter_body(live_msg, buffer, is_final=is_final)
        finally:
            if _image_attachments:
                await self._send_text_with_attachments("", _image_attachments)

    async def _finalize_typewriter_body(
        self,
        live_msg: discord.Message,
        buffer: str,
        *,
        is_final: bool,
    ) -> None:
        """#222: inner body of _finalize_typewriter; drain wrapper above
        owns the attachment-flush. This split lets us keep all the
        early ``return`` paths without scattering drain calls."""
        # E1: Process channel/thread markers before finalizing.
        buffer = await self._process_markers(buffer)

        if not is_final:
            if not buffer:
                return
            if len(buffer) <= DISCORD_MAX_LEN:
                chunks = [buffer]
            else:
                chunks = self._smart_split(buffer, limit=DISCORD_MAX_LEN) or [buffer[:DISCORD_MAX_LEN]]
            first = chunks[0]
            if live_msg is not None:
                await self._safe_edit(live_msg, content=first)
            else:
                await self._safe_send(content=first)
            for chunk in chunks[1:]:
                await self._safe_send(content=chunk)
            return

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
        # at the very start of the buffer), clear the cursor to a U+200B
        # placeholder via ``_clear_cursor_msg`` so the cursor is visibly
        # gone before the PNG arrives (Discord rejects ``content=""`` with
        # HTTP 50006; #142 §A3).
        first_seg, _, rest_text = stripped_text.partition(table_renders[0].placeholder)
        if first_seg.strip():
            first_chunks = self._smart_split(first_seg, limit=DISCORD_MAX_LEN) or [first_seg[:DISCORD_MAX_LEN]]
            first = await self._typewriter_apply(live_msg, first_chunks[0])
            if first is not None:
                self._last_msg = first
                self._last_msg_text = first_chunks[0]
            for chunk in first_chunks[1:]:
                sent = await self._safe_send(content=chunk)
                if sent is not None:
                    self._last_msg = sent
                    self._last_msg_text = chunk
        else:
            # Empty first segment — keep the existing cursor msg identity
            # but replace its content with ZWS. If the edit fails the
            # cursor stays with its previous (raw-markdown) content; we
            # accept that degraded outcome rather than send a fresh empty
            # message (which would itself fail with 50006).
            if live_msg is not None:
                ok = await self._clear_cursor_msg(live_msg)
                if ok:
                    self._last_msg = live_msg
                    self._last_msg_text = "\u200b"

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

        # review C2: a whitespace-only buffer is not real content. Sending it
        # hits Discord 50006 (cannot send an empty message), which logs a
        # spurious ERROR-level "DROPPED" line, while the "(no text response)"
        # placeholder below stays suppressed because ``saw_text`` is True (the
        # whitespace arrived as a text delta). Blank both so we fall through to
        # the placeholder and give the user a clean acknowledgement instead of
        # silence + a false content-loss error.
        if buffer and not buffer.strip():
            buffer = ""
            saw_text = False

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
                # #205 fix: summary used to be ``upload_buffer[:200]``,
                # which would include the raw markdown table if the
                # table happened to be near the start — producing the
                # "table renders twice (markdown + PNG)" prod bug.
                # Replace with a plain caption so the inline message
                # carries no table content; PNG follow-up provides the
                # visual, .md attachment provides the full text.
                line_count = upload_buffer.count("\n") + 1
                char_count = len(upload_buffer)
                summary = (
                    f"📎 **Long response** — {line_count} lines / "
                    f"{char_count} chars uploaded (see attached .md)."
                )
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
        reaction_target: discord.Message | None = None,
        bot_user: discord.abc.User | None = None,
    ) -> discord.Message | None:
        """Run ``op()`` with retry/backoff.

        Returns op's result on success; ``None`` on permanent failure.
        ``label`` is "send" or "edit" — used in log messages.
        ``content_len`` is the number of characters that will be lost on
        permanent failure; if non-zero we emit an ``ERROR`` log on giveup.
        ``between_attempts`` is invoked just before each retry sleep
        (used by ``_safe_send`` to ``seek(0)`` a file stream between tries).
        ``reaction_target`` is the message on which a 🌐 reaction is added
        when cumulative retries exceed 10s, and removed on recovery (#147).
        ``bot_user`` is the bot member used to remove its own reaction.
        """
        attempt_start_time = time.monotonic()
        network_reaction_added = False

        async def _ensure_network_reaction() -> None:
            """Fire-and-forget: add 🌐 if 10s elapsed and not already added."""
            nonlocal network_reaction_added
            if network_reaction_added or reaction_target is None:
                return
            if time.monotonic() - attempt_start_time < 10.0:
                return
            network_reaction_added = True
            try:
                await reaction_target.add_reaction("🌐")
            except Exception:  # noqa: BLE001
                log.debug("🌐 add_reaction failed (best-effort)", exc_info=True)

        async def _clear_network_reaction() -> None:
            """Fire-and-forget: remove 🌐 on recovery."""
            nonlocal network_reaction_added
            if not network_reaction_added or reaction_target is None:
                return
            try:
                if bot_user is not None:
                    await reaction_target.remove_reaction("🌐", bot_user)
                else:
                    await reaction_target.clear_reaction("🌐")
            except Exception:  # noqa: BLE001
                log.debug("🌐 remove_reaction failed (best-effort)", exc_info=True)
            finally:
                network_reaction_added = False

        for attempt in range(MAX_HTTP_RETRIES + 1):
            try:
                result = await op()
                await _clear_network_reaction()
                return result
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
                await _ensure_network_reaction()
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
                    await _clear_network_reaction()
                    return None
                log.warning(
                    "Discord %s transient failure (attempt %d/%d, status=%s); "
                    "sleeping %.2fs",
                    label, attempt + 1, MAX_HTTP_RETRIES + 1, status,
                    _BACKOFF[attempt],
                )
                if between_attempts is not None:
                    between_attempts()
                await _ensure_network_reaction()
                await asyncio.sleep(_BACKOFF[attempt])
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # #147: Discord network blip (TCP/SSL handshake, server reset).
                # Treat as retriable like 5xx.
                if attempt == MAX_HTTP_RETRIES:
                    if content_len:
                        verb = "DROPPED" if label == "send" else "UNDELIVERED"
                        log.error(
                            "Discord %s network-failed after %d attempts (%s); "
                            "%s %d chars",
                            label, attempt + 1, type(exc).__name__, verb, content_len,
                            exc_info=True,
                        )
                    else:
                        log.warning(
                            "Discord %s network-failed after %d attempts (%s)",
                            label, attempt + 1, type(exc).__name__, exc_info=True,
                        )
                    await _clear_network_reaction()
                    return None
                log.warning(
                    "Discord %s network blip (attempt %d/%d, %s); sleeping %.2fs",
                    label, attempt + 1, MAX_HTTP_RETRIES + 1,
                    type(exc).__name__, _BACKOFF[attempt],
                )
                if between_attempts is not None:
                    between_attempts()
                await _ensure_network_reaction()
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
                except Exception as exc:
                    # #223 PR-B: silent seek failure caused attachments to
                    # send as empty bytes on retry — truncated files in
                    # production with no log line.
                    log.warning(
                        "file.fp.seek(0) failed before retry; "
                        "attachment may be empty: %s",
                        exc,
                    )
            fs = kwargs.get("files")
            if fs:
                for entry in fs:
                    if hasattr(entry, "fp") and hasattr(entry.fp, "seek"):
                        try:
                            entry.fp.seek(0)
                        except Exception as exc:
                            log.warning(
                                "files[i].fp.seek(0) failed before retry; "
                                "attachment may be empty: %s",
                                exc,
                            )

        async def _op() -> discord.Message | None:
            return await self.target.send(**kwargs)

        # #147 R2 (C1): send has no natural "user is watching this" message
        # to react on (the new message doesn't exist until ``_op`` returns).
        # Fall back to ``self._last_msg`` — the most recent typewriter cursor
        # in the same channel — so the user still gets a 🌐 signal when a
        # blip stalls the *next* send. When ``_last_msg`` is None (very first
        # send of a fresh renderer), ``_retry_http`` short-circuits the
        # reaction lifecycle silently — the long retry budget still applies.
        prior = self._last_msg
        guild = getattr(prior, "guild", None) if prior is not None else None
        bot_user = getattr(guild, "me", None) if guild is not None else None
        msg = await self._retry_http(
            _op,
            label="send",
            content_len=len(content or ""),
            between_attempts=(
                _reset_file if ("file" in kwargs or "files" in kwargs) else None
            ),
            reaction_target=prior,
            bot_user=bot_user,
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

        v1.16 (#142 §A3) — pure transport. The empty-content → U+200B
        substitution previously inlined here was a single-caller concern
        (cursor-clear from ``_finalize_typewriter``); callers that need
        to "clear" a message with Discord-acceptable content must call
        :meth:`_clear_cursor_msg` instead. Passing ``content=""`` or
        whitespace-only content to this method will now hit Discord
        50006 ("Cannot send an empty message") as the underlying API
        always intended.
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

        # #147 R2 (C1): wire 🌐 reaction into the production edit path.
        # The reaction is attached to ``msg`` itself — the live cursor /
        # typewriter message the user is watching. ``msg.guild.me`` is the
        # bot's Member object in the guild; thread/channel ``Messageable``
        # without a guild (DM) is rare but handled by ``bot_user=None``,
        # which causes ``_retry_http`` to fall back to ``clear_reaction``.
        guild = getattr(msg, "guild", None)
        bot_user = getattr(guild, "me", None) if guild is not None else None
        result = await self._retry_http(
            _op,
            label="edit",
            content_len=len(content) if content else 0,
            reaction_target=msg,
            bot_user=bot_user,
        )
        # Sync shadow on success — see #113 / docs/investigations/stale-message-content.md.
        if result is not None and content is not None and msg is self._last_msg:
            self._last_msg_text = content
        return result is not None

    async def _clear_cursor_msg(self, msg: discord.Message) -> bool:
        """Edit ``msg`` to U+200B so it renders as invisible whitespace.

        Used when the cursor message is now logically empty — typically
        the v1.12 streaming-cursor cleanup path in ``_finalize_typewriter``
        where the entire buffer was a table placeholder and the
        pre-table segment is empty. Discord rejects ``content=""`` and
        any all-whitespace string with HTTP 400 code 50006
        ("Cannot send an empty message"); U+200B (zero-width space) is
        accepted as non-empty content and renders invisibly so the
        user sees the cursor as cleared rather than retaining the raw
        markdown text alongside the PNG follow-up (Bug C symptom in
        the v1.12 smoke run).

        v1.16 (#142 §A3) — extracted from ``_safe_edit`` so that
        method stays pure transport. ``_safe_edit`` no longer performs
        the empty → ZWS substitution; callers that need cursor-clear
        semantics must call this helper explicitly.
        """
        return await self._safe_edit(msg, content="\u200b")

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
        # v1.16 (#142 §A2) — share the CommonMark §4.5 fence tracker with
        # the production extractor. The pre-refactor bool toggle here was
        # latent-wrong for quad-backtick outer fences (an inner triple
        # toggled it off prematurely) — never reached in practice because
        # this method was the legacy-only path, but the Pillow-missing
        # fallback (#135 / PRD R6) now routes real traffic through it.
        fence_count = 0
        table_lines: list[str] = []

        for line in lines_in:
            stripped = line.strip()

            prev_fence = fence_count
            fence_count = _advance_fence_state(line, fence_count)
            if fence_count > 0 or prev_fence > 0:
                # Either we just opened a fence, are inside one, or just
                # closed one — emit verbatim and never table-parse the line.
                # On a fence-open line we also flush any pending table.
                if prev_fence == 0 and fence_count > 0 and in_table and table_lines:
                    result.append("```")
                    result.extend(table_lines)
                    result.append("```")
                    in_table = False
                    table_lines = []
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
        # v1.12 bug D — fence depth tracker per CommonMark §4.5. A fence
        # opens with N≥3 consecutive backticks and only closes on a line
        # whose backtick run length is ≥N. Tracking just a bool with
        # ``startswith("```")`` mis-parsed quad-backtick outer fences:
        # the inner ``\u0060\u0060\u0060`` line was treated as a fence
        # toggle and the markdown table inside leaked back out to PNG
        # extraction, violating PRD R5 (code fences preserved verbatim).
        # v1.16 (#142 §A2) — the state-machine is now the module-level
        # ``_advance_fence_state`` helper, shared with ``_format_tables``.
        fence_count = 0  # 0 ⇒ not in a fence; otherwise the opener's backtick run
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

            # Track code fences first — anything inside (or on the fence
            # boundary line itself) is opaque to table extraction. The
            # state machine lives in ``_advance_fence_state``; here we
            # only decide whether the current line is fence-owned.
            prev_fence = fence_count
            fence_count = _advance_fence_state(line, fence_count)
            if fence_count > 0 or prev_fence > 0:
                # Either we are inside a fence, just opened one, or just
                # closed one (the closer line itself belongs to the fence).
                result.append(line)
                i += 1
                continue

            if not _is_table_line(stripped):
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

        Markdown-aware (#274): prefer split points that do not cut through
        code fences, blockquote (``> ``) runs, or table (``| ``) runs. If
        no such block-aware boundary exists in range (e.g. a single block
        exceeds the limit), fall back to: paragraph (``\\n\\n``) → line
        (``\\n``) → space → hard cut. Code fences are still closed at the
        cut and re-opened in the next chunk so every chunk is self-
        contained even when the oversized-block fallback fires.
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

            # 0) Block-aware boundary (#274): prefer a line break that is
            #    NOT inside a ``` fence and NOT between two consecutive
            #    blockquote (``> ``) or table (``| ``) lines. Falls back
            #    to the older paragraph/line/space heuristic when no such
            #    boundary exists in [half, cut] (i.e. the current block
            #    is itself oversized — handled by the close/reopen logic
            #    below and ultimately by the long-reply .md fallback).
            idx = DiscordRenderer._find_block_aware_split(
                remaining, half, cut
            )
            if idx < 0:
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
    def _find_block_aware_split(text: str, lo: int, hi: int) -> int:
        """Find a line-boundary split position in ``[lo, hi]`` that does
        not cut through a markdown code fence, blockquote run, or table
        run (#274).

        Walks *text* line by line, tracking whether we're currently inside
        a ``\u0060\u0060\u0060`` fence. For each line that has a trailing
        ``\\n``, the position **after** that newline is a candidate split
        point iff:

        * we are not currently inside a fence, AND
        * we are not between two consecutive blockquote lines (both
          ``> ...``), AND
        * we are not between two consecutive table lines (both
          ``| ...``).

        Returns the **latest** such candidate position in ``[lo, hi]``,
        or ``-1`` if none exists (caller then falls back to the legacy
        paragraph/line/space heuristic).
        """
        if hi <= 0 or lo >= len(text):
            return -1
        if lo < 0:
            lo = 0
        lines = text.split("\n")
        pos = 0
        in_fence = False
        best = -1
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            # A line that *opens* or *closes* a fence toggles the state
            # before we evaluate the candidate at its trailing newline,
            # so the candidate sits *outside* the fence in both cases.
            if stripped.startswith("```"):
                in_fence = not in_fence
            line_end = pos + len(line)
            # All but the last entry of split("\n") are followed by a \n.
            if i < len(lines) - 1:
                after_nl = line_end + 1
                if after_nl > hi:
                    break
                if after_nl >= lo and not in_fence:
                    next_stripped = lines[i + 1].lstrip()
                    # Block-pair guards: keep consecutive blockquote and
                    # table rows together.
                    blockquote_pair = (
                        stripped.startswith(">")
                        and next_stripped.startswith(">")
                    )
                    table_pair = (
                        stripped.startswith("|")
                        and next_stripped.startswith("|")
                    )
                    if not blockquote_pair and not table_pair:
                        best = after_nl
                pos = after_nl
            else:
                pos = line_end
        return best

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
                "last message — your conversation will continue from where it "
                "crashed (#227).\n\n"
                f"Error: `{exc}`"
            )[:_RETRY_EMBED_DESC_MAX],
            color=COLOR_TOOL_FAILURE,
        )
        view = RetryView(on_retry=on_retry)
        # #147 R2 (C3 / PRD §Decision 6 closure): the original incident chain
        # was "Discord blip → render crash → retry-embed posted on the SAME
        # blip → embed post crashes too, no retry button visible". Route the
        # crash-with-retry embed through ``safe_send_message`` so the same
        # transient that caused the crash doesn't also eat the recovery UI.
        # ``safe_send_message`` returns None on giveup (logged at ERROR
        # inside ``safe_http``); the embed simply doesn't appear and the
        # user can re-send their message manually — strictly better than
        # the previous "single attempt, swallowed exception" behavior.
        sent = await safe_send_message(self.target, embed=embed, view=view)
        if sent is None:
            log.warning(
                "Crash-with-retry embed did not post (transient exhaustion)",
                extra={"target_id": getattr(self.target, "id", None)},
            )


# ---------------------------------------------------------------------------
# Retry view
# ---------------------------------------------------------------------------


_RETRY_EMBED_DESC_MAX = 4000
_RETRY_TIMEOUT_SECONDS = 600.0


class ToolResultsView(discord.ui.View):
    """Per-rolling-log persistent view holding one button per medium-tier
    tool result. Click → ephemeral followup with the result as a .txt
    file attachment, visible only to the clicker.

    Lifecycle: attached to the ``🔧 Tool Activity`` message via _safe_edit.
    Each ToolResultBlock that hits medium tier calls :meth:`add_result`
    which appends a new ``📄 #N {name}`` button. View edits are idempo-
    tent on (tool_use_id, ...) so duplicate events don't add dup buttons.

    Discord limits: max 25 components per view, 5 components per row.
    Each button uses 1 component slot → max 25 medium-tier results per
    turn before we stop accepting new ones (rare in practice).
    """

    # Cap so we don't try to over-pack the view
    _MAX_BUTTONS = 25
    # Discord ephemeral followup hard cap is 2000 chars (content); file
    # attachments are independent and capped at the bot's upload limit.
    # For medium tier (< 8000 chars) the file is always well under any
    # cap; we never hit content-length issues because the body lives in
    # the attachment.

    def __init__(self, *, author_id: int | None = None, timeout: float | None = None) -> None:
        # discord.py requires ``timeout=None`` for persistent views
        # (those registered via ``bot.add_view``). Per-instance items'
        # ``custom_id`` is the unique routing key; the view is dispatch-
        # ed from the bot's view store keyed on (custom_id, message_id).
        # Since the buttons in this view have stable custom_ids derived
        # from ``tool_use_id``, ``timeout=None`` is the correct choice.
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._results: dict[str, tuple[str, str]] = {}  # id -> (name, content)
        self._buttons: dict[str, discord.ui.Button] = {}  # id -> Button

    def add_result(self, *, tool_use_id: str, tool_name: str, content: str) -> bool:
        """Add a new result; return True iff a button was actually appen-
        ded. Idempotent on tool_use_id. Returns False once the 25-button
        cap is hit — caller should fall back to a different surface
        (e.g., the per-message file attachment path) for the overflow.
        """
        if tool_use_id in self._results:
            return False  # already added
        if len(self._results) >= self._MAX_BUTTONS:
            return False  # at cap
        self._results[tool_use_id] = (tool_name, content)
        index = len(self._results)
        # Custom_id must be unique per button across the bot's active
        # views; embed the tool_use_id so callbacks can dispatch.
        # #187 invariant: must match medium-tier rolling-log numbering at ~line 1417. If you change one, audit the other.
        button = discord.ui.Button(  # type: ignore[var-annotated]
            label=f"📄 #{index} {tool_name[:18]}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"clauded_toolresult_{tool_use_id[:64]}",
        )

        async def _callback(interaction: discord.Interaction) -> None:
            log.info(
                "ToolResultsView click: tool_use_id=%s user_id=%s",
                tool_use_id, interaction.user.id,
            )
            await self._dispatch(interaction, tool_use_id)

        button.callback = _callback  # type: ignore[assignment]
        self._buttons[tool_use_id] = button
        self.add_item(button)
        return True

    async def _dispatch(self, interaction: discord.Interaction, tool_use_id: str) -> None:
        """Send the .txt file as an ephemeral followup visible only to
        the user who clicked. If ``author_id`` was set at construction,
        non-author clicks get a polite refusal (so other channel members
        can't read another user's tool output)."""
        if self._author_id is not None and interaction.user.id != self._author_id:
            try:
                await interaction.response.send_message(
                    "🔒 This tool result is only viewable by the user who "
                    "started this turn.",
                    ephemeral=True,
                )
            except discord.HTTPException as exc:
                # #223 PR-B: auth-rejection ephemeral failed to post.
                # User sees nothing — record so we can correlate later.
                log.warning(
                    "ToolResultsView auth-rejection ephemeral failed: %s", exc,
                )
            return
        entry = self._results.get(tool_use_id)
        if entry is None:
            try:
                await interaction.response.send_message(
                    "⚠️ Result no longer available.", ephemeral=True
                )
            except discord.HTTPException as exc:
                log.warning(
                    "ToolResultsView 'no longer available' ephemeral failed: %s", exc,
                )
            return
        tool_name, content = entry
        file_bytes = content.encode("utf-8", errors="replace")
        file_name = f"{tool_name.lower()}_result.txt"
        try:
            await interaction.response.send_message(
                content=(
                    f"📄 **{tool_name} result** \u2014 "
                    f"{len(content)} chars / {content.count(chr(10)) + 1} lines"
                ),
                file=discord.File(fp=io.BytesIO(file_bytes), filename=file_name),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.warning("ToolResultsView dispatch failed: %s", exc)
            try:
                await interaction.response.send_message(
                    f"⚠️ Could not send result: {exc.text[:200] if hasattr(exc, 'text') else 'unknown'}",
                    ephemeral=True,
                )
            except discord.HTTPException as exc2:
                # #223 PR-B: failure-notify itself failed. User gets
                # nothing; log so we can correlate.
                log.warning(
                    "ToolResultsView failure-notify also failed: %s", exc2,
                )


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
            except discord.HTTPException as exc:
                # #223 PR-B: double-click defer failed. User sees nothing
                # extra; log so it's not lost.
                log.warning(
                    "RetryView double-click defer failed: %s", exc,
                )
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
    "ToolResultsView",
    "COLOR_CLAUDE",
    "COLOR_TOOL_RUNNING",
    "COLOR_TOOL_SUCCESS",
    "COLOR_TOOL_FAILURE",
    "COLOR_INFO",
    "COLOR_THINKING",
    "COLOR_WORKFLOW",
]
