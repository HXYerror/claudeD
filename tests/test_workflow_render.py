"""#292 S4 — tests for Dynamic Workflow renderer Task* handlers.

Uses SimpleNamespace to mock SDK Task events and the shared
FakeBridge / FakeTarget from conftest to drive DiscordRenderer
in isolation (no full render_response loop required).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from tests.conftest import FakeMessage, FakeTarget

from clauded.discord_renderer import (
    COLOR_THINKING,
    COLOR_TOOL_FAILURE,
    COLOR_TOOL_RUNNING,
    COLOR_TOOL_SUCCESS,
    COLOR_WORKFLOW,
    EDIT_INTERVAL_SECONDS,
    TASK_PROGRESS_EDIT_INTERVAL_SECONDS,
    DiscordRenderer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_renderer(*, bot=None) -> tuple[DiscordRenderer, FakeTarget]:
    """Create a renderer wired to a FakeTarget, optionally with a bot."""
    target = FakeTarget()
    renderer = DiscordRenderer(target, bot=bot)
    return renderer, target


def _started_event(
    task_id="t1",
    description="test task",
    task_type="workflow",
    tool_use_id=None,
    uuid="u1",
    session_id="s1",
):
    return SimpleNamespace(
        task_id=task_id,
        description=description,
        task_type=task_type,
        tool_use_id=tool_use_id,
        uuid=uuid,
        session_id=session_id,
    )


def _progress_event(
    task_id="t1",
    usage=None,
    last_tool_name="Bash",
):
    return SimpleNamespace(
        task_id=task_id,
        usage=usage or {"total_tokens": 1000, "tool_uses": 5, "duration_ms": 3000},
        last_tool_name=last_tool_name,
    )


def _notification_event(
    task_id="t1",
    status="completed",
    summary="Done!",
    usage=None,
):
    return SimpleNamespace(
        task_id=task_id,
        status=status,
        summary=summary,
        usage=usage or {"total_tokens": 2000, "tool_uses": 10, "duration_ms": 5000},
    )


def _updated_event(
    task_id="t1",
    status="killed",
):
    return SimpleNamespace(
        task_id=task_id,
        status=status,
    )


# ---------------------------------------------------------------------------
# TaskStarted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_started_sends_purple_banner():
    """TaskStarted → embed with COLOR_WORKFLOW, ⚡ title, 🔮 description."""
    renderer, target = _make_renderer()
    event = _started_event()
    await renderer._handle_task_started(event, subagent_renderers={})

    assert len(target._sent) == 1
    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_WORKFLOW
    assert "⚡" in embed.title
    assert "🔮" in embed.description


@pytest.mark.asyncio
async def test_task_started_stores_state():
    """After TaskStarted, _task_states has the task entry."""
    renderer, target = _make_renderer()
    event = _started_event(task_id="abc123")
    await renderer._handle_task_started(event, subagent_renderers={})

    assert "abc123" in renderer._task_states
    state = renderer._task_states["abc123"]
    assert state.description == "test task"
    assert state.task_type == "workflow"
    assert state.message is not None


# ---------------------------------------------------------------------------
# TaskProgress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_progress_edits_message():
    """TaskProgress edits existing message instead of sending a new one."""
    renderer, target = _make_renderer()

    # First, start a task to populate state
    event_start = _started_event()
    await renderer._handle_task_started(event_start, subagent_renderers={})
    assert len(target._sent) == 1

    # Force last_edit_at far in the past to bypass throttle
    renderer._task_states["t1"].last_edit_at = 0.0

    event_progress = _progress_event()
    await renderer._handle_task_progress(event_progress)

    # No new message should have been sent — only the existing one edited
    assert len(target._sent) == 1
    # The existing message should have been edited (embed updated)
    msg = target._sent[0]
    assert len(msg.embeds) == 1
    embed = msg.embeds[0]
    assert "Running" in embed.title or "🔄" in embed.title


@pytest.mark.asyncio
async def test_task_progress_throttle():
    """Two rapid progress events — second one is throttled (skipped)."""
    renderer, target = _make_renderer()

    event_start = _started_event()
    await renderer._handle_task_started(event_start, subagent_renderers={})

    # First progress: set last_edit_at to 0 so it passes
    renderer._task_states["t1"].last_edit_at = 0.0
    event_p1 = _progress_event(usage={"total_tokens": 100, "tool_uses": 1, "duration_ms": 1000})
    await renderer._handle_task_progress(event_p1)

    # Capture embed after first progress
    embed_after_first = target._sent[0].embeds[0]

    # Second progress immediately — should be throttled
    event_p2 = _progress_event(usage={"total_tokens": 999, "tool_uses": 99, "duration_ms": 9999})
    await renderer._handle_task_progress(event_p2)

    # The embed should NOT have been updated (still shows first progress values)
    embed_after_second = target._sent[0].embeds[0]
    assert embed_after_first.description == embed_after_second.description

    # But last_usage in state SHOULD have been updated (always stored)
    assert renderer._task_states["t1"].last_usage["total_tokens"] == 999


def test_task_progress_interval_decoupled_and_larger():
    """The progress card has its OWN interval, larger than the typewriter's, so
    lowering the card's edit frequency doesn't make streaming text laggy."""
    assert TASK_PROGRESS_EDIT_INTERVAL_SECONDS > EDIT_INTERVAL_SECONDS
    assert TASK_PROGRESS_EDIT_INTERVAL_SECONDS >= 3.0


@pytest.mark.asyncio
async def test_task_progress_gate_reads_card_interval(monkeypatch):
    """The card throttle uses TASK_PROGRESS_EDIT_INTERVAL_SECONDS (not the 1.2s
    typewriter constant): lowering it to 0 lets back-to-back events both edit."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PROGRESS_EDIT_INTERVAL_SECONDS", 0.0)
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    renderer._task_states["t1"].last_edit_at = 0.0
    await renderer._handle_task_progress(
        _progress_event(usage={"total_tokens": 1, "tool_uses": 1, "duration_ms": 1})
    )
    desc_after_first = target._sent[0].embeds[0].description

    # Second, immediate — with a 0s interval it must NOT be throttled.
    await renderer._handle_task_progress(
        _progress_event(usage={"total_tokens": 777, "tool_uses": 9, "duration_ms": 9})
    )
    desc_after_second = target._sent[0].embeds[0].description
    assert desc_after_first != desc_after_second
    assert "777" in desc_after_second


@pytest.mark.asyncio
async def test_task_progress_last_edit_at_from_completion(monkeypatch):
    """last_edit_at is stamped AFTER the edit completes, not from the pre-edit
    timestamp — so a slow/429-retried edit can't be followed by an immediate
    burst. Simulated with a _safe_edit that takes measurable wall time."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})
    state = renderer._task_states["t1"]
    state.last_edit_at = 0.0

    async def _slow_edit(*_a, **_k):
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(renderer, "_safe_edit", _slow_edit)
    t_before = time.time()
    await renderer._handle_task_progress(_progress_event())
    # Stamped from completion: at least the edit's duration after we started.
    assert state.last_edit_at >= t_before + 0.05


@pytest.mark.asyncio
async def test_task_progress_unknown_task_ignored():
    """Progress for unknown task_id is silently ignored."""
    renderer, target = _make_renderer()
    event = _progress_event(task_id="nonexistent")
    await renderer._handle_task_progress(event)
    assert len(target._sent) == 0


# ---------------------------------------------------------------------------
# TaskNotification (terminal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_notification_completed_green():
    """status=completed → COLOR_TOOL_SUCCESS, ✅ title."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    event = _notification_event(status="completed")
    await renderer._handle_task_notification(event)

    embed = target._sent[0].embeds[0]  # edited in place
    assert embed.color.value == COLOR_TOOL_SUCCESS
    assert "✅" in embed.title


@pytest.mark.asyncio
async def test_task_notification_failed_red():
    """status=failed → COLOR_TOOL_FAILURE, ❌ title."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    event = _notification_event(status="failed")
    await renderer._handle_task_notification(event)

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "❌" in embed.title


@pytest.mark.asyncio
async def test_task_notification_stopped_gray():
    """status=stopped → COLOR_THINKING (gray), ⏹️ title."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    event = _notification_event(status="stopped")
    await renderer._handle_task_notification(event)

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_THINKING
    assert "⏹️" in embed.title


@pytest.mark.asyncio
async def test_task_notification_cleans_state():
    """After terminal notification, task_id is removed from _task_states."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(task_id="t99"), subagent_renderers={})
    assert "t99" in renderer._task_states

    event = _notification_event(task_id="t99", status="completed")
    await renderer._handle_task_notification(event)

    assert "t99" not in renderer._task_states


# ---------------------------------------------------------------------------
# TaskUpdated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_updated_killed():
    """TaskUpdated with status=killed → terminal embed."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    event = _updated_event(status="killed")
    await renderer._handle_task_updated(event)

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_THINKING
    assert "⏹️" in embed.title
    assert "t1" not in renderer._task_states


@pytest.mark.asyncio
async def test_task_updated_duplicate_guard():
    """If task already cleaned from state, TaskUpdated is a no-op."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    # Manually pop the task (simulates Notification already handled)
    renderer._task_states.pop("t1", None)

    sent_before = len(target._sent)
    event = _updated_event(status="killed")
    await renderer._handle_task_updated(event)

    # The started embed is the only message; no new edits or sends
    assert len(target._sent) == sent_before
    # Verify the started embed is unchanged (still purple banner)
    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_WORKFLOW


@pytest.mark.asyncio
async def test_task_updated_non_terminal_skipped():
    """Non-terminal status (e.g. 'running') is ignored by TaskUpdated."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    event = _updated_event(status="running")
    await renderer._handle_task_updated(event)

    # Task should still be in state
    assert "t1" in renderer._task_states
    # Embed should still be the original started banner
    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_WORKFLOW


# ---------------------------------------------------------------------------
# Sync to bot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_to_bot():
    """TaskStarted syncs to bot._workflow_tasks; terminal removes it."""
    bot = MagicMock()
    bot._workflow_tasks = {}

    renderer, target = _make_renderer(bot=bot)
    await renderer._handle_task_started(_started_event(task_id="sync1"), subagent_renderers={})

    assert "sync1" in bot._workflow_tasks
    state = bot._workflow_tasks["sync1"]
    assert state.description == "test task"

    # Terminal notification should remove it
    event = _notification_event(task_id="sync1", status="completed")
    await renderer._handle_task_notification(event)

    assert "sync1" not in bot._workflow_tasks


# ---------------------------------------------------------------------------
# Edge: no bot reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_to_bot_no_bot_no_error():
    """When bot is None, _sync_task_to_bot is a no-op (no crash)."""
    renderer, target = _make_renderer(bot=None)
    await renderer._handle_task_started(_started_event(), subagent_renderers={})
    assert "t1" in renderer._task_states

    await renderer._handle_task_notification(
        _notification_event(status="completed"),
    )
    assert "t1" not in renderer._task_states
