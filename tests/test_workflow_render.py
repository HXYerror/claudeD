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
async def test_task_started_opens_panel():
    """TaskStarted → ONE rolling panel message with the task as a ⚡ row."""
    renderer, target = _make_renderer()
    event = _started_event()
    await renderer._handle_task_started(event, subagent_renderers={})

    assert len(target._sent) == 1  # one consolidated panel, not a per-task card
    embed = target._sent[0].embeds[0]
    assert "Tasks" in embed.title
    assert "running" in embed.title
    assert "test task" in embed.description  # the row
    assert "⚡" in embed.description
    assert embed.color.value == COLOR_WORKFLOW


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
async def test_task_progress_edits_panel_in_place():
    """TaskProgress edits the panel message in place (no new message) and
    surfaces the row's current tool."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})
    assert len(target._sent) == 1

    renderer._task_panel.last_edit_at = 0.0  # bypass throttle
    await renderer._handle_task_progress(_progress_event(last_tool_name="Bash"))

    assert len(target._sent) == 1  # edited, not a new message
    embed = target._sent[0].embeds[0]
    assert "test task" in embed.description
    assert "Bash" in embed.description  # tool surfaced on the running row


@pytest.mark.asyncio
async def test_task_progress_throttle():
    """A too-soon progress event does NOT re-edit the panel (throttled), but the
    usage is still stored in state."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    renderer._task_panel.last_edit_at = time.time()  # recent → next is throttled
    desc_before = target._sent[0].embeds[0].description

    await renderer._handle_task_progress(
        _progress_event(
            usage={"total_tokens": 999, "tool_uses": 99, "duration_ms": 9999},
            last_tool_name="Zzz",
        )
    )

    assert target._sent[0].embeds[0].description == desc_before  # not re-edited
    assert renderer._task_states["t1"].last_usage["total_tokens"] == 999
    # tidy: drop any pending trailing-flush task
    if renderer._task_panel.flush_task:
        renderer._task_panel.flush_task.cancel()


def test_task_progress_interval_decoupled_and_larger():
    """The progress card has its OWN interval, larger than the typewriter's, so
    lowering the card's edit frequency doesn't make streaming text laggy."""
    assert TASK_PROGRESS_EDIT_INTERVAL_SECONDS > EDIT_INTERVAL_SECONDS
    assert TASK_PROGRESS_EDIT_INTERVAL_SECONDS >= 3.0


@pytest.mark.asyncio
async def test_task_progress_gate_reads_panel_interval(monkeypatch):
    """The panel throttle uses TASK_PROGRESS_EDIT_INTERVAL_SECONDS: lowering it
    to 0 lets back-to-back progress events both edit the panel."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PROGRESS_EDIT_INTERVAL_SECONDS", 0.0)
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    await renderer._handle_task_progress(_progress_event(last_tool_name="ReadTool"))
    desc_after_first = target._sent[0].embeds[0].description

    # Second, immediate — with a 0s interval it must NOT be throttled.
    await renderer._handle_task_progress(_progress_event(last_tool_name="WriteTool"))
    desc_after_second = target._sent[0].embeds[0].description
    assert desc_after_first != desc_after_second
    assert "WriteTool" in desc_after_second


@pytest.mark.asyncio
async def test_panel_last_edit_at_from_completion(monkeypatch):
    """panel.last_edit_at is stamped AFTER the edit completes, not from the
    pre-edit timestamp — so a slow/429-retried edit can't be followed by an
    immediate burst. Simulated with a _safe_edit that takes measurable time."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})
    panel = renderer._task_panel
    panel.last_edit_at = 0.0  # bypass throttle so progress edits

    async def _slow_edit(*_a, **_k):
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(renderer, "_safe_edit", _slow_edit)
    t_before = time.time()
    await renderer._handle_task_progress(_progress_event())
    assert panel.last_edit_at >= t_before + 0.05


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
async def test_task_notification_completed_marks_row_done():
    """status=completed → the panel row becomes ✅ with a duration; panel green."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    await renderer._handle_task_notification(_notification_event(status="completed"))
    await renderer._flush_panel_now()  # finalize (as render_response does)

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_TOOL_SUCCESS
    assert "✅" in embed.description
    assert "done" in embed.title


@pytest.mark.asyncio
async def test_task_notification_failed_marks_row_failed():
    """status=failed → ❌ row with the reason; panel red; title counts failed."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    await renderer._handle_task_notification(
        _notification_event(status="failed", summary="boom happened")
    )
    await renderer._flush_panel_now()

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_TOOL_FAILURE
    assert "❌" in embed.description
    assert "failed" in embed.title
    assert "boom happened" in embed.description  # reason surfaced on the row


@pytest.mark.asyncio
async def test_task_notification_stopped_marks_row_stopped():
    """status=stopped → ⏹️ row; a lone stopped task colors the panel gray."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    await renderer._handle_task_notification(_notification_event(status="stopped"))
    await renderer._flush_panel_now()

    embed = target._sent[0].embeds[0]
    assert embed.color.value == COLOR_THINKING
    assert "⏹️" in embed.description
    assert "stopped" in embed.title


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
async def test_task_updated_killed_marks_row():
    """TaskUpdated status=killed → ⏹️ row, state cleaned, panel gray."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    await renderer._handle_task_updated(_updated_event(status="killed"))
    await renderer._flush_panel_now()

    embed = target._sent[0].embeds[0]
    assert "⏹️" in embed.description
    assert embed.color.value == COLOR_THINKING
    assert "t1" not in renderer._task_states


# ---------------------------------------------------------------------------
# Panel consolidation: rollover / windowing / trailing flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_consolidates_multiple_tasks_into_one_message():
    """Several tasks in one run share ONE panel message (no per-task spam)."""
    renderer, target = _make_renderer()
    for i in range(4):
        await renderer._handle_task_started(
            _started_event(task_id=f"t{i}", description=f"shot {i}"),
            subagent_renderers={},
        )
    await renderer._flush_panel_now()
    assert len(target._sent) == 1  # one consolidated panel for all four
    desc = target._sent[0].embeds[0].description
    for i in range(4):
        assert f"shot {i}" in desc


@pytest.mark.asyncio
async def test_panel_rolls_over_at_max_rows(monkeypatch):
    """Past TASK_PANEL_MAX_ROWS a fresh panel message is opened."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PANEL_MAX_ROWS", 3)
    renderer, target = _make_renderer()
    for i in range(4):
        await renderer._handle_task_started(
            _started_event(task_id=f"t{i}"), subagent_renderers={}
        )
    # 3 rows fill panel 1; the 4th rolls over to a second message.
    assert len(target._sent) == 2


@pytest.mark.asyncio
async def test_panel_rolls_over_after_quiet_gap(monkeypatch):
    """A new task after a quiet gap starts a fresh panel (a new 'run')."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PANEL_ROLLOVER_GAP_SECONDS", 5.0)
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(task_id="a"), subagent_renderers={})
    # Pretend the last activity was long ago.
    renderer._task_panel.last_activity_at = time.time() - 100
    await renderer._handle_task_started(_started_event(task_id="b"), subagent_renderers={})
    assert len(target._sent) == 2


@pytest.mark.asyncio
async def test_panel_folds_old_rows(monkeypatch):
    """Past TASK_PANEL_VISIBLE_ROWS the oldest rows collapse to '… N earlier …'."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PANEL_MAX_ROWS", 100)  # keep them in one panel
    monkeypatch.setattr(dr, "TASK_PANEL_VISIBLE_ROWS", 5)
    renderer, target = _make_renderer()
    for i in range(8):
        await renderer._handle_task_started(
            _started_event(task_id=f"t{i}", description=f"task {i}"),
            subagent_renderers={},
        )
    await renderer._flush_panel_now()
    desc = target._sent[0].embeds[0].description
    assert "earlier" in desc       # fold marker
    assert "task 7" in desc        # most recent stays visible
    assert "task 0" not in desc    # oldest folded away


@pytest.mark.asyncio
async def test_panel_trailing_flush_renders_latest():
    """A throttled-away terminal still lands once the panel is flushed."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(
        _started_event(task_id="x", description="do x"), subagent_renderers={}
    )
    renderer._task_panel.last_edit_at = time.time()  # recent → completion throttled

    await renderer._handle_task_notification(
        _notification_event(task_id="x", status="completed")
    )
    # Force the final flush (as render_response / the bg reader end do).
    await renderer._flush_panel_now()
    assert "✅" in target._sent[0].embeds[0].description


@pytest.mark.asyncio
async def test_panel_description_capped_at_discord_limit(monkeypatch):
    """A very long task list is truncated to Discord's 4096 embed-description
    limit (never raises)."""
    import clauded.discord_renderer as dr

    monkeypatch.setattr(dr, "TASK_PANEL_MAX_ROWS", 10_000)
    monkeypatch.setattr(dr, "TASK_PANEL_VISIBLE_ROWS", 10_000)
    renderer, target = _make_renderer()
    for i in range(200):
        await renderer._handle_task_started(
            _started_event(task_id=f"t{i}", description="x" * 60),
            subagent_renderers={},
        )
    await renderer._flush_panel_now()
    desc = target._sent[0].embeds[0].description
    assert len(desc) <= 4000


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
