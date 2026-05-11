"""Integration tests for v1.12 table PNG rendering (#134 / PRD R2 + R3.3).

These tests pin the wiring between the renderer pipeline and the new
table-extraction path:

1. ``_flush`` with a buffer containing prose + a markdown table sends the
   prose text first, then a follow-up message bearing a PNG attachment +
   the ``.md`` sidecar + a :class:`CopyTableTextView` instance.
2. ``_flush`` with a buffer that has no tables sends a single text
   message and never invokes the PNG follow-up path.
3. A markdown table nested inside a ``` code fence is NOT extracted —
   it's left intact in the text body, and no PNG message is emitted.
4. ``ClaudedBot.setup_hook`` registers the persistent
   :class:`CopyTableTextView` via ``self.add_view`` so that button
   clicks keep working across bot restarts (PRD R3.3).
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from PIL import Image

from clauded.cogs._table_view import CopyTableTextView
from clauded.discord_renderer import DiscordRenderer


# ---------------------------------------------------------------------------
# Fakes — mirror the lightweight pattern in test_renderer_retries.py.
# ---------------------------------------------------------------------------


class FakeMessage:
    _next_id = 0

    def __init__(self, content: str = "") -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.content = content
        self.kwargs: dict = {}

    async def edit(self, *, content=None, **kw):
        if content is not None:
            self.content = content
        return self


class FakeTarget:
    """Records every ``send`` call so tests can assert on kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages: list[FakeMessage] = []
        self.guild = None
        self.parent = None
        self.id = 1
        self.name = "fake"

    async def send(self, content=None, **kw) -> FakeMessage:
        call = {"content": content, **kw}
        self.calls.append(call)
        msg = FakeMessage(content or "")
        # Surface the kwargs on the returned message for assertions.
        msg.kwargs = call
        self.messages.append(msg)
        return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip backoff sleeps so tests stay fast."""
    import asyncio

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_bytes_signature_ok(b: bytes) -> bool:
    """Confirm ``b`` is a real PNG by parsing it with Pillow."""
    img = Image.open(io.BytesIO(b))
    return img.format == "PNG"


# ---------------------------------------------------------------------------
# 1. Buffer with a table → text-then-PNG follow-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_with_table_sends_text_then_png_followup():
    """Buffer = prose + table → 2 sends: text first, then PNG + view + .md."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    buffer = (
        "Here is a comparison of two options:\n"
        "| Name | Score |\n"
        "|------|-------|\n"
        "| Alpha | 10 |\n"
        "| Beta | 7 |\n"
        "Pick whichever fits."
    )

    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    # Exactly two messages: text body, then PNG follow-up.
    assert len(target.calls) == 2

    text_call, png_call = target.calls

    # ----- Text message -----
    text_content = text_call.get("content") or ""
    assert "Here is a comparison" in text_content
    assert "Pick whichever fits." in text_content
    # The raw markdown table is NOT present in the text body — it was
    # extracted out (a placeholder may remain).
    assert "| Alpha | 10 |" not in text_content
    assert "| Beta | 7 |" not in text_content
    # The text call carries no PNG attachments.
    assert "files" not in text_call or text_call.get("files") is None
    assert "view" not in text_call or text_call.get("view") is None

    # ----- PNG follow-up -----
    files = png_call.get("files")
    assert files is not None and len(files) == 2
    filenames = sorted(f.filename for f in files)
    assert filenames == ["table_0.md", "table_0.png"]
    # PNG attachment is a real PNG.
    png_file = next(f for f in files if f.filename.endswith(".png"))
    png_file.fp.seek(0)
    assert _png_bytes_signature_ok(png_file.fp.read())
    # Sidecar carries the verbatim markdown source (column pipes intact).
    md_file = next(f for f in files if f.filename.endswith(".md"))
    md_file.fp.seek(0)
    md_src = md_file.fp.read().decode()
    assert "| Alpha | 10 |" in md_src
    assert "| Beta | 7 |" in md_src
    # Persistent view attached.
    view = png_call.get("view")
    assert isinstance(view, CopyTableTextView)
    # _last_msg points at the PNG follow-up; shadow reset to "" so the
    # cost-footer splicer can't reach into the attachment-only message.
    assert renderer._last_msg is target.messages[-1]
    assert renderer._last_msg_text == ""


# ---------------------------------------------------------------------------
# 2. Buffer with no tables → existing fast-path behaviour preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_no_table_unchanged():
    """No table → exactly one send, no files / view, content is original prose."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    buffer = "Just regular prose without any markdown table content."

    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    assert len(target.calls) == 1
    only = target.calls[0]
    assert only.get("content") == buffer
    # No attachments, no view — pure text send.
    assert only.get("files") is None
    assert only.get("file") is None
    assert only.get("view") is None


# ---------------------------------------------------------------------------
# 3. Tables nested in a code fence are not extracted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_in_code_fence_not_replaced():
    """A ``|...|`` block inside ``` fence is left verbatim; no PNG message."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    buffer = (
        "Example output:\n"
        "```\n"
        "| id | label |\n"
        "|----|-------|\n"
        "| 1  | one   |\n"
        "```\n"
        "End of example."
    )

    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    # Exactly one send — no PNG follow-up because the table is fenced.
    assert len(target.calls) == 1
    only = target.calls[0]
    # The fenced table survives verbatim in the text body.
    text = only.get("content") or ""
    assert "| id | label |" in text
    assert "| 1  | one   |" in text
    assert "End of example." in text
    # And no follow-up attachment / view was sent.
    assert only.get("files") is None
    assert only.get("view") is None


# ---------------------------------------------------------------------------
# 4. setup_hook registers the persistent CopyTableTextView
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_view_registered_at_startup():
    """``ClaudedBot.setup_hook`` must call ``add_view(CopyTableTextView())``.

    Required by PRD R3.3 so the Copy-as-text button keeps responding to
    clicks after a bot restart (custom_id=``copy_table_text`` persistent).
    """
    from clauded.bot import ClaudedBot

    # Build a bot instance without going through ``commands.Bot.__init__``
    # (it wants a running event loop + token). We only need the
    # ``setup_hook`` method bound to a real instance so the
    # ``self.add_view(...)`` call exercised below dispatches correctly.
    bot = ClaudedBot.__new__(ClaudedBot)

    add_view_calls: list = []
    bot.add_view = MagicMock(side_effect=lambda v, **kw: add_view_calls.append(v))
    bot._cleanup_task = MagicMock()
    bot._cleanup_task.start = MagicMock()
    # NB: ``ClaudedBot.tree`` is a property inherited from ``commands.Bot``
    # — we can't replace it on a ``__new__``-built instance, but that's
    # fine: the ``add_view`` call we care about runs BEFORE the
    # ``self.tree.add_command(...)`` block, so any failure further down
    # is caught by the ``try/except`` below without falsifying the assert.

    # Side-step the ``claude --version`` subprocess.
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=Exception("no claude"))):
        try:
            await ClaudedBot.setup_hook(bot)
        except Exception:
            # The later slash-command registration / tree.sync block
            # fails on a half-initialised bot; the ``add_view`` invariant
            # is verified BEFORE that block, which is the only thing we
            # care about pinning here.
            pass

    # Exactly one ``add_view`` call, with a CopyTableTextView instance.
    assert len(add_view_calls) == 1, (
        f"expected 1 add_view call, got {len(add_view_calls)}"
    )
    assert isinstance(add_view_calls[0], CopyTableTextView)
    # The view's button uses the persistent custom_id required by R3.3.
    custom_ids = [
        getattr(c, "custom_id", None) for c in add_view_calls[0].children
    ]
    assert "copy_table_text" in custom_ids
