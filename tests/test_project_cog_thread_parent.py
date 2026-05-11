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
