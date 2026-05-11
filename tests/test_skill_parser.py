"""Tests for ``clauded.skill_parser.classify_command``.

Pinned by live probe on 2026-05-11 (see PRD docs/prd/v1.13-skill-list.md).
The Claude CLI emits source-tag suffixes on the description field;
this module is the canonical parser shared by ``/skill list`` and
future ``/skill info``.
"""

from __future__ import annotations

import pytest

from clauded.skill_parser import classify_command


@pytest.mark.parametrize(
    "cmd, expected",
    [
        # User skill
        (
            {"name": "crew", "description": "Multi-agent workflow (user)"},
            ("user", "crew", "Multi-agent workflow"),
        ),
        # Project skill
        (
            {"name": "testproj", "description": "Project-specific tooling (project)"},
            ("project", "testproj", "Project-specific tooling"),
        ),
        # Plugin skill
        (
            {"name": "myplug", "description": "From a plugin (plugin:myplug)"},
            ("plugin:myplug", "myplug", "From a plugin"),
        ),
        # Built-in (no suffix)
        (
            {"name": "clear", "description": "Clear the chat"},
            ("", "", ""),
        ),
        # Empty description — built-in / unrecognized
        (
            {"name": "noop", "description": ""},
            ("", "", ""),
        ),
        # None description — handled by "or ''"
        (
            {"name": "noop2", "description": None},
            ("", "", ""),
        ),
        # Edge: " (plugin:bar)" appears mid-string, description does NOT end
        # with ")". Must NOT classify as plugin.
        (
            {"name": "foo", "description": "foo (plugin:bar) more text"},
            ("", "", ""),
        ),
        # Edge: description ends with ")" but not the plugin marker.
        (
            {"name": "bar", "description": "does math (rounded)"},
            ("", "", ""),
        ),
        # Edge (I4, R1 engineer/tester): description ends with ")" AND
        # contains " (plugin:" — but the slice between "(plugin:" and
        # the trailing ")" is not a plain plugin name (extra paren).
        # Pin: this is treated as built-in / unrecognized, NOT a plugin.
        (
            {"name": "weird", "description": "foo (plugin:p) extra)"},
            ("", "", ""),
        ),
        # Edge: trailing whitespace before the suffix is rstripped.
        (
            {"name": "tidy", "description": "Trim trailing spaces   (user)"},
            ("user", "tidy", "Trim trailing spaces"),
        ),
        # Edge: missing description key.
        (
            {"name": "anon"},
            ("", "", ""),
        ),
    ],
)
def test_classify_command(cmd, expected) -> None:
    assert classify_command(cmd) == expected
