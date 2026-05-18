"""#181 — xlong tier: ToolResultBlock content >= 8000 chars surfaces as .txt attachment.

Pre-#181: results >= 8000 chars fell to bare `✅ Bash` else branch — user
saw the emoji but lost access to the content entirely.

Post-#181: rolling-log line shows `✅ Bash: N lines / M chars (see
attached file)` + a follow-up message with the .txt attachment.
"""
from __future__ import annotations

import inspect


def _renderer_source() -> str:
    from clauded import discord_renderer
    return inspect.getsource(discord_renderer)


def test_181_is_xlong_predicate_at_8000_boundary():
    """Threshold: `len(content) >= 8000` (boundary inclusive)."""
    src = _renderer_source()
    # Must reference the explicit threshold
    assert "len(content_str) >= 8000" in src, (
        "#181: xlong predicate must be `len(content_str) >= 8000`"
    )


def test_181_is_xlong_excludes_errors_and_empty():
    """xlong predicate excludes errors + empty content."""
    src = _renderer_source()
    # Pin the relevant subset of the predicate
    start = src.find("is_xlong = (")
    assert start != -1, "is_xlong assignment must exist"
    block = src[start : start + 400]
    assert "not is_err" in block
    assert "content_str.strip()" in block


def test_181_xlong_rolling_log_format():
    """Rolling log line: `{status} {name}: N lines / M chars (see attached file)`."""
    src = _renderer_source()
    # Pin the format substring
    assert "(see attached file)" in src, (
        "#181: xlong rolling-log line must end with `(see attached file)`"
    )
    # Pin the chars count is in the format
    start = src.find("(see attached file)")
    block = src[max(0, start - 300) : start + 60]
    assert "lines /" in block
    assert "chars" in block


def test_181_xlong_file_attachment_send():
    """xlong path sends `discord.File(fp=io.BytesIO(content_bytes), filename=...)`."""
    src = _renderer_source()
    # Pin the send call shape — encoded bytes + io.BytesIO + safe filename
    assert "content_str.encode(" in src, "#181: must encode content_str to bytes"
    assert 'filename=f"{safe_name}_result.txt"' in src, (
        "#181: file must use `{tool_lowercased}_result.txt` filename"
    )
    # Pin: caption format
    assert "📄" in src, "#181: file message uses 📄 prefix per AC"


def test_181_xlong_error_path_does_not_emit_file():
    """Error path bypasses xlong — `if is_xlong and not is_err and tool_id:`."""
    src = _renderer_source()
    # Pin the send guard
    assert "if is_xlong and not is_err and tool_id:" in src


def test_181_is_xlong_default_false():
    """Pre-loop default: `is_xlong = False` (mirrors #226 pattern for is_medium)."""
    src = _renderer_source()
    assert "is_xlong = False" in src, (
        "#181: is_xlong must have a pre-loop False default — same #226 "
        "no-match safety as is_medium"
    )


def test_181_is_short_unchanged():
    """Short tier predicate unchanged: < 200 chars."""
    src = _renderer_source()
    assert "len(content_str) < 200" in src


def test_181_is_medium_upper_bound_unchanged():
    """Medium tier upper bound: < 8000 (now exclusive of xlong)."""
    src = _renderer_source()
    assert "200 <= len(content_str) < 8000" in src
