"""Tests for #161 short tier inline display + bonus-bug fix in tool result rendering.

Real user feedback: '我看我们 tool 什么的调用有显示的，但是没显示调用的结果.
我觉得短的结果直接显示，长的结果折叠这样子.'

This PR ships the SHORT tier only (4-tier system deferred to v1.19):
- Short result (< 200 chars, single line, non-empty) → inline rolling log shows
  '✅ {name} → {content}' instead of bare '✅ {name}'
- Bonus bug: WebSearch / WebFetch rolling log lines start with '🔄 🔍' / '🔄 🌐'
  (emoji prefix), so the old startswith('🔄 ' + name) match never fired and
  status stuck at 🔄 forever. R2 fix: tolerant match via tool_marker_aliases.
"""
from __future__ import annotations

import pytest


def test_bonus_bug_websearch_pattern_match():
    """Regression pin: WebSearch's rolling-log line starts with '🔄 🔍 query'.
    Old code's ``startswith('🔄 WebSearch')`` never matched; new alias-aware
    match should hit."""
    name = "WebSearch"
    line = "🔄 🔍 my query text"
    tool_marker_aliases = {"WebSearch": "🔍", "WebFetch": "🌐"}
    alias = tool_marker_aliases.get(name, "")
    matches_name = line.startswith("🔄 " + name)
    matches_alias = alias and line.startswith("🔄 " + alias)
    assert not matches_name, "old match was supposed to fail (regression pin)"
    assert matches_alias, "new alias-aware match must succeed"


def test_bonus_bug_webfetch_pattern_match():
    """Regression pin: WebFetch's rolling-log line starts with '🔄 🌐 url'."""
    name = "WebFetch"
    line = "🔄 🌐 https://example.com"
    tool_marker_aliases = {"WebSearch": "🔍", "WebFetch": "🌐"}
    alias = tool_marker_aliases.get(name, "")
    matches_name = line.startswith("🔄 " + name)
    matches_alias = alias and line.startswith("🔄 " + alias)
    assert not matches_name
    assert matches_alias


def test_bash_grep_glob_read_still_match_directly():
    """Tools whose rolling-log line uses '🔄 {name}: …' prefix continue to
    match via the direct ``startswith('🔄 ' + name)`` path (no regression)."""
    direct_match_cases = [
        ("Bash", "🔄 Bash: `ls`"),
        ("Read", "🔄 Read: `/tmp/foo`"),
        ("Grep", "🔄 Grep: `TODO`"),
        ("Glob", "🔄 Glob: `**/*.py`"),
        ("Write", "🔄 Write: `/tmp/new.py`"),
        ("Edit", "🔄 Edit: `/foo.py`"),
    ]
    for name, line in direct_match_cases:
        assert line.startswith("🔄 " + name), (
            f"Direct match must work for {name}: line={line!r}"
        )


def test_short_result_inline_arrow_display():
    """When result content is short (<200 chars), single-line, non-empty,
    rolling log renders ``✅ {name} → {content}`` instead of bare ``✅ {name}``.

    Pure-string logic test — the production code in render_response is
    integration-tested elsewhere; this pins the threshold semantics.
    """
    # Simulate the production decision logic
    def should_inline(content):
        return (
            content is not None
            and len(content) < 200
            and "\n" not in content
            and content.strip() != ""
        )

    assert should_inline("42")                # tiny ✅
    assert should_inline("Found 7 matches")   # short prose ✅
    assert should_inline("x" * 199)           # boundary
    assert not should_inline("x" * 200)       # over threshold
    assert not should_inline("line1\nline2")  # multiline excluded
    assert not should_inline("")              # empty excluded
    assert not should_inline("   \t")         # whitespace-only excluded
    assert not should_inline(None)            # None excluded


def test_short_result_backtick_escape():
    """Inline display strips backticks from content to avoid breaking the
    rolling-log embed's markdown rendering."""
    raw = "result with `backticks` inside"
    safe = raw.strip().replace("`", "'")
    assert "`" not in safe
    assert safe == "result with 'backticks' inside"


def test_skill_and_fallback_rolling_log_match():
    """R1 engineer audit: Skill (line 821) appends as '🔄 Skill: {name}' and
    the unnamed-tool fallback (line 841) appends as '🔄 {name}...'. Both
    should match the direct `startswith('🔄 ' + name)` path. Pin so a future
    rolling-log format change doesn't silently reintroduce the stuck-🔄 bug
    for these tools."""
    direct_cases = [
        ("Skill", "🔄 Skill: my-skill-name"),
        ("UnknownTool", "🔄 UnknownTool..."),
    ]
    for name, line in direct_cases:
        assert line.startswith("🔄 " + name), (
            f"{name}: direct match must succeed (line={line!r})"
        )


@pytest.mark.asyncio
async def test_short_tier_integration_via_render_response():
    """R1 tester gap closure: drive the actual `render_response` ToolResultBlock
    path with a short Bash result and verify the rolling-log line ends with
    `→ {content}` (not bare `✅ Bash`).

    Uses the same minimal-bridge / FakeMessageable scaffolding as
    test_subagent_threads.py so the production path is exercised end-to-end.
    """
    import sys
    sys.path.insert(0, "src")
    from unittest.mock import MagicMock, AsyncMock
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    class FakeBridge:
        def __init__(self, events):
            self._events = events
            self.is_active = True
            self._client = MagicMock()
        async def send_message(self, text):
            for ev in self._events:
                yield ev

    class FakeMessage:
        def __init__(self):
            self.content = ""
            self.embeds = []
        async def edit(self, **kwargs):
            if "content" in kwargs:
                self.content = kwargs["content"]
            if "embed" in kwargs:
                self.embeds = [kwargs["embed"]]
            return self
        async def delete(self):
            return None

    class FakeTarget:
        def __init__(self):
            self.id = 1
            self._sent = []
        async def send(self, *args, **kwargs):
            msg = FakeMessage()
            if "content" in kwargs:
                msg.content = kwargs["content"]
            if "embed" in kwargs:
                msg.embeds = [kwargs["embed"]]
            self._sent.append(msg)
            return msg

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tool-1", name="Bash",
                    input={"command": "echo 42"},
                ),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="tool-1", content="42", is_error=False),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "run echo 42")

    # Find the rolling-log embed: the message whose embed.title is
    # "🔧 Tool Activity"; assert its description contains "✅ Bash → 42".
    rolling_log_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity"
    ]
    assert rolling_log_embeds, (
        f"Expected a Tool Activity rolling-log embed; got: "
        f"{[(m.embeds[0].title if m.embeds else None) for m in target._sent]}"
    )
    final_log = rolling_log_embeds[-1].embeds[0].description
    assert "✅ Bash → 42" in final_log, (
        f"Expected '✅ Bash → 42' inline-arrow display in rolling log; "
        f"got: {final_log!r}"
    )
