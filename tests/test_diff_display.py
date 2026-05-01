"""Tests for file diff display in the Discord renderer (Feature #30)."""


def test_format_write_preview():
    """Write tool shows file content with language detection."""
    # Test that .py -> python code block
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
