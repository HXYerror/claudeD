"""Unit tests for #250 — ``resolve_session_id`` helper.

Sibling of ``tests/test_binding_id_resolution.py`` (#209). Sessions
(live ``ClaudeBridge`` + per-thread settings such as
``_notify_enabled``) are keyed by ``thread.id`` and must never fall
through to a top-level :class:`discord.TextChannel` / :class:`DMChannel`
/ cache-miss surface. ``resolve_session_id`` enforces that contract by
returning ``None`` for any non-thread surface so the caller can surface
a uniform "Use this command inside a thread." refusal.

This file covers the AC1 unit-test surface of the issue (channel-type
matrix), the AC3 grep-lint surface (no cog calls
``session_manager.get_session(interaction.channel...`` raw — must flow
through ``resolve_session_id``), and the per-site refusal contract for
the 5 migrated cog sites (``/mode set``, ``/mode cycle``,
``/mode current``, ``/health``, ``/notify``).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.cogs._unbound import USE_IN_THREAD_MESSAGE, resolve_session_id


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


# ---------------------------------------------------------------------------
# AC3 — Audit grep lint (mirror of
# ``test_no_cog_passes_raw_channel_id_to_project_manager`` in
# tests/test_binding_id_resolution.py for the session-lookup surface).
#
# Manager protocol: pre-commit, temporarily revert one fix site to
# ``bot.session_manager.get_session(interaction.channel.id)`` (or
# ``getattr(interaction.channel, "id", None)``) — this test MUST fail
# pointing at that line. Revert the revert; this test passes again.
# ---------------------------------------------------------------------------


def test_no_cog_passes_raw_channel_to_session_manager() -> None:
    """Lint test: every cog call to ``bot.session_manager.get_session(...)``
    must source the thread id from ``resolve_session_id`` — i.e. from a
    variable named ``thread_id`` or ``tid``, never from a raw
    ``interaction.channel.*`` expression.

    This is a source-level substring grep (not AST) because perfect is
    the enemy of good for this defensive lint. Two forbidden inline
    patterns:

      1. ``session_manager.get_session(interaction.channel.id``
      2. ``session_manager.get_session(getattr(interaction.channel,``

    Bypass still possible by binding ``interaction.channel.id`` to a
    local var with a misleading name (e.g. ``tid = interaction.channel.id``);
    PRD §Risks documents this as accepted fragility, the trip-wire is
    the inline-access shape that produced the original five #250 bugs.

    Headline scope: ``cogs/mode.py`` + ``cogs/ops.py`` (the two files
    migrated by this PR) must have 0 hits. We assert the lint over all
    ``cogs/*.py`` so a future cog backsliding into the same pattern
    trips the test, matching the sibling-lint discipline established
    in #209's audit test.
    """
    cogs_dir = Path(__file__).resolve().parent.parent / "src" / "clauded" / "cogs"
    forbidden_patterns = (
        re.compile(
            r"session_manager\.get_session\(\s*interaction\.channel\.id\b",
            re.MULTILINE,
        ),
        re.compile(
            r"session_manager\.get_session\(\s*getattr\(\s*interaction\.channel\b",
            re.MULTILINE,
        ),
    )
    violations: list[str] = []
    for cog_file in cogs_dir.glob("*.py"):
        text = cog_file.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pat in forbidden_patterns:
                if pat.search(line):
                    violations.append(f"{cog_file.name}:{line_no}: {line.strip()}")
                    break  # don't double-report

    # Headline scope assertion: the two migrated files must be clean.
    headline = [v for v in violations if v.startswith(("mode.py:", "ops.py:"))]
    assert not headline, (
        "cogs/mode.py + cogs/ops.py must use resolve_session_id(interaction) "
        "(variable named `thread_id` / `tid`), not raw "
        "`interaction.channel.id` / `getattr(interaction.channel, ...)` when "
        "calling session_manager.get_session(). Violations:\n"
        + "\n".join(headline)
    )
    # Whole-cogs assertion (defense for future cogs).
    assert not violations, (
        "Cogs must use resolve_session_id(interaction) when calling "
        "session_manager.get_session(). Violations:\n" + "\n".join(violations)
    )


def test_audit_grep_catches_inline_interaction_channel_id_pattern() -> None:
    """Smoke test for the AC3 lint regex itself — positive + negative.

    Mirror of ``test_audit_grep_catches_inline_interaction_channel_id_pattern``
    in tests/test_binding_id_resolution.py. Guards against a regex typo
    silently disabling the lint (R1 architect finding pattern).
    """
    pat_dot = re.compile(
        r"session_manager\.get_session\(\s*interaction\.channel\.id\b"
    )
    pat_getattr = re.compile(
        r"session_manager\.get_session\(\s*getattr\(\s*interaction\.channel\b"
    )
    # Positive matches
    bad_dot = "bridge = bot.session_manager.get_session(interaction.channel.id)"
    bad_getattr = (
        "bridge = bot.session_manager.get_session("
        "getattr(interaction.channel, 'id', None))"
    )
    assert pat_dot.search(bad_dot) is not None
    assert pat_getattr.search(bad_getattr) is not None
    # Negative: resolved-var path OK.
    good = "bridge = bot.session_manager.get_session(thread_id)"
    assert pat_dot.search(good) is None
    assert pat_getattr.search(good) is None


# ---------------------------------------------------------------------------
# Per-site refusal tests — one per migrated cog site (5 total).
#
# Each test mocks an interaction whose channel is a TextChannel (not
# Thread), invokes the command callback, and asserts the response is
# an ephemeral reply containing the substring "thread". This pins the
# end-to-end contract: every migrated site refuses non-thread invocation
# with the unified ``USE_IN_THREAD_MESSAGE`` (rather than silently
# returning a "no active session" or producing a no-op write).
# ---------------------------------------------------------------------------


def _make_text_channel_interaction() -> MagicMock:
    """Build an Interaction whose channel is a top-level TextChannel.

    The 5 migrated callbacks all gate behind ``resolve_session_id`` which
    returns ``None`` for any non-Thread surface; a ``TextChannel`` is the
    most common refusal trigger and exercises the same code path as
    DMChannel + cache-miss (covered separately by the AC1 unit tests
    above).
    """
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 444
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = 444
    interaction.guild_id = 7777
    # Admin permissions so the /mode set + /mode cycle gates pass and we
    # actually reach the resolve_session_id check (not the admin refusal).
    interaction.user = MagicMock()
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot_with_session_manager() -> MagicMock:
    """Build a ClaudedBot mock with a session_manager that would raise if
    the refusal short-circuit fails (so we get a loud signal if a site
    regresses to ``get_session(None)`` rather than refusing first)."""
    from clauded.bot import ClaudedBot

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    # If a callback regresses and calls get_session anyway, raise loudly.
    bot.session_manager.get_session = MagicMock(
        side_effect=AssertionError(
            "regression: callback reached session_manager.get_session() in a "
            "non-thread context instead of refusing via USE_IN_THREAD_MESSAGE"
        )
    )
    bot._notify_enabled = {}
    bot._start_time = 0.0
    bot._claude_version = "test"
    bot._debug_logging = False
    bot.allow_unbound_fallback = False
    bot.project_manager = MagicMock()
    bot.project_manager._projects = {}
    bot.cost_tracker = MagicMock()
    return bot


def _assert_thread_refusal(interaction: MagicMock) -> None:
    """Shared assertion: response was an ephemeral message mentioning ``thread``.

    Centralized so a future tweak to the refusal copy (e.g. emoji prefix)
    only needs to update one assertion. Substring match on ``thread``
    (case-insensitive) intentionally lenient — pins the behavior class,
    not the exact wording.
    """
    assert interaction.response.send_message.await_count == 1, (
        "Expected exactly one ephemeral refusal reply"
    )
    call = interaction.response.send_message.await_args
    msg = call.args[0]
    assert "thread" in msg.lower(), (
        f"Refusal must mention 'thread'; got {msg!r}"
    )
    assert call.kwargs.get("ephemeral") is True, (
        "Refusal must be ephemeral"
    )
    # And it must be the centralized constant, not a drifted inline copy.
    assert msg == USE_IN_THREAD_MESSAGE, (
        f"Refusal must use the shared USE_IN_THREAD_MESSAGE constant; "
        f"got {msg!r}"
    )


@pytest.mark.asyncio
async def test_mode_set_in_text_channel_refuses_with_thread_hint() -> None:
    """``/mode set`` in a TextChannel must refuse with USE_IN_THREAD_MESSAGE."""
    from clauded.cogs.mode import mode_set

    interaction = _make_text_channel_interaction()
    interaction.client = _make_bot_with_session_manager()

    choice = discord.app_commands.Choice(name="plan 🔒", value="plan")
    await mode_set.callback(interaction, choice)

    _assert_thread_refusal(interaction)


@pytest.mark.asyncio
async def test_mode_cycle_in_text_channel_refuses_with_thread_hint() -> None:
    """``/mode cycle`` in a TextChannel must refuse with USE_IN_THREAD_MESSAGE."""
    from clauded.cogs.mode import mode_cycle

    interaction = _make_text_channel_interaction()
    interaction.client = _make_bot_with_session_manager()

    await mode_cycle.callback(interaction)

    _assert_thread_refusal(interaction)


@pytest.mark.asyncio
async def test_mode_current_in_text_channel_refuses_with_thread_hint() -> None:
    """``/mode current`` in a TextChannel must refuse with USE_IN_THREAD_MESSAGE."""
    from clauded.cogs.mode import mode_current

    interaction = _make_text_channel_interaction()
    interaction.client = _make_bot_with_session_manager()

    await mode_current.callback(interaction)

    _assert_thread_refusal(interaction)


@pytest.mark.asyncio
async def test_health_in_text_channel_refuses_with_thread_hint() -> None:
    """``/health`` in a TextChannel must refuse with USE_IN_THREAD_MESSAGE.

    ``/health`` is a slightly different surface than the /mode subcommands:
    it builds an embed first and only then gates behind resolve_session_id.
    The refusal must still fire (the embed must not be sent), so we assert
    on send_message exclusively — followup.send must NOT be called.
    """
    from clauded.cogs.ops import health_check

    interaction = _make_text_channel_interaction()
    interaction.client = _make_bot_with_session_manager()
    # Make list_sessions safe (called before the refusal branch).
    interaction.client.session_manager.list_sessions = MagicMock(return_value=[])

    await health_check.callback(interaction)

    _assert_thread_refusal(interaction)
    # Belt-and-suspenders: the embed-success path must NOT have been taken.
    # (send_message was called with the refusal string, not an embed kwarg.)
    call = interaction.response.send_message.await_args
    assert call.kwargs.get("embed") is None, (
        "/health refusal path must not also send the health embed"
    )


@pytest.mark.asyncio
async def test_notify_in_text_channel_refuses_with_thread_hint() -> None:
    """``/notify`` in a TextChannel must refuse with USE_IN_THREAD_MESSAGE.

    Critical because ``_notify_enabled`` is a per-thread dict; a no-op
    write under channel.id (the pre-fix behavior) would be silent and
    never read back. This test pins the loud-refusal contract.
    """
    from clauded.cogs.ops import notify_toggle

    interaction = _make_text_channel_interaction()
    bot = _make_bot_with_session_manager()
    interaction.client = bot

    await notify_toggle.callback(interaction)

    _assert_thread_refusal(interaction)
    # And _notify_enabled must remain untouched.
    assert bot._notify_enabled == {}, (
        "/notify refusal path must not mutate _notify_enabled"
    )
