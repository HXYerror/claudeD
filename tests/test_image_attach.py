"""#222 — surface markdown image refs as Discord attachments.

Coverage:
- Allowlist accept/reject matrix
- Symlink escape rejection
- Regex extraction (alt-text discarded, multi-image, case-insensitive)
- Missing-file / size-cap rejection
- Batch-10 send wiring

The new helpers live in ``clauded.discord_renderer``:
- ``_is_path_allowed(path, project_path)`` — pure function, easy unit test
- ``_IMG_PATTERN`` — regex
- ``DiscordRenderer._process_image_inlines`` — text strip + queue
- ``DiscordRenderer._send_text_with_attachments`` — batched send
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.discord_renderer import (
    DiscordRenderer,
    _IMG_MAX_BYTES,
    _IMG_PATTERN,
    _is_path_allowed,
)


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("![alt](/tmp/x.png)", ["/tmp/x.png"]),
    ("![alt](/tmp/x.PNG)", ["/tmp/x.PNG"]),
    ("![alt](/tmp/x.Png)", ["/tmp/x.Png"]),
    ("![alt](/tmp/x.JpEg)", ["/tmp/x.JpEg"]),
    ("![a](/tmp/a.png) and ![b](/tmp/b.jpg)", ["/tmp/a.png", "/tmp/b.jpg"]),
    ("![](/tmp/no-alt.gif)", ["/tmp/no-alt.gif"]),
    ("![alt](https://example.com/x.png)", ["https://example.com/x.png"]),
    # Mixed: regex matches webp and jpeg too
    ("![](/tmp/a.webp) ![](/tmp/b.jpeg)", ["/tmp/a.webp", "/tmp/b.jpeg"]),
])
def test_img_pattern_captures(text, expected):
    matches = _IMG_PATTERN.findall(text)
    paths = [m[1] for m in matches]
    assert paths == expected


def test_img_pattern_skips_unsupported_extensions():
    """Regex limited to png/jpg/jpeg/gif/webp."""
    text = "![alt](/tmp/script.sh) ![alt](/tmp/doc.pdf) ![alt](/tmp/x.png)"
    paths = [m[1] for m in _IMG_PATTERN.findall(text)]
    assert paths == ["/tmp/x.png"]


def test_img_pattern_no_match_when_not_image_syntax():
    text = "Open [the file](path.png) for review"  # no leading !
    assert _IMG_PATTERN.findall(text) == []


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_allowlist_accepts_tmp(tmp_path):
    """/tmp is the default allowed root (after resolving)."""
    # Create a real file under /tmp so resolve(strict=True) succeeds
    fp = Path("/tmp") / "test_222_allow.png"
    fp.touch()
    try:
        assert _is_path_allowed(fp, None) is True
    finally:
        fp.unlink(missing_ok=True)


def test_allowlist_rejects_etc_passwd():
    """The core security promise — /etc/passwd must never be lifted."""
    assert _is_path_allowed(Path("/etc/passwd"), None) is False


def test_allowlist_rejects_home_ssh():
    """$HOME is not in the allowlist; ~/.ssh/id_rsa must be rejected."""
    fake = Path.home() / ".ssh" / "id_rsa"
    # Doesn't even need to exist; resolve(strict=True) returns False on missing.
    assert _is_path_allowed(fake, None) is False


def test_allowlist_accepts_project_path(tmp_path):
    """Files under the bound project_path are allowed."""
    img = tmp_path / "build" / "report.png"
    img.parent.mkdir()
    img.touch()
    assert _is_path_allowed(img, tmp_path) is True


def test_allowlist_rejects_outside_project(tmp_path):
    """Even with a project_path set, files OUTSIDE it that are also
    outside /tmp are rejected."""
    elsewhere = Path("/Users") if Path("/Users").exists() else Path("/home")
    if not elsewhere.exists():
        pytest.skip("no /Users or /home on this host")
    # Find any existing file under elsewhere that's not under tmp_path
    candidates = [p for p in elsewhere.iterdir() if p.is_file()]
    if not candidates:
        pytest.skip("no files to test against")
    assert _is_path_allowed(candidates[0], tmp_path) is False


def test_allowlist_rejects_missing_file(tmp_path):
    """resolve(strict=True) returns False for non-existent paths."""
    assert _is_path_allowed(tmp_path / "doesnt-exist.png", tmp_path) is False


def test_allowlist_rejects_symlink_escape(tmp_path):
    """Critical security pin: symlink under /tmp pointing to /etc/passwd
    must be rejected after resolution."""
    if not Path("/etc/passwd").exists():
        pytest.skip("/etc/passwd not present (non-unix?)")
    link = Path("/tmp") / "test_222_escape_link"
    link.unlink(missing_ok=True)
    try:
        link.symlink_to("/etc/passwd")
        # link itself IS under /tmp but resolved target is /etc/passwd
        assert _is_path_allowed(link, None) is False, (
            "#222 SECURITY: symlink /tmp/x → /etc/passwd must be rejected "
            "(resolve() should leave the path outside allowlist)"
        )
    finally:
        link.unlink(missing_ok=True)


def test_allowlist_accepts_tmpdir_env(tmp_path, monkeypatch):
    """macOS $TMPDIR (per-user temp under /var/folders/...) is allowed."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    fp = tmp_path / "x.png"
    fp.touch()
    assert _is_path_allowed(fp, None) is True


# ---------------------------------------------------------------------------
# _process_image_inlines — instance method
# ---------------------------------------------------------------------------


def _make_renderer(project_path=None) -> DiscordRenderer:
    r = DiscordRenderer.__new__(DiscordRenderer)
    r._project_path = project_path
    r.target = MagicMock()
    return r


def test_process_image_inlines_strips_allowed_paths(tmp_path):
    """Allowed image refs are removed from text; their paths are queued."""
    img = tmp_path / "ok.png"
    img.touch()
    r = _make_renderer(project_path=tmp_path)
    text = f"Before ![alt]({img}) After"
    cleaned, attachments = r._process_image_inlines(text)
    assert cleaned == "Before  After"
    assert attachments == [img]


def test_process_image_inlines_leaves_rejected_paths(tmp_path, caplog):
    """Rejected paths (outside allowlist) stay in text verbatim + log warning."""
    import logging
    r = _make_renderer(project_path=tmp_path)  # tmp_path only
    # Use an extension-matching path that's OUTSIDE allowlist. Since we
    # need _is_path_allowed to be called, the path must pass the regex
    # filter; /etc/passwd alone doesn't (no .png/.jpg/etc extension).
    text = "![bad](/etc/totally-malicious-not-a-real-file.png)"
    caplog.set_level(logging.WARNING, logger="clauded.discord_renderer")
    cleaned, attachments = r._process_image_inlines(text)
    assert cleaned == text, "rejected path must NOT be stripped"
    assert attachments == []
    # Warning logged
    warns = [w for w in caplog.records if w.levelno == logging.WARNING]
    assert any("#222" in w.getMessage() and "rejected" in w.getMessage() for w in warns), (
        f"Expected #222 WARNING; got: {[w.getMessage() for w in caplog.records]}"
    )


def test_process_image_inlines_rejects_oversize(tmp_path, monkeypatch):
    """#222 AC5: Files > _IMG_MAX_BYTES are rejected with size in the log."""
    import logging
    from clauded import discord_renderer

    # Build a stub that reports a huge size without actually allocating
    img = tmp_path / "big.png"
    img.write_bytes(b"x")  # 1 byte; we'll fake stat
    monkeypatch.setattr(discord_renderer, "_IMG_MAX_BYTES", 0)  # force any size to fail
    r = _make_renderer(project_path=tmp_path)
    import logging
    caplog_records: list = []
    handler = logging.Handler()
    handler.emit = lambda r: caplog_records.append(r)
    logging.getLogger("clauded.discord_renderer").addHandler(handler)
    logging.getLogger("clauded.discord_renderer").setLevel(logging.WARNING)
    try:
        cleaned, attachments = r._process_image_inlines(f"![]({img})")
        assert attachments == []
        assert f"![]({img})" in cleaned  # not stripped
        # AC5 strengthened: log mentions size + cap explicitly
        size_warns = [
            rec for rec in caplog_records
            if rec.levelno == logging.WARNING
            and "size" in rec.getMessage() and "cap" in rec.getMessage()
        ]
        assert size_warns, (
            f"AC5: oversize WARNING must include 'size' + 'cap'; "
            f"got: {[r.getMessage() for r in caplog_records]}"
        )
    finally:
        logging.getLogger("clauded.discord_renderer").removeHandler(handler)


def test_process_image_inlines_multi_image_partial_reject(tmp_path):
    """One valid + one invalid → text keeps invalid + queues valid."""
    good = tmp_path / "ok.png"
    good.touch()
    r = _make_renderer(project_path=tmp_path)
    # /etc/totally-fake.png matches regex but doesn't exist anywhere,
    # so _is_path_allowed returns False and the markdown stays.
    bad_path = "/etc/totally-fake-shadow-mimic.png"
    text = f"![a]({good}) and ![b]({bad_path})"
    cleaned, attachments = r._process_image_inlines(text)
    assert attachments == [good]
    # The bad reference must remain
    assert bad_path in cleaned
    # But the good image was stripped
    assert str(good) not in cleaned


# ---------------------------------------------------------------------------
# _send_text_with_attachments — batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_with_attachments_one_batch(tmp_path):
    """≤10 attachments → single send call with all files."""
    paths = []
    for i in range(3):
        p = tmp_path / f"{i}.png"
        p.touch()
        paths.append(p)

    r = _make_renderer(project_path=tmp_path)
    sent_args = []
    async def _spy(**kwargs):
        sent_args.append(kwargs)
        return MagicMock(id=100)
    r._safe_send = _spy
    r._smart_split = lambda t, limit: [t]

    await r._send_text_with_attachments("Look:", paths)
    assert len(sent_args) == 1
    assert sent_args[0]["content"] == "Look:"
    assert len(sent_args[0]["files"]) == 3


@pytest.mark.asyncio
async def test_send_text_with_attachments_multi_batch(tmp_path):
    """11+ attachments → first 10 with text, rest in attachment-only follow-ups."""
    paths = []
    for i in range(15):
        p = tmp_path / f"{i}.png"
        p.touch()
        paths.append(p)

    r = _make_renderer(project_path=tmp_path)
    sent_args = []
    async def _spy(**kwargs):
        sent_args.append(kwargs)
        return MagicMock(id=100)
    r._safe_send = _spy
    r._smart_split = lambda t, limit: [t]

    await r._send_text_with_attachments("Captions", paths)
    assert len(sent_args) == 2
    assert sent_args[0]["content"] == "Captions"
    assert len(sent_args[0]["files"]) == 10
    # Second call: attachment-only (no content kwarg or None)
    assert sent_args[1].get("content") is None
    assert len(sent_args[1]["files"]) == 5


@pytest.mark.asyncio
async def test_send_text_with_attachments_empty_attachments_returns(tmp_path):
    """No attachments + no text → no send call."""
    r = _make_renderer(project_path=tmp_path)
    sent_args = []
    async def _spy(**kwargs):
        sent_args.append(kwargs)
        return MagicMock(id=100)
    r._safe_send = _spy
    await r._send_text_with_attachments("", [])
    assert sent_args == []


# ---------------------------------------------------------------------------
# Integration: project_path plumbing through __init__
# ---------------------------------------------------------------------------


def test_renderer_init_accepts_project_path():
    """__init__ accepts project_path kwarg per #222."""
    target = MagicMock()
    r = DiscordRenderer(target, bot=None, project_path=Path("/repo"))
    assert r._project_path == Path("/repo")


def test_renderer_init_project_path_default_none():
    """Backward compat: omitting project_path keeps it None."""
    target = MagicMock()
    r = DiscordRenderer(target, bot=None)
    assert r._project_path is None


# ---------------------------------------------------------------------------
# Integration: _finalize_typewriter wires _process_image_inlines into the
# pipeline. Mental revert R1 tester finding — ensure deletion of the
# try/finally drain breaks something.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_typewriter_drains_attachments_no_tables(tmp_path):
    """#222 R1 tester: integration test. _finalize_typewriter must call
    _send_text_with_attachments when text contains image markdown.
    Removing the try/finally drain would silently drop attachments —
    this test would FAIL.
    """
    img = tmp_path / "final.png"
    img.touch()
    target = MagicMock()
    target.send = AsyncMock(return_value=MagicMock(id=1))
    r = DiscordRenderer.__new__(DiscordRenderer)
    r._project_path = tmp_path
    r.target = target
    r._last_msg = None
    r._last_msg_text = ""
    r._bot = None

    drained_paths: list = []
    async def _spy_send_attachments(text, attachments):
        drained_paths.extend(attachments)
    r._send_text_with_attachments = _spy_send_attachments

    # Stub the rest of the pipeline
    r._process_markers = AsyncMock(side_effect=lambda t: t)
    r._extract_and_render_tables = AsyncMock(return_value=("hi", []))
    r._smart_split = lambda t, limit: [t]
    r._safe_send = AsyncMock(return_value=MagicMock(id=2))
    r._typewriter_apply = AsyncMock(return_value=MagicMock(id=3))

    live_msg = MagicMock()
    buffer = f"Look ![alt]({img}) here"
    await r._finalize_typewriter(live_msg, buffer)

    assert drained_paths == [img], (
        f"#222 wire-in: _finalize_typewriter must drain image attachments. "
        f"Got drained={drained_paths!r}"
    )


@pytest.mark.asyncio
async def test_finalize_typewriter_drains_attachments_is_final_false(tmp_path):
    """Same as above but for the pre-tool-use interleave path (is_final=False).

    The try/finally drain MUST fire on this early-return branch too.
    """
    img = tmp_path / "final.png"
    img.touch()
    r = DiscordRenderer.__new__(DiscordRenderer)
    r._project_path = tmp_path
    r.target = MagicMock()
    r._last_msg = None
    r._last_msg_text = ""
    r._bot = None

    drained: list = []
    async def _spy(text, attachments):
        drained.extend(attachments)
    r._send_text_with_attachments = _spy
    r._process_markers = AsyncMock(side_effect=lambda t: t)
    r._smart_split = lambda t, limit: [t]
    r._safe_send = AsyncMock(return_value=MagicMock(id=2))
    r._safe_edit = AsyncMock(return_value=True)

    buffer = f"![alt]({img})"
    await r._finalize_typewriter(None, buffer, is_final=False)
    assert drained == [img]


@pytest.mark.asyncio
async def test_finalize_typewriter_drains_even_on_exception(tmp_path):
    """R1 simplicity rationale: try/finally means even a mid-flight
    exception in the body still drains the attachments — we never
    leak handles or silently drop images.
    """
    img = tmp_path / "final.png"
    img.touch()
    r = DiscordRenderer.__new__(DiscordRenderer)
    r._project_path = tmp_path
    r.target = MagicMock()
    r._last_msg = None
    r._last_msg_text = ""
    r._bot = None

    drained: list = []
    async def _spy(text, attachments):
        drained.extend(attachments)
    r._send_text_with_attachments = _spy
    # Make markers raise
    async def _boom(t):
        raise RuntimeError("planted-mid-flight")
    r._process_markers = _boom

    buffer = f"![alt]({img})"
    with pytest.raises(RuntimeError, match="planted-mid-flight"):
        await r._finalize_typewriter(MagicMock(), buffer)
    assert drained == [img], (
        "try/finally must drain even if body raises"
    )
