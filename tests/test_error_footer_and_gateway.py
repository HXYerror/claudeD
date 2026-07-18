"""#audit(#13,#20): end-of-turn error surfacing in the cost footer + gateway
lifecycle observability (on_disconnect/on_resumed counters + /health fields).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeBridge, FakeTarget


# ---------------------------------------------------------------------------
# #13 — ResultMessage error terminal surfaces in the footer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_surfaces_result_error_terminal():
    """A ResultMessage that ends in error adds a 🛑 <status> segment so the
    user learns WHY the turn stopped instead of seeing a silent end."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="partial answer")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="error_max_turns", duration_ms=100, duration_api_ms=80,
            is_error=True, num_turns=1, session_id="sess-err",
            total_cost_usd=0.01, usage={"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🛑" in all_content, f"expected error segment; got {all_content!r}"
    assert "error_max_turns" in all_content


@pytest.mark.asyncio
async def test_footer_no_error_segment_on_success():
    """A normal turn adds NO 🛑 segment. Uses subtype='result' (the value the
    real SDK / existing e2e tests use for a good turn) to guard against
    over-triggering on any non-'success' subtype."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    events = [
        AssistantMessage(
            content=[TextBlock(text="all good")],
            model="claude-sonnet-4-5",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-ok",
            total_cost_usd=0.01, usage={"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await renderer.render_response(FakeBridge(events), "hi")

    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🛑" not in all_content


# ---------------------------------------------------------------------------
# #20 — gateway lifecycle observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_listeners_update_counters():
    from clauded.bot import ClaudedBot

    self_ = MagicMock()
    self_._gw_disconnects = 0
    self_._gw_resumes = 0
    self_._gw_last_disconnect_at = None
    self_._gw_last_resumed_at = None

    await ClaudedBot.on_disconnect(self_)
    await ClaudedBot.on_disconnect(self_)
    await ClaudedBot.on_resumed(self_)

    assert self_._gw_disconnects == 2
    assert self_._gw_resumes == 1
    assert self_._gw_last_disconnect_at is not None
    assert self_._gw_last_resumed_at is not None


@pytest.mark.asyncio
async def test_health_embed_shows_gateway_fields():
    import discord
    from clauded.cogs.ops import health_check
    from clauded.bot import ClaudedBot

    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.list_sessions = MagicMock(return_value={})
    bot.session_manager.get_session = MagicMock(return_value=None)
    bot.project_manager = MagicMock()
    bot.project_manager._projects = {}
    bot._start_time = 0
    bot._claude_version = "1.0"
    bot._gw_disconnects = 3
    bot._gw_resumes = 2
    bot.latency = 0.042  # seconds → 42 ms
    bot.is_ready = MagicMock(return_value=True)

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.id = 1
    interaction.response.send_message = AsyncMock()

    await health_check.callback(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert fields.get("Gateway") == "🟢 ready"
    assert fields.get("Latency") == "42 ms"
    assert fields.get("Reconnects") == "3 drop / 2 resume"
