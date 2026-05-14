"""/diff slash command — show git diff of the bound project.

PRD: #163 sub-task 4. Mirrors the bundled Claude CLI's `/diff` semantics:
visualize uncommitted changes in the channel's bound project so users
can review what Claude has changed before committing.

## Behavior

1. Resolve the channel's bound project path (via ``project_manager.get_path``).
2. Run ``git diff`` (unstaged) in that directory.
3. If unstaged diff is empty, fall back to ``git diff --staged``.
4. If both are empty → "No uncommitted changes" message.

## Output tiering

- Empty diff → ephemeral plain text "No uncommitted changes"
- Short (<3500 chars) → embed with ```diff fenced code block
- Long (≥3500 chars) → ``.diff`` file attachment (auto-syntax-highlighted
  by Discord's mobile client on click)

## Errors

- Not a git repo → friendly "Not a git repository" message
- Channel not bound → standard refuse-hint (via ``reject_if_unbound``)
- Subprocess failure → red error embed with class name (no stderr leak)

## Why subprocess and not GitPython

Adding a Python git library (GitPython, pygit2) for a single read-only
command is over-engineering. ``git diff`` via ``asyncio.create_subprocess_exec``
is what users expect from a ``/diff`` command anyway, and we get full
git config / aliases / pager-disable semantics for free.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

import discord
from discord import app_commands

from ..discord_renderer import COLOR_INFO, COLOR_TOOL_FAILURE
from ._unbound import NO_CHANNEL_MESSAGE, reject_if_unbound, resolve_binding_id

log = logging.getLogger("clauded.bot")


_DIFF_EMBED_THRESHOLD = 3500  # Discord embed description hard cap is 4096; leave headroom for code-fence wrapper
_DIFF_SUBPROCESS_TIMEOUT_S = 10.0


async def _run_git_diff(cwd: str, staged: bool) -> tuple[int, str, str]:
    """Run ``git diff`` (or ``git diff --staged``) in cwd.

    Returns (returncode, stdout, stderr). Uses asyncio subprocess to avoid
    blocking the event loop. Times out after ``_DIFF_SUBPROCESS_TIMEOUT_S``
    so a runaway git process (rare but possible with huge diffs or
    filesystem locks) can't stall the slash command.
    """
    args = ["git", "diff", "--no-color"]
    if staged:
        args.append("--staged")
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_PAGER": "cat", "LESS": "FRX"},
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_DIFF_SUBPROCESS_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "git diff timed out"
    return proc.returncode, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")


@app_commands.command(
    name="diff",
    description="Show git diff (unstaged then staged) of the bound project.",
)
async def diff_cmd(interaction: discord.Interaction) -> None:
    """Display the channel's bound project's uncommitted changes.

    Short diffs land as an embed; long diffs ship as a ``.diff`` attachment.
    Falls back to staged-diff if unstaged is empty. Friendly error when
    the path isn't a git repository.
    """
    from ..bot import ClaudedBot

    log.info("/diff channel=%s", interaction.channel_id)
    bot = interaction.client
    if not isinstance(bot, ClaudedBot):
        await interaction.response.send_message("❌ Bot not ready.", ephemeral=True)
        return

    # Refuse on unbound channel — /diff needs a project path. Reuse the
    # shared Group A refusal helper (mention-required toggle / set-mode /
    # set-mention-required / system-prompt all follow the same pattern).
    if await reject_if_unbound(interaction, bot):
        return

    # Resolve bound path. We already know the channel is bound (reject
    # above returned False), so get_path won't return None here. Use
    # resolve_binding_id so threads correctly inherit the parent's path —
    # raw interaction.channel_id would be the thread id in threads, which
    # is never written by /project bind and would self-contradict the
    # reject_if_unbound check above (#209).
    binding_id = resolve_binding_id(interaction)
    if binding_id is None:
        # Defensive — reject_if_unbound already handles None, but the
        # type-checker can't see that.
        await interaction.response.send_message(NO_CHANNEL_MESSAGE, ephemeral=True)
        return
    project_path = bot.project_manager.get_path(binding_id)
    if not project_path:
        await interaction.response.send_message(
            "❌ Channel not bound.", ephemeral=True
        )
        return

    # Subprocess can take a moment for large repos; defer to clear the
    # 3-second interaction deadline.
    await interaction.response.defer(ephemeral=True)

    # Unstaged diff first
    rc, stdout, stderr = await _run_git_diff(project_path, staged=False)
    if rc != 0:
        # rc != 0 is usually "not a git repo" or transient filesystem error
        err_lower = (stderr or "").lower()
        if "not a git" in err_lower or "no such file" in err_lower:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Not a git repository",
                    description=f"`{project_path}` isn't a git working tree.",
                    color=COLOR_TOOL_FAILURE,
                ),
                ephemeral=True,
            )
            return
        # Other failures (rc=-1 = timeout, or genuine error)
        await interaction.followup.send(
            embed=discord.Embed(
                title="❌ git diff failed",
                description=f"`exit={rc}` — see bot.log for details.",
                color=COLOR_TOOL_FAILURE,
            ),
            ephemeral=True,
        )
        log.warning("/diff git failed cwd=%s rc=%s stderr=%s", project_path, rc, stderr[:200])
        return

    diff_text = stdout
    source_label = "unstaged"
    if not diff_text.strip():
        # Try staged diff as a fallback
        rc2, stdout2, _stderr2 = await _run_git_diff(project_path, staged=True)
        if rc2 == 0 and stdout2.strip():
            diff_text = stdout2
            source_label = "staged"

    if not diff_text.strip():
        await interaction.followup.send(
            "✅ No uncommitted changes (working tree + index both clean).",
            ephemeral=True,
        )
        return

    # Tier 1: short → embed with code fence
    if len(diff_text) < _DIFF_EMBED_THRESHOLD:
        # CommonMark §4.5 / Discord rendering: use a 4-backtick outer fence
        # so any 3-backtick sequences inside the diff content (legitimate
        # in diffs of markdown / python / shell files containing code
        # examples) don't close the outer fence early. R1 tester +
        # simplicity flagged this as a fence-escape risk; pinning with a
        # dedicated test (test_diff_short_handles_triple_backticks).
        embed = discord.Embed(
            title=f"📋 Diff ({source_label})",
            description=f"````diff\n{diff_text}\n````",
            color=COLOR_INFO,
        )
        embed.set_footer(text=f"{project_path}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Tier 2: long → file attachment
    file_bytes = diff_text.encode("utf-8", errors="replace")
    file = discord.File(io.BytesIO(file_bytes), filename="changes.diff")
    summary = (
        f"📋 Diff ({source_label}) — {len(diff_text):,} chars, "
        f"{diff_text.count(chr(10))} lines"
    )
    await interaction.followup.send(content=summary, file=file, ephemeral=True)


__all__ = ["diff_cmd"]
