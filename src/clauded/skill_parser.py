"""SDK source-tag parser for ``/skill list``.

Parses the trailing ``" (user)" / " (project)" / " (plugin:<name>)"``
suffix that Claude CLI's ``get_server_info()["commands"]`` puts on each
command's description. Pinned by live probe 2026-05-11; built-ins have
no suffix and return ``("", "", "")``.
"""

from __future__ import annotations


def classify_command(cmd: dict) -> tuple[str, str, str]:
    """Return ``(group, display_name, display_desc)``; ``group=""`` for built-ins."""
    name = cmd.get("name", "")
    desc = cmd.get("description", "") or ""
    if desc.endswith(" (user)"):
        return ("user", name, desc[:-7].rstrip())
    if desc.endswith(" (project)"):
        return ("project", name, desc[:-10].rstrip())
    if desc.endswith(")") and " (plugin:" in desc:
        # Require the slice between "(plugin:" and the trailing ")" to be
        # paren-free so "X (plugin:p) extra)" (where the trailing ")" is
        # not the plugin marker's close) falls through to built-in.
        idx = desc.rfind(" (plugin:")
        plugin_name = desc[idx + 9:-1]
        if ")" in plugin_name or "(" in plugin_name:
            return ("", "", "")
        return (f"plugin:{plugin_name}", name, desc[:idx].rstrip())
    return ("", "", "")
