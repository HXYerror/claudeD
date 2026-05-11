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


# Discord per-field hard limit. The total-embed budget caps conservatively
# below Discord's 6000-char serialized ceiling to leave room for
# title/footer. Real pagination is a v1.14 follow-up (PRD §Out-of-scope).
_FIELD_VALUE_CAP = 1024
_EMBED_TOTAL_BUDGET = 5500
_DESC_TRUNCATE_AT = 120


def _build_skills_embed(
    groups: dict[str, list[tuple[str, str]]],
    *,
    is_bound: bool,
    total_skill_count: int,
) -> discord.Embed:
    """Render the grouped skills into a single ``discord.Embed``.

    Each group becomes one field with rows joined by newlines, capped
    at the per-field 1024-char limit by simple slice + ellipsis. If
    the total serialized embed exceeds ``_EMBED_TOTAL_BUDGET`` we
    drop trailing skills (last group, last rows) until it fits and
    add a single "…and N more" notice.

    Note: skill names and descriptions are user-visible, sourced from
    the operator's local ``~/.claude/skills/`` and project
    ``.claude/skills/`` directories — do not store secrets in skill
    *names*.
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

    # Group display order: Project → User (Global) → Plugin:<name> alphabetized.
    ordered_keys: list[str] = []
    if "project" in groups:
        ordered_keys.append("project")
    if "user" in groups:
        ordered_keys.append("user")
    ordered_keys.extend(sorted(k for k in groups if k.startswith("plugin:")))

    def _row(name: str, desc: str) -> str:
        if not desc:
            body = "_(no description)_"
        elif len(desc) > _DESC_TRUNCATE_AT:
            body = desc[:_DESC_TRUNCATE_AT] + "…"
        else:
            body = desc
        return f"• {name} — {body}"

    def _field_label(key: str) -> str:
        if key == "project":
            return "Project"
        if key == "user":
            return "User (Global)"
        return f"Plugin: {key[len('plugin:'):]}"  # "plugin:<name>"

    def _add_fields(group_rows: dict[str, list[str]]) -> None:
        for key in ordered_keys:
            if key not in group_rows or not group_rows[key]:
                continue
            value = "\n".join(group_rows[key])
            if len(value) > _FIELD_VALUE_CAP:
                value = value[: _FIELD_VALUE_CAP - 1] + "…"
            embed.add_field(name=_field_label(key), value=value, inline=False)

    # Initial render: all rows.
    rendered: dict[str, list[str]] = {
        key: [_row(n, d) for (n, d) in groups[key]] for key in ordered_keys
    }
    _add_fields(rendered)

    # If the serialized embed blew the total budget, drop trailing
    # rows (deterministic: last group's tail first) until it fits.
    if len(embed) > _EMBED_TOTAL_BUDGET:
        dropped = 0
        while len(embed) > _EMBED_TOTAL_BUDGET and any(rendered[k] for k in ordered_keys):
            # Pop from the last non-empty group.
            for key in reversed(ordered_keys):
                if rendered[key]:
                    rendered[key].pop()
                    dropped += 1
                    break
            embed.clear_fields()
            _add_fields(rendered)
        if dropped > 0:
            embed.add_field(
                name="\u200b",  # zero-width space — Discord requires non-empty name
                value=f"_…and {dropped} more skills (use Claude CLI to see all)_",
                inline=False,
            )

    return embed


@skill_group.command(name="list", description="List skills available to the current channel.")
async def skill_list(interaction: discord.Interaction) -> None:
    """List user/project/plugin skills the Claude session can auto-invoke.

    Two probe paths:

    * **Path A** — if there's already an active ``ClaudeBridge`` for this
      thread/channel, piggyback its connected client and call
      ``bridge.get_server_info()`` (~0–1 ms, cached snapshot).
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

    channel_id = resolve_channel_id(interaction)
    if channel_id is None:
        await interaction.followup.send(NO_CHANNEL_MESSAGE, ephemeral=True)
        return

    cwd, is_bound = bot.project_manager.get_path_or_default(channel_id)

    info: dict | None = None
    info_err: Exception | None = None

    # ---- Path A: piggyback the live bridge if there is one. ----
    # ``bridge.get_server_info()`` is a cache read of the SDK's
    # ``_initialization_result``; safe to call concurrently with an
    # in-flight ``send_message`` stream. We still wrap in a broad
    # ``except`` because a future SDK refactor could change that —
    # if it ever raises, Path B handles the cold path.
    bridge = bot.session_manager.get_session(channel_id)
    try:
        if bridge is not None:
            info = await bridge.get_server_info()
    except Exception as exc:
        log.debug("Path A get_server_info failed, falling through: %r", exc)
        info = None

    # ---- Path B: spin up a transient client. ----
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

    # ---- Classify and group ----
    commands_list = info.get("commands", []) or []
    groups: dict[str, list[tuple[str, str]]] = {}
    for cmd in commands_list:
        group, display_name, display_desc = classify_command(cmd)
        if not group:
            continue
        groups.setdefault(group, []).append((display_name, display_desc))

    total_skill_count = sum(len(v) for v in groups.values())

    embed = _build_skills_embed(
        groups, is_bound=is_bound, total_skill_count=total_skill_count
    )
    await interaction.followup.send(embed=embed, ephemeral=True)
