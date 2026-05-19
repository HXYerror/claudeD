"""#242 — pre-shrink large image attachments before Claude CLI sees them.

Claude CLI bundled binary has a hard-coded image limit pipeline (`Vc`
in the reverse-engineered binary):

- maxWidth: 2000 / maxHeight: 2000
- targetRawSize: 3.75 MB
- maxBase64Size: 5 MB

When an attachment exceeds ANY of these, the CLI runs an internal
``sharp.resize(maxWidth, maxHeight) + jpeg-quality-degrade`` chain. In
the worst case, the chain falls back to ``resize(400, 400, fit:inside)
.jpeg({quality: 20})`` — a ~4 KB blur that the user sees as "claude
says the image is broken / can't read the text".

Confirmed by live SDK probe (5/19): a 3840x2160 75 KB PNG fed through
Claude's Read tool came back as 1999x1124 (just under the 2000 limit).

This module pre-shrinks images to **1900x1900 / 3.5 MB** (5% safety
margin below the CLI thresholds) using high-quality LANCZOS resampling
and JPEG quality 92 (vs. CLI's worst-case quality 20). The CLI never
trips its internal resize chain, so the Vision API receives a
deliberately-down-sampled-by-us image rather than a CLI-mangled blur.

Fail-soft: any PIL exception logs WARN and leaves the original file
on disk. The Read pipeline then degrades to whatever CLI does, which
is the pre-#242 status quo.

Supported formats:

* ``.png`` / ``.jpg`` / ``.jpeg`` — resized + re-saved
* ``.webp`` — resized + re-saved (preserves animation only if single-frame)
* ``.bmp`` — resized + re-saved
* ``.gif`` / ``.svg`` — **skipped** (GIF can be animated, SVG is XML
  vector data); CLI will handle them per its own rules.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("clauded.image_preprocess")


# Conservative 5% safety margin below the CLI's hard limits.
# CLI binary uses (2000, 2000, 3.75MB). We aim for (1900, 1900, 3.5MB).
MAX_DIM_PX = 1900
MAX_BYTES = 3_500_000

JPEG_QUALITY = 92  # vs CLI worst-case 20.
JPEG_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg"})
RESIZABLE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
# GIF could be animated (PIL re-save destroys frames); SVG is XML. Skip both.


def maybe_shrink_image(path: Path) -> None:
    """Resize ``path`` in-place if it exceeds CLI image limits.

    No-op when the file is already within limits or has an
    unrecognised / animated-risk extension. Never raises: PIL
    failures fall back to a WARN log and leave the original file.
    """
    ext = path.suffix.lower()
    if ext not in RESIZABLE_EXTS:
        log.debug("#242: skip preprocess for %s (ext %s not in resizable set)", path.name, ext)
        return

    try:
        size_before = path.stat().st_size
    except OSError as exc:
        log.warning("#242: stat failed for %s: %s", path, exc)
        return

    # PIL is imported lazily — keeps clauded importable on hosts without
    # Pillow (CI smoke runs the module loader before installing deps).
    try:
        from PIL import Image
    except ImportError:
        log.warning("#242: Pillow not available; image preprocessing disabled")
        return

    try:
        img = Image.open(path)
        # ``Image.open`` is lazy; .load() forces the decode so a
        # corrupt file fails NOW (caught in the except) rather than
        # surfacing as a confusing error during the .resize() call.
        img.load()
    except Exception as exc:
        log.warning(
            "#242: PIL failed to open %s (%s); leaving original",
            path.name, exc,
        )
        return

    width, height = img.size

    # Two trigger conditions, matching the CLI's: either dimension
    # exceeds the cap, OR raw bytes exceed the size cap. Either one
    # forces a re-save.
    needs_resize = width > MAX_DIM_PX or height > MAX_DIM_PX
    needs_recompress = size_before > MAX_BYTES

    if not needs_resize and not needs_recompress:
        log.debug(
            "#242: %s already within limits (%dx%d, %d bytes); skip",
            path.name, width, height, size_before,
        )
        return

    try:
        if needs_resize:
            # thumbnail() preserves aspect ratio + does in-place resize on
            # the Image object.
            img.thumbnail((MAX_DIM_PX, MAX_DIM_PX), Image.Resampling.LANCZOS)

        # Re-save. JPEG path uses explicit quality; PNG/WebP/BMP use
        # format defaults + optimize=True for size.
        save_kwargs: dict = {"optimize": True}
        if ext in JPEG_EXTS:
            save_kwargs["quality"] = JPEG_QUALITY
            # If the source had transparency, JPEG can't preserve it.
            # Flatten onto white so we don't bomb on RGBA.
            if img.mode in ("RGBA", "LA", "P"):
                from PIL import Image as _Image
                bg = _Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg

        img.save(path, **save_kwargs)
        size_after = path.stat().st_size
        new_w, new_h = img.size
        log.info(
            "#242: preprocessed %s: %dx%d (%d bytes) -> %dx%d (%d bytes)",
            path.name, width, height, size_before, new_w, new_h, size_after,
        )
    except Exception as exc:
        log.warning(
            "#242: PIL save failed for %s (%s); leaving original",
            path.name, exc,
        )
        # Best-effort: if a partial write happened, the file is now
        # smaller-but-broken. We can't easily un-corrupt, but at minimum
        # the WARN tells PM what to look for.
