"""T1-B: hook-fed live per-agent workflow roster.

The CLI's ``workflowProgress`` payload never arrives in practice, so the
workflow progress embed is built from ``bot._agent_roster`` — a roster
maintained by the SubagentStart / PreToolUse / SubagentStop hooks. These tests
cover the roster bookkeeping helpers and the renderer's line formatter without
needing a live workflow.
"""
from __future__ import annotations

import time

from clauded.bot import ClaudedBot
from clauded.discord_renderer import DiscordRenderer


class _RosterHost:
    """Minimal stand-in exposing just ``_agent_roster`` so the bound
    ClaudedBot roster helpers can run without constructing a full bot."""

    def __init__(self) -> None:
        self._agent_roster: dict[int, dict[str, dict]] = {}


def test_roster_start_tool_clear_lifecycle():
    h = _RosterHost()
    ClaudedBot._roster_note_start(h, 111, "a1", "code-reviewer")
    ClaudedBot._roster_note_start(h, 111, "a2", "general-purpose")
    assert set(h._agent_roster[111]) == {"a1", "a2"}
    assert h._agent_roster[111]["a1"]["type"] == "code-reviewer"
    assert h._agent_roster[111]["a1"]["tool"] is None

    ClaudedBot._roster_note_tool(h, 111, "a1", "Read")
    assert h._agent_roster[111]["a1"]["tool"] == "Read"

    # tool update for an unknown agent/thread is a no-op (no crash)
    ClaudedBot._roster_note_tool(h, 999, "zz", "Bash")
    ClaudedBot._roster_note_tool(h, 111, "zz", "Bash")

    ClaudedBot._roster_clear(h, 111, "a1")
    assert set(h._agent_roster[111]) == {"a2"}
    # clearing the last agent prunes the thread entry entirely
    ClaudedBot._roster_clear(h, 111, "a2")
    assert 111 not in h._agent_roster


def test_format_roster_lines_shows_type_tool_elapsed():
    now = time.time()
    roster = {
        "a1": {"type": "code-reviewer", "tool": "Read", "started": now - 5},
        "a2": {"type": "general-purpose", "tool": None, "started": now - 2},
    }
    lines = DiscordRenderer._format_roster_lines(roster)
    assert len(lines) == 2
    assert "code-reviewer" in lines[0] and "🔧 Read" in lines[0]
    # no tool yet → thinking marker
    assert "general-purpose" in lines[1] and "💭" in lines[1]


def test_format_roster_lines_folds_past_ten():
    now = time.time()
    roster = {f"a{i}": {"type": "t", "tool": None, "started": now} for i in range(15)}
    lines = DiscordRenderer._format_roster_lines(roster)
    # 10 agent lines + 1 "… N more …" fold line
    assert len(lines) == 11
    assert "more" in lines[-1]


def test_format_roster_lines_empty_is_empty():
    assert DiscordRenderer._format_roster_lines({}) == []


def test_format_roster_lines_tolerates_bad_entries():
    # A malformed entry must not crash the formatter.
    lines = DiscordRenderer._format_roster_lines({"a1": "not-a-dict"})
    assert lines == []
