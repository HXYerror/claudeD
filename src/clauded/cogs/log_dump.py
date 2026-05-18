"""/log dump slash command (#224 epic Subtask 3).

Lets an operator hit ``/log dump`` to generate a diagnostic bundle and
attach the zip to the current thread.

Behavior:
- defer (work > 3s)
- run :func:`clauded.diagnostics.bundle.generate_bundle` in a thread
  executor so the event loop stays responsive
- followup with the bundle as an attachment
- on failure: ephemeral error message (no traceback leak)
"""
from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path

import discord
from discord import app_commands

from ..diagnostics import bundle as bundle_mod

log = logging.getLogger("clauded.cogs.log_dump")


log_group = app_commands.Group(
    name="log",
    description="Diagnostic log tools",
)


@log_group.command(
    name="dump",
    description="Generate a diagnostic bundle and attach it to this thread.",
)
async def log_dump(interaction: discord.Interaction) -> None:
    """Build + upload a /log dump bundle to the current thread."""
    # ``thinking=True`` shows a "Bot is thinking..." indicator while we
    # work. Bundle generation is mostly IO + small JSON; should be <5s.
    await interaction.response.defer(thinking=True, ephemeral=False)

    bot = interaction.client
    # Run bundle assembly in an executor so disk IO + zip compression
    # don't block the event loop.
    loop = asyncio.get_running_loop()
    try:
        out_path: Path = await loop.run_in_executor(
            None,
            functools.partial(
                bundle_mod.generate_bundle,
                bot=bot,
                generated_by="slash",
            ),
        )
    except Exception as exc:
        log.exception("#224: /log dump bundle generation failed")
        await interaction.followup.send(
            f"\u274c Failed to generate bundle: `{type(exc).__name__}`",
            ephemeral=True,
        )
        return

    try:
        size_kb = round(out_path.stat().st_size / 1024, 1)
    except OSError:
        size_kb = -1
    try:
        await interaction.followup.send(
            content=(
                f"\ud83d\udccb Diagnostic bundle (`{size_kb} KB`) \u2014 "
                f"send to PM for analysis."
            ),
            file=discord.File(out_path),
        )
    except Exception as exc:
        log.exception("#224: /log dump upload failed")
        await interaction.followup.send(
            f"\u274c Bundle generated at `{out_path}` but upload failed: "
            f"`{type(exc).__name__}`",
            ephemeral=True,
        )
