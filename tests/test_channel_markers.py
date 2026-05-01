"""Tests for channel/thread management marker patterns."""

import re

_THREAD_PATTERN = re.compile(r'\[CREATE_THREAD:\s*(.+?)\]')
_CHANNEL_PATTERN = re.compile(r'\[CREATE_CHANNEL:\s*(.+?)\]')


def test_thread_pattern_match():
    text = "Hello [CREATE_THREAD: My Thread] world"
    match = _THREAD_PATTERN.search(text)
    assert match is not None
    assert match.group(1) == "My Thread"


def test_channel_pattern_match():
    text = "Creating [CREATE_CHANNEL: dev-chat] now"
    match = _CHANNEL_PATTERN.search(text)
    assert match is not None
    assert match.group(1) == "dev-chat"


def test_no_match():
    text = "No markers here"
    assert _THREAD_PATTERN.search(text) is None
    assert _CHANNEL_PATTERN.search(text) is None


def test_multiple_markers():
    text = "[CREATE_THREAD: A] and [CREATE_THREAD: B]"
    matches = _THREAD_PATTERN.findall(text)
    assert len(matches) == 2
    assert matches[0] == "A"
    assert matches[1] == "B"
