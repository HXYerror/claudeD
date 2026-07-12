"""Helpers for reading/writing CLI-native config (#294).

``.claude/agents/<name>.md`` and ``.mcp.json`` are the Claude CLI's own
canonical storage. This module centralises the I/O so ``cogs/agent.py``,
``cogs/mcp.py``, and the one-time startup migration all agree on the
layout, atomicity, and locking.

Two write paths:

- :func:`write_agent_md` — YAML frontmatter (``name`` / ``description``)
  followed by the prompt body. Parseable by the CLI *and* by our own
  ``cogs/agent._parse_agent_md`` fallback.
- :func:`add_mcp_server` / :func:`remove_mcp_server` — atomic
  read-modify-write of the project's ``.mcp.json`` (mcpServers dict).

All writes go through :func:`_json_store.atomic_write_json` (for
``.mcp.json``) or a similar unique-tmp + ``os.replace`` idiom (for
``.md``) so a torn write can never leave a half-file on disk.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from ._json_store import atomic_write_json

log = logging.getLogger("clauded.cli_native")

# One module-level lock guards every ``.mcp.json`` and every
# ``.claude/agents/*.md`` write across every project. Per-project locking
# would be nicer but Discord's slash-command dispatch is already
# effectively serial per interaction and these writes are microseconds —
# a single lock is more than enough to prevent concurrent tmp-file
# collisions.
_FILE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# .claude/agents/<name>.md
# ---------------------------------------------------------------------------


def agent_md_path(project_path: Path | str, name: str) -> Path:
    """Return the canonical path for an agent's ``.md`` under a project.

    Raises ValueError if name contains path separators or traversal sequences.
    """
    # C1 guard: reject path-separator / traversal in name
    if any(c in name for c in ("/", "\\", "\x00")) or ".." in name:
        raise ValueError(f"Agent name contains invalid characters: {name!r}")
    safe_name = name.strip().replace(" ", "-")
    if not safe_name:
        raise ValueError("Agent name is empty after sanitization")
    return Path(project_path) / ".claude" / "agents" / f"{safe_name}.md"


def write_agent_md(
    project_path: Path | str,
    name: str,
    prompt: str,
    description: str = "",
) -> Path:
    """Write ``.claude/agents/<name>.md`` atomically.

    Frontmatter is a minimal 2-key YAML block (``name`` + ``description``);
    ``description`` is single-lined (any embedded ``\\n`` / ``\\r`` is
    replaced with a space) so the naive line-based parser in
    ``cogs/agent._parse_agent_md`` round-trips cleanly. The prompt body
    follows the frontmatter verbatim.

    The write is atomic: content is first written to a unique
    ``.<pid>.<rand>.tmp`` sibling and ``os.replace``-d into place.
    """
    target = agent_md_path(project_path, name)
    target.parent.mkdir(parents=True, exist_ok=True)

    desc_line = (description or "").replace("\r", " ").replace("\n", " ")
    # C2: YAML-escape name and description to prevent frontmatter injection
    safe_name = name.replace('"', '\\"')
    safe_desc = desc_line.replace('"', '\\"')
    body = prompt or ""
    # Ensure trailing newline so ``cat`` / editors behave.
    if body and not body.endswith("\n"):
        body = body + "\n"
    content = f'---\nname: "{safe_name}"\ndescription: "{safe_desc}"\n---\n{body}'

    tmp = target.with_suffix(f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with _FILE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def delete_agent_md(project_path: Path | str, name: str) -> bool:
    """Delete ``.claude/agents/<name>.md``. Returns True iff a file was removed.

    Idempotent — a missing file is not an error.
    """
    target = agent_md_path(project_path, name)
    with _FILE_LOCK:
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("delete_agent_md(%s): %s", target, exc)
            return False


# ---------------------------------------------------------------------------
# .mcp.json
# ---------------------------------------------------------------------------


def mcp_json_path(project_path: Path | str) -> Path:
    """Return the canonical path for a project's ``.mcp.json``."""
    return Path(project_path) / ".mcp.json"


def _load_mcp_json(path: Path) -> dict:
    """Return the parsed ``.mcp.json`` payload, or an empty skeleton.

    Any I/O or parse error is treated as "no file yet" and returns a
    fresh skeleton — matching the CLI which auto-creates the file on
    first server registration.
    """
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(".mcp.json (%s) unreadable: %s — starting fresh", path, exc)
        return {"mcpServers": {}}
    if not isinstance(data, dict):
        return {"mcpServers": {}}
    if "mcpServers" not in data or not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    return data


def add_mcp_server(
    project_path: Path | str,
    name: str,
    config: dict[str, Any],
) -> None:
    """Add ``config`` under ``mcpServers[name]`` in the project's ``.mcp.json``.

    Raises ``ValueError`` if a server with the same name already exists,
    mirroring :meth:`ProjectManager.add_mcp_server` for consistent UX.
    """
    path = mcp_json_path(project_path)
    with _FILE_LOCK:
        data = _load_mcp_json(path)
        servers = data.setdefault("mcpServers", {})
        if name in servers:
            raise ValueError(f"MCP server {name!r} already exists in .mcp.json.")
        servers[name] = config
        atomic_write_json(path, data, _FILE_LOCK)


def remove_mcp_server(project_path: Path | str, name: str) -> bool:
    """Remove ``mcpServers[name]`` from the project's ``.mcp.json``.

    Returns True iff an entry was removed. Idempotent: missing file /
    missing entry returns False without raising.
    """
    path = mcp_json_path(project_path)
    with _FILE_LOCK:
        data = _load_mcp_json(path)
        servers = data.get("mcpServers") or {}
        if name not in servers:
            return False
        del servers[name]
        data["mcpServers"] = servers
        atomic_write_json(path, data, _FILE_LOCK)
        return True


__all__ = [
    "agent_md_path",
    "write_agent_md",
    "delete_agent_md",
    "mcp_json_path",
    "add_mcp_server",
    "remove_mcp_server",
]
