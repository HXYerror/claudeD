"""#292 S4 — tests for ``/workflow`` cog slash commands.

Uses lightweight mocks for ``discord.Interaction`` and the bot to test
the cog command functions directly (bypassing the command framework).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.cogs.workflow import (
    _format_duration,
    _resolve_task_id,
    workflow_detail,
    workflow_kill,
    workflow_list,
)
from clauded.discord_renderer import (
    COLOR_INFO,
    COLOR_TOOL_FAILURE,
    COLOR_TOOL_SUCCESS,
    _TaskState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_interaction(*, workflow_tasks=None, session_manager=None, session_id="sess1"):
    """Build a minimal Interaction mock with bot + response tracking."""
    bot = MagicMock()
    bot._workflow_tasks = workflow_tasks or {}
    bot.session_manager = session_manager

    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot

    # Track sent responses
    response = AsyncMock()
    interaction.response = response

    # For kill: resolve_session_id reads channel_id from the interaction
    interaction.channel = MagicMock()
    interaction.channel.id = 12345
    interaction.channel_id = 12345
    # Give it a parent so resolve_session_id treats it as a thread
    interaction.channel.parent = MagicMock()

    return interaction


def _make_task_state(
    description="test task",
    task_type="workflow",
    started_at=None,
    last_usage=None,
):
    return _TaskState(
        description=description,
        task_type=task_type,
        started_at=started_at or time.time(),
        last_usage=last_usage,
    )


# ---------------------------------------------------------------------------
# /workflow list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_list_empty():
    """/workflow list with no tasks → 'No running' message."""
    interaction = _make_interaction()
    await workflow_list.callback(interaction)

    interaction.response.send_message.assert_called_once()
    call_kwargs = interaction.response.send_message.call_args
    embed = call_kwargs.kwargs.get("embed") or call_kwargs[1].get("embed")
    assert "No running" in embed.description


@pytest.mark.asyncio
async def test_workflow_list_with_tasks():
    """/workflow list with tasks → embed shows task info."""
    tasks = {
        "abcdef1234567890": _make_task_state(
            description="build feature",
            last_usage={"total_tokens": 5000, "tool_uses": 10, "duration_ms": 3000},
        ),
    }
    interaction = _make_interaction(workflow_tasks=tasks)
    await workflow_list.callback(interaction)

    call_kwargs = interaction.response.send_message.call_args
    embed = call_kwargs.kwargs.get("embed") or call_kwargs[1].get("embed")
    assert "abcdef12" in embed.description
    assert "build feature" in embed.description
    assert "(1)" in embed.title


# ---------------------------------------------------------------------------
# /workflow kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_kill_happy(monkeypatch):
    """/workflow kill with valid ID → bridge.stop_task called."""
    bridge = AsyncMock()
    bridge.stop_task = AsyncMock()

    session_manager = MagicMock()
    session_manager.get_session.return_value = bridge

    tasks = {
        "abcdef1234567890": _make_task_state(description="some task"),
    }
    interaction = _make_interaction(
        workflow_tasks=tasks,
        session_manager=session_manager,
    )

    # Patch resolve_session_id to return a valid session id
    monkeypatch.setattr(
        "clauded.cogs.workflow.resolve_session_id",
        lambda _: "sess1",
    )

    await workflow_kill.callback(interaction, task_id="abcdef12")

    bridge.stop_task.assert_called_once_with("abcdef1234567890")
    call_kwargs = interaction.response.send_message.call_args
    msg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content", "")
    assert "⏹️" in msg or "Stopping" in msg


@pytest.mark.asyncio
async def test_workflow_kill_not_found():
    """/workflow kill with bad ID → error message."""
    interaction = _make_interaction(workflow_tasks={})
    await workflow_kill.callback(interaction, task_id="nonexist")

    call_kwargs = interaction.response.send_message.call_args
    msg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("content", "")
    assert "❌" in msg or "No running task" in msg


# ---------------------------------------------------------------------------
# /workflow detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_detail_happy():
    """/workflow detail with valid ID → embed with task info."""
    tasks = {
        "abcdef1234567890": _make_task_state(
            description="implement auth",
            task_type="local_workflow",
            last_usage={"total_tokens": 3000, "tool_uses": 7, "duration_ms": 2000},
        ),
    }
    interaction = _make_interaction(workflow_tasks=tasks)
    await workflow_detail.callback(interaction, task_id="abcdef12")

    call_kwargs = interaction.response.send_message.call_args
    embed = call_kwargs.kwargs.get("embed") or call_kwargs[1].get("embed")
    assert embed.title == "⚡ Workflow Task Detail"
    # Check fields contain expected data
    field_values = [f.value for f in embed.fields]
    assert any("abcdef12" in v for v in field_values)
    assert any("implement auth" in v for v in field_values)
    assert any("local_workflow" in v for v in field_values)
