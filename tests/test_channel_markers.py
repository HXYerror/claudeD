"""Tests for channel/thread management marker patterns (imports from source)."""

from clauded.discord_renderer import _THREAD_PATTERN, _CHANNEL_PATTERN


def test_thread_pattern_match():
    """Basic thread marker is parsed correctly."""
    text = "Hello [CREATE_THREAD: My Thread] world"
    match = _THREAD_PATTERN.search(text)
    assert match is not None
    assert match.group(1) == "My Thread"


def test_channel_pattern_match():
    """Basic channel marker is parsed correctly."""
    text = "Creating [CREATE_CHANNEL: dev-chat] now"
    match = _CHANNEL_PATTERN.search(text)
    assert match is not None
    assert match.group(1) == "dev-chat"


def test_no_match():
    """Text without markers produces no matches."""
    text = "No markers here"
    assert _THREAD_PATTERN.search(text) is None
    assert _CHANNEL_PATTERN.search(text) is None


def test_no_false_positive():
    """Marker keywords without brackets don't match."""
    assert _THREAD_PATTERN.search("CREATE_THREAD without brackets") is None
    assert _CHANNEL_PATTERN.search("CREATE_CHANNEL without brackets") is None


def test_multiple_markers():
    """Multiple markers in one string are all found."""
    text = "[CREATE_THREAD: A] and [CREATE_THREAD: B]"
    matches = _THREAD_PATTERN.findall(text)
    assert len(matches) == 2
    assert matches[0] == "A"
    assert matches[1] == "B"


def test_thread_pattern_captures_name():
    """Group 1 captures exactly the thread name."""
    m = _THREAD_PATTERN.search("[CREATE_THREAD: test]")
    assert m is not None
    assert m.group(1) == "test"


def test_channel_pattern_captures_name():
    """Group 1 captures exactly the channel name."""
    m = _CHANNEL_PATTERN.search("[CREATE_CHANNEL: general]")
    assert m is not None
    assert m.group(1) == "general"


def test_whitespace_variations():
    """Patterns tolerate extra whitespace."""
    assert _THREAD_PATTERN.search("[CREATE_THREAD:   lots of spaces  ]") is not None
    assert _CHANNEL_PATTERN.search("[CREATE_CHANNEL:   spaces  ]") is not None


def test_mixed_markers():
    """Both thread and channel markers in one string."""
    text = "[CREATE_THREAD: t1] [CREATE_CHANNEL: c1]"
    assert _THREAD_PATTERN.search(text) is not None
    assert _CHANNEL_PATTERN.search(text) is not None
    assert _THREAD_PATTERN.search(text).group(1) == "t1"
    assert _CHANNEL_PATTERN.search(text).group(1) == "c1"
