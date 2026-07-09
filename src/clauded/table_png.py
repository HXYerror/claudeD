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

# --- Scale (HiDPI) -------------------------------------------------------
# Render at 2x for Retina/mobile sharpness (#206 sub-issue B1). All pixel
# sizes below are derived from this constant so layout proportions are
# preserved; only the bitmap resolution changes.
SCALE = 2

# --- Layout --------------------------------------------------------------

PAD = 16 * SCALE
CELL_X = 12 * SCALE
CELL_Y = 8 * SCALE
ROW_H = 24 * SCALE
HEAD_H = 28 * SCALE
ACCENT_W = 4 * SCALE
LINE_SPACING_EXTRA = 16 * SCALE  # added per extra <br>-induced line in a cell
TEXT_SPACING = 2 * SCALE          # PIL multiline_text spacing
FONT_BODY_SIZE = 13 * SCALE
FONT_HEAD_SIZE = 14 * SCALE

# --- Display rules -------------------------------------------------------

MAX_CELL_CHARS = 120
ELLIPSIS = "…"

# DoS guards (review I4). Caller catches the ValueError and falls back to
# emitting the original markdown verbatim — see C2 try/except in
# DiscordRenderer._extract_and_render_tables.
#
# Pixel-budget is scaled by SCALE**2 so the *logical* row/col limit a user
# can fit is unchanged after the 2x HiDPI bump (#206 sub-task S3).
MAX_COLS = 20
MAX_ROWS = 200
MAX_TABLE_PIXELS = 8000 * 4000 * (SCALE ** 2)  # logical 8000×4000 budget

# Matches markdown links ``[name](url)``. Conservative — no nested brackets.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# --- Emoji → text fallback (#206 sub-issue A1) ---------------------------
# Pillow's default freetype can't render Apple Color Emoji (bitmap font)
# and PingFang/Menlo don't carry most emoji codepoints — cells render
# blank. Map common emoji to short text labels before drawing; strip
# unmapped emoji silently rather than rendering tofu.
EMOJI_TEXT_MAP = {
    "✅": "[OK]", "❌": "[FAIL]", "⚠️": "[WARN]",
    "📋": "[TODO]", "🎯": "[GOAL]", "🚀": "[GO]",
    "🔥": "[HOT]", "⏰": "[TIME]", "📦": "[PKG]",
    "🐛": "[BUG]", "💡": "[IDEA]", "🎉": "[DONE]",
    "⭐": "[STAR]", "🔍": "[FIND]", "📝": "[NOTE]",
    "🔒": "[LOCK]", "🌐": "[NET]", "⚡": "[FAST]",
    "🔧": "[TOOL]", "📊": "[DATA]", "📈": "[UP]",
    "📉": "[DOWN]", "✨": "[NEW]", "🛑": "[STOP]",
    "⏸️": "[PAUSE]", "▶️": "[PLAY]", "🔄": "[SYNC]",
    "📌": "[PIN]", "🏷️": "[TAG]", "📁": "[DIR]",
}

# Longer keys (e.g. emoji + VS16 selector) must match before their
# shorter prefix forms.
_EMOJI_PATTERN = re.compile(
    "|".join(re.escape(e) for e in sorted(EMOJI_TEXT_MAP, key=len, reverse=True))
)

# Broad unmapped-emoji strip. Covers the major pictographic blocks
# (Misc Symbols/Dingbats through the Symbols & Pictographs Extended-A
# block at U+1FAFF). Variation Selector-16 (U+FE0F) is also stripped
# so a stranded selector doesn't render as a visible glyph.
_UNMAPPED_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")


def _replace_known_emoji(text: str) -> str:
    """Replace mapped emoji with text labels; drop unmapped emoji silently.

    Returns the input unchanged for non-string / empty input.
    """
    if not text:
        return text
    text = _EMOJI_PATTERN.sub(lambda m: EMOJI_TEXT_MAP[m.group(0)], text)
    text = _UNMAPPED_EMOJI_RE.sub("", text)
    return text


# --- Font resolution -----------------------------------------------------

# Module-level so tests can monkeypatch the candidate list.
#
# #219: priority order = CJK-capable first, then ASCII-only fallbacks.
# A `_load_font` `getmask('中')` probe rejects fonts that LOAD but lack
# CJK glyphs (the original Menlo-tofu bug — fontconfig says "yes I'll
# load Menlo" then renders Chinese as empty boxes).
#
# CJK fonts up top so a happy mac still resolves to PingFang in one step.
# Linux candidates kept for future docker / linux deployment (the project
# is mac-only today, but future-proofing is free here).
FONT_CANDIDATES = (
    # macOS CJK
    "/System/Library/Fonts/PingFang.ttc",                            # primary (PRD #112 R4.2)
    "/System/Library/Fonts/STHeiti Light.ttc",                       # macOS legacy CJK
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",                    # macOS GB CJK
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",          # broad coverage
    # Windows CJK (#289)
    "C:\\Windows\\Fonts\\msyh.ttc",                                   # 微软雅黑
    "C:\\Windows\\Fonts\\simsun.ttc",                                  # 宋体
    "C:\\Windows\\Fonts\\msyhbd.ttc",                                  # 雅黑 Bold
    "C:\\Windows\\Fonts\\arial.ttf",                                   # ASCII fallback
    # Linux CJK (docker / future deploys)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    # ASCII-only fallback (no CJK — only chosen if probe rejects all CJK options)
    "/System/Library/Fonts/Menlo.ttc",
)

# #219: a glyph the CJK probe must successfully draw. ``中`` is high-
# frequency in both simplified + traditional; if a font produces an empty
# mask for this code point we treat the font as CJK-unsupported and
# advance to the next candidate.
_CJK_PROBE_CHAR = "中"

# #219: Pillow's getmask() for a missing glyph returns the font's
# ".notdef" tofu box — a non-empty rectangle. To distinguish "font has
# this glyph" from "font is rendering the tofu placeholder" we compare
# the probe's mask to a Unicode private-use codepoint that NO font has
# (by spec). Identical masks → both are tofu → font has no CJK.
_PUA_TOFU_REFERENCE = "\uE000"


def _font_has_cjk(font: ImageFont.FreeTypeFont) -> bool:
    """True iff ``font`` actually rasterizes a real (non-tofu) glyph for ``中``.

    The naive ``getmask().size > 0`` test fails because Menlo (#219 root
    cause) loads cleanly AND returns a non-empty hollow-rectangle mask
    for CJK code points — the ".notdef" placeholder. To discriminate, we
    compare ``中`` 's mask to the mask of ``U+E000`` (Unicode private-
    use area). No font defines a glyph at U+E000, so its mask IS the
    tofu placeholder. If the two masks match exactly → 中 is also tofu
    → reject the font.

    A CJK-capable font produces a distinct, larger, ink-rich mask for
    中 than for U+E000 (typically the U+E000 mask is also 0-sized).
    """
    try:
        cjk_mask = font.getmask(_CJK_PROBE_CHAR)
    except Exception:
        return False
    if cjk_mask is None or cjk_mask.size == (0, 0):
        # Glyph missing outright — also tofu (and rarer than ".notdef").
        return False
    try:
        tofu_mask = font.getmask(_PUA_TOFU_REFERENCE)
    except Exception:
        # Can't get the reference; conservatively say no.
        return False
    # If size or pixel data match, CJK is rendering as tofu.
    if cjk_mask.size != tofu_mask.size:
        return True
    try:
        return bytes(cjk_mask) != bytes(tofu_mask)
    except Exception:
        return cjk_mask.size != tofu_mask.size


def _load_font(size: int):
    """Try each candidate path; fall back to ``load_default`` (never crashes).

    #219: a font that LOADS without exception is not enough — the original
    Menlo bug had ``ImageFont.truetype(menlo)`` succeed cleanly but render
    Chinese as empty boxes (Menlo has no CJK in cmap). We now require the
    font to ACTUALLY rasterize the CJK probe glyph before accepting it.

    Fallback chain:
    - For each candidate path, try to load it. Exceptions → next.
    - If loaded font has CJK coverage → return it.
    - If loaded font has NO CJK coverage → remember it as "ASCII fallback"
      and keep looking for a CJK-capable font.
    - If no CJK font found, fall back to the last successfully-loaded
      ASCII-only font (so we still render English correctly).
    - Final fallback: ``ImageFont.load_default()``.
    """
    ascii_fallback = None
    for path in FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            # #219: broaden except. Some Pillow versions raise non-OSError
            # types (IndexError on bad ttc face, ValueError on cmap parse,
            # etc.) and we want to advance rather than crash the renderer.
            continue
        if _font_has_cjk(f):
            return f
        # Remember the FIRST successfully-loaded ASCII-only font as the
        # last-resort fallback. Later iterations don't overwrite this so
        # we don't "upgrade" from Menlo to some random ASCII path.
        if ascii_fallback is None:
            ascii_fallback = f
    if ascii_fallback is not None:
        return ascii_fallback
    return ImageFont.load_default()


# --- Cell preprocessing --------------------------------------------------

def _format_cell(text: str) -> str:
    """Apply display-only transforms to a cell.

    - Markdown links ``[name](url)`` → ``name (url)`` (PNG can't render links).
    - Strip backticks (Pillow has no inline-code style).
    - ``<br>`` → newline (drawn as multiline).
    - Replace mapped emoji with text labels; drop unmapped emoji (#206).
    - Truncate to ``MAX_CELL_CHARS`` with ``…``.
    """
    if text is None:
        return ""
    s = str(text)
    s = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", s)
    s = s.replace("`", "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = _replace_known_emoji(s)
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

    font_body = _load_font(FONT_BODY_SIZE)
    font_head = _load_font(FONT_HEAD_SIZE)

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
        row_heights.append(max(ROW_H, ROW_H + (lines - 1) * LINE_SPACING_EXTRA))

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
        # R1 simplicity: previously hardcoded `-2` offset and `width=1`
        # line stroke didn't scale at 2x → hairline-thin on Retina.
        # Derive both from SCALE so all visual elements share the same
        # 2x density.
        draw.text((x + CELL_X, y + CELL_Y - 2 * SCALE), h, fill=HEAD_TEXT, font=font_head)
        x += col_w[ci]
        if ci < ncols - 1:
            draw.line((x, y, x, y + HEAD_H), fill=LINE, width=1 * SCALE)
    y += HEAD_H
    # Accent under the header.
    draw.line((PAD, y, PAD + total_w, y), fill=ACCENT, width=2 * SCALE)

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
                    (x + CELL_X, y + CELL_Y - 2 * SCALE),
                    cell,
                    fill=TEXT,
                    font=font_body,
                    spacing=TEXT_SPACING,
                )
            x += col_w[ci]
            if ci < ncols - 1:
                draw.line((x, y, x, y + rh), fill=LINE, width=1 * SCALE)
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
    "SCALE",
    "EMOJI_TEXT_MAP",
]
