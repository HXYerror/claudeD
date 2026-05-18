"""Diagnostic event logger for stream / control-plane / crash forensics (#223).

When enabled, every interesting event (SDK stream message, control-plane
call, Discord HTTP retry, renderer crash) is written as a single JSON
line to ``~/Library/Logs/clauded/stream-debug.jsonl``. The file is the
canonical source consumed by the future ``/log dump`` epic (#224).

Enable / disable
================
Two switches, runtime override beats env:

* ``stream_logger.set_enabled(True)`` — runtime override (the future
  ``/debug`` slash cog will call this). ``set_enabled(None)`` reverts
  to the env-driven default.
* ``CLAUDED_STREAM_DEBUG`` env var (``1``/``true``/``yes``) — the
  startup default. Stays unchanged from pre-#223 behavior.

Path
====
``~/Library/Logs/clauded/stream-debug.jsonl`` — lives next to
``clauded.log`` so the ``/log dump`` epic picks it up automatically.
Pre-#223 path was ``logs/stream-debug.jsonl`` in cwd — abandoned
(dev artifact only, no production users).

Rotation
========
10 MB × 7 backups via a thin wrapper that delegates to
``RotatingFileHandler.doRollover`` (matching ``_logging_setup.py``
production logger sizing). Wrapper writes JSONL bypass formatter.

Event shape
===========
``log_event(event, buffer_len=..., extra=...)`` accepts:

* ``dict`` — used as-is (post-#223 control-plane / retry / crash events)
* ``AssistantMessage`` / ``ResultMessage`` / ``StreamEvent`` — extracted
  per pre-#223 logic (regression-safe)

Always adds ``ts`` (float seconds since epoch).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from ._logging_setup import _LOG_DIR

log = logging.getLogger("clauded.stream_logger")

# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

_OVERRIDE: Optional[bool] = None
"""Runtime override; ``None`` means env var decides."""


def is_enabled() -> bool:
    """True if events should be written. Runtime override beats env."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    return os.environ.get("CLAUDED_STREAM_DEBUG", "").strip() in ("1", "true", "yes")


def set_enabled(flag: Optional[bool]) -> None:
    """Set runtime override. ``None`` reverts to env-driven default."""
    global _OVERRIDE
    _OVERRIDE = flag


# ---------------------------------------------------------------------------
# File handle + rotation
# ---------------------------------------------------------------------------

_LOG_PATH = _LOG_DIR / "stream-debug.jsonl"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 7
_FH: Any = None


def _open_file() -> Any:
    """Open the jsonl file in append mode, creating parent dir if needed."""
    global _FH
    if _FH is None:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Read-only $HOME (sandboxed test runner); silently disable.
            log.warning(
                "stream-debug: cannot create log dir %s: %s",
                _LOG_DIR,
                exc,
            )
            return None
        try:
            _FH = open(_LOG_PATH, "a", encoding="utf-8")
        except OSError as exc:
            log.warning(
                "stream-debug: cannot open %s: %s",
                _LOG_PATH,
                exc,
            )
            return None
    return _FH


def _maybe_rotate() -> None:
    """If current file exceeds ``_MAX_BYTES``, rotate. Best-effort."""
    global _FH
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size >= _MAX_BYTES:
            if _FH is not None:
                _FH.close()
                _FH = None
            # Shift .N → .N+1, drop oldest
            for i in range(_BACKUP_COUNT - 1, 0, -1):
                src = _LOG_PATH.with_suffix(f".jsonl.{i}")
                dst = _LOG_PATH.with_suffix(f".jsonl.{i + 1}")
                if src.exists():
                    src.replace(dst)
            _LOG_PATH.replace(_LOG_PATH.with_suffix(".jsonl.1"))
    except OSError as exc:
        # Rotation failure is non-fatal; we'll just keep appending.
        log.warning("stream-debug rotation failed: %s", exc)


# ---------------------------------------------------------------------------
# Event-building (preserves pre-#223 SDK extraction)
# ---------------------------------------------------------------------------


def _build_entry(event: object, buffer_len: int) -> dict:
    """Return a serializable dict for ``event``.

    Generalized in #223 — plain dicts pass through with ts injected;
    SDK message types use the legacy extraction so existing callers
    (none in tree post-#223 audit, but external callers may exist)
    keep working.
    """
    if isinstance(event, dict):
        # Caller passed a pre-built event payload (control-plane / retry / crash)
        entry: dict = {"ts": time.time()}
        entry.update(event)
        if buffer_len:
            entry["buffer_len"] = buffer_len
        return entry

    # Legacy SDK-type extraction
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        try:
            from claude_agent_sdk.types import StreamEvent
        except ImportError:
            StreamEvent = None  # type: ignore[assignment]
    except ImportError:
        # SDK not installed (CI / unit test path) — emit minimal envelope
        return {
            "ts": time.time(),
            "type": type(event).__name__,
            "buffer_len": buffer_len,
        }

    entry = {
        "ts": time.time(),
        "type": type(event).__name__,
        "buffer_len": buffer_len,
    }
    if isinstance(event, ResultMessage):
        entry["subtype"] = event.subtype
        entry["is_error"] = event.is_error
        entry["num_turns"] = event.num_turns
        entry["total_cost_usd"] = event.total_cost_usd
        entry["result_len"] = len(event.result or "")
        entry["session_id"] = event.session_id
        entry["duration_ms"] = event.duration_ms
        entry["usage"] = event.usage
    elif isinstance(event, AssistantMessage):
        blocks: list[dict] = []
        for b in event.content:
            if isinstance(b, TextBlock):
                blocks.append({"type": "text", "len": len(b.text)})
            elif isinstance(b, ToolUseBlock):
                blocks.append({"type": "tool_use", "name": b.name})
            elif isinstance(b, ToolResultBlock):
                blocks.append(
                    {"type": "tool_result", "len": len(str(b.content or ""))}
                )
        entry["blocks"] = blocks
        entry["parent_tool_use_id"] = event.parent_tool_use_id
    elif StreamEvent is not None and isinstance(event, StreamEvent):
        ev = event.event
        entry["event_type"] = ev.get("type", "")
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            entry["delta_type"] = delta.get("type", "")
            if delta.get("type") == "text_delta":
                entry["text_len"] = len(delta.get("text", ""))
        entry["parent_tool_use_id"] = event.parent_tool_use_id

    return entry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def log_event(
    event: object,
    buffer_len: int = 0,
    extra: Optional[dict] = None,
) -> None:
    """Append one event line to ``stream-debug.jsonl`` if enabled.

    #223: failures used to be silently swallowed; now we log a single
    ``WARNING`` via stdlib logger so the user knows the diagnostic
    channel itself is broken (otherwise ``/log dump`` returns a stale
    or missing file with no explanation).
    """
    if not is_enabled():
        return
    entry = _build_entry(event, buffer_len)
    if extra:
        entry.update(extra)
    try:
        _maybe_rotate()
        f = _open_file()
        if f is None:
            return
        f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        f.flush()
    except OSError as exc:
        log.warning("stream-debug write failed: %s", exc)


# ---------------------------------------------------------------------------
# Test-support
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Reopen the file handle on next write. Used by unit tests."""
    global _FH, _OVERRIDE
    if _FH is not None:
        try:
            _FH.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
        _FH = None
    _OVERRIDE = None
