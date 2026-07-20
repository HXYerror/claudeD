"""#audit(#15): disk-session helpers for the /session history·open·rename·tag·
delete commands.

Centralizes the ``claude_agent_sdk`` import and wraps EVERY sync, filesystem-
backed SDK call in ``asyncio.to_thread`` — a directory scan / jsonl edit / unlink
must never run on the live bot's event loop. Mirrors how cogs/mcp.py keeps its
``_resolve_live_bridge`` / autocomplete helpers out of the command bodies.
"""
from __future__ import annotations

import asyncio

from discord import app_commands


def _sdk():
    # Lazy import (matches cogs/skill.py / cogs/context.py) so a missing/old SDK
    # surfaces as a clean runtime error, not an import-time crash of the cog.
    import claude_agent_sdk as s
    return s


def _resolve_project_dir(interaction, bot) -> str | None:
    """Bound project directory for this channel/thread (parent-keyed), or None."""
    from ._unbound import resolve_binding_id  # parent-keyed for threads (#197/#209)
    bid = resolve_binding_id(interaction)
    return bot.project_manager.get_path(bid) if bid is not None else None


# --- sync SDK calls, each hopped onto a thread ---------------------------------


async def _list_sessions(directory, limit=25, offset=0):
    s = _sdk()
    return await asyncio.to_thread(s.list_sessions, directory, limit, offset, True)


async def _get_info(directory, sid):
    s = _sdk()
    return await asyncio.to_thread(s.get_session_info, sid, directory)


async def _rename(directory, sid, title):
    s = _sdk()
    await asyncio.to_thread(s.rename_session, sid, title, directory)


async def _tag(directory, sid, tag):
    s = _sdk()
    await asyncio.to_thread(s.tag_session, sid, tag, directory)


async def _delete(directory, sid):
    s = _sdk()
    await asyncio.to_thread(s.delete_session, sid, directory)


# --- pure helpers -------------------------------------------------------------


def _fmt_session_label(info) -> str:
    title = (
        getattr(info, "custom_title", None)
        or getattr(info, "summary", None)
        or (getattr(info, "first_prompt", None) or "")[:80]
        or "(untitled)"
    )
    parts = [title[:60]]
    if getattr(info, "git_branch", None):
        parts.append(info.git_branch)
    if getattr(info, "tag", None):
        parts.append(f"#{info.tag}")
    return " · ".join(parts)


def _resolve_session_id(sessions, token: str) -> str:
    """Resolve a full UUID or a >=8-char unique prefix to a full session id."""
    token = (token or "").strip()
    ids = [x.session_id for x in sessions]
    if token in ids:
        return token
    if len(token) >= 8:
        pref = [i for i in ids if i.startswith(token)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            raise ValueError(f'Ambiguous id "{token}" matches {len(pref)} sessions.')
    raise ValueError(f'No session matches "{token}" in this project.')


async def _session_id_autocomplete(interaction, current):
    from ..bot import ClaudedBot
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        return []
    directory = _resolve_project_dir(interaction, bot)
    if not directory:
        return []
    try:
        sessions = await _list_sessions(directory, limit=25)
    except Exception:
        return []
    cur = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for x in sessions:
        lbl = _fmt_session_label(x)
        if cur in lbl.lower() or cur in x.session_id:
            out.append(app_commands.Choice(name=lbl[:100], value=x.session_id))
        if len(out) >= 25:
            break
    return out
