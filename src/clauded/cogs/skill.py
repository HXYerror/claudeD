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

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    CLINotFoundError,
    CLIConnectionError,
)

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE

log = logging.getLogger("clauded.bot")


skill_group = app_commands.Group(
    name="skill",
    description="List skills available to the Claude session.",
)


def _classify(cmd: dict) -> tuple[str, str, str]:
    """Return ``(group, displayName, displayDescription)`` for a CLI command.

    ``group`` is one of ``"user"``, ``"project"``, ``"plugin:<name>"``, or
    ``""`` for built-in commands (which the caller filters out). The
    displayed description has the source-tag suffix stripped.

    See PRD §Architecture for the source-tag-suffix encoding.
    """
    name = cmd.get("name", "")
    desc = cmd.get("description", "") or ""
    if desc.endswith(" (user)"):
        return ("user", name, desc[:-7].rstrip())
    if desc.endswith(" (project)"):
        return ("project", name, desc[:-10].rstrip())
    if desc.endswith(")") and " (plugin:" in desc:
        # " (plugin:foo)" — extract plugin name
        idx = desc.rfind(" (plugin:")
        plugin_name = desc[idx + 9:-1]
        return (f"plugin:{plugin_name}", name, desc[:idx].rstrip())
    return ("", "", "")  # built-in — filter out


# Discord limits we observe defensively. Per-field value cap is the hard
# 1024-char Discord limit; the 4000-char total budget comes from the PRD
# truncation guard (see PRD edge-case ">4000 chars total embed content").
_FIELD_VALUE_CAP = 1024
_EMBED_TOTAL_BUDGET = 4000
_DESC_TRUNCATE_AT = 120


def _format_row(name: str, desc: str) -> str:
    """Render a single skill row: ``• <name> — <truncated desc>``."""
    if not desc:
        body = "_(no description)_"
    elif len(desc) > _DESC_TRUNCATE_AT:
        body = desc[:_DESC_TRUNCATE_AT] + "…"
    else:
        body = desc
    return f"• {name} — {body}"


def _ordered_group_keys(groups: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Return group keys in the PRD display order.

    Order: Project, User (Global), then each Plugin alphabetized. Missing
    groups are simply absent.
    """
    keys: list[str] = []
    if "project" in groups:
        keys.append("project")
    if "user" in groups:
        keys.append("user")
    plugin_keys = sorted(k for k in groups if k.startswith("plugin:"))
    keys.extend(plugin_keys)
    return keys


def _group_field_name(key: str) -> str:
    if key == "project":
        return "Project"
    if key == "user":
        return "User (Global)"
    if key.startswith("plugin:"):
        return f"Plugin: {key[len('plugin:'):]}"
    return key  # defensive


def _build_skills_embed(
    groups: dict[str, list[tuple[str, str]]],
    *,
    is_bound: bool,
    total_skill_count: int,
) -> discord.Embed:
    """Render the grouped skills into a single ``discord.Embed``.

    Enforces both Discord's per-field 1024-char limit AND the PRD
    4000-char total-embed budget by trimming rows from the tail and
    appending a "…and N more" notice if anything had to be dropped.
    """
    embed = discord.Embed(title=f"🧰 Skills ({total_skill_count})", color=COLOR_INFO)

    if not is_bound:
        embed.set_footer(
            text=(
                "💡 Unbound channel — showing global skills only. "
                "Run /project bind <path> to see project skills."
            )
        )

    if total_skill_count == 0:
        embed.description = "No user or project skills installed."
        return embed

    # Build (key, [rendered_row, …]) in display order. We render every row
    # up-front so we can compute the truncation tail accurately.
    ordered: list[tuple[str, list[str]]] = []
    for key in _ordered_group_keys(groups):
        rows = [_format_row(n, d) for (n, d) in groups[key]]
        ordered.append((key, rows))

    # Walk groups in order, packing rows into each field while respecting
    # the per-field 1024-char cap and the per-embed 4000-char cap. Rows
    # we couldn't fit get counted as "dropped".
    dropped = 0
    truncation_field_overhead = 64  # rough budget for the "…and N more" line

    def current_len(e: discord.Embed) -> int:
        # discord.py's ``Embed.__len__`` returns the serialized length.
        return len(e)

    for key, rows in ordered:
        field_name = _group_field_name(key)
        if not rows:
            continue
        accumulated_lines: list[str] = []
        accumulated_len = 0  # tracks accumulated body length only
        for row in rows:
            # +1 for the joining newline if there's already something there.
            extra = len(row) + (1 if accumulated_lines else 0)
            new_field_len = accumulated_len + extra
            if new_field_len > _FIELD_VALUE_CAP:
                dropped += 1
                continue
            # Tentative new total: simulate adding this row to the field.
            # We compute the would-be total length by adding the row
            # (and possibly a newline) plus, on first row of a new field,
            # the field-name + structural overhead (~ len(name) + ~6).
            structural = 0 if accumulated_lines else len(field_name) + 6
            tentative_total = (
                current_len(embed)
                + extra
                + structural
                + truncation_field_overhead
            )
            if tentative_total > _EMBED_TOTAL_BUDGET:
                dropped += 1
                continue
            accumulated_lines.append(row)
            accumulated_len += extra
        if accumulated_lines:
            embed.add_field(
                name=field_name,
                value="\n".join(accumulated_lines),
                inline=False,
            )

    if dropped > 0:
        embed.add_field(
            name="\u200b",  # zero-width space — Discord requires a non-empty name
            value=f"_…and {dropped} more skills (use Claude CLI to see all)_",
            inline=False,
        )

    return embed


def _resolve_channel_id(interaction: discord.Interaction) -> int | None:
    """Resolve the channel id used for project/session lookups.

    Returns ``None`` for DMs / no-channel contexts so the caller can
    surface a friendly error. Threads resolve to their parent channel
    (matching ``_unbound.reject_if_unbound``).
    """
    ch = interaction.channel
    if ch is None:
        # DM, cache miss, or permission gap.
        return interaction.channel_id
    if isinstance(ch, discord.DMChannel):
        return None
    if isinstance(ch, discord.Thread):
        return ch.parent_id or interaction.channel_id
    return ch.id


@skill_group.command(name="list", description="List skills available to the current channel.")
async def skill_list(interaction: discord.Interaction) -> None:
    """List user/project/plugin skills the Claude session can auto-invoke.

    Two probe paths:

    * **Path A** — if there's already an active ``ClaudeBridge`` for this
      thread/channel, piggyback its connected client and call
      ``get_server_info()`` directly (~0–1 ms, cached snapshot).
    * **Path B** — otherwise, spin up a transient ``ClaudeSDKClient`` with
      the channel's resolved cwd and the appropriate ``setting_sources``
      (``["user", "project", "local"]`` if bound, ``["user"]`` if not),
      then ``get_server_info()`` (~2–4 s cold start).

    See PRD ``docs/prd/v1.13-skill-list.md`` §Architecture.
    """
    from ..bot import ClaudedBot

    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # ---- Channel resolution (PRD §Architecture "Channel resolution") ----
    channel_id = _resolve_channel_id(interaction)
    if channel_id is None:
        await interaction.followup.send(
            "❌ This command must be run in a channel.", ephemeral=True
        )
        return

    cwd, is_bound = bot.project_manager.get_path_or_default(channel_id)

    info: dict | None = None
    info_err: Exception | None = None

    # ---- Path A: piggyback the live bridge if there is one. ----
    # NOTE: we deliberately touch ``bridge._client`` here. The PRD risk
    # table (docs/prd/v1.13-skill-list.md §Risks) flags this as private-
    # attribute access; mitigation is to pin SDK ≥ 0.1.80 and ask
    # upstream for a public accessor in v1.14.
    try:
        bridge = bot.session_manager.get_session(channel_id)
        if bridge is not None and bridge.is_active and bridge._client is not None:
            info = await bridge._client.get_server_info()
    except Exception as exc:  # pragma: no cover - defensive; Path B retries
        log.debug("Path A get_server_info failed, falling through: %r", exc)
        info = None

    # ---- Path B: spin up a transient client. ----
    if info is None:
        setting_sources = ["user", "project", "local"] if is_bound else ["user"]
        try:
            async with ClaudeSDKClient(
                ClaudeAgentOptions(cwd=str(cwd), setting_sources=setting_sources)
            ) as tmp:
                info = await tmp.get_server_info()
        except CLINotFoundError as exc:
            info_err = exc
        except CLIConnectionError as exc:
            info_err = exc
        except Exception as exc:  # noqa: BLE001 — surface any failure as a friendly embed
            info_err = exc

    if info_err is not None or info is None:
        if info_err is None:
            # info is None with no exception — synthetic message preserves the
            # "type name + value" shape so users see something useful.
            desc = "`NoServerInfo`: get_server_info() returned None"
        else:
            desc = f"`{type(info_err).__name__}`: {info_err}"
        embed = discord.Embed(
            title="❌ Skills unavailable",
            description=desc,
            color=COLOR_TOOL_FAILURE,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # ---- Classify and group ----
    commands_list = info.get("commands", []) or []
    groups: dict[str, list[tuple[str, str]]] = {}
    for cmd in commands_list:
        group, display_name, display_desc = _classify(cmd)
        if not group:
            continue
        groups.setdefault(group, []).append((display_name, display_desc))

    total_skill_count = sum(len(v) for v in groups.values())

    embed = _build_skills_embed(
        groups, is_bound=is_bound, total_skill_count=total_skill_count
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["skill_group", "skill_list", "_classify"]
