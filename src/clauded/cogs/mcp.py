"""MCP server management commands: /mcp group."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id
from .. import _cli_native
from ..discord_renderer import COLOR_INFO

log = logging.getLogger("clauded.bot")


# Emoji per connection status — keeps the /mcp list embed readable when
# the CLI reports many servers with mixed health (#293).
_STATUS_ICON = {
    "connected": "🟢",
    "pending": "🟡",
    "needs-auth": "🔒",
    "disabled": "⚪",
    "failed": "🔴",
}


def _format_live_server(entry: dict) -> tuple[str, str]:
    """Render a single ``McpServerStatus`` dict into an embed ``(name, value)`` pair.

    Best-effort — the SDK's schema may add keys over time; we just show
    what we recognize (name, status, scope, transport-specific config).
    """
    name = str(entry.get("name") or "?")
    status = str(entry.get("status") or "?")
    scope = str(entry.get("scope") or "")
    icon = _STATUS_ICON.get(status, "•")
    header = f"{icon} {status}"
    if scope:
        header += f" · {scope}"

    config = entry.get("config") or {}
    stype = str(config.get("type") or "stdio")
    detail_lines: list[str] = []
    if stype == "http" or stype == "sse":
        url = config.get("url", "N/A")
        detail_lines.append(f"URL: `{url}`")
    elif stype == "stdio":
        cmd = config.get("command", "?")
        args = config.get("args") or []
        sargs = " ".join(str(a) for a in args)
        detail_lines.append(f"Command: `{cmd}`")
        if sargs:
            detail_lines.append(f"Args: `{sargs}`")
    else:
        detail_lines.append(f"Type: `{stype}`")

    err = entry.get("error")
    if status == "failed" and err:
        # Keep error short and back-tick-safe (mirror /model current #210 defense).
        safe_err = str(err).replace("`", "'")[:200]
        detail_lines.append(f"⚠️ {safe_err}")

    tools = entry.get("tools") or []
    if isinstance(tools, list) and tools:
        detail_lines.append(f"Tools: {len(tools)}")

    value = header + "\n" + "\n".join(detail_lines) if detail_lines else header
    return (f"{name} ({stype})", value)


mcp_group = app_commands.Group(
    name="mcp",
    description="Manage MCP servers for Claude.",
    default_permissions=discord.Permissions(administrator=True),
)


@mcp_group.command(name="add", description="Add a stdio MCP server")
@app_commands.describe(name="Server name", command="Command to run", args="Space-separated arguments")
async def mcp_add(
    interaction: discord.Interaction, name: str, command: str, args: str = ""
) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    config: dict = {"type": "stdio", "command": command}
    if args:
        config["args"] = args.split()
    # #294: primary storage is the CLI-native ``.mcp.json`` under the
    # bound project. ``project_manager.add_mcp_server`` still runs so the
    # legacy per-channel shadow store stays in sync (backwards compat +
    # its name-validation gate). If the shadow write succeeds but the
    # ``.mcp.json`` write fails, we roll back so the two stores agree.
    try:
        bot.project_manager.add_mcp_server(binding_id, name, config)
    except ValueError as exc:
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(binding_id)
    if project_path is None:
        bot.project_manager.remove_mcp_server(binding_id, name)
        await interaction.response.send_message(
            "\u274c Could not resolve project path for this channel.",
            ephemeral=True,
        )
        return
    try:
        _cli_native.add_mcp_server(project_path, name, config)
    except ValueError as exc:
        # ``.mcp.json`` already had the same-named entry (e.g. hand-edited).
        # Roll back the shadow store so the two views can't diverge.
        bot.project_manager.remove_mcp_server(binding_id, name)
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    except OSError as exc:
        bot.project_manager.remove_mcp_server(binding_id, name)
        log.exception("add_mcp_server: .mcp.json write failed at %s", project_path)
        await interaction.response.send_message(
            f"\u274c Failed to write .mcp.json: {exc}", ephemeral=True
        )
        return
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: stdio\nCommand: `{command}`" + (f"\nArgs: `{args}`" if args else ""),
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="add-url", description="Add an HTTP MCP server")
@app_commands.describe(name="Server name", url="Server URL")
async def mcp_add_url(interaction: discord.Interaction, name: str, url: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    config: dict = {"type": "http", "url": url}
    # #294: dual-write ``.mcp.json`` + legacy shadow, with rollback on
    # failure. See mcp_add for the full rationale.
    try:
        bot.project_manager.add_mcp_server(binding_id, name, config)
    except ValueError as exc:
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    project_path = bot.project_manager.get_path(binding_id)
    if project_path is None:
        bot.project_manager.remove_mcp_server(binding_id, name)
        await interaction.response.send_message(
            "\u274c Could not resolve project path for this channel.",
            ephemeral=True,
        )
        return
    try:
        _cli_native.add_mcp_server(project_path, name, config)
    except ValueError as exc:
        bot.project_manager.remove_mcp_server(binding_id, name)
        await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
        return
    except OSError as exc:
        bot.project_manager.remove_mcp_server(binding_id, name)
        log.exception("add_mcp_server (url): .mcp.json write failed at %s", project_path)
        await interaction.response.send_message(
            f"\u274c Failed to write .mcp.json: {exc}", ephemeral=True
        )
        return
    embed = discord.Embed(
        title=f"\u2705 MCP server `{name}` added",
        description=f"Type: http\nURL: `{url}`",
        color=COLOR_INFO,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@mcp_group.command(name="list", description="List configured MCP servers")
async def mcp_list(interaction: discord.Interaction) -> None:
    """List MCP servers the CLI has loaded for this channel (#293).

    Precedence:

    1. **Active session** — call ``bridge.get_mcp_status()`` and render
       the live ``mcpServers`` array (project ``.mcp.json`` + user
       settings + plugin-declared + our own ``clauded-scheduler``, with
       connection state).
    2. **No active session** (or SDK call fails) — fall back to the
       previous behavior of listing the bot's own stored configuration
       via ``project_manager.get_mcp_servers``. Unbound channels are
       still refused up-front (unchanged Group-A policy).
    """
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return

    _deferred = False

    # --- Path A: live bridge → SDK's mcp status -----------------------
    session_id = interaction.channel_id
    bridge = (
        bot.session_manager.get_session(session_id)
        if session_id is not None
        else None
    )
    if bridge is not None:
        await interaction.response.defer(ephemeral=True)
        _deferred = True
        status: dict | None = None
        try:
            status = await asyncio.wait_for(bridge.get_mcp_status(), timeout=10)
        except Exception as exc:
            log.debug("/mcp list: get_mcp_status failed: %r", exc)
            status = None
        if status is not None:
            servers = status.get("mcpServers") or []
            if not servers:
                _send = interaction.followup.send if _deferred else interaction.response.send_message
                await _send(
                    "No MCP servers loaded by the current session.",
                    ephemeral=True,
                )
                return
            embed = discord.Embed(
                title=f"\U0001f50c MCP Servers ({len(servers)})",
                color=COLOR_INFO,
            )
            for entry in servers:
                if not isinstance(entry, dict):
                    continue
                field_name, field_value = _format_live_server(entry)
                if len(field_value) > 1024:
                    field_value = field_value[:1020] + "…"
                embed.add_field(name=field_name, value=field_value, inline=False)
            _send = interaction.followup.send if _deferred else interaction.response.send_message
            await _send(embed=embed, ephemeral=True)
            return

    # --- Path B: no live session → fall back to stored config ---------
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        _send = interaction.followup.send if _deferred else interaction.response.send_message
        await _send(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    servers = bot.project_manager.get_mcp_servers(binding_id)
    if not servers:
        _send = interaction.followup.send if _deferred else interaction.response.send_message
        await _send(
            "No MCP servers configured. "
            "-# Start a session to see all servers loaded by the CLI.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(title="\U0001f50c MCP Servers", color=COLOR_INFO)
    for sname, sconfig in servers.items():
        stype = sconfig.get("type", "stdio")
        if stype == "http":
            detail = f"URL: `{sconfig.get('url', 'N/A')}`"
        else:
            cmd = sconfig.get("command", "?")
            sargs = " ".join(sconfig.get("args", []))
            detail = f"Command: `{cmd}`" + (f"\nArgs: `{sargs}`" if sargs else "")
        embed.add_field(name=f"{sname} ({stype})", value=detail, inline=False)
    # Footer nudge — stored config is a subset of what the CLI will load.
    embed.set_footer(
        text="Showing bot-configured servers only. "
        "Start a session to see everything the CLI loaded (project + user + plugins)."
    )
    _send = interaction.followup.send if _deferred else interaction.response.send_message
    await _send(embed=embed, ephemeral=True)


@mcp_group.command(name="remove", description="Remove an MCP server")
@app_commands.describe(name="Server name")
async def mcp_remove(interaction: discord.Interaction, name: str) -> None:
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("Bot not ready.", ephemeral=True)
        return
    if await reject_if_unbound(interaction, bot):
        return
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    # #294: remove from BOTH stores. Either side may be the only holder
    # (migration edge case, or user hand-edited ``.mcp.json``), so we
    # report success as long as either removed something.
    project_path = bot.project_manager.get_path(binding_id)
    file_removed = False
    if project_path is not None:
        try:
            file_removed = _cli_native.remove_mcp_server(project_path, name)
        except OSError:
            log.exception("remove_mcp_server: .mcp.json write failed at %s", project_path)
            file_removed = False
    manager_removed = bot.project_manager.remove_mcp_server(binding_id, name)
    if file_removed or manager_removed:
        embed = discord.Embed(
            title=f"\u2705 MCP server `{name}` removed",
            color=COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            f"\u274c MCP server `{name}` not found.", ephemeral=True
        )
