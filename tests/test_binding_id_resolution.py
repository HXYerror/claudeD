"""Regression tests for #209 — ``resolve_binding_id`` helper + 7-site fix.

#209 is the mirror of #197: a thread inherits its parent channel's
project binding, so all cog sites that touch ``ProjectManager`` state
(``is_bound``, ``get_path``, ``bind``, ``unbind``, ``set_channel_mode``,
``get_extra_dirs``, ``remove_extra_dir``, ``get_project``) must resolve
the channel id via the new :func:`resolve_binding_id` helper that walks
thread → parent_id, not via raw ``interaction.channel_id``.

This file covers:

1. Unit tests for ``resolve_binding_id`` itself (thread / bare-channel / None).
2. Integration tests for the 7 previously-bugged cog sites (``/project bind``
   in thread asserts ``pm.bind(parent_id, ...)``; ``/project info`` in thread
   asserts all 5 ProjectManager sub-calls receive parent_id; ``/diff`` in
   thread asserts ``pm.get_path(parent_id)``).
3. **Audit test** (the most important one): a substring grep over every
   cog file that fails CI if any call to ``bot.project_manager.<method>``
   sources its id from a variable literally named ``channel_id`` (the
   raw-thread-id footgun). The test forces every cog to flow through
   ``resolve_binding_id`` (or any var named ``binding_id``); a developer
   accidentally reverting a fix to ``project_manager.bind(channel_id, ...)``
   will trip it.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.cogs._unbound import resolve_binding_id
from clauded.cogs.diff import diff_cmd
from clauded.cogs.project import project_bind, project_info
from clauded.project_manager import ProjectManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def pm(tmp_path: Path, projects_root: Path) -> ProjectManager:
    return ProjectManager(
        data_dir=str(tmp_path / "data"), projects_root=str(projects_root)
    )


def _make_thread_interaction(*, thread_id: int, parent_id: int) -> MagicMock:
    """Build an Interaction whose channel is a thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = thread
    interaction.channel_id = thread_id
    interaction.guild_id = 7777
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_text_channel_interaction(*, channel_id: int) -> MagicMock:
    """Build an Interaction whose channel is a top-level TextChannel."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    # TextChannel has no parent_id attribute; getattr(...) returns None.
    # Explicitly remove parent_id so getattr falls through to default.
    del channel.parent_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = channel_id
    interaction.guild_id = 7777
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# Unit tests for resolve_binding_id
# ---------------------------------------------------------------------------


def test_resolve_binding_id_thread_returns_parent_id() -> None:
    """In a thread, resolve_binding_id returns the parent channel's id."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = 999
    thread.parent_id = 555
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = thread
    interaction.channel_id = 999

    assert resolve_binding_id(interaction) == 555


def test_resolve_binding_id_bare_channel_returns_channel_id() -> None:
    """In a bare TextChannel (no parent_id), returns channel.id."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 444
    # spec=discord.TextChannel gives us a MagicMock with TextChannel's
    # attrs only; parent_id isn't one of them, so getattr returns the
    # default in the helper.
    del channel.parent_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = 444

    assert resolve_binding_id(interaction) == 444


def test_resolve_binding_id_none_channel_returns_channel_id_fallback() -> None:
    """When ``interaction.channel`` is None (DM / cache miss), falls
    back to ``interaction.channel_id`` (which itself may be None)."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = None
    interaction.channel_id = None

    assert resolve_binding_id(interaction) is None


def test_resolve_binding_id_none_channel_with_channel_id_returns_id() -> None:
    """If channel is None but channel_id is populated (rare cache case),
    return that id rather than crashing."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = None
    interaction.channel_id = 777

    assert resolve_binding_id(interaction) == 777


# ---------------------------------------------------------------------------
# Integration: 7-site fix verification (in-thread → uses parent_id)
# ---------------------------------------------------------------------------


def _make_bot(pm: ProjectManager) -> MagicMock:
    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = pm
    return bot


@pytest.mark.asyncio
async def test_project_bind_in_thread_writes_to_parent(
    pm: ProjectManager, projects_root: Path
) -> None:
    """/project bind in a thread must call pm.bind(parent_id, ...) — was
    the headline bug of #209 (would silently write thread.id → path,
    never read back)."""
    parent_id, thread_id = 10001, 10002
    proj = projects_root / "headline_bug"
    proj.mkdir()

    interaction = _make_thread_interaction(thread_id=thread_id, parent_id=parent_id)
    bot = _make_bot(pm)
    interaction.client = bot

    await project_bind.callback(interaction, path=str(proj))

    # Binding lives under the PARENT row, not the thread row.
    assert pm.is_bound(parent_id) is True
    assert pm.is_bound(thread_id) is False
    assert pm.get_path(parent_id) == str(proj.resolve())


@pytest.mark.asyncio
async def test_project_info_in_thread_reads_from_parent(
    pm: ProjectManager, projects_root: Path
) -> None:
    """/project info in a thread must read all 5 ProjectManager sub-calls
    (get_project, get_system_prompt, get_channel_mode, get_mention_required,
    get_guild_root indirectly) from the parent row, not the thread row.

    Verified by binding only the parent and asserting the info command
    surfaces that binding (it would say "not bound" if it hit thread.id).
    """
    parent_id, thread_id = 11001, 11002
    proj = projects_root / "info_test"
    proj.mkdir()
    pm.bind(parent_id, str(proj))
    pm.set_system_prompt(parent_id, "test prompt")

    interaction = _make_thread_interaction(thread_id=thread_id, parent_id=parent_id)
    bot = _make_bot(pm)
    interaction.client = bot

    # Spy on the methods to verify each call receives parent_id, not thread_id.
    pm_spy = MagicMock(wraps=pm)
    bot.project_manager = pm_spy

    await project_info.callback(interaction)

    # /project info must call each method with parent_id.
    pm_spy.get_project.assert_called_with(parent_id)
    pm_spy.get_system_prompt.assert_called_with(parent_id)
    pm_spy.get_channel_mode.assert_called_with(parent_id)
    pm_spy.get_mention_required.assert_called_with(parent_id)
    # NONE of these should have ever been called with the thread id.
    for call in pm_spy.get_project.call_args_list:
        assert call.args[0] != thread_id
    for call in pm_spy.get_system_prompt.call_args_list:
        assert call.args[0] != thread_id


@pytest.mark.asyncio
async def test_diff_in_thread_resolves_parent_path(
    pm: ProjectManager, projects_root: Path
) -> None:
    """/diff in a thread must call pm.get_path(parent_id) so the diff
    runs in the parent's bound directory (not a non-existent thread.id
    row that would self-contradict reject_if_unbound)."""
    parent_id, thread_id = 12001, 12002
    proj = projects_root / "diff_test"
    proj.mkdir()
    pm.bind(parent_id, str(proj))

    interaction = _make_thread_interaction(thread_id=thread_id, parent_id=parent_id)
    bot = _make_bot(pm)
    interaction.client = bot

    pm_spy = MagicMock(wraps=pm)
    bot.project_manager = pm_spy

    # _run_git_diff would spawn a real subprocess; short-circuit by
    # patching it to return "no changes" so /diff returns cleanly.
    import clauded.cogs.diff as diff_mod
    orig_run = diff_mod._run_git_diff

    async def _fake_run(cwd: str, staged: bool):
        return 0, "", ""

    diff_mod._run_git_diff = _fake_run
    try:
        await diff_cmd.callback(interaction)
    finally:
        diff_mod._run_git_diff = orig_run

    # get_path was called with parent_id, never with thread_id.
    assert any(call.args[0] == parent_id for call in pm_spy.get_path.call_args_list)
    assert all(call.args[0] != thread_id for call in pm_spy.get_path.call_args_list)


# ---------------------------------------------------------------------------
# Audit test (THE critical one — manager will manually revert one site
# pre-commit and confirm this fails, then revert the revert)
# ---------------------------------------------------------------------------


def test_no_cog_passes_raw_channel_id_to_project_manager() -> None:
    """Lint test: every cog call to ``bot.project_manager.<method>(...)``
    must source the channel id from ``resolve_binding_id`` — i.e. from a
    variable named ``binding_id``, never ``channel_id``.

    This is a source-level substring grep (not AST) because perfect is the
    enemy of good for this defensive lint. Forbidden pattern:
    ``project_manager.<method>(channel_id`` (any whitespace before
    ``channel_id``). Allowed: ``project_manager.<method>(binding_id``.

    Manual verification protocol (executed pre-commit, documented in PR):
      1. Temporarily revert one fix site to ``bot.project_manager.bind(channel_id, ...)``
      2. Run this test — it MUST fail with a violation pointing at that line.
      3. Revert the revert; this test passes again.
    """
    cogs_dir = Path(__file__).resolve().parent.parent / "src" / "clauded" / "cogs"
    forbidden_pattern = re.compile(
        r"project_manager\.\w+\(\s*channel_id\b",
        re.MULTILINE,
    )
    violations = []
    for cog_file in cogs_dir.glob("*.py"):
        text = cog_file.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if forbidden_pattern.search(line):
                violations.append(f"{cog_file.name}:{line_no}: {line.strip()}")
    assert not violations, (
        "Cogs must use resolve_binding_id(interaction) (variable named "
        "`binding_id`), not raw `channel_id`, when calling "
        "project_manager.*. Violations:\n" + "\n".join(violations)
    )
