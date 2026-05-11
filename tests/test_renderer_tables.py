"""Integration tests for v1.12 table PNG rendering (PRD R2 + R3.3).

These tests pin the wiring between the renderer pipeline and the table-
extraction path:

1. ``_flush`` with a buffer containing prose-before + a table + prose-after
   sends three messages in order: text-before, PNG follow-up, text-after.
   This is the PRD R2.2 interleaving contract (review C1).
2. ``_flush`` with a buffer that has no tables sends a single text
   message and never invokes the PNG follow-up path.
3. A markdown table nested inside a ``` code fence is NOT extracted —
   it's left intact in the text body, and no PNG message is emitted.
4. ``CopyTableTextView`` is a persistent view with the right custom_id
   (review simplicity: lightweight replacement for the prior heavy
   ``setup_hook`` integration test).
"""

from __future__ import annotations

import io
import logging

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
# 1. Buffer with a table → interleaved text-before, PNG, text-after
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_with_table_sends_text_then_png_followup():
    """Buffer = prose-before + table + prose-after → 3 sends in PRD R2.2 order.

    Pre-fix the call sequence was [text-with-placeholder, PNG] and the
    user saw ``[TABLE_PNG_0]`` literally in the text body (review C1).
    Post-fix it's [text-before, PNG, text-after] with no placeholder text
    in any user-visible content.
    """
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

    # PRD R2.2 — three messages: text-before, PNG, text-after.
    assert len(target.calls) == 3
    pre_text_call, png_call, post_text_call = target.calls

    # ----- Pre-table text -----
    pre_content = pre_text_call.get("content") or ""
    assert "Here is a comparison" in pre_content
    # No placeholder leak (review C1 assertion).
    assert "[TABLE_PNG_" not in pre_content, "placeholder leaked to Discord"
    # No raw table rows in the prose body.
    assert "| Alpha | 10 |" not in pre_content
    assert "| Beta | 7 |" not in pre_content
    assert pre_text_call.get("files") is None
    assert pre_text_call.get("view") is None

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

    # ----- Post-table text -----
    post_content = post_text_call.get("content") or ""
    assert "Pick whichever fits." in post_content
    assert "[TABLE_PNG_" not in post_content, "placeholder leaked to Discord"
    assert post_text_call.get("files") is None
    assert post_text_call.get("view") is None

    # ``_last_msg`` ends pointing at the final text send (the prose tail).
    assert renderer._last_msg is target.messages[-1]
    # Shadow tracks the final text content so the cost-footer splicer can
    # safely append onto the prose tail.
    assert renderer._last_msg_text == post_content


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
    assert "[TABLE_PNG_" not in text, "placeholder leaked to Discord"
    # And no follow-up attachment / view was sent.
    assert only.get("files") is None
    assert only.get("view") is None


# ---------------------------------------------------------------------------
# 4. CopyTableTextView has persistent custom_id
# ---------------------------------------------------------------------------


def test_copy_view_has_persistent_custom_id():
    """Lightweight pin for PRD R3.3: the Copy-as-text view is persistent
    (``timeout=None``) and its button uses the stable ``custom_id`` that
    ``bot.setup_hook``'s ``add_view`` global handler dispatches against.

    Replaces the heavier ``test_persistent_view_registered_at_startup``
    which had to drive a half-initialised ``ClaudedBot`` instance.
    """
    view = CopyTableTextView()
    assert view.timeout is None  # persistent across restarts
    custom_ids = [getattr(c, "custom_id", None) for c in view.children]
    assert "copy_table_text" in custom_ids


# ---------------------------------------------------------------------------
# 5. Long-upload .md path re-splices markdown source (review C3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_upload_md_contains_table_source_not_placeholder(monkeypatch):
    """When ``_flush`` falls back to ``claude-response.md`` upload, the
    file body must contain the original markdown table — NOT a leaked
    ``[TABLE_PNG_N]`` placeholder (review C3).
    """
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    # Force the >4-chunk long-upload branch.
    monkeypatch.setattr(
        DiscordRenderer,
        "_smart_split",
        staticmethod(lambda *a, **kw: ["c1", "c2", "c3", "c4", "c5"]),
    )

    buffer = (
        "Intro.\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "Outro."
    )

    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    # First send is the upload (content + file), follow-up is the PNG.
    upload_call = next(c for c in target.calls if c.get("file") is not None)
    f = upload_call["file"]
    f.fp.seek(0)
    body = f.fp.read().decode()
    # The .md must NOT carry placeholders.
    assert "[TABLE_PNG_" not in body, "placeholder leaked into .md upload"
    # And the original table markdown is back in the file body.
    assert "| A | B |" in body
    assert "| 1 | 2 |" in body
    # PNG follow-up still went out.
    assert any(c.get("files") for c in target.calls)


# ---------------------------------------------------------------------------
# 6. Render exception falls back to verbatim emit (review C2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_exception_emits_table_verbatim(monkeypatch):
    """If ``render_table_png`` raises, the original table lines must come
    back in the text body (no exception propagates, no silent drop).
    """
    from clauded import discord_renderer as dr_mod

    def _boom(_headers, _rows):
        raise RuntimeError("simulated Pillow failure")

    monkeypatch.setattr(dr_mod, "render_table_png", _boom)

    target = FakeTarget()
    renderer = DiscordRenderer(target)

    buffer = (
        "Before.\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "After."
    )

    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    # Exactly one text message — no PNG follow-up was sent.
    assert all(c.get("files") is None for c in target.calls)
    # The table source survives verbatim in the user-visible text.
    text = " ".join((c.get("content") or "") for c in target.calls)
    assert "Before." in text
    assert "After." in text
    assert "| A | B |" in text
    assert "| 1 | 2 |" in text
    assert "[TABLE_PNG_" not in text, "placeholder leaked on render failure"


# ---------------------------------------------------------------------------
# 7. _send_table_renders logs on permanent drop (review I2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_table_renders_logs_on_permanent_drop(_no_sleep, caplog):
    """If ``_safe_send`` returns ``None`` (permanent failure), the loop
    must log ``ERROR`` so the silent drop becomes visible (review I2).
    """
    from clauded.discord_renderer import TableRender

    target = FakeTarget()
    renderer = DiscordRenderer(target)

    # Stub _safe_send to permanently fail (returns None).
    async def _always_none(*_a, **_kw):
        return None
    renderer._safe_send = _always_none  # type: ignore[assignment]

    fake_render = TableRender(
        headers=["A", "B"],
        rows=[["1", "2"]],
        png_bytes=b"\x89PNGfake",
        markdown_source="| A | B |\n|---|---|\n| 1 | 2 |",
        placeholder="\n[TABLE_PNG_0]\n",
    )

    with caplog.at_level(logging.ERROR, logger="clauded.discord_renderer"):
        await renderer._send_table_renders([fake_render])

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "Table 0" in msg
    assert "PNG send permanently failed" in msg


# ---------------------------------------------------------------------------
# 8. Cost-footer × PNG message contract (review I8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_footer_attaches_to_png_message():
    """Contract pin (manager pick (a)): the cost footer rides the PNG
    message — after ``_flush`` finishes for a buffer that ends in a table,
    ``renderer._last_msg`` is the PNG message and a subsequent
    ``_safe_edit`` of ``_last_msg`` with the footer content overwrites
    that PNG message's content (attachments survive, per discord.py).
    """
    target = FakeTarget()
    renderer = DiscordRenderer(target)

    # Buffer ENDS with a table — no prose tail follows.
    buffer = (
        "Compare:\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )
    await renderer._flush(
        live_msg=None,
        buffer=buffer,
        typewriter=False,
        saw_text=True,
        tool_msgs={},
    )

    # Last call must be the PNG send (files= present).
    last_call = target.calls[-1]
    assert last_call.get("files") is not None, "PNG must be last send"
    assert renderer._last_msg is target.messages[-1]
    # Shadow reset to "" — see _send_table_renders docstring + #113.
    assert renderer._last_msg_text == ""

    # Simulate the cost-footer write path: edit the current _last_msg
    # (a PNG) with the footer string. ``Message.edit(content=…)`` does
    # NOT strip attachments — the test pins that the renderer's shadow
    # tracks the new content for any subsequent splice.
    footer = "-# 💰 $0.10 │ ⏱️ 5.0s"
    ok = await renderer._safe_edit(renderer._last_msg, content=footer)
    assert ok is True
    assert renderer._last_msg.content == footer
