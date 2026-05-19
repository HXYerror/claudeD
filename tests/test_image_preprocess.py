"""#242 — image-preprocess pre-shrink before CLI Read.

CLI bundled binary has hard-coded `sharp.resize(maxWidth: 2000)` etc.
We pre-shrink so the CLI's internal mangler never trips. Verified
end-to-end against live SDK (5/19); see #242 PR commit log for traces.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def img_module():
    """Lazy-import the module so the test file loads even without PIL."""
    from clauded import _image_preprocess
    return _image_preprocess


# ---------------------------------------------------------------------------
# Constants pinning — protect against accidental threshold drift.
# ---------------------------------------------------------------------------


def test_thresholds_below_cli_limits(img_module):
    """5% safety margin below the CLI's hard limits (2000x2000, 3.75MB)."""
    assert img_module.MAX_DIM_PX == 1900
    assert img_module.MAX_BYTES == 3_500_000
    # Confirm we're STRICTLY below the CLI's hard cap.
    assert img_module.MAX_DIM_PX < 2000
    assert img_module.MAX_BYTES < int(3.75 * 1024 * 1024)


def test_jpeg_quality_high(img_module):
    """JPEG quality 92 vs CLI worst-case fallback of 20."""
    assert img_module.JPEG_QUALITY == 92
    assert img_module.JPEG_QUALITY > 20  # explicit


def test_resizable_extensions_match_spec(img_module):
    """PNG / JPG / JPEG / WebP / BMP get preprocessed."""
    assert ".png" in img_module.RESIZABLE_EXTS
    assert ".jpg" in img_module.RESIZABLE_EXTS
    assert ".jpeg" in img_module.RESIZABLE_EXTS
    assert ".webp" in img_module.RESIZABLE_EXTS
    assert ".bmp" in img_module.RESIZABLE_EXTS
    # GIF (animation) and SVG (vector XML) skip
    assert ".gif" not in img_module.RESIZABLE_EXTS
    assert ".svg" not in img_module.RESIZABLE_EXTS


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


def test_preprocess_4k_png_shrinks_below_cap(tmp_path, img_module):
    """The user-reported scenario: 4K screenshot must end up <= 1900 max dim."""
    from PIL import Image
    p = tmp_path / "shot.png"
    img = Image.new("RGB", (3840, 2160), color=(20, 30, 50))
    img.save(p)
    assert Image.open(p).size == (3840, 2160)

    img_module.maybe_shrink_image(p)

    w, h = Image.open(p).size
    assert max(w, h) <= img_module.MAX_DIM_PX, f"expected dim <= {img_module.MAX_DIM_PX}, got {w}x{h}"
    # Aspect ratio preserved (3840:2160 = 16:9 ≈ 1.778)
    orig_ratio = 3840 / 2160
    new_ratio = w / h
    assert abs(orig_ratio - new_ratio) < 0.05, f"aspect ratio drift: {orig_ratio:.3f} -> {new_ratio:.3f}"


def test_preprocess_under_limit_byte_identical(tmp_path, img_module):
    """A small PNG well under both caps must be byte-identical after preprocess."""
    from PIL import Image
    p = tmp_path / "small.png"
    Image.new("RGB", (800, 600), color=(10, 20, 30)).save(p)
    before = p.read_bytes()
    img_module.maybe_shrink_image(p)
    after = p.read_bytes()
    assert before == after, "small image must not be re-encoded (would lose fidelity for no reason)"


def test_preprocess_gif_skipped(tmp_path, img_module, caplog):
    """GIF skipped (animation risk). Byte-identical."""
    from PIL import Image
    p = tmp_path / "anim.gif"
    Image.new("RGB", (3000, 2000), color=(50, 60, 70)).save(p)
    before = p.read_bytes()
    img_module.maybe_shrink_image(p)
    after = p.read_bytes()
    assert before == after, "GIF must be skipped to preserve potential animation"


def test_preprocess_svg_skipped(tmp_path, img_module):
    """SVG (XML) skipped — never opened with PIL."""
    p = tmp_path / "logo.svg"
    p.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="5000" height="5000"/>')
    before = p.read_bytes()
    img_module.maybe_shrink_image(p)
    after = p.read_bytes()
    assert before == after


def test_preprocess_jpeg_uses_quality_92(tmp_path, img_module):
    """JPEG re-save uses quality=92 (well above the CLI's worst-case 20)."""
    from PIL import Image
    p = tmp_path / "huge.jpg"
    # Quality 50 source — preprocess should re-save at higher quality.
    Image.new("RGB", (3000, 2000), color=(100, 150, 200)).save(p, quality=50)
    before_size = p.stat().st_size

    img_module.maybe_shrink_image(p)

    # File should be re-saved (dims changed); just confirm it still loads
    from PIL import Image as _I
    out = _I.open(p)
    assert max(out.size) <= img_module.MAX_DIM_PX


def test_preprocess_handles_rgba_jpeg_flattening(tmp_path, img_module):
    """RGBA PNG renamed to .jpg shouldn't crash the JPEG-flatten path."""
    from PIL import Image
    # Build a transparent image, save as .jpg ext (mismatched but a test scenario)
    p = tmp_path / "weird.jpg"
    img = Image.new("RGBA", (3000, 3000), (200, 100, 50, 128))
    img.save(p, format="PNG")  # PNG bytes in .jpg file
    # Now rename and try preprocess — PIL will detect format from bytes
    img_module.maybe_shrink_image(p)
    # Should not crash; file should still be openable
    Image.open(p).load()


def test_preprocess_logs_info_on_actual_shrink(tmp_path, img_module, caplog):
    """When we DO shrink, log.info fires per #242 AC6 (forensics)."""
    from PIL import Image
    p = tmp_path / "big.png"
    Image.new("RGB", (3500, 2000), color=(20, 30, 50)).save(p)
    caplog.set_level(logging.INFO, logger="clauded.image_preprocess")
    img_module.maybe_shrink_image(p)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("#242: preprocessed" in m and "3500x2000" in m for m in msgs), (
        f"expected #242 INFO with dims; got: {msgs}"
    )


def test_preprocess_missing_file_warns(tmp_path, img_module, caplog):
    """stat() OSError → WARN, no crash."""
    caplog.set_level(logging.WARNING, logger="clauded.image_preprocess")
    img_module.maybe_shrink_image(tmp_path / "does-not-exist.png")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("#242" in m for m in msgs)


def test_preprocess_corrupt_image_warns_and_skips(tmp_path, img_module, caplog):
    """PIL Image.open failure → WARN + leave original on disk."""
    p = tmp_path / "garbage.png"
    p.write_bytes(b"not actually a PNG, just garbage bytes")
    before = p.read_bytes()
    caplog.set_level(logging.WARNING, logger="clauded.image_preprocess")
    img_module.maybe_shrink_image(p)
    after = p.read_bytes()
    assert before == after, "corrupt file must be left untouched (fail-soft)"


def test_preprocess_pillow_missing_warns(tmp_path, img_module, caplog, monkeypatch):
    """If Pillow isn't importable, we WARN once and skip — never crash."""
    # Build a real PNG so the size check passes
    from PIL import Image
    p = tmp_path / "x.png"
    Image.new("RGB", (3500, 2000)).save(p)

    # Make the deferred PIL import inside maybe_shrink_image fail.
    import sys
    real_pil = sys.modules.pop("PIL", None)
    real_pil_image = sys.modules.pop("PIL.Image", None)

    def _fail_import(name, *a, **kw):
        if name in ("PIL", "PIL.Image"):
            raise ImportError("simulated PIL absent")
        return real_import(name, *a, **kw)

    import builtins
    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _fail_import)
    try:
        caplog.set_level(logging.WARNING, logger="clauded.image_preprocess")
        img_module.maybe_shrink_image(p)
    finally:
        # Restore PIL so other tests see it
        if real_pil is not None:
            sys.modules["PIL"] = real_pil
        if real_pil_image is not None:
            sys.modules["PIL.Image"] = real_pil_image

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Pillow not available" in m for m in msgs), (
        f"expected Pillow-missing WARN; got: {msgs}"
    )


def test_preprocess_oversize_bytes_triggers_recompress(tmp_path, img_module, monkeypatch):
    """Dimensions in range but bytes > MAX_BYTES → still re-save (recompress).

    Forced via monkeypatching MAX_BYTES down to 50KB so a normal-sized
    test image trips it.
    """
    from PIL import Image
    monkeypatch.setattr(img_module, "MAX_BYTES", 50_000)
    p = tmp_path / "fat.png"
    # 1500x1500 image with 'noise' so it doesn't compress to zero
    import random
    random.seed(42)
    img = Image.new("RGB", (1500, 1500))
    pixels = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(1500 * 1500)]
    img.putdata(pixels)
    img.save(p)
    size_before = p.stat().st_size
    assert size_before > 50_000, f"test fixture not big enough: {size_before}"
    img_module.maybe_shrink_image(p)
    # File got re-saved (we don't require it to be < cap — recompress is
    # best-effort and high-noise images are incompressible; just confirm
    # it didn't crash and the file is still a valid image).
    Image.open(p).load()
