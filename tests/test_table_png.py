"""Tests for ``clauded.table_png.render_table_png`` (v1.12 / #131)."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from clauded import table_png
from clauded.table_png import (
    MAX_CELL_CHARS,
    _format_cell,
    render_table_png,
)


def _parse(b: bytes) -> Image.Image:
    """Open PNG bytes; raises if invalid."""
    img = Image.open(io.BytesIO(b))
    assert img.format == "PNG"
    return img


def test_simple_table_renders_valid_png():
    """A vanilla 3-col × 3-row ASCII table produces parseable PNG bytes."""
    headers = ["A", "B", "C"]
    rows = [
        ["a1", "b1", "c1"],
        ["a2", "b2", "c2"],
        ["a3", "b3", "c3"],
    ]
    out = render_table_png(headers, rows)
    assert isinstance(out, bytes) and len(out) > 0
    img = _parse(out)
    # Width / height must be positive (sanity).
    assert img.width > 0 and img.height > 0


def test_cjk_table_renders_no_crash():
    """CJK cells render without exception and produce valid PNG."""
    headers = ["项目", "状态", "说明"]
    rows = [
        ["你好", "完成", "测试中文"],
        ["世界", "进行", "另一行"],
    ]
    out = render_table_png(headers, rows)
    _parse(out)


def test_emoji_table_renders_no_crash():
    """Emoji cells render without exception."""
    headers = ["Mood", "Status", "Note"]
    rows = [
        ["😀", "✅", "happy 🎉"],
        ["😢", "❌", "sad 💔"],
    ]
    out = render_table_png(headers, rows)
    _parse(out)


def test_long_cell_truncated():
    """Cells longer than 120 chars are truncated with an ellipsis."""
    long_text = "x" * 200
    formatted = _format_cell(long_text)
    assert formatted.endswith("…")
    assert len(formatted) == MAX_CELL_CHARS
    # Public renderer must still produce valid PNG with the long cell.
    out = render_table_png(["H"], [[long_text]])
    _parse(out)


def test_backticks_stripped_from_display():
    """Backticks are stripped from cell content for PNG display."""
    assert _format_cell("`effort`") == "effort"
    assert _format_cell("a `b` c") == "a b c"
    # Renderer must still produce valid PNG with backticked cells.
    out = render_table_png(["Col"], [["`effort`"]])
    _parse(out)


def test_pillow_fallback_when_fonts_missing(monkeypatch):
    """If no system font path resolves, fall back to ``load_default``."""
    # Force every truetype lookup to fail.
    monkeypatch.setattr(
        table_png, "FONT_CANDIDATES", ("/nonexistent/font.ttf",)
    )
    out = render_table_png(["A", "B"], [["1", "2"]])
    _parse(out)


def test_empty_rows_does_not_crash():
    """Empty / blank cell content renders without exception."""
    # Empty cell strings.
    out = render_table_png(["A", "B"], [["", ""]])
    _parse(out)
    # Empty row (no cells at all) — renderer pads to header count.
    out = render_table_png(["A", "B"], [[]])
    _parse(out)
    # No rows at all (header-only — still valid output).
    out = render_table_png(["A", "B"], [])
    _parse(out)
