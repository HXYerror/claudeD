"""Persistent Discord UI view: copy-as-text button for rendered table PNGs (#112, #133).

The view is stateless and persistent (``timeout=None`` + stable ``custom_id``).
Markdown source for the table is carried as a ``.md`` sidecar attachment on the
parent message, so the view can be re-registered after a bot restart via
``bot.add_view(CopyTableTextView())`` without needing any external store.
"""

from __future__ import annotations

import io

import discord
from discord import ui


class CopyTableTextView(ui.View):
    """Persistent view exposing a single 'Copy as text' button.

    The button reads the markdown source from the ``.md`` sidecar attachment
    of ``interaction.message`` and replies ephemerally with its contents
    (inline code-fenced for short tables, file attachment for long ones).
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)  # persistent across bot restarts

    @ui.button(
        label="📋 Copy as text",
        style=discord.ButtonStyle.secondary,
        custom_id="copy_table_text",  # stable id required for persistence
    )
    async def copy(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        # Filename must start with ``table_`` AND end with ``.md`` — this
        # prevents collision with the long-upload fallback message which is
        # named ``claude-response.md`` (review I7). Without the prefix
        # guard, a Copy button on a multi-attachment message could fire
        # against the wrong sidecar.
        md_attachment = next(
            (
                a
                for a in interaction.message.attachments
                if a.filename.startswith("table_") and a.filename.endswith(".md")
            ),
            None,
        )
        if md_attachment is None:
            await interaction.response.send_message(
                "❌ Couldn't find markdown source attachment.",
                ephemeral=True,
            )
            return

        content_bytes = await md_attachment.read()
        text = content_bytes.decode("utf-8", errors="replace")

        if len(text) <= 1900:
            await interaction.response.send_message(
                f"```\n{text}\n```",
                ephemeral=True,
            )
        else:
            f = discord.File(io.BytesIO(content_bytes), filename="table.md")
            await interaction.response.send_message(
                "Markdown source attached:",
                file=f,
                ephemeral=True,
            )
