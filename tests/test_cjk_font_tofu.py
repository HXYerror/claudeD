"""#219 — CJK font tofu fix via glyph-coverage probe.

Tests:
- `_font_has_cjk` returns True for real PingFang, False for Menlo
- `_load_font` skips ASCII-only fonts when a CJK font is available
- `_load_font` falls back to ASCII-only when no CJK font available
- `_load_font` falls back to `load_default` when all paths fail
- broaden-except: non-OSError exceptions during truetype() don't crash
- FONT_CANDIDATES order: CJK first, ASCII (Menlo) last
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clauded.table_png import (
    FONT_CANDIDATES,
    _CJK_PROBE_CHAR,
    _font_has_cjk,
    _load_font,
)


# ---------------------------------------------------------------------------
# Probe semantics
# ---------------------------------------------------------------------------


def test_cjk_probe_char_is_zhong():
    """Anchor the probe character. Changing this changes the CJK contract."""
    assert _CJK_PROBE_CHAR == "中"


def test_font_has_cjk_true_for_real_cjk_font():
    """A font that CAN render Chinese returns True from the probe."""
    pingfang = Path("/System/Library/Fonts/PingFang.ttc")
    if not pingfang.exists():
        pytest.skip("PingFang not present on this host")
    from PIL import ImageFont
    f = ImageFont.truetype(str(pingfang), 14)
    assert _font_has_cjk(f) is True


def test_font_has_cjk_false_for_ascii_only_font():
    """The whole point: Menlo loads cleanly but has no CJK glyphs.

    This is the #219 root cause — the original code returned Menlo
    and silently produced tofu. The new probe must reject it.
    """
    menlo = Path("/System/Library/Fonts/Menlo.ttc")
    if not menlo.exists():
        pytest.skip("Menlo not present on this host")
    from PIL import ImageFont
    f = ImageFont.truetype(str(menlo), 14)
    assert _font_has_cjk(f) is False, (
        "#219: Menlo loads but has no CJK in its cmap; the probe MUST "
        "return False so _load_font falls through to a CJK-capable font"
    )


def test_font_has_cjk_handles_exceptions():
    """If the probe machinery itself raises, fail gracefully (return False)."""
    f = MagicMock()
    f.getmask = MagicMock(side_effect=RuntimeError("planted-probe-fail"))
    assert _font_has_cjk(f) is False


def test_font_has_cjk_handles_none_mask():
    """Some Pillow versions can return None for missing glyphs."""
    f = MagicMock()
    f.getmask = MagicMock(return_value=None)
    assert _font_has_cjk(f) is False


# ---------------------------------------------------------------------------
# _load_font fallback chain
# ---------------------------------------------------------------------------


def test_load_font_returns_pingfang_on_happy_mac():
    """Sanity: on a mac with PingFang installed, _load_font returns PingFang
    (not Menlo, not Arial Unicode)."""
    if not Path("/System/Library/Fonts/PingFang.ttc").exists():
        pytest.skip("PingFang not present")
    f = _load_font(14)
    name, _style = f.getname()
    assert "PingFang" in name, (
        f"#219: happy mac must resolve to PingFang first, got {name!r}"
    )


def test_load_font_skips_ascii_only_when_cjk_available(monkeypatch):
    """If candidates list = [Menlo, PingFang], we should still pick PingFang.

    Verifies the probe rejects Menlo even though it loaded successfully.
    """
    menlo = "/System/Library/Fonts/Menlo.ttc"
    pingfang = "/System/Library/Fonts/PingFang.ttc"
    if not (Path(menlo).exists() and Path(pingfang).exists()):
        pytest.skip("Required fonts missing")

    from clauded import table_png
    monkeypatch.setattr(table_png, "FONT_CANDIDATES", (menlo, pingfang))
    f = _load_font(14)
    name, _ = f.getname()
    assert "PingFang" in name, (
        f"#219: when Menlo loads but lacks CJK, _load_font must keep "
        f"looking and find PingFang. Got: {name!r}"
    )


def test_load_font_falls_back_to_ascii_when_no_cjk(monkeypatch):
    """All candidates lack CJK → return the first-loaded ASCII font.

    Better to render English correctly than to load_default() everything.
    """
    menlo = "/System/Library/Fonts/Menlo.ttc"
    if not Path(menlo).exists():
        pytest.skip("Menlo not present")

    from clauded import table_png
    # No CJK-capable font in the list
    monkeypatch.setattr(table_png, "FONT_CANDIDATES", (menlo,))
    f = _load_font(14)
    name, _ = f.getname()
    assert "Menlo" in name or "menlo" in name.lower(), (
        f"#219: with no CJK font available, fall back to first-loaded "
        f"ASCII font (Menlo). Got: {name!r}"
    )


def test_load_font_falls_back_to_default_when_all_fail(monkeypatch):
    """No font path can be loaded → ImageFont.load_default()."""
    from clauded import table_png
    monkeypatch.setattr(
        table_png,
        "FONT_CANDIDATES",
        ("/nonexistent/1.ttf", "/nonexistent/2.ttf"),
    )
    f = _load_font(14)
    # load_default() returns a Pillow internal font; just confirm it exists
    assert f is not None


def test_load_font_broadens_except_to_all_exceptions(monkeypatch):
    """#219: Pillow can raise IndexError / ValueError on bad ttc faces;
    the old `except (OSError, IOError)` missed those. The new `except
    Exception` advances to the next candidate."""
    from PIL import ImageFont
    from clauded import table_png

    real_truetype = ImageFont.truetype
    call_count = {"n": 0}
    def _flaky_truetype(path, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise IndexError("planted-ttc-face-index-out-of-range")
        if call_count["n"] == 2:
            raise ValueError("planted-cmap-parse-fail")
        # Third call succeeds with a real font (via the real Pillow API)
        return real_truetype("/System/Library/Fonts/PingFang.ttc", *args, **kwargs)

    monkeypatch.setattr(table_png, "FONT_CANDIDATES", ("/a", "/b", "/c"))
    monkeypatch.setattr(ImageFont, "truetype", _flaky_truetype)
    f = _load_font(14)
    assert call_count["n"] == 3, (
        f"#219: non-OSError exceptions must NOT crash _load_font; "
        f"expected 3 attempts, got {call_count['n']}"
    )


# ---------------------------------------------------------------------------
# Candidate list shape
# ---------------------------------------------------------------------------


def test_candidate_list_cjk_before_ascii():
    """Order matters: CJK-capable paths must come before Menlo so the
    probe can short-circuit on a happy mac."""
    pingfang_idx = next(
        (i for i, p in enumerate(FONT_CANDIDATES) if "PingFang" in p), None
    )
    menlo_idx = next(
        (i for i, p in enumerate(FONT_CANDIDATES) if "Menlo" in p), None
    )
    assert pingfang_idx is not None and menlo_idx is not None
    assert pingfang_idx < menlo_idx, (
        f"#219: PingFang (idx {pingfang_idx}) must come before Menlo "
        f"(idx {menlo_idx}) so the probe finds CJK before ASCII-only"
    )


def test_candidate_list_has_linux_fallbacks():
    """Future-proofing: docker / linux deploys need Noto / WQY."""
    paths = " ".join(FONT_CANDIDATES)
    assert "NotoSansCJK" in paths or "noto" in paths.lower(), (
        "#219: Linux CJK fallbacks (Noto Sans CJK) must be in the list"
    )
    assert "wqy" in paths.lower(), (
        "#219: WenQuanYi CJK fallback should be in the list"
    )


def test_candidate_list_has_macos_legacy_cjk():
    """Old macOS / minimal installs: STHeiti / Hiragino as additional macs."""
    paths = " ".join(FONT_CANDIDATES)
    # At least one of these should be there
    has_legacy = any(
        marker in paths for marker in ("STHeiti", "Hiragino", "Arial Unicode")
    )
    assert has_legacy, (
        "#219: macOS legacy CJK paths (STHeiti / Hiragino / Arial Unicode) "
        "needed for non-default-locale installs"
    )
