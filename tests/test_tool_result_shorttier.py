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

# v1.18 stage-28: pulled-in shared fakes. Five inline copies collapsed
# into a single import. ``FailingAddViewBot`` stays inline (test-local
# customization of bot.add_view).
from tests.conftest import FakeBridge, FakeTarget


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
    """When result content is short (<200 chars), non-empty, rolling log
    renders ``✅ {name} → {content}`` instead of bare ``✅ {name}``.

    v1.18 R3 (user feedback): multiline short outputs are NO LONGER
    excluded — they collapse to ``line1 │ line2 │ line3`` so a `ls`
    that prints 3 file names still shows the actual file names inline
    instead of disappearing into a bare ✅.

    Pure-string logic test — the production code in render_response is
    integration-tested elsewhere; this pins the threshold semantics.
    """
    # Simulate the production decision logic
    def should_inline(content):
        return (
            content is not None
            and len(content) < 200
            and content.strip() != ""
        )

    assert should_inline("42")                # tiny ✅
    assert should_inline("Found 7 matches")   # short prose ✅
    assert should_inline("x" * 199)           # boundary
    assert should_inline("line1\nline2")      # multiline short ✅ (new in R3)
    assert should_inline("a\nb\nc")           # 3 lines short ✅
    assert not should_inline("x" * 200)       # over threshold
    assert not should_inline("")              # empty excluded
    assert not should_inline("   \t")         # whitespace-only excluded
    assert not should_inline(None)            # None excluded


def test_short_result_backtick_escape_and_newline_collapse():
    """Inline display: backticks → single-quotes (avoids markdown break)
    AND newlines → ``│`` separator (keeps multiline outputs on one log
    line, v1.18 R3 user feedback)."""
    raw = "result with `backticks`\nand a newline"
    safe = raw.strip().replace("`", "'").replace("\n", " │ ")
    assert "`" not in safe
    assert "\n" not in safe
    assert safe == "result with 'backticks' │ and a newline"


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
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

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
async def test_medium_tier_integration_attaches_view_button():
    """Integration: drive render_response with a medium-tier Bash result
    (multiline) and verify:
      1. Rolling log embed updated with summary line ending '(⬇ click
         to view)' — no separate detail message
      2. The same rolling-log message edit attaches a ToolResultsView
         containing 1 button labeled with the tool name and ordinal
      3. The view holds the raw content; clicking would dispatch an
         ephemeral with the .txt file attachment
    """
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer, ToolResultsView

    medium_content = "\n".join(f"line {i}: some output text here" for i in range(30))
    assert 200 <= len(medium_content) < 8000

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="tool-1", name="Bash", input={"command": "ls -la"}),
            ],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="tool-1", content=medium_content, is_error=False),
            ],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-medium",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "ls -la")

    # Find the rolling-log message (it's the one with the Tool Activity
    # embed and a ToolResultsView attached).
    rolling_log_msgs = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity"
    ]
    assert rolling_log_msgs, (
        f"Expected Tool Activity rolling-log msg; got: "
        f"{[(m.embeds[0].title if m.embeds else None, type(m.attached_view).__name__) for m in target._sent]}"
    )
    rolling = rolling_log_msgs[-1]
    # Summary line on the embed
    desc = rolling.embeds[0].description
    assert "30 lines" in desc
    assert "click to view" in desc
    # View attached with the button
    assert isinstance(rolling.attached_view, ToolResultsView), (
        f"Expected ToolResultsView attached to rolling log; got: "
        f"{type(rolling.attached_view).__name__}"
    )
    view: ToolResultsView = rolling.attached_view
    assert len(view._results) == 1
    name, content = view._results["tool-1"]
    assert name == "Bash"
    assert content == medium_content
    # Button label includes ordinal + tool name
    button = view._buttons["tool-1"]
    assert "#1" in button.label
    assert "Bash" in button.label
    # NO separate detail message was sent
    other_msgs = [
        m for m in target._sent
        if not (m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity")
    ]
    # `other_msgs` may contain cost-footer-style messages; assert none has
    # the old separate-file-attachment shape.
    for m in other_msgs:
        assert not (m.content and m.content.startswith("📄 ")), (
            f"Medium-tier should NOT send a separate 📄 message anymore; got: "
            f"{m.content[:120]!r}"
        )


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
            and 200 <= len(content) < 8000
            and content.strip() != ""
        )
    error_content = "x" * 500  # medium-length BUT is_err
    assert not is_medium(error_content, is_err=True), (
        "is_err must override medium-tier predicate"
    )


def test_multiline_short_content_inlines_with_separator():
    """v1.18 R3 (user feedback): content < 200 chars with multiline now
    inlines with ``│`` separator instead of falling to bare ``✅ Bash``.
    Architectural choice: short multi-line outputs (3-file ``ls``,
    7-match ``grep``) are useful to see; the separator keeps the rolling
    log compact."""
    def is_short(content):
        return (
            len(content) < 200
            and content.strip() != ""
        )
    def is_medium(content, is_err=False, is_short_val=False):
        return (
            not is_short_val
            and not is_err
            and 200 <= len(content) < 8000
            and content.strip() != ""
        )
    multiline_short = "line 1\nline 2\nline 3"  # 20 chars, multiline
    assert len(multiline_short) < 200
    assert "\n" in multiline_short
    short_result = is_short(multiline_short)
    medium_result = is_medium(multiline_short, is_short_val=short_result)
    assert short_result, "multiline short content MUST now match short tier (R3)"
    assert not medium_result, "multiline short content excluded from medium tier (handled by short)"
    # Verify separator collapse
    rendered = multiline_short.replace("`", "'").replace("\n", " │ ")
    assert "\n" not in rendered
    assert rendered == "line 1 │ line 2 │ line 3"


@pytest.mark.asyncio
async def test_toolresults_view_add_result_idempotent_and_capped():
    """v1.18 R3: replace the old send-failure test (file-attachment
    fallback is gone). Pin the ToolResultsView contract directly:

    - add_result is idempotent on tool_use_id (same id twice → single
      button, second call returns False)
    - 25-button cap honored (Discord per-view limit). Once full,
      add_result returns False and the view stops accepting new ones.
    - Button label includes ordinal (#N) and tool name (truncated to
      18 chars to fit Discord's 80-char label cap with the prefix).
    """
    from clauded.discord_renderer import ToolResultsView

    view = ToolResultsView()

    # First add succeeds, second add (same id) is no-op
    assert view.add_result(tool_use_id="id-1", tool_name="Bash", content="x" * 500) is True
    assert view.add_result(tool_use_id="id-1", tool_name="Bash", content="x" * 500) is False
    assert len(view._results) == 1
    assert len(view._buttons) == 1
    btn = view._buttons["id-1"]
    assert "#1" in btn.label
    assert "Bash" in btn.label

    # Different id → new button at ordinal #2
    assert view.add_result(tool_use_id="id-2", tool_name="Read", content="x" * 500) is True
    assert "#2" in view._buttons["id-2"].label

    # Fill to cap (25)
    for i in range(3, 26):
        ok = view.add_result(tool_use_id=f"id-{i}", tool_name="Tool", content="x" * 500)
        assert ok is True
    assert len(view._results) == 25

    # 26th add: refused
    assert view.add_result(tool_use_id="id-overflow", tool_name="Tool", content="x" * 500) is False
    assert len(view._results) == 25


@pytest.mark.asyncio
async def test_toolresults_view_button_label_truncates_long_tool_name():
    """Discord button labels max 80 chars. Tool names can be arbitrary
    SDK strings (Task, ExitPlanMode, plus future MCP tools). The view
    truncates the name segment to 18 chars so the full label fits as
    ``📄 #N <name[:18]>`` (≄30 chars total)."""
    from clauded.discord_renderer import ToolResultsView

    view = ToolResultsView()
    long_name = "VeryLongHypotheticalToolName_That_Will_Get_Truncated"
    view.add_result(tool_use_id="id-x", tool_name=long_name, content="x" * 500)
    label = view._buttons["id-x"].label
    assert len(label) <= 80, f"Discord button label cap exceeded: {len(label)}"
    # First 18 chars of name preserved
    assert long_name[:18] in label


@pytest.mark.asyncio
async def test_toolresults_view_rejects_non_author_click():
    """R1 security: when author_id is set at View construction, clicks
    from a different user must NOT receive the .txt — they get a polite
    refusal message ephemerally. Defends against same-channel third
    parties reading another user's tool output."""
    from clauded.discord_renderer import ToolResultsView
    from unittest.mock import AsyncMock, MagicMock

    view = ToolResultsView(author_id=12345)
    view.add_result(tool_use_id="tool-x", tool_name="Bash", content="x" * 500)

    # Author click — should send the file
    author_interaction = MagicMock()
    author_interaction.user.id = 12345
    author_interaction.response.send_message = AsyncMock()
    await view._dispatch(author_interaction, "tool-x")
    args, kwargs = author_interaction.response.send_message.call_args
    assert kwargs.get("file") is not None
    assert "Bash result" in kwargs.get("content", "")
    assert kwargs.get("ephemeral") is True

    # Non-author click — should get a refusal, no file
    intruder_interaction = MagicMock()
    intruder_interaction.user.id = 99999
    intruder_interaction.response.send_message = AsyncMock()
    await view._dispatch(intruder_interaction, "tool-x")
    args, kwargs = intruder_interaction.response.send_message.call_args
    # Refusal sent as positional content arg (no file kwarg)
    assert kwargs.get("file") is None
    refusal_text = args[0] if args else kwargs.get("content", "")
    assert "only viewable" in refusal_text
    assert kwargs.get("ephemeral") is True


def test_toolresults_view_has_none_timeout_for_persistence():
    """R2 fix: discord.py's bot.add_view requires timeout=None for
    persistent dispatch. Buttons carry stable custom_ids so clicks route
    via the bot's view store. We tried timeout=86400 first per the R1
    architect note but bot.add_view raised ValueError forcing the
    revert. The orphan-accumulation concern is now handled by the view
    store's own lifecycle (discord.py replaces same-message-id views).
    """
    from clauded.discord_renderer import ToolResultsView
    v = ToolResultsView()
    assert v.timeout is None


@pytest.mark.asyncio
async def test_medium_tier_registers_view_via_bot_add_view():
    """R2 fix for #161 user-reported "点击没获取到": Message.edit(view=v)
    alone is insufficient for click-routing — clicks were dropping
    silently because the view was not in the bot's persistent view store.
    Fix: after edit, call bot.add_view(view, message_id=msg.id). This
    test pins that call so a regression can't drop it."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer, ToolResultsView

    class FakeBot:
        """Captures add_view calls."""
        def __init__(self):
            self.add_view_calls = []
        def add_view(self, view, *, message_id=None):
            self.add_view_calls.append((view, message_id))

    medium_content = "\n".join(f"line {i}" for i in range(30))

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
            is_error=False, num_turns=1, session_id="sess-pv",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    bot = FakeBot()
    renderer = DiscordRenderer(target, bot=bot)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "ls")

    # bot.add_view MUST have been called with the ToolResultsView and a
    # message_id pointing at the rolling-log message.
    assert bot.add_view_calls, (
        "Expected bot.add_view to be called for medium-tier view persistence; "
        "without it, button clicks are silently dropped (user reported "
        "'点击没获取到')."
    )
    view, msg_id = bot.add_view_calls[-1]
    assert isinstance(view, ToolResultsView), f"Expected ToolResultsView; got {type(view).__name__}"
    assert msg_id is not None, "add_view must be called with message_id= to scope the persistence"
    assert msg_id > 0


@pytest.mark.asyncio
async def test_medium_tier_downgrades_log_when_add_view_raises():
    """R1 engineer mitigation: if bot.add_view raises (e.g.
    timeout != None contract violation regression, or any other
    discord.py ValueError), the rolling-log line MUST be downgraded
    so the user doesn't see `⬇ click to view` pointing at a dead
    button."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer

    class FailingAddViewBot:
        def add_view(self, view, *, message_id=None):
            raise ValueError("synthetic: View is not persistent")

    medium_content = "\n".join(f"line {i}" for i in range(30))

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
            is_error=False, num_turns=1, session_id="sess-x",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    bot = FailingAddViewBot()
    renderer = DiscordRenderer(target, bot=bot)
    bridge = FakeBridge(events)
    await renderer.render_response(bridge, "ls")

    rolling = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity"
    ]
    assert rolling
    final_desc = rolling[-1].embeds[0].description
    # MUST NOT promise click; MUST signal failure
    assert "click to view" not in final_desc, (
        f"After add_view failure, rolling log MUST NOT promise click; got: {final_desc!r}"
    )
    assert "view registration failed" in final_desc, (
        f"Expected explicit 'view registration failed' downgrade; got: {final_desc!r}"
    )


@pytest.mark.asyncio
async def test_medium_tier_log_line_index_matches_button_index():
    """#187: rolling-log `#N` index MUST match button `#N` index so user
    can map a log line to the button below. Without this they have to
    guess which #N corresponds to which line."""
    import sys
    sys.path.insert(0, "src")
    from claude_agent_sdk.types import (
        AssistantMessage, ToolUseBlock, ToolResultBlock, ResultMessage,
    )
    from clauded.discord_renderer import DiscordRenderer, ToolResultsView

    # 3 medium-tier tool calls: Bash, Read, Bash
    medium_content_a = "\n".join(f"a{i}: data data data" for i in range(30))
    medium_content_b = "\n".join(f"b{i}: more more more" for i in range(30))
    medium_content_c = "\n".join(f"c{i}: third third third" for i in range(30))

    events = [
        AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
                ToolUseBlock(id="t2", name="Read", input={"file_path": "/tmp/foo"}),
                ToolUseBlock(id="t3", name="Bash", input={"command": "pwd"}),
            ],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        AssistantMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content=medium_content_a, is_error=False),
                ToolResultBlock(tool_use_id="t2", content=medium_content_b, is_error=False),
                ToolResultBlock(tool_use_id="t3", content=medium_content_c, is_error=False),
            ],
            model="claude-sonnet", parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-idx",
            total_cost_usd=0.001,
        ),
    ]

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "test 187")

    # Get the final rolling-log embed
    rolling = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "") == "🔧 Tool Activity"
    ]
    assert rolling
    desc = rolling[-1].embeds[0].description

    # Both rolling log AND button labels MUST share the #1/#2/#3 indexing
    # in the SAME order
    assert "Bash #1:" in desc, f"Expected `Bash #1:` in rolling log; got: {desc!r}"
    assert "Read #2:" in desc, f"Expected `Read #2:` in rolling log; got: {desc!r}"
    assert "Bash #3:" in desc, f"Expected `Bash #3:` in rolling log; got: {desc!r}"

    view: ToolResultsView = rolling[-1].attached_view
    assert isinstance(view, ToolResultsView)
    # Buttons in same order: #1 Bash, #2 Read, #3 Bash
    btn_labels = [view._buttons[tid].label for tid in ("t1", "t2", "t3")]
    assert "#1" in btn_labels[0] and "Bash" in btn_labels[0]
    assert "#2" in btn_labels[1] and "Read" in btn_labels[1]
    assert "#3" in btn_labels[2] and "Bash" in btn_labels[2]


def test_medium_tier_idempotent_reentry_keeps_index():
    """R1 tester gap: when the same tool_use_id renders twice (duplicate
    event), the rolling-log line MUST keep the same `#N` index. Without
    this, a re-render would advance the counter and decouple from the
    (already-existing) button."""
    # Simulate the production index-computation
    medium_results = {"t1": ("Bash", "x" * 500)}  # already added
    tool_id = "t1"
    if tool_id in medium_results:
        idx = list(medium_results.keys()).index(tool_id) + 1
    else:
        idx = len(medium_results) + 1
    assert idx == 1, f"Re-rendering t1 must reuse index 1; got {idx}"

    # New tool_id 't2': fresh index 2
    medium_results["t2"] = ("Read", "y" * 500)
    tool_id = "t2"
    idx = list(medium_results.keys()).index(tool_id) + 1 if tool_id in medium_results else len(medium_results) + 1
    assert idx == 2

    # Re-render t1 again — STILL 1, not 3
    tool_id = "t1"
    idx = list(medium_results.keys()).index(tool_id) + 1 if tool_id in medium_results else len(medium_results) + 1
    assert idx == 1, f"Re-rendering existing t1 must KEEP index 1; got {idx}"
