"""Tests for /diff (#163 sub-task 4).

Shows git diff of the bound project. Output tiering:
- Empty → "No uncommitted changes" plain text
- <3500 chars → embed with ```diff fence
- ≥3500 chars → .diff file attachment

Errors: not-a-git-repo, subprocess failure, channel not bound.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from clauded.bot import ClaudedBot
from clauded.config import Config
from clauded.cost_tracker import CostTracker
from clauded.project_manager import ProjectManager
from clauded.session_manager import SessionManager
from clauded.session_store import SessionStore


@pytest.fixture
def bot(tmp_path: Path) -> ClaudedBot:
    cfg = Config(
        discord_bot_token="tok", claude_model="sonnet",
        claude_permission_mode="default", projects_root=str(tmp_path),
        allow_unbound_fallback=False,
    )
    pm = ProjectManager(data_dir=str(tmp_path / "data"), projects_root=str(tmp_path))
    sm = SessionManager(session_store=SessionStore(data_dir=str(tmp_path / "data")))
    bot = ClaudedBot.__new__(ClaudedBot)
    bot.config = cfg
    bot.project_manager = pm
    bot.session_manager = sm
    bot.cost_tracker = CostTracker()
    bot.agent_manager = MagicMock()
    bot._start_time = 0.0
    bot._claude_version = "test"
    bot._debug_logging = False
    bot._pre_tool_notifications = False
    bot._notify_enabled = {}
    bot.allow_unbound_fallback = False
    bot._connection = MagicMock()
    return bot


def _make_interaction(bot: ClaudedBot, channel_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.channel_id = channel_id
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    interaction.channel = ch
    interaction.guild_id = 4242
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_real_git_repo(tmp_path: Path, modify: bool = True) -> Path:
    """Create a real git repo for integration tests; optionally make a modification."""
    repo = tmp_path / "myproj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "foo.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    if modify:
        (repo / "foo.txt").write_text("hello\nworld\n")
    return repo


# ---------------------------------------------------------------------------
# Pure helpers — no SDK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_git_diff_returns_unstaged_changes(tmp_path: Path) -> None:
    """Real git repo with unstaged change → diff output contains '+world'."""
    from clauded.cogs.diff import _run_git_diff
    repo = _make_real_git_repo(tmp_path, modify=True)
    rc, stdout, stderr = await _run_git_diff(str(repo), staged=False)
    assert rc == 0
    assert "+world" in stdout
    assert "foo.txt" in stdout


@pytest.mark.asyncio
async def test_run_git_diff_empty_when_clean(tmp_path: Path) -> None:
    """Clean repo → empty stdout, rc=0."""
    from clauded.cogs.diff import _run_git_diff
    repo = _make_real_git_repo(tmp_path, modify=False)
    rc, stdout, _ = await _run_git_diff(str(repo), staged=False)
    assert rc == 0
    assert stdout.strip() == ""


@pytest.mark.asyncio
async def test_run_git_diff_not_a_repo(tmp_path: Path) -> None:
    """Non-git directory → rc != 0, stderr mentions 'not a git'."""
    from clauded.cogs.diff import _run_git_diff
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    rc, stdout, stderr = await _run_git_diff(str(notrepo), staged=False)
    assert rc != 0
    assert "not a git" in stderr.lower()


# ---------------------------------------------------------------------------
# /diff cog callback — happy path + tiering + errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_returns_no_changes_when_clean(
    bot: ClaudedBot, tmp_path: Path
) -> None:
    """Clean working tree + clean index → 'No uncommitted changes'."""
    from clauded.cogs.diff import diff_cmd
    repo = _make_real_git_repo(tmp_path, modify=False)
    channel_id = 12345
    bot.project_manager.bind(channel_id, str(repo))

    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)
    msg = interaction.followup.send.call_args[0][0]
    assert "No uncommitted changes" in msg


@pytest.mark.asyncio
async def test_diff_short_renders_embed(bot: ClaudedBot, tmp_path: Path) -> None:
    """Short unstaged diff (< 3500 chars) → embed with ```diff fence."""
    from clauded.cogs.diff import diff_cmd
    repo = _make_real_git_repo(tmp_path, modify=True)
    channel_id = 23456
    bot.project_manager.bind(channel_id, str(repo))

    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)
    embed = interaction.followup.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Diff (unstaged)" in embed.title
    assert "```diff" in embed.description
    assert "+world" in embed.description


@pytest.mark.asyncio
async def test_diff_long_attaches_file(bot: ClaudedBot, tmp_path: Path) -> None:
    """Large diff (≥3500 chars) → file attachment, not embed."""
    from clauded.cogs.diff import diff_cmd
    repo = _make_real_git_repo(tmp_path, modify=False)
    # Create a large change
    huge_line = "x" * 100
    big_content = "\n".join([huge_line] * 50)  # ~5000 chars
    (repo / "foo.txt").write_text(big_content)

    channel_id = 34567
    bot.project_manager.bind(channel_id, str(repo))

    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)

    # File attachment was sent
    call_kwargs = interaction.followup.send.call_args.kwargs
    assert "file" in call_kwargs
    file = call_kwargs["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "changes.diff"
    # Embed should NOT be set when attachment is used
    assert "embed" not in call_kwargs


@pytest.mark.asyncio
async def test_diff_falls_back_to_staged_when_unstaged_empty(
    bot: ClaudedBot, tmp_path: Path
) -> None:
    """Unstaged clean but index has staged change → diff shows staged."""
    from clauded.cogs.diff import diff_cmd
    repo = _make_real_git_repo(tmp_path, modify=False)
    # Stage a change
    (repo / "foo.txt").write_text("hello\nstaged-only\n")
    subprocess.run(["git", "add", "foo.txt"], cwd=repo, check=True)
    # Unstaged should now be empty (working tree == index)
    rc_unstaged, out_unstaged, _ = await _make_unstaged_run(repo)
    assert out_unstaged.strip() == ""  # sanity

    channel_id = 45678
    bot.project_manager.bind(channel_id, str(repo))
    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)
    embed = interaction.followup.send.call_args.kwargs.get("embed")
    assert embed is not None, "should render staged diff as embed"
    assert "Diff (staged)" in embed.title
    assert "+staged-only" in embed.description


async def _make_unstaged_run(repo: Path):
    """Helper: run git diff (unstaged) via the same subprocess module."""
    from clauded.cogs.diff import _run_git_diff
    return await _run_git_diff(str(repo), staged=False)


@pytest.mark.asyncio
async def test_diff_not_a_git_repo_friendly_error(
    bot: ClaudedBot, tmp_path: Path
) -> None:
    """Bound path is not a git repo → red 'Not a git repository' embed."""
    from clauded.cogs.diff import diff_cmd
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    channel_id = 56789
    bot.project_manager.bind(channel_id, str(plain_dir))
    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)
    embed = interaction.followup.send.call_args.kwargs.get("embed")
    assert embed is not None
    assert "Not a git repository" in embed.title


@pytest.mark.asyncio
async def test_diff_unbound_channel_refuses(bot: ClaudedBot) -> None:
    """Unbound channel → standard refuse-hint (no git diff attempted)."""
    from clauded.cogs.diff import diff_cmd
    interaction = _make_interaction(bot, channel_id=99999)
    # Bot's config has allow_unbound_fallback=False; channel never bound.
    await diff_cmd.callback(interaction)
    # reject_if_unbound sends the refuse message via response.send_message
    # (NOT followup, because response.is_done() was False on entry)
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args[0][0]
    assert "❌" in msg
    assert "not bound" in msg.lower() or "/project bind" in msg


@pytest.mark.asyncio
async def test_diff_response_is_ephemeral(bot: ClaudedBot, tmp_path: Path) -> None:
    """All response paths use ephemeral=True (admin-style info command)."""
    from clauded.cogs.diff import diff_cmd
    repo = _make_real_git_repo(tmp_path, modify=False)
    channel_id = 67890
    bot.project_manager.bind(channel_id, str(repo))
    interaction = _make_interaction(bot, channel_id)
    await diff_cmd.callback(interaction)
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True
