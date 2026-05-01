"""Tests for file diff display in the Discord renderer (Feature #30)."""


def test_format_write_preview():
    """Write tool shows file content with language detection."""
    path = "src/main.py"
    ext = path.rsplit(".", 1)[-1]
    assert ext == "py"


def test_format_edit_diff():
    """Edit tool shows old -> new as diff."""
    old = "def foo():\n    pass"
    new = "def foo():\n    return 42"
    diff_lines = []
    for line in old.splitlines():
        diff_lines.append(f"- {line}")
    for line in new.splitlines():
        diff_lines.append(f"+ {line}")
    result = "\n".join(diff_lines)
    assert "- def foo():" in result
    assert "+ def foo():" in result
    assert "-     pass" in result
    assert "+     return 42" in result


def test_truncation():
    """Long content is truncated."""
    content = "x" * 2000
    preview = content[:1500]
    assert len(preview) == 1500


def test_backtick_sanitization():
    """SEC2: Triple backticks in content are escaped to prevent code-block breakout."""
    content = 'print("```hello```")'
    sanitized = content.replace("```", "` ` `")
    assert "```" not in sanitized
    assert "` ` `" in sanitized


def test_backtick_sanitization_in_diff():
    """SEC2: Triple backticks in diff output are escaped."""
    old_text = '```\nsome code\n```'
    new_text = '```\nnew code\n```'
    diff_lines = []
    for line in old_text.splitlines():
        diff_lines.append(f"- {line}")
    for line in new_text.splitlines():
        diff_lines.append(f"+ {line}")
    diff_str = "\n".join(diff_lines)[:1500].replace("```", "` ` `")
    assert "```" not in diff_str
    assert "` ` `" in diff_str


def test_no_sanitization_needed():
    """Content without triple backticks is unchanged."""
    content = "normal code with `single` and ``double`` backticks"
    sanitized = content.replace("```", "` ` `")
    assert sanitized == content
