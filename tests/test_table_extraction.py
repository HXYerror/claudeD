"""Tests for ``DiscordRenderer._extract_and_render_tables`` (v1.12).

Spec: docs/prd/v1.12-table-rendering.md R1.

Tests cover well-formed extraction, code-fence preservation, malformed
input, ordering across multiple tables, single-column rejection, verbatim
preservation of ``markdown_source`` for the Copy-as-text button, header-only
rejection (R1.4), empty-body cell preservation (R6.4), and current
"no escape" semantics for ``\\|`` inside cells.

``_extract_and_render_tables`` is async (dispatches PNG render through
``asyncio.to_thread`` — review I3), so tests run under ``pytest.mark.asyncio``.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from clauded.discord_renderer import DiscordRenderer, TableRender


def _is_png(b: bytes) -> bool:
    img = Image.open(io.BytesIO(b))
    return img.format == "PNG"


@pytest.mark.asyncio
async def test_simple_table_extracted():
    """A plain markdown table → 1 TableRender + text-with-placeholder."""
    text = (
        "Here is a table:\n"
        "| Name | Age |\n"
        "|------|-----|\n"
        "| Alice | 30 |\n"
        "| Bob | 25 |\n"
        "End."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
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


@pytest.mark.asyncio
async def test_table_in_code_fence_left_alone():
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
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # Text comes back unchanged.
    assert out == text


@pytest.mark.asyncio
async def test_malformed_table_not_extracted():
    """Header row with no following ``|...|`` line → not a table, verbatim.

    (Note: a header followed by a same-cell-count ``|...|`` row IS now
    accepted under the relaxed shape — see
    ``test_table_without_separator_extracted``. This test pins the case
    where the next line is plain prose so there's nothing to parse as
    either a separator or a data row.)
    """
    text = (
        "Heading:\n"
        "| Name | Age |\n"
        "End."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # The pipe line survives untouched.
    assert "| Name | Age |" in out
    assert "End." in out


@pytest.mark.asyncio
async def test_multiple_tables_each_rendered():
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
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 2
    assert renders[0].placeholder == "\n[TABLE_PNG_0]\n"
    assert renders[1].placeholder == "\n[TABLE_PNG_1]\n"
    assert renders[0].headers == ["A", "B"]
    assert renders[1].headers == ["X", "Y"]
    # Ordering preserved in the output text.
    assert out.index("[TABLE_PNG_0]") < out.index("[TABLE_PNG_1]")
    assert "Middle paragraph." in out


@pytest.mark.asyncio
async def test_single_column_table_not_extracted():
    """``| Header |`` style single-column tables are emitted verbatim."""
    text = (
        "Single column:\n"
        "| Header |\n"
        "|--------|\n"
        "| only |\n"
        "End."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    assert "| Header |" in out
    assert "| only |" in out


@pytest.mark.asyncio
async def test_markdown_source_preserved():
    """``TableRender.markdown_source`` equals the original table text verbatim."""
    header = "| Col1 | Col2 |"
    sep = "|------|------|"
    row1 = "| a    | b    |"
    row2 = "| c    | d    |"
    text = "Prelude.\n" + "\n".join([header, sep, row1, row2]) + "\nEpilogue."
    _, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    assert renders[0].markdown_source == "\n".join([header, sep, row1, row2])


# ---------------------------------------------------------------------------
# Coverage additions (PR #137 round-1 review).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_only_table_not_extracted():
    """PRD R1.4 — a header row + separator row with NO body rows must be
    emitted verbatim (no TableRender, no placeholder). Pins the extractor
    against a future refactor that would render an empty-body table.
    """
    text = (
        "Heading:\n"
        "| Name | Age |\n"
        "|------|-----|\n"
        "End of section."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # All three original lines survive verbatim.
    assert "| Name | Age |" in out
    assert "|------|-----|" in out
    assert "End of section." in out


@pytest.mark.asyncio
async def test_empty_body_cells_preserved():
    """PRD R6.4 — cells that are empty between pipes (``| | x |``) must be
    preserved as empty strings in the parsed row data so PNG rendering
    keeps the column count consistent with the header.
    """
    text = (
        "Mixed:\n"
        "| A | B | C |\n"
        "|---|---|---|\n"
        "|   | x |   |\n"
        "| y |   | z |\n"
        "Done."
    )
    _, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    r = renders[0]
    assert r.headers == ["A", "B", "C"]
    # Empty cells survive as empty strings (not dropped, not None).
    assert r.rows == [["", "x", ""], ["y", "", "z"]]


@pytest.mark.asyncio
async def test_escaped_pipe_in_cell():
    """Document current "no escape" semantics: ``\\|`` inside a cell is
    treated like any other ``|`` and splits the cell. Pinning behaviour so
    a future "support escaped pipes" change is a deliberate decision.
    """
    text = (
        "| Name | Value |\n"
        "|------|-------|\n"
        "| a\\|b | c |\n"
    )
    _, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    r = renders[0]
    # ``a\|b`` splits at the backslash-pipe → the cell becomes 3 fragments,
    # producing a 3-column row even though headers have 2 columns. Pin the
    # observed "no escape" behaviour: PNG renderer's normalisation
    # (truncate/pad to header count) happens later — at extraction we
    # simply split on every ``|``.
    assert r.headers == ["Name", "Value"]
    assert len(r.rows) == 1
    # 3 split fragments — confirms the escape was NOT honoured.
    assert len(r.rows[0]) == 3
    assert r.rows[0] == ["a\\", "b", "c"]


# ---------------------------------------------------------------------------
# Fix 1 (v1.12): relaxed shape — accept tables without a ``|---|---|``
# separator row when the next line is another ``|...|`` with matching
# cell count. Claude SDK frequently emits tables in this shape, so the
# strict matcher was dropping production tables into the legacy
# code-fence fallback path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_without_separator_extracted():
    """Header followed directly by data rows (no ``|---|---|`` separator)
    with matching cell count → 1 TableRender, headers + rows parsed, and
    ``markdown_source`` contains a synthesized separator so the ``.md``
    sidecar stays GFM-valid.
    """
    text = "| a | b |\n| 1 | 2 |\n| 3 | 4 |"
    _, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    r = renders[0]
    assert r.headers == ["a", "b"]
    assert r.rows == [["1", "2"], ["3", "4"]]
    # ``markdown_source`` carries a synthesized 2-column GFM separator
    # so downloads of the ``.md`` sidecar render correctly anywhere
    # GFM is honoured.
    assert "|---|---|" in r.markdown_source
    # And the synthesized separator is positioned between header and rows.
    lines = r.markdown_source.splitlines()
    assert lines[0].strip() == "| a | b |"
    assert lines[1].strip() == "|---|---|"
    assert lines[2].strip() == "| 1 | 2 |"
    assert lines[3].strip() == "| 3 | 4 |"


@pytest.mark.asyncio
async def test_separator_still_recognized_when_present():
    """Strict GFM path (header + separator + rows) is unchanged."""
    text = (
        "| Name | Age |\n"
        "|------|-----|\n"
        "| Alice | 30 |\n"
        "| Bob | 25 |"
    )
    _, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert len(renders) == 1
    r = renders[0]
    assert r.headers == ["Name", "Age"]
    assert r.rows == [["Alice", "30"], ["Bob", "25"]]
    # On the strict path the separator is verbatim from input, NOT the
    # synthesized ``|---|---|`` shape (it has 6-dash spans here).
    assert "|------|-----|" in r.markdown_source


@pytest.mark.asyncio
async def test_no_separator_mismatched_cells_falls_back_verbatim():
    """Header + next ``|...|`` row with different cell count → not a table.

    The cell-count guard prevents the relaxed path from gluing two
    unrelated pipe-shaped lines together. Both lines must survive
    verbatim in the output.
    """
    text = "| a | b | c |\n| 1 | 2 |"
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == []
    # Both lines survive in the output unchanged.
    assert "| a | b | c |" in out
    assert "| 1 | 2 |" in out


# ---------------------------------------------------------------------------
# v1.12 Bug D — quad-backtick outer fence preserves inner table verbatim.
# CommonMark §4.5: a fence opens with N≥3 backticks and only closes on a
# line whose backtick run length is ≥N. The pre-fix tracker toggled on any
# ``startswith("```")`` line so an inner triple-backtick boundary inside a
# quad-backtick outer fence closed the fence prematurely, letting the
# inner markdown table leak out to PNG extraction (violates PRD R5).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quad_backtick_outer_fence_preserves_inner_table():
    """Outer ````` ```` ````` fence wrapping a triple-backtick block that
    wraps a markdown table → 0 TableRenders, returned text equals input.

    User-facing intent: "show me literally this markdown, don't render it".
    The extractor must not touch a single character inside the quad fence.
    """
    text = (
        "Before.\n"
        "````\n"
        "```\n"
        "| h |\n"
        "|---|\n"
        "| v |\n"
        "```\n"
        "````\n"
        "After."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == [], "quad-fence inner table must NOT be extracted"
    assert out == text, "returned text must equal input verbatim"


@pytest.mark.asyncio
async def test_triple_backtick_fence_still_works():
    """Regression pin: simple ``` fence around a markdown table still
    suppresses extraction (pre-existing R5 behaviour unchanged by the
    quad-fence fix).
    """
    text = (
        "Before.\n"
        "```\n"
        "| h |\n"
        "|---|\n"
        "| v |\n"
        "```\n"
        "After."
    )
    out, renders = await DiscordRenderer._extract_and_render_tables(text)
    assert renders == [], "triple-fence inner table must NOT be extracted"
    assert out == text, "returned text must equal input verbatim"
