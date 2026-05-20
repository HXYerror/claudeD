"""Unit tests for #250 — ``resolve_session_id`` helper.

Sibling of ``tests/test_binding_id_resolution.py`` (#209). Sessions
(live ``ClaudeBridge`` + per-thread settings such as
``_notify_enabled``) are keyed by ``thread.id`` and must never fall
through to a top-level :class:`discord.TextChannel` / :class:`DMChannel`
/ cache-miss surface. ``resolve_session_id`` enforces that contract by
returning ``None`` for any non-thread surface so the caller can surface
a uniform "Use this command inside a thread." refusal.

This file covers the AC1 unit-test surface of the issue (channel-type
matrix). The 5-site behavior change is exercised end-to-end by the
existing cog tests (test_permission_mode.py for /mode set/cycle/current,
ad-hoc smoke for /health + /notify).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord

from clauded.cogs._unbound import resolve_session_id


def test_resolve_session_id_thread_returns_thread_id() -> None:
    """In a Thread, returns the thread's own id (not parent_id)."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = 999
    thread.parent_id = 555
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = thread
    interaction.channel_id = 999

    assert resolve_session_id(interaction) == 999


def test_resolve_session_id_text_channel_returns_none() -> None:
    """Top-level TextChannel must return None (force "use in thread" refusal)."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 444
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = 444

    assert resolve_session_id(interaction) is None


def test_resolve_session_id_dm_returns_none() -> None:
    """DM must also return None — no live session can exist outside a thread."""
    dm = MagicMock(spec=discord.DMChannel)
    dm.id = 333
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = dm
    interaction.channel_id = 333

    assert resolve_session_id(interaction) is None


def test_resolve_session_id_none_channel_returns_none() -> None:
    """Cache miss / permission gap — channel is None — must return None."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = None
    interaction.channel_id = 222

    assert resolve_session_id(interaction) is None
