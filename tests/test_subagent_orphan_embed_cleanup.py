"""#212 — remove speculative TaskOutput/AgentOutput/TaskStop/AgentStop embed paths.

v1.6 PRD F4 wired up sub-agent intermediate-output embeds with guessed
SDK tool names + schema. SDK never documented these; field name
``output`` was never observed in prod data; the resulting embeds rendered
empty + orphan (no anchor link to the spawning ``Task`` tool-use).

Approach C: delete the speculative paths; emit a stream_logger event so
future re-introduction is data-driven (#223 PR-A stream_logger is alive).
"""
from __future__ import annotations

import inspect

import pytest

from clauded.discord_renderer import DiscordRenderer


def test_no_taskoutput_embed_path():
    """The speculative `if name in ("TaskOutput", "AgentOutput"):` ToolUseBlock
    branch must be removed. v1.6 F4 schema guess never matched prod."""
    src = inspect.getsource(DiscordRenderer)
    assert 'if name in ("TaskOutput", "AgentOutput")' not in src, (
        "#212: speculative TaskOutput/AgentOutput embed path must be removed"
    )
    # Title string also gone (no place left to emit it)
    assert "📤 Subtask Output" not in src, (
        "#212: '📤 Subtask Output' embed title must be removed"
    )


def test_no_taskstop_embed_path():
    """`TaskStop`/`AgentStop` was emitting empty red embeds. Removed."""
    src = inspect.getsource(DiscordRenderer)
    assert 'if name in ("TaskStop", "AgentStop")' not in src, (
        "#212: speculative TaskStop/AgentStop embed path must be removed"
    )
    assert "⏹️ Subtask Stopped" not in src, (
        "#212: '⏹️ Subtask Stopped' embed title must be removed"
    )


def test_subagent_tool_use_forensics_log_present():
    """#212: when any sub-agent-ish tool name appears, we emit a
    SubAgentToolUse event to stream_logger so we capture real schema
    for future re-introduction (data-driven, not guessed)."""
    src = inspect.getsource(DiscordRenderer)
    # The forensic log block must exist
    assert "#212 forensics" in src
    assert '"type": "SubAgentToolUse"' in src
    # The list of names must include all 6 (Task/Agent + Output + Stop)
    for n in (
        "TaskOutput", "AgentOutput",
        "TaskStop", "AgentStop",
        "Task", "Agent",
    ):
        assert f'"{n}"' in src, (
            f"#212: sub-agent tool name {n!r} must appear in forensics list"
        )


def test_taskstop_tool_result_still_short_circuits():
    """The tool_RESULT branch for `TaskStop` still continues (skips generic
    tool-result rendering), so even if CLI emits TaskStop ToolResult we
    don't generate noise — just stream-log it (via the tool_use forensics
    above)."""
    src = inspect.getsource(DiscordRenderer)
    assert 'if result_name == "TaskStop"' in src
    # The body of that branch is a single ``continue``
    # We just pin the branch existence + that it carries our #212 note
    assert "#212" in src


def test_task_agent_branch_preserved():
    """Sanity: the *working* Task/Agent sub-thread creation path (line 1079)
    is NOT removed by this cleanup. Subtask Complete + dispatched still work."""
    src = inspect.getsource(DiscordRenderer)
    assert 'if name in ("Task", "Agent"):' in src
    # The thread-creation flow is preserved
    assert "Subtask #" in src  # the title format from the Task branch
