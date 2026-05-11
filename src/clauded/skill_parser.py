"""SDK source-tag parser shared by /skill list (and future /skill info).

The Claude Code CLI's ``get_server_info()["commands"]`` encodes the
source of each command as a trailing description suffix:

* ``" (user)"`` — user-level skill from ``~/.claude/skills/``
* ``" (project)"`` — project-local skill from ``<project>/.claude/skills/``
* ``" (plugin:<name>)"`` — plugin-provided command

Built-in commands (``clear``, ``compact``, ``init``, …) have no suffix.

This module parses that convention into a structured
``(group, name, description)`` tuple. The grammar is undocumented SDK
surface — pinned by live probe on 2026-05-11 and acceptance-tested in
``tests/test_skill_parser.py``.
"""

from __future__ import annotations


def classify_command(cmd: dict) -> tuple[str, str, str]:
    """Return ``(group, display_name, display_desc)`` for a CLI command.

    ``group`` is one of ``"user"``, ``"project"``, ``"plugin:<name>"``,
    or ``""`` for built-in / unrecognized commands (which the caller is
    expected to filter out). The displayed description has the
    source-tag suffix stripped.
    """
    name = cmd.get("name", "")
    desc = cmd.get("description", "") or ""
    if desc.endswith(" (user)"):
        return ("user", name, desc[:-7].rstrip())
    if desc.endswith(" (project)"):
        return ("project", name, desc[:-10].rstrip())
    if desc.endswith(")") and " (plugin:" in desc:
        # " (plugin:foo)" — extract plugin name. We require the slice
        # between "(plugin:" and the trailing ")" to be paren-free so
        # that descriptions like "X (plugin:p) extra)" (where the
        # trailing ")" is not the plugin marker's close) fall through.
        idx = desc.rfind(" (plugin:")
        plugin_name = desc[idx + 9:-1]
        if ")" in plugin_name or "(" in plugin_name:
            return ("", "", "")
        return (f"plugin:{plugin_name}", name, desc[:idx].rstrip())
    return ("", "", "")  # built-in — filter out


__all__ = ["classify_command"]
