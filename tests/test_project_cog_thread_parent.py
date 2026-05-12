"""Integration tests for eng-2: ``/project system-prompt`` + ``/project add-dir``
must resolve to the parent channel id when run inside a thread.

Threads inherit their parent's bound state; if the cog passed
``interaction.channel_id`` (the thread id) instead of ``parent_id`` to
``set_system_prompt`` / ``add_extra_dir``, state would attach to a row
that's never read back, silently losing data.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.cogs.project import project_add_dir, project_system_prompt
from clauded.project_manager import ProjectManager


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


def _make_thread_interaction(
    *, thread_id: int, parent_id: int
) -> MagicMock:
    """Build an Interaction whose channel is a thread (so the cog must
    consult ``parent_id``).
    """
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
    interaction.response.send_modal = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot(pm: ProjectManager) -> MagicMock:
    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = pm
    return bot


@pytest.mark.asyncio
async def test_project_add_dir_in_thread_attaches_to_parent(
    pm: ProjectManager, projects_root: Path
) -> None:
    """``/project add-dir`` from inside a thread must call
    ``add_extra_dir`` with the parent channel id, not the thread id.
    """
    parent_id, thread_id = 5001, 5002
    proj = projects_root / "p"
    proj.mkdir()
    pm.bind(parent_id, str(proj))

    extra = projects_root / "extra"
    extra.mkdir()

    interaction = _make_thread_interaction(
        thread_id=thread_id, parent_id=parent_id
    )
    bot = _make_bot(pm)
    interaction.client = bot

    await project_add_dir.callback(interaction, path=str(extra))

    # The extra dir must show up under the PARENT row, not the thread row.
    assert str(extra.resolve()) in pm.get_extra_dirs(parent_id)
    assert pm.get_extra_dirs(thread_id) == []


@pytest.mark.asyncio
async def test_project_system_prompt_modal_targets_parent(
    pm: ProjectManager, projects_root: Path
) -> None:
    """``/project system-prompt`` from inside a thread must hand the modal
    a ``channel_id`` equal to the parent. We catch the modal as it's
    dispatched and inspect its private channel_id field.
    """
    parent_id, thread_id = 6001, 6002
    proj = projects_root / "p"
    proj.mkdir()
    pm.bind(parent_id, str(proj))

    interaction = _make_thread_interaction(
        thread_id=thread_id, parent_id=parent_id
    )
    bot = _make_bot(pm)
    interaction.client = bot

    await project_system_prompt.callback(interaction)

    # send_modal received a SystemPromptModal scoped to parent_id.
    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.await_args.args[0]
    # The modal stores the id as ``_channel_id`` (cogs/project.py).
    assert sent_modal._channel_id == parent_id


# ---------------------------------------------------------------------------
# v1.17 #138 — /project set-mention-required (R1 review fixes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_mention_required_in_thread_writes_to_parent(
    pm: ProjectManager, projects_root: Path
) -> None:
    """Invoking the cog inside a thread must persist to the PARENT channel id
    (not the thread id), so the gate at ``bot.py:_handle_channel_message``
    actually reads it back. Engineer R1 critical #2 regression pin."""
    parent_id = 50001
    thread_id = 50002
    proj = projects_root / "boundproj"
    proj.mkdir()
    pm.bind(parent_id, str(proj))

    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = pm
    interaction = _make_thread_interaction(thread_id=thread_id, parent_id=parent_id)
    interaction.client = bot
    interaction.response.send_message = AsyncMock()

    from clauded.cogs.project import project_set_mention_required
    await project_set_mention_required.callback(interaction, False)

    # Wrote to parent_id, not thread_id
    assert pm.get_mention_required(parent_id) is False
    assert pm.get_mention_required(thread_id) is True  # default — never written
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_mention_required_unbound_channel_friendly_error(
    pm: ProjectManager,
) -> None:
    """Unbound channel → ValueError from _assert_bound → friendly ❌ reply.
    Engineer R1 critical #1 regression pin (was catching wrong exception class)."""
    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = pm

    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.id = 60001
    interaction.channel_id = 60001
    interaction.guild_id = 8888
    interaction.client = bot
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    from clauded.cogs.project import project_set_mention_required
    # Note: channel 60001 is NOT bound; _assert_bound raises ValueError;
    # cog must catch it and reply ephemerally rather than propagate.
    await project_set_mention_required.callback(interaction, False)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    msg = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("content", "")
    assert "❌" in msg
    # Channel state untouched
    assert pm.get_mention_required(60001) is True
