"""Agent management commands: /agent group."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

from ._unbound import reject_if_unbound, resolve_channel_id
from ..discord_renderer import COLOR_INFO

log = logging.getLogger("clauded.bot")


def _parse_agent_md(path: Path) -> tuple[str, str] | None:
    """Parse ``name`` + ``description`` from a ``.claude/agents/*.md`` frontmatter.

    Returns ``(name, description)`` or ``None`` if the file has no readable
    frontmatter. Best-effort — the Claude CLI's own parser is authoritative;
    we just need enough to render the /agent list fallback when no live
    session is available. #293.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Frontmatter is delimited by leading ``---`` and a trailing ``---``.
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = text[3:end]
    name: str | None = None
    desc: str | None = None
    for line in fm.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key == "name" and value:
            name = value
        elif key == "description" and value:
            desc = value
    fallback_name = path.stem
    return (name or fallback_name, desc or "")


def _read_agents_from_dir(dir_path: Path) -> dict[str, str]:
    """Return ``{name: description}`` for every parseable ``.md`` under ``dir_path``.

    Silent on I/O errors — this is best-effort fallback display (#293).
    """
    out: dict[str, str] = {}
    try:
        entries = list(dir_path.glob("*.md"))
    except OSError:
        return out
    for p in entries:
        parsed = _parse_agent_md(p)
        if parsed is None:
            continue
        name, desc = parsed
        out.setdefault(name, desc)
    return out


agent_group = app_commands.Group(
    name="agent",
    description="Manage custom Claude agents.",
)


@agent_group.command(name="create", description="Create a custom agent")
@app_commands.describe(name="Agent name", prompt="Agent system prompt", description="Optional description")
async def agent_create(
    interaction: discord.Interaction, name: str, prompt: str, description: str = ""
) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    try:
        bot.agent_manager.create(name, prompt, description)
    except ValueError as exc:
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"\u2705 Agent `{name}` created",
        description=f"Prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="list", description="List available agents")
async def agent_list(interaction: discord.Interaction) -> None:
    """List agents visible to the current channel (#293).

    Precedence:

    1. **Active session** (bridge exists): call ``bridge.get_server_info()``
       and pull the ``agents`` list — this is what the CLI has actually
       loaded (built-ins + project ``.claude/agents/`` + user
       ``~/.claude/agents/``). Merge with local ``/agent create`` entries
       from ``data/agents.json`` so runtime-created custom agents still
       show up.
    2. **No active session**: read ``.md`` files directly from the bound
       project's ``.claude/agents/`` (when bound) plus
       ``~/.claude/agents/``, and merge with ``data/agents.json``. This
       satisfies AC1 (see ``pm-agent.md``) without spawning a transient
       SDK client.
    """
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    _deferred = False

    # {name: {description, source, prompt_preview?}}. ``source`` is one of
    # ``"sdk"``, ``"local"``, or ``"file"`` — used only to nudge the UI copy
    # (no user-visible tag today, but keeps merge-order deterministic).
    merged: dict[str, dict[str, str]] = {}

    # --- Path A: live bridge (best data) ------------------------------
    session_id = interaction.channel_id
    bridge = (
        bot.session_manager.get_session(session_id)
        if session_id is not None
        else None
    )
    if bridge is not None:
        await interaction.response.defer(ephemeral=True)
        _deferred = True
        try:
            info = await asyncio.wait_for(bridge.get_server_info(), timeout=10)
        except Exception as exc:
            log.debug("/agent list: get_server_info failed: %r", exc)
            info = None
        if info:
            for entry in info.get("agents", []) or []:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or ""
                if not name:
                    continue
                desc = entry.get("description") or ""
                merged.setdefault(name, {"description": desc, "source": "sdk"})

    # --- Path B: filesystem fallback when no bridge -------------------
    if bridge is None or not merged:  # E4: fallback if SDK returned no agents
        binding_id = resolve_channel_id(interaction)
        if binding_id is not None:
            cwd, is_bound = bot.project_manager.get_path_or_default(binding_id)
            if is_bound:
                project_dir = Path(cwd) / ".claude" / "agents"
                for name, desc in _read_agents_from_dir(project_dir).items():
                    merged.setdefault(name, {"description": desc, "source": "file"})
        try:
            user_dir = Path.home() / ".claude" / "agents"
        except (RuntimeError, OSError):
            user_dir = None
        if user_dir is not None:
            for name, desc in _read_agents_from_dir(user_dir).items():
                merged.setdefault(name, {"description": desc, "source": "file"})

    # --- Always merge: local /agent create entries --------------------
    for name, info_dict in bot.agent_manager.list_all().items():
        prompt = info_dict.get("prompt", "") or ""
        desc = info_dict.get("description", "") or ""
        prompt_preview = prompt[:100] + ("…" if len(prompt) > 100 else "")
        # Local agents override so their prompt preview is visible even
        # if the SDK returned a same-named entry with just a description.
        merged[name] = {
            "description": desc,
            "source": "local",
            "prompt_preview": prompt_preview,
        }

    if not merged:
        _send = interaction.followup.send if _deferred else interaction.response.send_message
        await _send(
            "No agents defined. Use `/agent create` or drop a `.md` file "
            "in `.claude/agents/`.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title=f"\U0001f916 Agents ({len(merged)})", color=COLOR_INFO)
    for aname, ainfo in sorted(merged.items()):
        desc = ainfo.get("description", "")
        value = desc or "_(no description)_"
        preview = ainfo.get("prompt_preview")
        if preview:
            value = f"{value}\n`{preview}`" if desc else f"`{preview}`"
        # Discord field value cap = 1024
        if len(value) > 1024:
            value = value[:1020] + "…"
        embed.add_field(name=aname, value=value, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="use", description="Use a custom agent in this thread")
@app_commands.describe(name="Agent name")
async def agent_use(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    agent = bot.agent_manager.get(name)
    if not agent:
        await interaction.response.send_message(f"\u274c Agent `{name}` not found.", ephemeral=True)
        return
    agents_json = {name: {"description": agent["description"], "prompt": agent["prompt"]}}
    # #277: preserve context across the recreate by passing resume_session_id
    thread_id = getattr(interaction.channel, "id", None)
    sid = bot._get_resume_session_id(thread_id)
    bridge = await bot._recreate_session(
        interaction, agent_name=name, custom_agents=agents_json, resume_session_id=sid,
    )
    if bridge:
        embed = discord.Embed(
            title=f"\U0001f916 Agent `{name}` activated",
            description=f"{agent['description']}\n✅ Context preserved.",
            color=COLOR_INFO,
        )
        await interaction.followup.send(embed=embed)


@agent_group.command(name="delete", description="Delete a custom agent")
@app_commands.describe(name="Agent name")
async def agent_delete(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    if bot.agent_manager.delete(name):
        embed = discord.Embed(
            title=f"\U0001f5d1\ufe0f Agent `{name}` deleted",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            f"\u274c Agent `{name}` not found.", ephemeral=True
        )


async def agent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        return []
    agents = bot.agent_manager.list_all()
    return [app_commands.Choice(name=n, value=n) for n in agents if current.lower() in n.lower()][:25]

agent_use.autocomplete("name")(agent_autocomplete)
