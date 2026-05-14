"""#205 — table renders TWICE in one turn (PNG + code-fence fallback).

Per PRD #112 R5: PNG path active → code-fence fallback NEVER fires.
Bug: prod screenshot shows same table rendered both ways. Reproduce
via integration tests to nail down the exact path.
"""
import pytest
from unittest.mock import MagicMock
from tests.conftest import FakeBridge, FakeTarget
from claude_agent_sdk.types import (
    AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    StreamEvent, ResultMessage,
)


TABLE_MD = """\
| # | Task | Status |
|---|------|--------|
| 1 | Foo  | ✅     |
| 2 | Bar  | ✅     |
| 3 | Baz  | ❌     |
"""


def _stream_event_text(text):
    """Build a StreamEvent that yields one text_delta chunk."""
    return StreamEvent(
        uuid="stream-uuid",
        session_id="sess",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )


@pytest.mark.asyncio
async def test_pure_text_with_table_no_tool_no_double_render():
    """Stream: text with a table, no tool calls.

    Before fix: would-be repro of the user's screenshot scenario IF
    table dumping happens twice.
    After fix: PNG path active → exactly 1 PNG + cleaned text, no
    raw markdown code fence containing the table.
    """
    from clauded.discord_renderer import DiscordRenderer

    full_text = f"Here is the table:\n{TABLE_MD}\nDone."

    events = [
        # Stream the text via text_delta events (the typewriter path)
        _stream_event_text(full_text),
        # Then the AssistantMessage that wraps it (skipped by renderer as duplicate)
        AssistantMessage(
            content=[TextBlock(text=full_text)],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "show me a table")

    # Audit: raw markdown table pipe header must not appear in any
    # user-visible message content (only the PNG attachment counts).
    raw_table_hits = 0
    for msg in target._sent:
        if msg.content and "| # | Task | Status |" in msg.content:
            raw_table_hits += 1
    assert raw_table_hits == 0, (
        f"#205 regression: raw markdown table leaked into Discord message "
        f"{raw_table_hits} time(s) alongside PNG render.\n"
        + "\n---\n".join(
            (m.content or "<no content>") for m in target._sent
        )
    )
    # PNG attachment IS sent (PILLOW_AVAILABLE in test env)
    png_hits = sum(
        1 for m in target._sent for a in (m.attachments or [])
        if getattr(a, "filename", "").endswith(".png")
    )
    assert png_hits == 1, f"Expected exactly 1 PNG; got {png_hits}"


@pytest.mark.asyncio
async def test_205_long_response_md_upload_summary_does_not_leak_table():
    """#205 EXACT repro: when stripped text > 4 chunks, _flush uploads
    response as .md attachment and sends a 'summary' inline. Pre-fix,
    summary was upload_buffer[:200] which included the re-spliced raw
    markdown table if the table sat near the start — producing the
    bug where same table renders BOTH as raw markdown (in summary) AND
    as PNG follow-up.

    Post-fix: summary is a plain caption ("📎 Long response… see attached
    .md"); raw table only lives in the .md file (downloadable), and the
    PNG follow-up is the chat-visible visual.
    """
    from clauded.discord_renderer import DiscordRenderer

    # Build a long response that pushes past the >4-chunks threshold
    # so _flush takes the .md-upload path.
    long_text = (
        "Here is the table:\n"
        + TABLE_MD
        + "\n\nNow the explanation:\n"
        + ("This is a line of explanation that needs to be quite long. " * 200)
    )
    events = [
        _stream_event_text(long_text),
        AssistantMessage(
            content=[TextBlock(text=long_text)],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "long with table")

    # No message's content may contain the raw markdown table
    for i, msg in enumerate(target._sent):
        if msg.content and "| # | Task | Status |" in msg.content:
            pytest.fail(
                f"#205 regression: raw markdown table leaked in message [{i}]:\n"
                f"  content: {msg.content[:400]!r}\n"
                f"  attachments: {[getattr(a, 'filename', '?') for a in (msg.attachments or [])]}"
            )

    # Sanity: the .md upload still carries the full content
    md_msgs = [
        m for m in target._sent
        for a in (m.attachments or [])
        if getattr(a, "filename", "") == "claude-response.md"
    ]
    assert md_msgs, "Expected the .md upload attachment for long responses"
    # And exactly 1 PNG follow-up for the table
    png_msgs = [
        m for m in target._sent
        for a in (m.attachments or [])
        if getattr(a, "filename", "").endswith(".png")
    ]
    assert len(png_msgs) == 1, f"Expected 1 PNG follow-up; got {len(png_msgs)}"


@pytest.mark.asyncio
async def test_text_table_then_tool_use_mid_stream_no_double():
    """Stream: text+table, then ToolUseBlock interrupts (mid-stream),
    then more text.

    This is the Path A hypothesis: mid-stream _finalize_typewriter
    with is_final=False dumps the pre-tool buffer (containing the
    table) as raw markdown into live_msg. Then final flush re-extracts
    via PNG. Net: both visible. The fix should suppress the raw dump.
    """
    from clauded.discord_renderer import DiscordRenderer

    pre_tool_text = f"Here is the table:\n{TABLE_MD}\nWaiting for tool..."
    post_tool_text = "\nDone after tool."

    events = [
        _stream_event_text(pre_tool_text),
        # AssistantMessage with text + ToolUseBlock — text via stream,
        # tool causes the mid-stream finalize trigger
        AssistantMessage(
            content=[
                TextBlock(text=pre_tool_text),
                ToolUseBlock(id="t1", name="Bash", input={"command": "echo done"}),
            ],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        # Tool result
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        # Post-tool text
        _stream_event_text(post_tool_text),
        AssistantMessage(
            content=[TextBlock(text=post_tool_text)],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "show me a table then run bash")

    # Count raw markdown table leaks
    raw_table_hits = 0
    for msg in target._sent:
        if msg.content and "| # | Task | Status |" in msg.content:
            raw_table_hits += 1

    assert raw_table_hits == 0, (
        f"#205 path-A regression: raw markdown table leaked from mid-stream "
        f"_finalize_typewriter(is_final=False) dump. Got {raw_table_hits} hits. "
        f"Contents:\n" + "\n---\n".join(
            (m.content[:200] if m.content else "<no content>") for m in target._sent
        )
    )
