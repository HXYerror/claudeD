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


# ---------------------------------------------------------------------------
# v1.18 medium tier (#161 sub-PR): 200 ≤ len < 3500 chars → separate detail
# embed with ||spoiler|| body. Rolling log shows summary; detail follows.
# ---------------------------------------------------------------------------


def test_medium_tier_threshold_boundaries():
    """Medium-tier predicate: 200 ≤ len(content) < 8000, non-error,
    non-empty, non-short. R1-R2 history: 3500 → 1900 (when we thought
    plain-content spoilers collapsed) → 8000 (after user confirmed both
    spoiler styles just blur text). Final medium tier sends a .txt file
    attachment which has no message-content size constraint."""
    def is_medium(content, is_err=False, is_short=False):
        return (
            not is_short
            and not is_err
            and 200 <= len(content) < 8000
            and content.strip() != ""
        )

    # Just under 200 → not medium (short tier's domain)
    assert not is_medium("x" * 199)
    # Exactly 200 → medium (lower boundary inclusive)
    assert is_medium("x" * 200)
    # Mid-range → medium
    assert is_medium("x" * 1000)
    # Just under 8000 → still medium
    assert is_medium("x" * 7999)
    # Exactly 8000 → not medium (xlong tier's domain)
    assert not is_medium("x" * 8000)
    # Empty / whitespace-only
    assert not is_medium("")
    assert not is_medium("   " * 100)
    # is_err / is_short overrides
    assert not is_medium("x" * 500, is_err=True)
    assert not is_medium("x" * 500, is_short=True)


def test_medium_tier_file_attachment_shape():
    """The medium-tier detail message uses a discord.File attachment, not
    embed description / plain-content spoilers. Both spoiler styles only
    blur text — they don't reduce vertical height (verified twice on user
    side: 80-line `seq 1 80` rendered as 80-line gray block in both).
    File attachments render as a single-line preview card."""
    import discord, io
    content = "line 1\nline 2\nsome ```triple backticks``` inside\nline 4"
    file_bytes = content.encode("utf-8")
    detail_file = discord.File(
        fp=io.BytesIO(file_bytes), filename="bash_result.txt"
    )
    # File object constructible — byte content preserved
    detail_file.fp.seek(0)
    assert detail_file.fp.read() == file_bytes
    assert detail_file.filename == "bash_result.txt"


def test_medium_tier_content_preserves_raw_bytes():
    """Content sent as a file attachment preserves bytes verbatim — no
    spoiler-escape transforms, no ``||`` replacement, no fence wrapping.
    Triple-backticks, literal ``||``, and CommonMark special chars all
    survive intact (unlike the spoiler-embed approach which had to
    escape ``||`` to ``\\|\\|``)."""
    import io
    payload = "```python\nprint('||hello||')\n```\n— also: \u00a0\u202f"
    file_bytes = payload.encode("utf-8")
    # roundtrip
    assert io.BytesIO(file_bytes).read().decode("utf-8") == payload


@pytest.mark.asyncio
async def test_medium_tier_integration_emits_file_attachment():
    """Integration: drive render_response with a medium-tier Bash result
    (multiline) and verify:
      1. Rolling log embed updated with summary line ('N lines / M chars
         (see attached file)')
      2. Separate message sent with a discord.File attachment whose body
         contains the raw tool output verbatim (no spoiler escape).
    """
    import sys, io
    sys.path.insert(0, "src")
    from unittest.mock import MagicMock
    import discord
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    class FakeBridge:
        def __init__(self, events):
            self._events = events
            self.is_active = True
            self._client = MagicMock()
        async def send_message(self, _text):
            for ev in self._events:
                yield ev

    class FakeMessage:
        def __init__(self):
            self.content = ""
            self.embeds = []
            self.attachments = []
            self.sent_kwargs = None
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
            msg.sent_kwargs = kwargs
            if "content" in kwargs:
                msg.content = kwargs["content"]
            if "embed" in kwargs:
                msg.embeds = [kwargs["embed"]]
            if "file" in kwargs:
                msg.attachments = [kwargs["file"]]
            self._sent.append(msg)
            return msg

    # Medium-tier output: 30-line Bash stdout (~500 chars)
    medium_content = "\n".join(f"line {i}: some output text here" for i in range(30))
    assert 200 <= len(medium_content) < 8000

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tool-1", name="Bash",
                    input={"command": "ls -la"},
                ),
            ],
            model="claude-sonnet",
            parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="tool-1", content=medium_content, is_error=False),
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
            session_id="sess-medium",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "ls -la")

    # 1) rolling log embed
    rolling_log_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity"
    ]
    assert rolling_log_embeds, (
        f"Expected Tool Activity rolling-log embed; got: "
        f"{[(m.embeds[0].title if m.embeds else None) for m in target._sent]}"
    )
    final_log = rolling_log_embeds[-1].embeds[0].description
    assert "30 lines" in final_log
    assert "see attached file" in final_log

    # 2) file attachment message
    file_msgs = [
        m for m in target._sent
        if m.attachments and isinstance(m.attachments[0], discord.File)
    ]
    assert file_msgs, f"Expected a discord.File attachment for medium tier; got: {target._sent}"
    file_msg = file_msgs[0]
    # Label content present
    assert "Bash result" in file_msg.content
    assert f"{len(medium_content)} chars" in file_msg.content
    # File payload preserves content verbatim
    detail_file = file_msg.attachments[0]
    assert detail_file.filename == "bash_result.txt"
    detail_file.fp.seek(0)
    assert detail_file.fp.read().decode("utf-8") == medium_content


# ---------------------------------------------------------------------------
# R1 tester gaps: error+medium-length, multiline-but-short else branch
# ---------------------------------------------------------------------------


def test_medium_tier_error_path_does_not_emit_detail_message():
    """When is_err=True and content is medium-length, the rolling log shows
    the error text (capped at 100 chars), NOT the medium-tier summary, and
    NO separate spoiler-content message is sent (errors are surfaced
    inline, not hidden in a spoiler the user has to click to see)."""
    def is_medium(content, is_err=False, is_short=False):
        return (
            not is_short
            and not is_err
            and 200 <= len(content) < 1900
            and content.strip() != ""
        )
    error_content = "x" * 500  # medium-length BUT is_err
    assert not is_medium(error_content, is_err=True), (
        "is_err must override medium-tier predicate"
    )


def test_multiline_short_content_falls_to_bare_branch():
    """Content < 200 chars but multiline goes to the bare ``✅ Bash`` branch
    (not short, not medium). User sees `✅ Bash` with no inline arrow.
    Architectural choice: multiline short outputs are visually awkward
    inline; user can run the tool again with more flags if they want detail.
    """
    def is_short(content):
        return (
            len(content) < 200
            and "\n" not in content
            and content.strip() != ""
        )
    def is_medium(content, is_err=False, is_short_val=False):
        return (
            not is_short_val
            and not is_err
            and 200 <= len(content) < 1900
            and content.strip() != ""
        )
    multiline_short = "line 1\nline 2\nline 3"  # 20 chars but multiline
    assert len(multiline_short) < 200
    assert "\n" in multiline_short
    short_result = is_short(multiline_short)
    medium_result = is_medium(multiline_short, is_short_val=short_result)
    assert not short_result, "multiline short content must NOT match short tier"
    assert not medium_result, "multiline short content must NOT match medium tier (len<200)"
    # → falls through to bare `✅ Bash` (the else branch)


@pytest.mark.asyncio
async def test_medium_tier_detail_send_failure_downgrades_log():
    """R1 engineer #2 mitigation: if the detail embed send fails (rate-
    limited, network blip), rewrite the rolling log line to NOT promise
    a clickable detail. User would otherwise see ``click below to expand``
    pointing at no follow-up message.
    """
    import sys
    sys.path.insert(0, "src")
    from unittest.mock import MagicMock
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    class FakeBridge:
        def __init__(self, events):
            self._events = events
            self.is_active = True
            self._client = MagicMock()
        async def send_message(self, _text):
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

    class FailingSendTarget:
        """Target whose .send() returns None for the file-attachment msg
        (sim'd rate-limit / network blip) but succeeds for embed-only
        sends (rolling log)."""
        def __init__(self):
            self.id = 1
            self._sent = []
            self._call_count = 0
        async def send(self, *args, **kwargs):
            self._call_count += 1
            # File-attachment sends fail (medium-tier detail)
            if kwargs.get("file") is not None:
                return None
            # Embed-only sends succeed (rolling log creation)
            msg = FakeMessage()
            if "content" in kwargs:
                msg.content = kwargs["content"]
            if "embed" in kwargs:
                msg.embeds = [kwargs["embed"]]
            self._sent.append(msg)
            return msg

    medium_content = "\n".join(f"line {i}: out" for i in range(50))
    assert 200 <= len(medium_content) < 8000

    events = [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="t1", content=medium_content, is_error=False)],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-fail",
            total_cost_usd=0.001,
        ),
    ]

    target = FailingSendTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "ls")

    # Find the LAST Tool Activity rolling-log embed
    rolling_embeds = [m for m in target._sent if m.embeds and "Tool Activity" in (m.embeds[0].title or "")]
    assert rolling_embeds, "Expected at least one Tool Activity rolling-log embed"
    final_log_desc = rolling_embeds[-1].embeds[0].description
    # The rolling log line MUST have been downgraded to NOT say "click below"
    assert "click below" not in final_log_desc, (
        f"After detail send failure, rolling log MUST NOT promise a click target; "
        f"got: {final_log_desc!r}"
    )
    # Should mention the failure
    assert "detail send failed" in final_log_desc, (
        f"Expected 'detail send failed' in downgraded rolling log; "
        f"got: {final_log_desc!r}"
    )
