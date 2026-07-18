"""#321 — task_type gate must NOT drop non-workflow background tasks.

Root cause fixed: ``DiscordRenderer._dispatch_task_event`` used to gate ALL
Task* rendering on ``"workflow" in task_type.lower()``. Real background tasks
use task_type ``local_bash`` / ``local_agent`` / ``local_workflow``; only the
last contains the substring "workflow", so ``local_bash`` + ``local_agent``
Task events were silently dropped and the user saw no progress.

These tests assert that a ``local_bash`` TaskStarted gets tracked into
``_task_states`` (via both the direct handler and the ``_dispatch_task_event``
routing) and that its TaskProgress is then handled. Light fakes only — no full
render loop, and (per repo constraints) authored to be ast-parseable without
running pytest here.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import FakeTarget

from clauded.discord_renderer import DiscordRenderer
import clauded.discord_renderer as dr


# ---------------------------------------------------------------------------
# Helpers — light fakes mirroring tests/test_workflow_render.py
# ---------------------------------------------------------------------------

def _make_renderer():
    target = FakeTarget()
    return DiscordRenderer(target, bot=None), target


def _started_event(task_id="bash1", description="run build", task_type="local_bash"):
    return SimpleNamespace(
        task_id=task_id,
        description=description,
        task_type=task_type,
        tool_use_id=None,
        uuid="u1",
        session_id="s1",
    )


def _progress_event(task_id="bash1", usage=None, last_tool_name="Bash"):
    return SimpleNamespace(
        task_id=task_id,
        usage=usage or {"total_tokens": 10, "tool_uses": 1, "duration_ms": 500},
        last_tool_name=last_tool_name,
        data={},
    )


# ---------------------------------------------------------------------------
# Direct handler: every task_type is tracked + labelled by real type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_bash_started_is_tracked():
    """#321: a local_bash TaskStarted lands in _task_states (not dropped)."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})

    assert "bash1" in renderer._task_states
    state = renderer._task_states["bash1"]
    assert state.task_type == "local_bash"
    assert state.message is not None
    # Labelled as a background task, not "Dynamic Workflow".
    assert "🖥️" in target._sent[0].embeds[0].title


@pytest.mark.asyncio
async def test_local_bash_progress_is_handled():
    """#321: TaskProgress for a tracked local_bash task edits in place."""
    renderer, target = _make_renderer()
    await renderer._handle_task_started(_started_event(), subagent_renderers={})
    assert len(target._sent) == 1

    # Bypass the edit throttle.
    renderer._task_states["bash1"].last_edit_at = 0.0
    await renderer._handle_task_progress(_progress_event())

    # No new message — the existing one was edited (progress rendered).
    assert len(target._sent) == 1
    assert renderer._task_states["bash1"].last_usage["total_tokens"] == 10


def test_task_type_label_distinguishes_types():
    """#321: each real task_type gets a distinct banner label."""
    assert "🖥️" in DiscordRenderer._task_type_label("local_bash")
    assert "🤖" in DiscordRenderer._task_type_label("local_agent")
    assert "⚡" in DiscordRenderer._task_type_label("local_workflow")
    assert "⚡" in DiscordRenderer._task_type_label("workflow")
    # Unknown / missing → generic, never a crash.
    assert DiscordRenderer._task_type_label("something_else")
    assert DiscordRenderer._task_type_label(None)


# ---------------------------------------------------------------------------
# Dispatch gate: local_bash routes through and is consumed (returns True)
# ---------------------------------------------------------------------------

class _FakeStarted:
    """Stand-in for the SDK TaskStartedMessage (isinstance-matched)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeProgress:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.asyncio
async def test_dispatch_tracks_local_bash_and_handles_progress(monkeypatch):
    """#321: _dispatch_task_event tracks a local_bash TaskStarted (returns
    True) and then routes its TaskProgress, instead of dropping both because
    the task_type lacks the substring "workflow"."""
    renderer, target = _make_renderer()

    # Point the module's isinstance gates at our light fakes.
    monkeypatch.setattr(dr, "_SdkTaskStartedMessage", _FakeStarted)
    monkeypatch.setattr(dr, "_SdkTaskProgressMessage", _FakeProgress)

    started = _FakeStarted(
        task_id="bash1",
        description="run build",
        task_type="local_bash",
        tool_use_id=None,
        uuid="u1",
        session_id="s1",
    )
    consumed = await renderer._dispatch_task_event(started, subagent_renderers={})
    assert consumed is True
    assert "bash1" in renderer._task_states  # tracked, not dropped

    renderer._task_states["bash1"].last_edit_at = 0.0
    progress = _FakeProgress(
        task_id="bash1",
        usage={"total_tokens": 42, "tool_uses": 2, "duration_ms": 900},
        last_tool_name="Bash",
        data={},
    )
    consumed_p = await renderer._dispatch_task_event(progress, subagent_renderers={})
    assert consumed_p is True
    assert renderer._task_states["bash1"].last_usage["total_tokens"] == 42
