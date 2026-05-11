"""Skill discovery commands: /skill group.

Adds ``/skill list`` (PRD docs/prd/v1.13-skill-list.md, issue #109): lists
the user/project/plugin slash-commands that the Claude CLI will inject
into the current channel's session. Built-in commands (``clear``,
``compact``, …) are filtered out so users only see "skills" — i.e., the
content of ``~/.claude/skills/`` and ``<project>/.claude/skills/``.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ..skill_parser import classify_command
from ._unbound import NO_CHANNEL_MESSAGE, resolve_channel_id

log = logging.getLogger("clauded.bot")


skill_group = app_commands.Group(
    name="skill",
    description="List skills available to the Claude session.",
)


_DESC_TRUNCATE_AT = 120
_UNBOUND_FOOTER = (
    "💡 Unbound channel — showing global skills only. "
    "Run /project bind <path> to see project skills."
)


def _ordered_group_keys(grouped: dict[str, list[tuple[str, str]]]) -> list[str]:
    keys: list[str] = []
    if "project" in grouped:
        keys.append("project")
    if "user" in grouped:
        keys.append("user")
    keys.extend(sorted(k for k in grouped if k.startswith("plugin:")))
    return keys


def _field_label(key: str) -> str:
    if key == "project":
        return "Project"
    if key == "user":
        return "User (Global)"
    return f"Plugin: {key[len('plugin:'):]}"


def _build_skills_embed(grouped: dict[str, list[tuple[str, str]]], is_unbound: bool) -> discord.Embed:
    """Render grouped skills into an ephemeral blue embed.

    Per-field cap 1024 chars (Discord limit); per-row desc truncated to 120 chars.
    If total embed length exceeds 5500 chars (under Discord's 6000), drop trailing
    rows globally and add a single trailing field noting how many were dropped.
    """
    total = sum(len(rows) for rows in grouped.values())
    embed = discord.Embed(title=f"🧰 Skills ({total})", color=COLOR_INFO)
    if total == 0:
        embed.description = "No user or project skills installed."
        if is_unbound:
            embed.set_footer(text=_UNBOUND_FOOTER)
        return embed

    # Build all rows up front in a flat list (group, name, desc-line); we'll
    # drop from the tail if we exceed the embed budget.
    flat: list[tuple[str, str]] = []  # (group_label, "• name — desc")
    for key in _ordered_group_keys(grouped):
        for name, desc in grouped[key]:
            d = desc.strip() or "_(no description)_"
            if len(d) > _DESC_TRUNCATE_AT:
                d = d[: _DESC_TRUNCATE_AT - 1].rstrip() + "…"
            flat.append((_field_label(key), f"• {name} — {d}"))

    dropped = 0
    while True:
        # Pack consecutive same-group rows into fields, each ≤1024 chars.
        e = discord.Embed(title=embed.title, color=COLOR_INFO)
        i = 0
        while i < len(flat):
            label = flat[i][0]
            chunk: list[str] = []
            chunk_len = 0
            while i < len(flat) and flat[i][0] == label:
                row = flat[i][1]
                if chunk_len + len(row) + 1 > 1024:
                    break
                chunk.append(row)
                chunk_len += len(row) + 1
                i += 1
            e.add_field(name=label, value="\n".join(chunk), inline=False)
        if dropped:
            e.add_field(name="\u200b", value=f"_…and {dropped} more skills (use Claude CLI to see all)_", inline=False)
        if len(e) <= 5500 or not flat:
            embed = e
            break
        flat.pop()
        dropped += 1

    if is_unbound:
        embed.set_footer(text=_UNBOUND_FOOTER)
    return embed


@skill_group.command(name="list", description="List skills available to the current channel.")
async def skill_list(interaction: discord.Interaction) -> None:
    """List user/project/plugin skills the Claude session can auto-invoke.

    See PRD ``docs/prd/v1.13-skill-list.md`` §Architecture for Path A
    (bridge piggyback) vs Path B (transient SDK client) details.
    """
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    channel_id = resolve_channel_id(interaction)
    if channel_id is None:
        await interaction.followup.send(NO_CHANNEL_MESSAGE, ephemeral=True)
        return

    cwd, is_bound = bot.project_manager.get_path_or_default(channel_id)

    info: dict | None = None
    info_err: Exception | None = None

    # Path A: piggyback the live bridge if there is one.
    bridge = bot.session_manager.get_session(channel_id)
    try:
        if bridge is not None:
            info = await bridge.get_server_info()
    except Exception as exc:
        log.debug("Path A get_server_info failed, falling through: %r", exc)
        info = None

    # Path B: spin up a transient client.
    if info is None:
        setting_sources = ["user", "project", "local"] if is_bound else ["user"]
        try:
            async with ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    cwd=str(cwd), setting_sources=setting_sources
                )
            ) as tmp:
                info = await tmp.get_server_info()
        except Exception as exc:  # noqa: BLE001 — any failure → friendly red embed
            info_err = exc

    if info_err is not None or info is None:
        if info_err is None:
            desc = "Claude CLI returned no command list."
        else:
            # Class name only — exception bodies may echo cli_path /
            # home dirs / env from SDK error strings. Full exception
            # is in DEBUG logs above for operators.
            desc = f"`{type(info_err).__name__}`"
        embed = discord.Embed(
            title="❌ Skills unavailable",
            description=desc,
            color=COLOR_TOOL_FAILURE,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Classify and group.
    commands_list = info.get("commands", []) or []
    groups: dict[str, list[tuple[str, str]]] = {}
    for cmd in commands_list:
        group, display_name, display_desc = classify_command(cmd)
        if not group:
            continue
        groups.setdefault(group, []).append((display_name, display_desc))

    embed = _build_skills_embed(groups, is_unbound=not is_bound)
    await interaction.followup.send(embed=embed, ephemeral=True)
