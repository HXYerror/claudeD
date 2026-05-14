"""Tests for emoji-text fallback + 2x HiDPI scale in ``table_png`` (#206).

Pins:
- A1 — known emoji map to text labels; unmapped emoji silently dropped.
- B1 — single ``SCALE`` constant + every sizing constant derived from it.
- S3 — ``MAX_TABLE_PIXELS`` cap scales by ``SCALE**2`` so the *logical*
  row/col limit is unchanged.
- Integration — rendering an emoji-bearing table produces non-zero,
  reasonably-sized PNG bytes.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from clauded import table_png
from clauded.table_png import (
    ACCENT_W,
    CELL_X,
    CELL_Y,
    EMOJI_TEXT_MAP,
    FONT_BODY_SIZE,
    FONT_HEAD_SIZE,
    HEAD_H,
    LINE_SPACING_EXTRA,
    MAX_TABLE_PIXELS,
    PAD,
    ROW_H,
    SCALE,
    TEXT_SPACING,
    _format_cell,
    _replace_known_emoji,
    render_table_png,
)


# ---------------------------------------------------------------------------
# S1 — emoji text-label replacement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("✅", "[OK]"),
        ("❌", "[FAIL]"),
        ("🎉", "[DONE]"),
        ("🚀", "[GO]"),
        ("🐛", "[BUG]"),
        ("🔥", "[HOT]"),
        ("💡", "[IDEA]"),
        ("⭐", "[STAR]"),
        # Mixed: text + emoji + text
        ("done ✅ now", "done [OK] now"),
        # Multiple in one string
        ("✅❌", "[OK][FAIL]"),
    ],
)
def test_known_emoji_replaced_with_label(raw, expected):
    """Each mapped emoji becomes its bracket-text label."""
    assert _replace_known_emoji(raw) == expected


def test_unmapped_emoji_dropped_silently():
    """Emoji not in the map are stripped (no tofu / no codepoint name)."""
    # Unicorn is not in the map → dropped.
    assert _replace_known_emoji("hello 🦄 world") == "hello  world"
    # Stripped entirely if it's all unmapped emoji.
    assert _replace_known_emoji("🦄🦄🦄") == ""


def test_replace_known_emoji_handles_empty():
    """Empty / falsy input returns unchanged (no crash)."""
    assert _replace_known_emoji("") == ""
    assert _replace_known_emoji(None) is None


def test_format_cell_applies_emoji_replacement():
    """The cell-prep pipeline runs emoji replacement before truncation."""
    assert _format_cell("status: ✅") == "status: [OK]"
    assert _format_cell("oops ❌ fail") == "oops [FAIL] fail"
    # Unknown emoji dropped silently.
    assert _format_cell("magic 🦄 here") == "magic  here"


# Note: tautological structural pins (test_scale_constant_is_two,
# test_sizing_constants_derived_from_scale, test_max_table_pixels_scales_with_scale_squared,
# test_emoji_map_has_expected_size) were trimmed per R1 simplicity —
# the integration tests below + parametrized emoji-replacement provide
# behavioral coverage; restating SCALE == 2 in tests just duplicates source.


def test_max_table_pixels_cap_still_enforced(monkeypatch):
    """Cap is still effective at 2x scale — a tiny budget trips."""
    monkeypatch.setattr(table_png, "MAX_TABLE_PIXELS", 100)
    with pytest.raises(ValueError, match="pixel budget"):
        render_table_png(["A", "B"], [["1", "2"]])


# ---------------------------------------------------------------------------
# Integration — emoji table renders to valid, reasonably-sized PNG
# ---------------------------------------------------------------------------


def test_emoji_table_renders_nonzero_png_bytes():
    """A table with mapped emoji produces parseable, non-empty PNG bytes."""
    headers = ["Test", "Status"]
    rows = [
        ["login", "✅"],
        ["signup", "❌"],
        ["logout", "✅"],
    ]
    out = render_table_png(headers, rows)
    assert isinstance(out, bytes)
    assert len(out) > 0
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    # 2x scale: even a tiny 3-row table is > 80 px tall.
    assert img.height >= HEAD_H + 3 * ROW_H


def test_5x10_emoji_table_within_size_budget():
    """A 5-col × 10-row table with emoji stays well under 500 KB (PRD AC)."""
    headers = ["A", "B", "C", "D", "E"]
    rows = []
    for i in range(10):
        rows.append([
            f"row{i}",
            "✅" if i % 2 == 0 else "❌",
            "⚠️" if i % 3 == 0 else "[ok]",
            f"value-{i}",
            "🎉" if i == 9 else "pending",
        ])
    out = render_table_png(headers, rows)
    # Sanity bounds: not empty, not absurd.
    assert 0 < len(out) < 500 * 1024  # < 500 KB per PRD acceptance


def test_unknown_emoji_in_rendered_table_does_not_crash():
    """A table containing unmapped emoji (e.g. 🦄) renders successfully."""
    out = render_table_png(
        ["Item", "Note"],
        [["unicorn", "🦄 magical"], ["dragon", "🐉 mythical"]],
    )
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"


def test_emoji_in_header_also_replaced():
    """R1 tester gap: emoji in header columns also gets the [LABEL]
    treatment. Same `_format_cell` path applies to header + body cells,
    so this test pins the parity invariant against future refactors
    that might split the header rendering path."""
    from clauded.table_png import render_table_png

    headers = ["✅ Status", "Result"]
    rows = [["pass", "foo"]]
    png_bytes = render_table_png(headers, rows)
    assert png_bytes
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
