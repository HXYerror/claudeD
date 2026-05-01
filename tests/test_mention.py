"""Tests for @bot mention trigger logic."""


def test_strip_mention():
    """Bot mention is stripped from message content."""
    bot_id = 12345
    raw = f"<@{bot_id}> explain this code"
    cleaned = raw.replace(f'<@{bot_id}>', '').strip()
    assert cleaned == "explain this code"


def test_strip_mention_with_nickname():
    bot_id = 12345
    raw = f"<@!{bot_id}> explain this code"
    cleaned = raw.replace(f'<@!{bot_id}>', '').strip()
    assert cleaned == "explain this code"


def test_mention_only_falls_back():
    bot_id = 12345
    raw = f"<@{bot_id}>"
    cleaned = raw.replace(f'<@{bot_id}>', '').strip()
    result = cleaned or "Hello"
    assert result == "Hello"
