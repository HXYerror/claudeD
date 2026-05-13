"""/context slash command — visualize Claude context-window usage.

PRD: #163 sub-task 3. Mirrors the bundled Claude CLI's `/context` semantics:
shows a colored grid / progress bar of current context-window usage so
users can self-manage long sessions and know when to `/session clear` or
`/session compact`.

Implementation follows the v1.13 ``/skill list`` two-path pattern:

1. **Path A** — piggyback on the channel's active bridge via
   ``bridge.get_context_usage()``. Fast (~tens of ms), uses the
   already-warm SDK client.
2. **Path B** — when no active session, spin up a temporary
   ``ClaudeSDKClient`` with ``setting_sources`` matching the channel's
   bound state. Slower (~2-4 s cold start), but lets users check
   baseline context budget for the model without first sending a
   message.

The CLI's ``/context`` returns a structured ``ContextUsageResponse``:

    {
      'categories': [{'name': str, 'tokens': int, 'color': str}, ...],
      'totalTokens': int,
      'maxTokens': int,
      'percentage': float,
      'model': str,
      'mcpTools': {...}, 'memoryFiles': {...}, 'agents': {...},
    }

We render the percentage + an ASCII progress bar + top-5 categories by
token count. Detailed breakdowns (MCP tools, memory files, per-agent)
are out of scope for v1; they'd bloat the embed. v1.19 can add
``/context --detail`` if useful.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
)

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ._unbound import NO_CHANNEL_MESSAGE, resolve_channel_id

log = logging.getLogger("clauded.bot")

# Defensive cap on Path B subprocess spawn + query. The cold-start of a
# transient ClaudeSDKClient can hang when the parent turn is hammering
# the host (heavy CPU/IO). Past this budget, surface a friendly error
# rather than letting Discord's interaction time out.
_PATH_B_TIMEOUT_S = 10


_PROGRESS_BAR_WIDTH = 20
_TOP_N_CATEGORIES = 5


def _format_progress_bar(percentage: float, width: int = _PROGRESS_BAR_WIDTH) -> str:
    """Render an ASCII progress bar for a 0-100 percentage.

    Uses block characters so the bar reads cleanly in Discord's monospace
    embed field. Caps at 100% even if SDK reports overflow.
    """
    pct = max(0.0, min(100.0, percentage))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def _format_tokens(n: int) -> str:
    """Format token count: 92531 -> 92.5k, 523 -> 523."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _build_context_embed(usage: dict, source_label: str) -> discord.Embed:
    """Render a ContextUsageResponse dict into a Discord embed.

    ``source_label`` indicates which path generated the data ("active session"
    vs "fresh session"). Surface this so users understand whether the
    numbers reflect their actual conversation state or just the model's
    baseline budget.
    """
    total = int(usage.get("totalTokens", 0))
    max_tokens = int(usage.get("maxTokens", 0))
    percentage = float(usage.get("percentage", 0))
    model = str(usage.get("model", "unknown"))

    bar = _format_progress_bar(percentage)
    pct_color = (
        COLOR_TOOL_FAILURE if percentage >= 90
        else 0xF59E0B if percentage >= 75  # yellow warn
        else COLOR_INFO
    )

    embed = discord.Embed(
        title=f"📊 Context: {percentage:.1f}%",
        description=(
            f"`{bar}` {_format_tokens(total)} / {_format_tokens(max_tokens)} tokens\n"
            f"-# Model: `{model}` · Source: {source_label}"
        ),
        color=pct_color,
    )

    # Top-N categories by token count (defensive on shape — empty list OK)
    categories = usage.get("categories") or []
    if categories:
        sorted_cats = sorted(
            categories,
            key=lambda c: int(c.get("tokens", 0)),
            reverse=True,
        )
        lines = []
        for cat in sorted_cats[:_TOP_N_CATEGORIES]:
            name = str(cat.get("name", "?"))
            tokens = int(cat.get("tokens", 0))
            cat_pct = (tokens / max_tokens * 100) if max_tokens else 0
            lines.append(f"• `{name}` — {_format_tokens(tokens)} ({cat_pct:.1f}%)")
        if len(sorted_cats) > _TOP_N_CATEGORIES:
            lines.append(f"-# … and {len(sorted_cats) - _TOP_N_CATEGORIES} more")
        embed.add_field(
            name="Top categories",
            value="\n".join(lines),
            inline=False,
        )

    return embed


@app_commands.command(
    name="context",
    description="Visualize Claude context-window usage (current session or model baseline).",
)
async def context_cmd(interaction: discord.Interaction) -> None:
    """Show context-window usage as a colored progress bar + top-5 categories.

    Path A (active session): uses the live bridge's `get_context_usage`.
    Path B (no active session): spins up a temp ClaudeSDKClient to query
    baseline budget for the bound (or fallback) cwd.

    Errors (CLI not installed, connection refused, malformed response):
    surface as a red embed instead of crashing the interaction.
    """
    from ..bot import ClaudedBot

    log.info("/context channel=%s", interaction.channel_id)
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("❌ Bot not ready.", ephemeral=True)
        return

    # Fix C — defer BEFORE any potentially slow operation (incl. id resolution,
    # binding/session lookups). If anything between here and the followup
    # raises, Discord still shows "thinking…" instead of "did not respond".
    await interaction.response.defer(ephemeral=True)

    # Fix A — split the two id meanings:
    #   binding_id: parent_id when in a thread → correct for project binding
    #               lookup (a thread inherits its parent's bound state).
    #   session_id: thread_id when in a thread → correct for session lookup
    #               (session_manager is keyed by thread id, not parent id).
    # In a bare channel both are channel_id, so behavior is unchanged.
    binding_id = resolve_channel_id(interaction)
    if binding_id is None:
        await interaction.followup.send(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    session_id = interaction.channel_id

    # Path A: active bridge piggyback (cheap, ~tens of ms).
    bridge = (
        bot.session_manager.get_session(session_id)
        if session_id is not None
        else None
    )
    usage = None
    source_label = "active session"
    if bridge is not None:
        try:
            usage = await bridge.get_context_usage()
        except Exception:  # noqa: BLE001 — fail-soft to Path B
            log.warning("/context Path A failed; falling back to temp client", exc_info=True)
            usage = None

    # Path B: temp client fallback (mirrors /skill list pattern).
    if usage is None:
        source_label = "fresh session (model baseline)"
        cwd, is_bound = bot.project_manager.get_path_or_default(binding_id)
        setting_sources = ["user", "project", "local"] if is_bound else ["user"]
        try:
            # Fix B — cap subprocess spawn + query at 10s. The cold-start
            # can hang when the parent turn is hammering the host. Without
            # this guard, Discord's interaction window would lapse.
            async with asyncio.timeout(_PATH_B_TIMEOUT_S):
                async with ClaudeSDKClient(
                    ClaudeAgentOptions(cwd=str(cwd), setting_sources=setting_sources)
                ) as tmp:
                    usage = await tmp.get_context_usage()
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("/context Path B timed out after %ss", _PATH_B_TIMEOUT_S)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Context unavailable",
                    description=(
                        "Couldn't query context window in time "
                        f"({_PATH_B_TIMEOUT_S}s timeout). "
                        "This usually happens when the bot is processing a large turn. "
                        "Try again once the active turn completes."
                    ),
                    color=COLOR_TOOL_FAILURE,
                ),
                ephemeral=True,
            )
            return
        except (CLINotFoundError, CLIConnectionError, Exception) as exc:  # noqa: BLE001
            log.warning("/context Path B failed", exc_info=True)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Context unavailable",
                    description=f"`{type(exc).__name__}`",
                    color=COLOR_TOOL_FAILURE,
                ),
                ephemeral=True,
            )
            return

    if usage is None:
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ Context unavailable",
                description="`NoUsage`: get_context_usage() returned None",
                color=COLOR_TOOL_FAILURE,
            ),
            ephemeral=True,
        )
        return

    embed = _build_context_embed(usage, source_label=source_label)
    await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["context_cmd"]
