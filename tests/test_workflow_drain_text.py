"""#316: a Workflow turn must render its trailing summary text + real footer.

The CLI turns a Workflow-tool dispatch into an EARLY ResultMessage; the
workflow's summary text (and its own ResultMessage) are pushed AFTER it and land
in the post-turn drain window. The old drain handled only Task* events and
DROPPED the continuation, so the turn showed "(Claude returned no text
response)" with a zero footer despite the summary existing in the transcript.

The drain now:
- G1: captures continuation AssistantMessage text (main-channel only), so it
  renders instead of the placeholder;
- G2: rebuilds the footer from the continuation's own ResultMessage, and when a
  workflow is still pending with no text, suppresses the misleading placeholder
  in favor of a heads-up.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from clauded.discord_renderer import DiscordRenderer
from tests.conftest import FakeBridge, FakeTarget


def _result(*, cost: float = 0.0, usage: dict | None = None, sid: str = "s-wf") -> ResultMessage:
    return ResultMessage(
        subtype="result", duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id=sid,
        total_cost_usd=cost, usage=usage or {},
    )


def _asst(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)], model="claude-x", parent_tool_use_id=None
    )


class _DrainBridge(FakeBridge):
    """FakeBridge that also scripts ``receive_pending()`` for the post-turn drain."""

    def __init__(self, events, pending, **kw):
        super().__init__(events, **kw)
        self._pending = pending

    async def receive_pending(self, per_message_timeout=None):
        for ev in self._pending:
            yield ev


async def _seed_pending_task(renderer: DiscordRenderer, task_id: str = "t1") -> None:
    """Populate ``_task_states`` (as a live TaskStarted would) so the drain runs."""
    await renderer._handle_task_started(
        SimpleNamespace(
            task_id=task_id, description="review workflow", task_type="local_workflow",
            tool_use_id=None, uuid="u1", session_id="s-wf",
        ),
        {},
    )


@pytest.mark.asyncio
async def test_workflow_summary_text_rendered_from_drain() -> None:
    """G1: continuation text arriving in the drain window is rendered, not dropped."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await _seed_pending_task(renderer)

    main = [_result(usage={})]  # early dispatch result, no text yet
    pending = [
        _asst("✅ workflow finished: 3 PRs reviewed"),
        _result(cost=0.05, usage={"input_tokens": 4321, "output_tokens": 890}),
    ]
    await renderer.render_response(_DrainBridge(main, pending), "run the review workflow")

    blob = " ".join(m.content for m in target._sent if m.content)
    assert "workflow finished: 3 PRs reviewed" in blob, (
        "#316 G1: the workflow summary text from the drain must be rendered"
    )
    assert "(Claude returned no text response)" not in blob


@pytest.mark.asyncio
async def test_workflow_footer_rebuilt_from_continuation_result() -> None:
    """G2: the footer reflects the continuation's usage, not the dispatch zeros."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await _seed_pending_task(renderer)

    main = [_result(usage={})]  # dispatch result: empty usage → would footer 0/0
    pending = [
        _asst("done"),
        _result(cost=0.05, usage={"input_tokens": 4321, "output_tokens": 890}),
    ]
    await renderer.render_response(_DrainBridge(main, pending), "go")

    blob = " ".join(m.content for m in target._sent if m.content)
    assert "📤" in blob, "footer should render"
    # 4321 input tokens → '4.3k' via _fmt_tokens; proves the footer used the
    # continuation result, not the empty dispatch result (which would show 0).
    assert "4.3k" in blob, f"#316 G2: footer must reflect real usage. Got: {blob}"


@pytest.mark.asyncio
async def test_no_placeholder_when_workflow_still_pending() -> None:
    """G2: drain ends with a task pending and no text → suppress the misleading
    placeholder, post a heads-up instead."""
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    await _seed_pending_task(renderer)  # stays pending: empty drain

    await renderer.render_response(_DrainBridge([_result(usage={})], []), "kick off workflow")

    blob = " ".join(m.content for m in target._sent if m.content)
    assert "(Claude returned no text response)" not in blob, (
        "#316 G2: don't claim 'no response' while a workflow is still running"
    )
    assert "still running" in blob, "expected the ⏳ background heads-up"
