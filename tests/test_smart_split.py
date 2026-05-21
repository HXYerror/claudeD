"""Unit tests for ``DiscordRenderer._smart_split`` and ``_detect_open_fence_lang``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clauded.discord_renderer import DISCORD_MAX_LEN, DiscordRenderer


@pytest.fixture
def renderer() -> DiscordRenderer:
    """Return a renderer with a mock target — _smart_split doesn't touch it."""
    return DiscordRenderer(target=MagicMock())


# ---------------------------------------------------------------------------
# Basic splitting
# ---------------------------------------------------------------------------


def test_smart_split_short_text(renderer: DiscordRenderer) -> None:
    """Text under the limit is returned as a single chunk."""
    assert renderer._smart_split("hello world", limit=100) == ["hello world"]


def test_smart_split_text_exactly_at_limit(renderer: DiscordRenderer) -> None:
    """Text whose length equals the limit is returned as a single chunk."""
    text = "a" * 50
    assert renderer._smart_split(text, limit=50) == [text]


def test_smart_split_empty(renderer: DiscordRenderer) -> None:
    """Empty input yields the empty list."""
    assert renderer._smart_split("", limit=100) == []


def test_smart_split_default_limit_is_discord_max(renderer: DiscordRenderer) -> None:
    """The default limit matches Discord's safety margin constant."""
    text = "x" * (DISCORD_MAX_LEN - 1)
    assert renderer._smart_split(text) == [text]


# ---------------------------------------------------------------------------
# Boundary preferences: paragraph > line > space > hard cut
# ---------------------------------------------------------------------------


def test_smart_split_paragraph_boundary(renderer: DiscordRenderer) -> None:
    """A paragraph break (``\\n\\n``) is preferred when present."""
    # Build: 60 chars of "a", paragraph break, 60 of "b". Limit 80 forces a
    # split — the paragraph break sits at index 60, well above limit//2.
    text = ("a" * 60) + "\n\n" + ("b" * 60)
    chunks = renderer._smart_split(text, limit=80)
    assert len(chunks) == 2
    # The cut sits *after* the ``\n\n`` so the first chunk owns both newlines;
    # lstrip("\n") on the tail prevents leading-blank-line bleed in chunk 2.
    assert chunks[0] == ("a" * 60) + "\n\n"
    assert chunks[1] == "b" * 60


def test_smart_split_line_boundary(renderer: DiscordRenderer) -> None:
    """A line break (``\\n``) is used when no paragraph break is in range."""
    text = ("a" * 60) + "\n" + ("b" * 60)
    chunks = renderer._smart_split(text, limit=80)
    assert len(chunks) == 2
    # Cut sits after the ``\n``; the tail's leading newline is then stripped.
    assert chunks[0] == ("a" * 60) + "\n"
    assert chunks[1] == "b" * 60


def test_smart_split_space_boundary(renderer: DiscordRenderer) -> None:
    """A space is used when no newline is present."""
    text = ("a" * 60) + " " + ("b" * 60)
    chunks = renderer._smart_split(text, limit=80)
    assert len(chunks) == 2
    # Cut sits after the space; the tail starts with the next non-newline char.
    assert chunks[0] == ("a" * 60) + " "
    assert chunks[1] == "b" * 60


def test_smart_split_hard_cut(renderer: DiscordRenderer) -> None:
    """When no good break exists, the chunk is hard-cut at ``limit``."""
    # Solid run of one character — no paragraph, line, or space break.
    text = "a" * 200
    # fence_reserve = 4, so each chunk is at most limit - 4 chars.
    chunks = renderer._smart_split(text, limit=80)
    assert "".join(chunks) == text
    # Since there are no fences in the text, no fence reserve fixups apply
    # to the *content*, but the cut index is limit - fence_reserve = 76.
    assert all(len(c) <= 80 for c in chunks)
    assert len(chunks[0]) == 76  # 80 - len("\n```")


# ---------------------------------------------------------------------------
# Code-fence protection
# ---------------------------------------------------------------------------


def test_smart_split_unclosed_fence_is_closed_and_reopened(
    renderer: DiscordRenderer,
) -> None:
    """An unclosed ``\u0060\u0060\u0060`` block is closed at the cut and reopened after."""
    text = "```\n" + ("a" * 200) + "\n```\nafter"
    chunks = renderer._smart_split(text, limit=80)
    # First chunk ends inside the fence: it must be closed with "\n```".
    assert chunks[0].endswith("\n```")
    # The next chunk (still inside the original block) reopens the fence.
    assert chunks[1].startswith("```\n")
    # Every chunk must have an even number of fences (self-contained).
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_smart_split_fence_with_language_tag_reopens_with_lang(
    renderer: DiscordRenderer,
) -> None:
    """The reopened fence preserves the original language tag."""
    text = "```python\n" + ("x" * 200) + "\n```\nafter"
    chunks = renderer._smart_split(text, limit=80)
    assert chunks[0].endswith("\n```")
    # The reopen must include the python language tag.
    assert chunks[1].startswith("```python\n")
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_smart_split_multiple_code_blocks(renderer: DiscordRenderer) -> None:
    """A long body containing several closed code blocks splits cleanly."""
    block_a = "```python\n" + ("a" * 50) + "\n```"
    block_b = "```js\n" + ("b" * 50) + "\n```"
    text = block_a + "\n\nmiddle text\n\n" + block_b
    chunks = renderer._smart_split(text, limit=90)
    # Every chunk must have balanced fences.
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk
    # The reconstructed text (after fence-reopen surgery) must contain the
    # same characters as the input minus the bookkeeping fences.
    joined = "".join(chunks)
    assert "middle text" in joined


# ---------------------------------------------------------------------------
# _detect_open_fence_lang
# ---------------------------------------------------------------------------


def test_detect_open_fence_lang_with_language() -> None:
    assert DiscordRenderer._detect_open_fence_lang("```python\nhello") == "python"


def test_detect_open_fence_lang_without_language() -> None:
    assert DiscordRenderer._detect_open_fence_lang("```\nhello") == ""


def test_detect_open_fence_lang_no_fence_returns_empty() -> None:
    assert DiscordRenderer._detect_open_fence_lang("just text, no fence") == ""


def test_detect_open_fence_lang_picks_last_open_fence() -> None:
    """When multiple open fences exist, the *currently open* one wins."""
    # Two fences (closed pair) followed by a third open one with lang "rust".
    chunk = "```py\nfoo\n```\nbetween\n```rust\nbar"
    assert DiscordRenderer._detect_open_fence_lang(chunk) == "rust"


# ---------------------------------------------------------------------------
# Markdown block-awareness (#274): don't cut through code blocks,
# blockquote runs, or table runs when the block fits in a single chunk.
# ---------------------------------------------------------------------------


def test_smart_split_keeps_code_block_intact_across_2000_boundary(
    renderer: DiscordRenderer,
) -> None:
    """A ` ```bash ` block straddling the 2000-char cut stays whole (#274).

    Without block-awareness the line-boundary rfind would land on a ``\\n``
    inside the code body, splitting the fence in half. With block-awareness
    the cut moves *before* the opening fence so the block survives intact.
    """
    # 158 filler lines × 12 chars = 1896 chars; then a ~300-char code block.
    # Total ~2200 > limit so a split is forced. The code block sits across
    # the 1996 cut window.
    prefix = "filler line\n" * 158  # 1896 chars
    code = "```bash\n" + ("echo 'hello world'\n" * 15) + "```"
    text = prefix + code + "\n\ntail"
    chunks = renderer._smart_split(text, limit=2000)

    # The entire code block must live inside a single chunk — never split.
    assert any(code in c for c in chunks), (
        "Expected the ```bash``` block to be kept intact in one chunk; "
        f"got chunks={[len(c) for c in chunks]}"
    )
    # Every chunk must still be self-contained (balanced fences).
    for c in chunks:
        assert c.count("```") % 2 == 0, c


def test_smart_split_keeps_blockquote_run_intact(
    renderer: DiscordRenderer,
) -> None:
    """Consecutive ``> `` lines aren't cut mid-run (#274).

    The legacy line-boundary heuristic would happily split between two
    ``> ...`` lines, breaking the quote across messages. Block-awareness
    pushes the cut to *before* the quote begins.
    """
    prefix = "filler line\n" * 158  # 1896 chars
    quote = "\n".join(f"> quote line {i}" for i in range(10))  # 149 chars
    text = prefix + quote
    chunks = renderer._smart_split(text, limit=2000)

    assert any(quote in c for c in chunks), (
        "Expected the blockquote run to live entirely in one chunk; "
        f"got chunks={[len(c) for c in chunks]}"
    )
    # A real mid-blockquote split would leave one chunk ending in ``> ...``
    # and the next starting in ``> ...``. Verify no such adjacency.
    for prev, curr in zip(chunks, chunks[1:]):
        prev_last = prev.rstrip("\n").splitlines()[-1] if prev.strip() else ""
        curr_first = curr.lstrip("\n").splitlines()[0] if curr.strip() else ""
        assert not (
            prev_last.lstrip().startswith(">")
            and curr_first.lstrip().startswith(">")
        ), f"Blockquote split across chunks: {prev_last!r} | {curr_first!r}"


def test_smart_split_keeps_table_intact(renderer: DiscordRenderer) -> None:
    """Markdown table header + separator + body stay together (#274).

    Without block-awareness, the rfind("\\n", ...) lands on a row delim-
    iter inside the table body and splits header/separator away from
    the trailing rows — Discord then renders the orphaned rows as plain
    text. The block-aware check refuses to split between two ``| ...``
    lines and pushes the cut to *before* the table.
    """
    prefix = "filler line\n" * 158  # 1896 chars
    rows = "\n".join(f"| a{i}   | b{i}   |" for i in range(10))
    table = "| col1 | col2 |\n|------|------|\n" + rows
    text = prefix + table
    chunks = renderer._smart_split(text, limit=2000)

    assert any(table in c for c in chunks), (
        "Expected the markdown table to live entirely in one chunk; "
        f"got chunks={[len(c) for c in chunks]}"
    )
    # A real mid-table split would leave one chunk ending in ``| ...`` and
    # the next starting in ``| ...``. Verify no such adjacency.
    for prev, curr in zip(chunks, chunks[1:]):
        prev_last = prev.rstrip("\n").splitlines()[-1] if prev.strip() else ""
        curr_first = curr.lstrip("\n").splitlines()[0] if curr.strip() else ""
        assert not (
            prev_last.lstrip().startswith("|")
            and curr_first.lstrip().startswith("|")
        ), f"Table split across chunks: {prev_last!r} | {curr_first!r}"


# ---------------------------------------------------------------------------
# _format_tables – code-fence awareness
# ---------------------------------------------------------------------------


def test_format_tables_inside_code_block():
    """Tables already inside a code fence are not double-wrapped."""
    text = "Output:\n```\n| id | name |\n|----|\n| 1  | Alice |\n```\nDone."
    result = DiscordRenderer._format_tables(text)
    assert result.count("```") == 2  # only the original pair, no double-wrapping


def test_format_tables_normal():
    """A bare markdown table is wrapped in a code block."""
    text = "Results:\n| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |\nEnd."
    result = DiscordRenderer._format_tables(text)
    assert "```" in result  # table wrapped
    assert "| Alice | 30 |" in result


@pytest.mark.xfail(reason="#274 AC4: oversized block → .md fallback not yet wired")
def test_oversized_code_block_degrades_gracefully():
    """A single fenced code block > 2000 chars should not produce a
    chunk that exceeds Discord's 2000 limit without the fence being
    properly closed/reopened."""
    code = "x = 1\n" * 400  # ~2400 chars
    text = f"```python\n{code}```\nAfter the block."
    chunks = _smart_split(text, 2000)
    for c in chunks:
        assert len(c) <= 2000, f"Chunk exceeds 2000: {len(c)} chars"
