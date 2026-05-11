"""PNG renderer for markdown tables (v1.12 / #131).

Single entry point: :func:`render_table_png` — given parsed headers + rows,
produces PNG bytes suitable for sending as a Discord attachment.

Design (per PRD R4):
- Pillow direct draw, no HTML/CSS pipeline.
- Dark Discord theme, blurple accent stripe on the left.
- Mac system fonts (Menlo for body/header, PingFang as CJK fallback,
  Arial Unicode as broad fallback, ``ImageFont.load_default`` as last resort).
- No bundled fonts — Linux deploy hardening is a separate subtask.
- ``font.getbbox`` measures actual rendered width (correct for CJK + emoji).
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw, ImageFont


# --- Theme ---------------------------------------------------------------

BG = (32, 34, 37)
HEAD_BG = (35, 39, 42)
ROW_EVEN = (47, 49, 54)
ROW_ODD = (54, 57, 63)
TEXT = (220, 221, 222)
HEAD_TEXT = (255, 255, 255)
ACCENT = (88, 101, 242)   # Discord blurple
LINE = (60, 64, 72)

# --- Layout --------------------------------------------------------------

PAD = 16
CELL_X = 12
CELL_Y = 8
ROW_H = 24
HEAD_H = 28
ACCENT_W = 4

# --- Display rules -------------------------------------------------------

MAX_CELL_CHARS = 120
ELLIPSIS = "…"

# DoS guards (review I4). Caller catches the ValueError and falls back to
# emitting the original markdown verbatim — see C2 try/except in
# DiscordRenderer._extract_and_render_tables.
MAX_COLS = 20
MAX_ROWS = 200
MAX_TABLE_PIXELS = 8000 * 4000  # ~96 MB at 3 bytes/pixel

# Matches markdown links ``[name](url)``. Conservative — no nested brackets.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


# --- Font resolution -----------------------------------------------------

# Module-level so tests can monkeypatch the candidate list.
# PingFang first: it has good Latin coverage AND CJK glyphs, so it works as
# the primary font on the happy path while preventing tofu boxes for Chinese
# text (PRD R4.2 — review I6).
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",                      # CJK first (PRD R4.2)
    "/System/Library/Fonts/Menlo.ttc",                         # mono fallback
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",    # broad fallback
)


def _load_font(size: int):
    """Try each candidate path; fall back to ``load_default`` (never crashes)."""
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# --- Cell preprocessing --------------------------------------------------

def _format_cell(text: str) -> str:
    """Apply display-only transforms to a cell.

    - Markdown links ``[name](url)`` → ``name (url)`` (PNG can't render links).
    - Strip backticks (Pillow has no inline-code style).
    - ``<br>`` → newline (drawn as multiline).
    - Truncate to ``MAX_CELL_CHARS`` with ``…``.
    """
    if text is None:
        return ""
    s = str(text)
    s = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", s)
    s = s.replace("`", "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    if len(s) > MAX_CELL_CHARS:
        s = s[: MAX_CELL_CHARS - 1] + ELLIPSIS
    return s


def _measure(text: str, font) -> int:
    """Return rendered pixel width of ``text`` for ``font``."""
    if not text:
        return 0
    # Multiline: take the widest line.
    width = 0
    for line in text.split("\n"):
        bbox = font.getbbox(line)
        width = max(width, bbox[2] - bbox[0])
    return width


def _line_count(text: str) -> int:
    """Number of visual lines (≥1) in a formatted cell."""
    if not text:
        return 1
    return text.count("\n") + 1


# --- Public API ----------------------------------------------------------

def render_table_png(headers: list[str], rows: list[list[str]]) -> bytes:
    """Render a markdown table to PNG bytes.

    ``headers``: list of column header strings.
    ``rows``: list of row lists (each row is a list of cell strings).
              Rows shorter than ``headers`` are padded with empty cells.

    Display transforms (per PRD R4.3 / R6.4-R6.6):
    - Strips ` backticks from cells.
    - Renders ``[name](url)`` markdown links as ``name (url)`` plain text.
    - ``<br>`` → newline (cell grows in height).
    - Truncates cells longer than 120 chars with ``…``.
    - Empty cells render blank (no crash).

    Returns PNG bytes parseable by ``PIL.Image.open(BytesIO(b))``.

    Raises
    ------
    ValueError
        If ``headers`` / ``rows`` exceed :data:`MAX_COLS` / :data:`MAX_ROWS`
        or the computed image area exceeds :data:`MAX_TABLE_PIXELS`. The
        caller is expected to catch and fall back to verbatim markdown.
    """
    headers = list(headers) if headers else [""]
    ncols = len(headers)

    if ncols > MAX_COLS:
        raise ValueError(
            f"table too wide: {ncols} cols (max {MAX_COLS})"
        )
    if rows is not None and len(rows) > MAX_ROWS:
        raise ValueError(
            f"table too tall: {len(rows)} rows (max {MAX_ROWS})"
        )

    # Normalise rows: pad/truncate to ``ncols``.
    norm_rows: list[list[str]] = []
    for r in (rows or []):
        r = list(r) if r else []
        if len(r) < ncols:
            r = r + [""] * (ncols - len(r))
        elif len(r) > ncols:
            r = r[:ncols]
        norm_rows.append(r)

    # Apply display transforms once up front.
    fmt_headers = [_format_cell(h) for h in headers]
    fmt_rows = [[_format_cell(c) for c in r] for r in norm_rows]

    font_body = _load_font(13)
    font_head = _load_font(14)

    # Column widths — max of header width + every row cell width.
    col_w: list[int] = []
    for ci in range(ncols):
        w = _measure(fmt_headers[ci], font_head)
        for r in fmt_rows:
            w = max(w, _measure(r[ci], font_body))
        col_w.append(w + CELL_X * 2)

    # Row heights — grow for multiline ``<br>`` cells.
    row_heights = []
    for r in fmt_rows:
        lines = max((_line_count(c) for c in r), default=1)
        row_heights.append(max(ROW_H, ROW_H + (lines - 1) * 16))

    total_w = sum(col_w) if col_w else 0
    W = max(total_w + PAD * 2, PAD * 2 + 8)
    H = PAD * 2 + HEAD_H + sum(row_heights)

    if W * H > MAX_TABLE_PIXELS:
        raise ValueError(
            f"table pixel budget exceeded: {W}×{H} > {MAX_TABLE_PIXELS}"
        )

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Left blurple accent stripe.
    draw.rectangle((0, 0, ACCENT_W, H), fill=ACCENT)

    # --- Header row ----------------------------------------------------
    y = PAD
    draw.rectangle((PAD, y, PAD + total_w, y + HEAD_H), fill=HEAD_BG)
    x = PAD
    for ci, h in enumerate(fmt_headers):
        draw.text((x + CELL_X, y + CELL_Y - 2), h, fill=HEAD_TEXT, font=font_head)
        x += col_w[ci]
        if ci < ncols - 1:
            draw.line((x, y, x, y + HEAD_H), fill=LINE, width=1)
    y += HEAD_H
    # Accent under the header.
    draw.line((PAD, y, PAD + total_w, y), fill=ACCENT, width=2)

    # --- Data rows -----------------------------------------------------
    for ri, r in enumerate(fmt_rows):
        rh = row_heights[ri]
        bg = ROW_EVEN if ri % 2 == 0 else ROW_ODD
        draw.rectangle((PAD, y, PAD + total_w, y + rh), fill=bg)
        x = PAD
        for ci, cell in enumerate(r):
            if cell:
                # multiline_text handles single-line strings too.
                draw.multiline_text(
                    (x + CELL_X, y + CELL_Y - 2),
                    cell,
                    fill=TEXT,
                    font=font_body,
                    spacing=2,
                )
            x += col_w[ci]
            if ci < ncols - 1:
                draw.line((x, y, x, y + rh), fill=LINE, width=1)
        y += rh

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


__all__ = [
    "render_table_png",
    "MAX_CELL_CHARS",
    "MAX_COLS",
    "MAX_ROWS",
    "MAX_TABLE_PIXELS",
]
