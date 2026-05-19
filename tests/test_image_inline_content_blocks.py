"""#242 round 2 — inline image content blocks.

Pins the new image-attachment flow:

- Images (png/jpg/jpeg/gif/webp) → returned as `list[dict]` content
  blocks: image source first, then text block at the end
- Non-image attachments (pdf/zip/.py/etc.) → return `str` with
  `[User attached file: X]` path-in-text hint
- BMP / SVG (image extensions but not Anthropic-vision-supported) →
  fall back to path-in-text
- `claude_bridge.send_message` accepts str | list[dict]; list path
  wraps in `client.query(AsyncIterable[dict])`

Spike-verified working end-to-end (see #242 comment "Spike 3"); this
file pins the contract so refactors can't silently regress.
"""
from __future__ import annotations

import base64
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_mock_message(
    *,
    content: str = "",
    attachments: list = None,
):
    """Build a discord.Message-shaped MagicMock for _compose_user_text."""
    import discord
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    msg.attachments = attachments or []
    return msg


def _make_fake_attachment(*, name: str, payload: bytes):
    """A `discord.Attachment` impostor that writes `payload` on .save()."""
    att = MagicMock()
    att.filename = name
    att.id = abs(hash(name)) % 10_000_000
    async def _save(target):
        Path(target).write_bytes(payload)
    att.save = AsyncMock(side_effect=_save)
    return att


# ---------------------------------------------------------------------------
# _compose_user_text — image vs non-image dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_no_attachments_returns_plain_str():
    """No attachments → return (str, None) — legacy text-only path."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    msg = _build_mock_message(content="hello world")
    content, tmp_dir = await bot._compose_user_text(msg)
    assert content == "hello world"
    assert tmp_dir is None


@pytest.mark.asyncio
async def test_compose_png_returns_content_blocks():
    """PNG attachment → list[dict] with image block + text block."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # fake PNG header + filler
    att = _make_fake_attachment(name="photo.png", payload=png_bytes)
    msg = _build_mock_message(content="what's in this?", attachments=[att])
    content, tmp_dir = await bot._compose_user_text(msg)
    assert isinstance(content, list), f"expected list, got {type(content).__name__}"
    # image block + text block
    types = [b.get("type") for b in content]
    assert types == ["image", "text"], f"expected [image, text], got {types}"
    image_blk = content[0]
    assert image_blk["source"]["type"] == "base64"
    assert image_blk["source"]["media_type"] == "image/png"
    # Verify the base64 decodes back to our exact bytes
    decoded = base64.b64decode(image_blk["source"]["data"])
    assert decoded == png_bytes
    # Text block carries the original user prose
    assert content[1]["text"] == "what's in this?"
    assert tmp_dir is not None


@pytest.mark.asyncio
async def test_compose_multiple_images_one_text_block():
    """Multiple image attachments → multiple image blocks + ONE text block."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    a = _make_fake_attachment(name="a.png", payload=b"\x89PNG\r\n\x1a\n\x00" * 4)
    b = _make_fake_attachment(name="b.jpg", payload=b"\xff\xd8\xff\xe0" * 4)
    c = _make_fake_attachment(name="c.webp", payload=b"RIFFxxxxWEBP" + b"\x00" * 8)
    msg = _build_mock_message(content="compare these", attachments=[a, b, c])
    content, tmp_dir = await bot._compose_user_text(msg)
    assert isinstance(content, list)
    types = [b.get("type") for b in content]
    # 3 images then 1 text
    assert types == ["image", "image", "image", "text"]
    # Verify media types map correctly
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[2]["source"]["media_type"] == "image/webp"
    assert content[3]["text"] == "compare these"


@pytest.mark.asyncio
async def test_compose_pdf_stays_path_in_text():
    """PDF (non-image) → still path-in-text str return."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    att = _make_fake_attachment(name="doc.pdf", payload=b"%PDF-1.4\n" + b"\x00" * 32)
    msg = _build_mock_message(content="summarise this pdf", attachments=[att])
    content, tmp_dir = await bot._compose_user_text(msg)
    # Non-image → returns str, not list
    assert isinstance(content, str), f"expected str, got {type(content).__name__}"
    assert "doc.pdf" in content
    assert "[User attached file:" in content
    assert "summarise this pdf" in content


@pytest.mark.asyncio
async def test_compose_mixed_image_and_pdf():
    """Image + PDF: list[dict] with image block + text block holding both
    the PDF path-in-text AND the user prose."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    img = _make_fake_attachment(name="x.png", payload=b"\x89PNG\r\n\x1a\n\x00" * 4)
    pdf = _make_fake_attachment(name="report.pdf", payload=b"%PDF-1.4\n" + b"\x00" * 32)
    msg = _build_mock_message(content="match", attachments=[img, pdf])
    content, _ = await bot._compose_user_text(msg)
    assert isinstance(content, list)
    # 1 image + 1 text
    types = [b.get("type") for b in content]
    assert types == ["image", "text"]
    text_value = content[1]["text"]
    assert "report.pdf" in text_value, "PDF path-in-text must be in the text block"
    assert "[User attached file:" in text_value
    assert "match" in text_value, "user prose must also be in text block"


@pytest.mark.asyncio
async def test_compose_bmp_and_svg_path_in_text_not_inline():
    """BMP / SVG: image-ish extensions but NOT Anthropic-vision-supported.
    Must fall back to path-in-text (claude can decide to Read)."""
    from clauded.bot import ClaudedBot
    bot = ClaudedBot.__new__(ClaudedBot)
    bmp = _make_fake_attachment(name="x.bmp", payload=b"BM" + b"\x00" * 32)
    svg = _make_fake_attachment(name="y.svg", payload=b"<svg></svg>")
    msg = _build_mock_message(content="look", attachments=[bmp, svg])
    content, _ = await bot._compose_user_text(msg)
    # No vision-inline images → returns str (legacy path)
    assert isinstance(content, str), f"BMP/SVG should not be inlined as image block; got {type(content).__name__}"
    assert "x.bmp" in content
    assert "y.svg" in content
    assert "[User attached image:" in content


# ---------------------------------------------------------------------------
# claude_bridge.send_message — accepts str | list[dict]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_str_path_uses_plain_query():
    """str content goes to `client.query(str)` directly."""
    from clauded.claude_bridge import ClaudeBridge
    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._client = MagicMock()
    bridge._active = True
    bridge._last_activity = 0

    captured = []
    async def _query(arg):
        captured.append(("query", arg))
    bridge._client.query = _query
    async def _receive():
        return
        yield  # pragma: no cover
    bridge._client.receive_response = _receive

    async for _ in bridge.send_message("hello plain"):
        pass

    assert captured == [("query", "hello plain")]


@pytest.mark.asyncio
async def test_send_message_list_path_wraps_in_async_iterable():
    """list[dict] content goes through AsyncIterable-of-raw-message envelope."""
    from clauded.claude_bridge import ClaudeBridge
    bridge = ClaudeBridge.__new__(ClaudeBridge)
    bridge._client = MagicMock()
    bridge._active = True
    bridge._last_activity = 0

    captured_envelope = []

    async def _query(arg):
        # If `arg` is an async iterable, drain it and capture yields
        if hasattr(arg, "__aiter__"):
            async for item in arg:
                captured_envelope.append(item)
        else:
            captured_envelope.append(("STRING_PATH", arg))

    bridge._client.query = _query
    async def _receive():
        return
        yield  # pragma: no cover
    bridge._client.receive_response = _receive

    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "<b64>"}},
        {"type": "text", "text": "what is this?"},
    ]
    async for _ in bridge.send_message(blocks):
        pass

    # Should have yielded exactly one envelope
    assert len(captured_envelope) == 1
    env = captured_envelope[0]
    assert env["type"] == "user"
    assert env["message"]["role"] == "user"
    assert env["message"]["content"] == blocks


# ---------------------------------------------------------------------------
# _image_preprocess is gone
# ---------------------------------------------------------------------------


def test_image_preprocess_module_removed():
    """#242 round 2: deleted _image_preprocess.py per user spec."""
    import importlib.util
    spec = importlib.util.find_spec("clauded._image_preprocess")
    assert spec is None, "clauded._image_preprocess should not exist anymore"


def test_bot_no_longer_imports_maybe_shrink_image():
    """bot.py must not reference maybe_shrink_image."""
    from clauded import bot
    src = inspect.getsource(bot)
    assert "maybe_shrink_image" not in src, (
        "bot.py still calls maybe_shrink_image; #242 round 2 spec says remove it"
    )
    assert "_image_preprocess" not in src, (
        "bot.py still imports _image_preprocess module"
    )


# ---------------------------------------------------------------------------
# Vision media-type allowlist
# ---------------------------------------------------------------------------


def test_vision_inline_extensions_anthropic_supported_only():
    """Only Anthropic-vision-supported extensions go inline."""
    from clauded.bot import _VISION_INLINE_EXTENSIONS, _VISION_MEDIA_TYPE
    # Anthropic supports png, jpeg, gif, webp
    assert _VISION_INLINE_EXTENSIONS == {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    # Every inline ext has a corresponding media type mapping
    for ext in _VISION_INLINE_EXTENSIONS:
        assert ext in _VISION_MEDIA_TYPE, f"{ext} missing media_type mapping"


def test_vision_media_types_correct():
    """Media-type strings match Anthropic API contract."""
    from clauded.bot import _VISION_MEDIA_TYPE
    assert _VISION_MEDIA_TYPE[".png"] == "image/png"
    assert _VISION_MEDIA_TYPE[".jpg"] == "image/jpeg"
    assert _VISION_MEDIA_TYPE[".jpeg"] == "image/jpeg"
    assert _VISION_MEDIA_TYPE[".gif"] == "image/gif"
    assert _VISION_MEDIA_TYPE[".webp"] == "image/webp"
