"""Logging + runtime-dir setup, extracted from ``bot.py`` (v1.18 stage-28 trim).

Owns the three pieces of module-level state that ``commands.Bot`` does not need
to know about:

* :data:`_LOG_DIR` — production rotating-log destination (``~/Library/Logs/clauded``)
* :data:`_CACHE_DIR` — companion runtime cache dir (heartbeat lives here); kept
  alongside ``_LOG_DIR`` so :func:`_ensure_runtime_dirs` can do both with one
  Darwin guard. ``bot.py`` re-exports it for ``_HEARTBEAT_PATH`` composition.
* :func:`_ensure_runtime_dirs` — one-shot mkdir at startup
* :func:`_configure_logging` — pytest-aware ``basicConfig``/``RotatingFileHandler``

Behavior is unchanged from the in-``bot.py`` version (PR #149 R2 contract): same
RotatingFileHandler params, same stderr fallback, same ``PYTEST_CURRENT_TEST``
detection, same swallowed ``OSError`` for read-only homes.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# --------------------------------------------------------------------------
# macOS LaunchAgent paths (mirrors scripts/install-launchagent.sh etc.).
# When changing any of these, grep the repo for the matching string in the
# bash scripts + plist template.
# --------------------------------------------------------------------------
_LOG_DIR: Path
_CACHE_DIR: Path
if sys.platform == "win32":
    _appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    _LOG_DIR = _appdata / "clauded" / "logs"
    _CACHE_DIR = _appdata / "clauded" / "cache"
else:
    _LOG_DIR = Path.home() / "Library" / "Logs" / "clauded"
    _CACHE_DIR = Path.home() / "Library" / "Caches" / "clauded"


def _ensure_runtime_dirs() -> None:
    """Create ``_LOG_DIR`` and ``_CACHE_DIR`` once at process start.

    Replaces the per-tick ``parent.mkdir`` calls in ``_touch_heartbeat`` and
    ``_configure_logging`` so a 30 s heartbeat loop and a 1-call logging-setup
    don't each redo the dir checks (PR #149 R2 engineer suggestion). Swallows
    ``OSError`` so a read-only or sandboxed home doesn't crash startup; the
    individual write call sites handle the consequence.
    """
    if sys.platform not in ("darwin", "win32"):
        return
    for d in (_LOG_DIR, _CACHE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def _configure_logging() -> None:
    """Set up app logging — RotatingFileHandler in production, stderr-only in tests.

    Detects pytest via ``PYTEST_CURRENT_TEST`` so test runs don't pollute
    ``~/Library/Logs/clauded/``. In production on macOS, attaches a 10 MB × 7
    rotating file handler plus a stderr handler so launchd's
    ``StandardErrorPath`` still captures boot diagnostics. On non-Darwin
    (Linux/Windows dev boxes) and on ``OSError`` (e.g. read-only ``$HOME``)
    falls back to ``basicConfig`` to stderr — same path as pytest — to avoid
    silently creating macOS-shaped junk directories outside macOS.
    """
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        logging.basicConfig(level=logging.INFO, format=fmt)
        return
    if sys.platform not in ("darwin", "win32"):
        logging.basicConfig(level=logging.INFO, format=fmt)
        return
    # _LOG_DIR was created at startup by _ensure_runtime_dirs(); if it's still
    # absent (e.g. read-only home, sandboxed test runner) fall back to stderr.
    if not _LOG_DIR.exists():
        logging.basicConfig(level=logging.INFO, format=fmt)
        return
    handler = RotatingFileHandler(
        _LOG_DIR / "clauded.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(fmt))
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stderr_handler)
