"""#183 — synthetic API-error AssistantMessage must render as a red embed,
not silently drop to the 'no text response' placeholder.
"""
import pytest
import sys
sys.path.insert(0, "src")

from claude_agent_sdk.types import (
    AssistantMessage, TextBlock, ResultMessage,
)
from clauded.discord_renderer import DiscordRenderer

from tests.conftest import FakeBridge, FakeTarget  # noqa: E402  (shared fakes)


# The exact 400 error from the user's production session
API_ERROR_TEXT = (
    'API Error: 400 {"error":{"message":"litellm.BadRequestError: '
    'Github_copilotException - prompt token count of 170384 exceeds '
    'the limit of 168000. Received Model Group=claude-sonnet-4-6\\n'
    'Available Model Group Fallbacks=None","type":null,"param":null,"code":"400"}}'
)


@pytest.mark.asyncio
async def test_api_error_assistant_renders_red_embed():
    """AssistantMessage with error != None and a TextBlock body must
    render a red ``❌ Provider error: {kind}`` embed visible in chat."""
    events = [
        AssistantMessage(
            content=[TextBlock(text=API_ERROR_TEXT)],
            model="<synthetic>",
            parent_tool_use_id=None,
            error="invalid_request",
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")

    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    assert error_embeds, (
        f"Expected red ❌ Provider error embed; got titles: "
        f"{[(m.embeds[0].title if m.embeds else None) for m in target._sent]}"
    )
    embed = error_embeds[0].embeds[0]
    assert "invalid_request" in embed.title
    # Body should contain the actual API error text inside a code fence
    assert "170384" in embed.description
    assert "168000" in embed.description
    # Plus the hint about /session clear
    assert "/session clear" in embed.description


@pytest.mark.asyncio
async def test_api_error_overrides_no_text_placeholder():
    """When an API error embed is rendered, the catch-all
    ``(Claude returned no text response)`` placeholder MUST NOT fire."""
    events = [
        AssistantMessage(
            content=[TextBlock(text=API_ERROR_TEXT)],
            model="<synthetic>",
            parent_tool_use_id=None,
            error="rate_limit",
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")

    placeholder_msgs = [
        m for m in target._sent
        if "no text response" in (m.content or "")
    ]
    assert not placeholder_msgs, (
        f"Placeholder must NOT fire when an API error was already shown; "
        f"sent contents: {[m.content[:80] for m in target._sent if m.content]}"
    )


@pytest.mark.asyncio
async def test_normal_textblock_still_skipped_when_error_is_none():
    """Pin: legitimate streamed-text duplication still skips TextBlock
    when error is None. Without this guard the API-error fix could
    accidentally start rendering all assistant TextBlocks twice (once
    via streaming, once via the AssistantMessage replay)."""
    # Two TextBlocks on a normal AssistantMessage (error=None).
    # The renderer's TextBlock branch skips them entirely; with no
    # tools and no streaming text, saw_text stays False, so the
    # placeholder DOES fire — proves the API-error early-out only
    # activates when error is not None.
    events = [
        AssistantMessage(
            content=[TextBlock(text="Normal model output")],
            model="claude-sonnet-4-6",
            parent_tool_use_id=None,
            error=None,  # legitimate response, not synthetic
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-ok",
            total_cost_usd=0.001,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hello")

    # No red error embed
    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    assert not error_embeds, "Should NOT render error embed when error is None"
    # Placeholder DOES fire (because TextBlock is skipped as a streaming
    # duplicate and no real stream event came through in this test)
    placeholder_msgs = [
        m for m in target._sent
        if "no text response" in (m.content or "")
    ]
    assert placeholder_msgs, (
        "Without streamed text, the placeholder MUST still fire for "
        "non-error AssistantMessages (existing behavior pin)"
    )


@pytest.mark.asyncio
async def test_api_error_with_empty_textblock_falls_back_to_kind():
    """Defensive: if for some reason the synthetic message has no
    TextBlock body, the embed body still surfaces the error kind."""
    events = [
        AssistantMessage(
            content=[],  # no blocks
            model="<synthetic>",
            parent_tool_use_id=None,
            error="server_error",
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")

    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    assert error_embeds
    embed = error_embeds[0].embeds[0]
    assert "server_error" in embed.title
    assert "no error body" in embed.description or "server_error" in embed.description


@pytest.mark.asyncio
async def test_api_error_text_truncated_when_huge():
    """Discord embed description cap is 4096. A giant API-error traceback
    must be truncated with a tail marker so the embed renders at all."""
    huge_error = "API Error: " + ("x" * 10000)
    events = [
        AssistantMessage(
            content=[TextBlock(text=huge_error)],
            model="<synthetic>",
            parent_tool_use_id=None,
            error="unknown",
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")

    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    embed = error_embeds[0].embeds[0]
    assert len(embed.description) < 4096, (
        f"Embed description exceeds Discord cap: {len(embed.description)}"
    )
    assert "truncated" in embed.description


@pytest.mark.parametrize("error_kind", [
    "authentication_failed",
    "billing_error",
    "rate_limit",
    "invalid_request",
    "server_error",
    "unknown",
])
@pytest.mark.asyncio
async def test_api_error_renders_for_all_six_literal_values(error_kind):
    """R1 tester: ``AssistantMessageError`` Literal has 6 values. Pin all
    of them so future SDK additions don't silently fall through (the
    detection is structural — any non-None ``error`` field hits the
    renderer — but parametrize anyway as defense against the Literal
    becoming open-ended)."""
    events = [
        AssistantMessage(
            content=[TextBlock(text=f"API Error: 400 ({error_kind})")],
            model="<synthetic>",
            parent_tool_use_id=None,
            error=error_kind,
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")
    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    assert error_embeds, f"No error embed rendered for kind={error_kind!r}"
    assert error_kind in error_embeds[0].embeds[0].title


@pytest.mark.asyncio
async def test_api_error_with_triple_backticks_in_body_does_not_break_fence():
    """R1 security: upstream errors can contain literal ``` (an inner
    exception repr, a JSON-encoded code sample, etc.). Without escaping,
    they would close our outer ```...``` fence early and leave Discord
    rendering markdown after the close. Verify the replacement strategy
    keeps the actual error readable and prevents the fence break."""
    payload = 'Error parsing: ```python\nprint("hi")\n```'
    events = [
        AssistantMessage(
            content=[TextBlock(text=payload)],
            model="<synthetic>",
            parent_tool_use_id=None,
            error="invalid_request",
        ),
        ResultMessage(
            subtype="result", duration_ms=10, duration_api_ms=5,
            is_error=False, num_turns=1, session_id="sess-err",
            total_cost_usd=0.0,
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "继续")
    error_embeds = [
        m for m in target._sent
        if m.embeds and (m.embeds[0].title or "").startswith("❌ Provider error")
    ]
    desc = error_embeds[0].embeds[0].description
    # The outer ```...``` fence wraps the body. Inside the body, the
    # literal ``` from the payload MUST have been broken up so it
    # doesn't terminate the outer fence early.
    inner = desc.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    assert "```" not in inner, (
        f"Inner code fence body still contains raw triple-backtick — "
        f"outer fence would break. Inner: {inner[:120]!r}"
    )
    # Original text content still present (zero-width joiners don't
    # affect visual reading)
    assert "python" in inner
    assert "print" in inner


def test_assistant_message_error_field_exists_on_sdk():
    """Pin: SDK's AssistantMessage MUST expose `.error` attribute, so the
    duck-typed ``getattr(event, "error", None)`` detection in
    ``discord_renderer.py`` keeps catching synthetic API errors. If the
    SDK ever renames or removes the field (e.g., switches to
    ``error_info.kind``), this test fails with the actionable message
    below — alerting maintainers to update the renderer.

    Defensive: also wraps ``dataclasses.fields()`` so a future SDK
    switch from ``@dataclass`` to ``TypedDict``/Pydantic doesn't show
    up as a confusing ``TypeError`` (R1 engineer #2 follow-up).
    """
    from claude_agent_sdk.types import AssistantMessage
    try:
        from dataclasses import fields
        field_names = {f.name for f in fields(AssistantMessage)}
    except TypeError:
        # AssistantMessage is no longer a dataclass; fall back to
        # attribute introspection.
        field_names = {n for n in dir(AssistantMessage) if not n.startswith("_")}
    assert "error" in field_names, (
        "claude_agent_sdk.types.AssistantMessage no longer has an "
        "`error` field; update discord_renderer.py's API-error detection."
    )
