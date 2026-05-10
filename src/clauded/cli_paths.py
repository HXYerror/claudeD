"""CLI path resolution for the Claude binary.

The ``claude-agent-sdk`` ships with a bundled ``claude`` binary, but we
prefer the operator's system-installed CLI so updates to the CLI flow
through to the bot without re-installing the SDK package. The resolver
checks ``$PATH`` first, then a small list of well-known install
locations (Homebrew, ``/usr/local/bin``, ``~/.local/bin``,
``~/.npm-global/bin``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_CANDIDATES = (
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".local" / "bin" / "claude"),
    str(Path.home() / ".npm-global" / "bin" / "claude"),
)


def resolve_claude_cli() -> str | None:
    """Resolve the operator's Claude CLI path.

    Returns the absolute path to a usable executable, or ``None`` if not
    found. When ``None``, the SDK falls back to its bundled CLI.

    Resolution order:

    1. ``shutil.which("claude")`` against the active ``$PATH``.
    2. Fixed candidate list of common install locations.

    Each candidate must be a real file AND executable by the current
    user — broken symlinks and non-executable artifacts are skipped to
    avoid handing the SDK an unusable path.
    """
    p = shutil.which("claude")
    if p:
        cp = Path(p)
        if cp.is_file() and os.access(cp, os.X_OK):
            return p
    for candidate in _CANDIDATES:
        cp = Path(candidate)
        if cp.is_file() and os.access(cp, os.X_OK):
            return candidate
    return None


__all__ = ["resolve_claude_cli"]
