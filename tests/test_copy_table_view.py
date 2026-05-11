"""Tests for CopyTableTextView persistent button (#112, #133).

The view is stateless: it reads the markdown source from the parent message's
``.md`` sidecar attachment and replies ephemerally. Tests use ``AsyncMock`` for
the async ``attachment.read`` and ``interaction.response.send_message``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.cogs._table_view import CopyTableTextView


def _make_attachment(filename: str, content: bytes) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.read = AsyncMock(return_value=content)
    return att


def _make_interaction(attachments: list) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.message = MagicMock()
    inter.message.attachments = attachments
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_copy_returns_ephemeral_text_for_short_table() -> None:
    md_source = "| a | b |\n|---|---|\n| 1 | 2 |"
    png = _make_attachment("table_0.png", b"\x89PNG...")
    md = _make_attachment("table_0.md", md_source.encode("utf-8"))
    inter = _make_interaction([png, md])

    view = CopyTableTextView()
    button = MagicMock()
    # Unwrap the _ItemCallback wrapper installed by @ui.button
    await view.copy.callback.callback(view, inter, button)

    inter.response.send_message.assert_awaited_once()
    _, kwargs = inter.response.send_message.call_args
    args = inter.response.send_message.call_args.args
    sent_content = args[0] if args else kwargs.get("content")
    assert sent_content == f"```\n{md_source}\n```"
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_copy_returns_file_for_long_table() -> None:
    # > 1900 chars triggers the file path
    md_source = "x" * 2000
    png = _make_attachment("table_0.png", b"\x89PNG...")
    md = _make_attachment("table_0.md", md_source.encode("utf-8"))
    inter = _make_interaction([png, md])

    view = CopyTableTextView()
    button = MagicMock()
    await view.copy.callback.callback(view, inter, button)

    inter.response.send_message.assert_awaited_once()
    _, kwargs = inter.response.send_message.call_args
    assert "file" in kwargs
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_copy_handles_missing_attachment_gracefully() -> None:
    png = _make_attachment("table_0.png", b"\x89PNG...")
    inter = _make_interaction([png])  # no .md sidecar

    view = CopyTableTextView()
    button = MagicMock()
    await view.copy.callback.callback(view, inter, button)

    inter.response.send_message.assert_awaited_once()
    args = inter.response.send_message.call_args.args
    kwargs = inter.response.send_message.call_args.kwargs
    sent_content = args[0] if args else kwargs.get("content")
    assert "Couldn't find" in sent_content or "couldn't find" in sent_content.lower()
    assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# Review I7: filename prefix-match — only ``table_*.md`` sidecars match.
# A ``claude-response.md`` (long-upload fallback) on the same message
# must NOT trigger the Copy button against the wrong file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_ignores_non_table_md_attachments() -> None:
    """``claude-response.md`` is the long-upload fallback name; it must
    not satisfy the Copy-as-text view's attachment lookup (review I7)."""
    long_upload = _make_attachment(
        "claude-response.md", b"this is the long-upload, not a table sidecar"
    )
    inter = _make_interaction([long_upload])

    view = CopyTableTextView()
    button = MagicMock()
    await view.copy.callback.callback(view, inter, button)

    inter.response.send_message.assert_awaited_once()
    args = inter.response.send_message.call_args.args
    kwargs = inter.response.send_message.call_args.kwargs
    sent_content = args[0] if args else kwargs.get("content")
    # Falls through to the "couldn't find" branch — never reads the
    # claude-response.md by mistake.
    assert "Couldn't find" in sent_content or "couldn't find" in sent_content.lower()
    long_upload.read.assert_not_awaited()
