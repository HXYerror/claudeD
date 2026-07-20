"""#panel: the Task*/subagent panel must headline a clear one-line description
of what each subagent/task is doing (started + terminal), not a truncated
"🔮 Task: …" buried under type/id.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import FakeTarget


@pytest.mark.asyncio
async def test_task_started_panel_headlines_description_and_kind():
    from clauded.discord_renderer import DiscordRenderer

    target = FakeTarget()
    r = DiscordRenderer(target)
    event = SimpleNamespace(
        task_id="tid1234567890",
        description="Trace the subagent leak precisely",
        task_type="local_agent",
        tool_use_id="tu1",
        data={"subagent_type": "Explore"},
    )
    await r._handle_task_started(event, {})

    embed = target._sent[0].embeds[0]
    blob = f"{embed.title} {embed.description}"
    assert "Trace the subagent leak precisely" in blob  # description headlined
    assert "Explore" in blob  # which agent


@pytest.mark.asyncio
async def test_task_started_description_not_truncated_at_60():
    from clauded.discord_renderer import DiscordRenderer

    long_desc = "Investigate the reconnect storm and correlate it with the DNS failures across every session"
    target = FakeTarget()
    r = DiscordRenderer(target)
    event = SimpleNamespace(
        task_id="tidX", description=long_desc, task_type="local_agent",
        tool_use_id="tu", data={},
    )
    await r._handle_task_started(event, {})
    embed = target._sent[0].embeds[0]
    # The full sentence (>60 chars) survives — old code cut it at [:60].
    assert long_desc in embed.description


@pytest.mark.asyncio
async def test_task_terminal_prepends_task_description():
    from clauded.discord_renderer import DiscordRenderer, _TaskState

    target = FakeTarget()
    r = DiscordRenderer(target)
    r._task_states["tid1"] = _TaskState(
        description="Research X thoroughly", subagent_type="general-purpose",
    )
    await r._render_terminal_task("tid1", "completed", "Found 3 issues.", None)

    embed = target._sent[-1].embeds[0]
    assert "Research X thoroughly" in embed.description  # WHAT finished
    assert "Found 3 issues" in embed.description  # its result
