"""#204 — sub-agent rolling-log short-tier parity with main thread.

Pre-#204 behavior: sub-agent rolling log just swapped 🔄 → ✅/❌, losing
the inline content surfaced on the main thread by #161/#165/#180.

This PR ports the short-tier (< 200 chars, non-empty) and error-text
formatting to the sub-agent path. Medium-tier button is out of scope
(needs per-sub-thread view-store work).
"""
from __future__ import annotations

import inspect


def _get_subagent_resultblock_source() -> str:
    """Pull the sub-agent ToolResultBlock handler source via inspect."""
    from clauded import discord_renderer
    src = inspect.getsource(discord_renderer)
    start = src.find("#204: extract content")
    assert start != -1, "#204 marker comment missing"
    end = src.find("if sub_renderer._tool_log_msg", start)
    return src[start:end]


def test_204_short_tier_format_present():
    """Short-tier line format `{status} {name} → {safe}` appears in subagent path."""
    section = _get_subagent_resultblock_source()
    assert "tool_label} \u2192 {safe}" in section, (
        "#204: short-tier format with arrow + content missing in sub-agent path"
    )


def test_204_error_tier_format_present():
    """Error-tier line: `{status} {name}: {error_text[:100]}`."""
    section = _get_subagent_resultblock_source()
    assert "tool_label}: {error_text}" in section


def test_204_uses_extract_block_content_text():
    """#204: sub-agent path now uses the same content extraction helper as main."""
    section = _get_subagent_resultblock_source()
    assert "_extract_block_content_text(block.content)" in section


def test_204_short_predicate_matches_main():
    """Short predicate: `< 200 chars and non-empty`."""
    section = _get_subagent_resultblock_source()
    assert "len(content_str) < 200" in section


def test_204_backtick_strip_and_newline_collapse():
    """Pin the formatting behavior — content gets backticks stripped + \\n collapsed."""
    section = _get_subagent_resultblock_source()
    # Backtick strip — the source has a .replace call with a backtick
    assert "`" in section and "replace" in section, "backtick strip pattern missing"
    # Newline collapse to │
    assert "\u2502" in section, "newline collapse to │ missing"


def test_204_medium_tier_button_out_of_scope():
    """Per #204, the medium-tier button is OUT of scope. The sub-agent
    path must NOT have a button or `add_view` plumbing."""
    section = _get_subagent_resultblock_source()
    assert "ToolResultsView" not in section
    assert "is_medium" not in section


def test_204_fallback_to_legacy_line_format_when_not_short_or_err():
    """When content is empty or too long for short tier, fall back to
    legacy ``{status} {body}`` format (current behavior preserved)."""
    section = _get_subagent_resultblock_source()
    assert "{status} {body}" in section
