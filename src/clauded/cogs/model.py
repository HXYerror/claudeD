"""Model / effort / max-turns / fallback / bare commands."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE, COLOR_TOOL_SUCCESS

from ._unbound import _reply

log = logging.getLogger("clauded.bot")


# #186: hybrid hardcoded table of known model aliases + metadata.
# Maintained here; reviewer is responsible for refreshing when Anthropic
# releases new SKUs. Order is intentional - user-facing list preserves
# this ordering (most-common balanced first, then deep, then fast,
# then context-window-extended variants).
# #247: refresh to current SKUs (claude-sonnet-4-6, claude-opus-4-7,
# claude-haiku-4-5, claude-sonnet-4-6-1m). Old table referenced
# claude-sonnet-4-5 / claude-opus-4-1 / claude-haiku-3-5 - a full
# generation behind what the SDK was returning on ResultMessage.
KNOWN_MODELS: dict[str, dict[str, str | int]] = {
    "sonnet":   {"id": "claude-sonnet-4-6",     "context": 200_000, "tier": "balanced"},
    "opus":     {"id": "claude-opus-4-7",       "context": 1_000_000, "tier": "deep"},
    "haiku":    {"id": "claude-haiku-4-5",      "context": 200_000, "tier": "fast"},
    "sonnet-1m":{"id": "claude-sonnet-4-6-1m",  "context": 1_000_000, "tier": "balanced"},
}


def _fmt_context(n: int) -> str:
    """`200000` -> `200k`; `1000000` -> `1M`. Defensive on non-ints."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n // 1_000_000}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _resolve_session_bridge(bot, channel):
    """Shared session lookup for /model list and /model current.

    Look up the session by ``channel.id``. Returns the bridge or ``None``.
    ``channel`` may be ``None`` (e.g. DM or cache miss).
    """
    if channel is None:
        return None
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    return bot.session_manager.get_session(channel_id)


def _current_model_for_thread(bot, channel) -> str | None:
    """Return the active model name for the session bound to ``channel``,
    else ``None``. Resolves through ``bridge.model`` which already follows
    the override > sdk-reported > config-default chain.

    #198: ``bridge.model`` may legitimately return ``None`` now (no
    override, no env, no first turn yet). Callers should treat ``None``
    as a valid signal rather than 'no session'.

    #247: now goes through :func:`_resolve_session_bridge` so this helper
    shares thread→parent fallback semantics with ``/model current``
    (eliminates the list/current inconsistency).
    """
    bridge = _resolve_session_bridge(bot, channel)
    if bridge is None:
        return None
    return getattr(bridge, "model", None)


def _model_source_for_bridge(bridge) -> tuple[str, str | None]:
    """Return (source, value) describing where the bridge's model came from.

    #198: ``/model current`` needs to distinguish the 4 tier cases so it
    can render an accurate description. We inspect tier fields directly
    rather than the collapsed ``bridge.model`` property.

    Returns one of:
    - ``("override", "<name>")``  - user ran ``/model switch``
    - ``("env", "<value>")``      - ``CLAUDE_MODEL`` env var was set
    - ``("sdk", "<value>")``      - SDK reported it on a ``ResultMessage``
    - ``("unset", None)``         - pre-first-turn, no override, no env
    """
    override = getattr(bridge, "_model_override", None)
    if override:
        return ("override", override)
    config = getattr(bridge, "_config", None)
    env_model = getattr(config, "claude_model", None) if config else None
    if env_model:
        return ("env", env_model)
    sdk_model = getattr(bridge, "_sdk_model", None)
    if sdk_model:
        return ("sdk", sdk_model)
    return ("unset", None)


model_group = app_commands.Group(
    name="model",
    description="View / switch Claude model for this thread",
)


@model_group.command(name="switch", description="Switch Claude model for this thread")
@app_commands.describe(name="Model: sonnet, opus, haiku, or full model ID")
async def model_switch(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    thread_id = getattr(interaction.channel, "id", None)
    bridge = bot.session_manager.get_session(thread_id) if thread_id is not None else None
    if bridge is not None and getattr(bridge, "is_active", False):
        await interaction.response.defer()
        try:
            await bridge.set_model(name)
        except Exception as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Model switch failed",
                    description=f"```\n{exc}\n```",
                    color=COLOR_TOOL_FAILURE,
                ),
            )
            return
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"🔄 Switched to `{name}`",
                description=(
                    "✅ Model switched. Context preserved.\n\n"
                    "-# ⏱️ **Per-session** — bot restart returns to CLI default."
                ),
                color=COLOR_INFO,
            )
        )
        return

    # No active session — recreate. #audit(#7): pass resume_session_id so a
    # dead/restarted session preserves prior context, matching every sibling
    # recreate command (/effort, /max-turns, /fallback, /bare, /tools.*). Only
    # the model choice is ephemeral (#210); the transcript is still resumed.
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(
        interaction, model_override=name, resume_session_id=sid
    )
    if bridge:
        context_note = (
            "✅ Context preserved from the prior session."
            if sid
            else "⚠️ No prior context (no saved session for this thread)."
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"🔄 Switched to `{name}`",
                description=(
                    f"✅ Model set for new session.\n{context_note}\n\n"
                    "-# ⏱️ **Per-session** — bot restart returns to CLI default."
                ),
                color=COLOR_INFO,
            )
        )


@model_group.command(name="list", description="List available models + show current")
async def model_list(interaction: discord.Interaction) -> None:
    """List models the SDK reports as available for the current session (#293).

    Precedence:

    1. **Active session** — call ``bridge.get_server_info()`` and use its
       ``models`` array (CLI-authoritative). Fall back to ``KNOWN_MODELS``
       only if the SDK call fails or returns nothing.
    2. **No active session** — display ``KNOWN_MODELS`` as before, but
       clearly flagged as static reference data.

    ``current`` marker resolution unchanged (``bridge.model`` chain).
    """
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    current = _current_model_for_thread(bot, interaction.channel)
    bridge = _resolve_session_bridge(bot, interaction.channel)

    _deferred = False
    sdk_models: list[dict] = []
    if bridge is not None:
        await interaction.response.defer(ephemeral=True)
        _deferred = True
        try:
            info = await asyncio.wait_for(bridge.get_server_info(), timeout=10)
        except Exception as exc:
            log.debug("/model list: get_server_info failed: %r", exc)
            info = None
        if info:
            raw = info.get("models") or []
            if isinstance(raw, list):
                sdk_models = [m for m in raw if isinstance(m, dict)]

    lines: list[str] = []
    if sdk_models:
        for m in sdk_models:
            value = str(m.get("value") or "").strip()
            display_name = str(m.get("displayName") or value or "?").strip()
            description = str(m.get("description") or "").strip()
            # Enrich with KNOWN_MODELS context/tier when we recognize the alias
            # so the display keeps its familiar shape without re-hardcoding.
            enrich = KNOWN_MODELS.get(value)
            ctx_suffix = ""
            if enrich is not None:
                ctx_suffix = f" — {enrich['tier']}, {_fmt_context(enrich['context'])} context"
            marker = (
                "🟢 " if current and (current == value or (enrich and current == enrich["id"]))
                else "• "
            )
            # Cap description to keep the embed under Discord's 4096 char
            # limit on description; ``models`` typically returns 5 entries
            # so the total stays well under.
            desc_short = description[:120] + ("…" if len(description) > 120 else "")
            row = f"{marker}**{display_name}** (`{value}`){ctx_suffix}"
            if desc_short:
                row += f"\n  {desc_short}"
            lines.append(row)
    else:
        for alias, meta in KNOWN_MODELS.items():
            ctx = _fmt_context(meta["context"])
            tier = meta["tier"]
            model_id = meta["id"]
            marker = "🟢 " if current and (current == alias or current == model_id) else "• "
            lines.append(f"{marker}**{alias}** (`{model_id}`) - {tier}, {ctx} context")

    desc = "\n".join(lines)
    if current:
        header = f"**Current**: `{current}`\n\n**Available models**:\n"
    elif bridge is not None:
        header = "**Current**: _(unset - will use CLI default)_\n\n**Available models**:\n"
    else:
        header = "_No active session. Run inside a thread to see current model._\n\n**Available models**:\n"

    footer_note = (
        "\n\nUse `/model switch <name>` to switch.\n"
        "-# Switching resets the conversation context."
    )
    if not sdk_models:
        footer_note += (
            "\n-# ⚠️ Static reference — start a session to see the CLI's actual models."
        )

    embed = discord.Embed(
        title="🤖 Model Selection",
        description=header + desc + footer_note,
        color=COLOR_INFO,
    )
    await _reply(interaction, _deferred, embed=embed)


@model_group.command(name="current", description="Show current Claude model for this thread")
async def model_current(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #198: don't collapse via ``bridge.model`` - we need to know which
    # tier the value came from so we can label it correctly. Resolve the
    # bridge directly and dispatch on the 4 tier cases.
    # #247 Bug C: share session-resolution logic with ``/model list`` via
    # ``_resolve_session_bridge`` so the two commands cannot disagree on
    # whether a session exists for the current channel/thread.
    bridge = _resolve_session_bridge(bot, interaction.channel)
    if bridge is None:
        await interaction.response.send_message(
            "i️ No active session. Run inside a thread to see current model.",
            ephemeral=True,
        )
        return

    source, value = _model_source_for_bridge(bridge)

    # Case 4: nothing pinned anywhere AND no SDK turn yet - let the user
    # know the SDK/CLI will resolve the default on the first turn.
    if source == "unset":
        embed = discord.Embed(
            title="🤖 Current Model",
            description=(
                "_(unset - will use CLI default; ask Claude something to "
                "discover the actual model)_"
            ),
            color=COLOR_TOOL_SUCCESS,
        )
        await interaction.response.send_message(embed=embed)
        return

    # Cases 1-3: we have a concrete model name. Build the KNOWN_MODELS
    # metadata block when the name matches an alias or full id.
    assert value is not None  # narrowed by the source != "unset" branch
    matched = None
    for alias, info in KNOWN_MODELS.items():
        if value == alias or value == info["id"]:
            matched = (alias, info)
            break

    if source == "override":
        # User ran /model switch - full metadata, no suffix on the title
        # (matches existing UX from before this PR).
        if matched:
            alias, info = matched
            ctx = _fmt_context(info["context"])
            desc = (
                f"• **alias**: `{alias}`\n"
                f"• **id**: `{info['id']}`\n"
                f"• **tier**: {info['tier']}\n"
                f"• **context**: {ctx}"
            )
        else:
            desc = f"• **id**: `{value}`\n• _(not in known-models table)_"
    elif source == "env":
        # Admin-pinned via CLAUDE_MODEL env var.
        desc = f"`{value}` (CLAUDE_MODEL env)"
    else:
        # source == "sdk" - observed from a ResultMessage post-first-turn.
        # #210 R1 security: ``value`` originates in attacker-influenceable
        # ``ResultMessage.model``; strip backticks + cap length before
        # embedding in inline code fence (defense-in-depth).
        safe_value = str(value).replace("`", "'")[:120]
        desc = f"`{safe_value}` (CLI default)"

    embed = discord.Embed(
        title="🤖 Current Model",
        description=desc,
        color=COLOR_TOOL_SUCCESS,
    )
    await interaction.response.send_message(embed=embed)


async def model_switch_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """#186 enhanced: include metadata in the choice display so users see
    context / tier / id before committing."""
    out: list[app_commands.Choice[str]] = []
    cur_low = (current or "").lower()
    for alias, info in KNOWN_MODELS.items():
        if cur_low and cur_low not in alias.lower():
            continue
        display = f"{alias} \u2014 {info['tier']}, {_fmt_context(info['context'])} ({info['id']})"
        # Discord choice name cap = 100 chars
        out.append(app_commands.Choice(name=display[:100], value=alias))
        if len(out) >= 25:
            break
    return out

model_switch.autocomplete("name")(model_switch_autocomplete)


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
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, effort=level.value, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="🧠 Effort Level Set",
            description=f"Thinking effort set to **{level.value}**.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="max-turns", description="Set maximum turns for Claude session")
@app_commands.describe(number="Maximum number of turns")
async def max_turns_cmd(interaction: discord.Interaction, number: int) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if number < 1:
        await interaction.response.send_message("Number must be at least 1.", ephemeral=True)
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, max_turns=number, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="🔄 Max Turns Set",
            description=f"Max turns set to **{number}**.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="fallback-model", description="Set fallback model for Claude session")
@app_commands.describe(model="Fallback model name or ID")
async def fallback_model_cmd(interaction: discord.Interaction, model: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, fallback_model=model, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="🔄 Fallback Model Set",
            description=f"Fallback model set to **{model}**.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@app_commands.command(name="bare", description="Toggle bare/minimal Claude mode")
async def toggle_bare(interaction: discord.Interaction) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(interaction, bare=True, resume_session_id=sid)
    if bridge:
        embed = discord.Embed(
            title="🔧 Bare Mode Enabled",
            description="Session restarted in bare/minimal mode.\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)
