"""Optional stream event logger for debugging truncation issues.

Enable by setting CLAUDED_STREAM_DEBUG=1 in .env.
Logs every StreamEvent/AssistantMessage/ResultMessage to logs/stream-debug.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("clauded.stream_logger")

_ENABLED = os.environ.get("CLAUDED_STREAM_DEBUG", "").strip() in ("1", "true", "yes")
_LOG_DIR = Path("logs")
_LOG_FILE: Any = None


def _ensure_file():
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = open(_LOG_DIR / "stream-debug.jsonl", "a")
    return _LOG_FILE


def log_event(event: object, buffer_len: int = 0, extra: dict | None = None) -> None:
    """Log a stream event to the debug file."""
    if not _ENABLED:
        return
    
    from claude_code_sdk import AssistantMessage, TextBlock, ResultMessage, ToolUseBlock, ToolResultBlock
    try:
        from claude_code_sdk.types import StreamEvent
    except ImportError:
        StreamEvent = None
    
    entry: dict = {
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
        # Grab raw data if available (SDK strips stop_reason)
        entry["usage"] = event.usage
    elif isinstance(event, AssistantMessage):
        blocks = []
        for b in event.content:
            if isinstance(b, TextBlock):
                blocks.append({"type": "text", "len": len(b.text)})
            elif isinstance(b, ToolUseBlock):
                blocks.append({"type": "tool_use", "name": b.name})
            elif isinstance(b, ToolResultBlock):
                blocks.append({"type": "tool_result", "len": len(str(b.content or ""))})
        entry["blocks"] = blocks
        entry["parent_tool_use_id"] = event.parent_tool_use_id
    elif StreamEvent and isinstance(event, StreamEvent):
        ev = event.event
        entry["event_type"] = ev.get("type", "")
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            entry["delta_type"] = delta.get("type", "")
            if delta.get("type") == "text_delta":
                entry["text_len"] = len(delta.get("text", ""))
        entry["parent_tool_use_id"] = event.parent_tool_use_id
    
    if extra:
        entry.update(extra)
    
    try:
        f = _ensure_file()
        f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        f.flush()
    except Exception:
        pass
