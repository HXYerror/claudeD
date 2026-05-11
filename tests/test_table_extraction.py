"""Tests for ``DiscordRenderer._extract_and_render_tables`` (v1.12 / #132).

Spec: docs/prd/v1.12-table-rendering.md R1 and issue #132.

Six tests covering well-formed extraction, code-fence preservation, malformed
input, ordering across multiple tables, single-column rejection, and verbatim
preservation of ``markdown_source`` for the Copy-as-text button (#133).
"""
from __future__ import annotations

import io

from PIL import Image

from clauded.discord_renderer import DiscordRenderer, TableRender


def _is_png(b: bytes) -> bool:
    img = Image.open(io.BytesIO(b))
    return img.format == "PNG"


def test_simple_table_extracted():
    """A plain markdown table → 1 TableRender + text-with-placeholder."""
    text = (
        "Here is a table:\n"
        "| Name | Age |\n"
        "|------|-----|\n"
        "| Alice | 30 |\n"
        "| Bob | 25 |\n"
        "End."
    )
    out, renders = DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    r = renders[0]
    assert isinstance(r, TableRender)
    assert r.headers == ["Name", "Age"]
    assert r.rows == [["Alice", "30"], ["Bob", "25"]]
    assert _is_png(r.png_bytes)
    # Placeholder replaces the table in the returned text.
    assert r.placeholder == "\n[TABLE_PNG_0]\n"
    assert r.placeholder in out
    assert "| Alice | 30 |" not in out
    assert "Here is a table:" in out
    assert "End." in out


def test_table_in_code_fence_left_alone():
    """Tables inside ``` fences are passed through verbatim, no extraction."""
    text = (
        "Output:\n"
        "```\n"
        "| id | name |\n"
        "|----|------|\n"
        "| 1  | Alice |\n"
        "```\n"
        "Done."
    )
    out, renders = DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # Text comes back unchanged.
    assert out == text


def test_malformed_table_not_extracted():
    """Header row with no separator row → not a table, verbatim."""
    text = (
        "Heading:\n"
        "| Name | Age |\n"
        "| Alice | 30 |\n"
        "End."
    )
    out, renders = DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # The pipe lines survive untouched.
    assert "| Name | Age |" in out
    assert "| Alice | 30 |" in out


def test_multiple_tables_each_rendered():
    """Two well-formed tables separated by prose → 2 TableRenders, in order."""
    text = (
        "First:\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\nMiddle paragraph.\n\n"
        "Second:\n"
        "| X | Y |\n"
        "|---|---|\n"
        "| 9 | 8 |\n"
        "End."
    )
    out, renders = DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 2
    assert renders[0].placeholder == "\n[TABLE_PNG_0]\n"
    assert renders[1].placeholder == "\n[TABLE_PNG_1]\n"
    assert renders[0].headers == ["A", "B"]
    assert renders[1].headers == ["X", "Y"]
    # Ordering preserved in the output text.
    assert out.index("[TABLE_PNG_0]") < out.index("[TABLE_PNG_1]")
    assert "Middle paragraph." in out


def test_single_column_table_not_extracted():
    """``| Header |`` style single-column tables are emitted verbatim."""
    text = (
        "Single column:\n"
        "| Header |\n"
        "|--------|\n"
        "| only |\n"
        "End."
    )
    out, renders = DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    assert "| Header |" in out
    assert "| only |" in out


def test_markdown_source_preserved():
    """``TableRender.markdown_source`` equals the original table text verbatim."""
    header = "| Col1 | Col2 |"
    sep = "|------|------|"
    row1 = "| a    | b    |"
    row2 = "| c    | d    |"
    text = "Prelude.\n" + "\n".join([header, sep, row1, row2]) + "\nEpilogue."
    _, renders = DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    assert renders[0].markdown_source == "\n".join([header, sep, row1, row2])
