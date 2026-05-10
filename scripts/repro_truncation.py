"""Headless reproduction harness for the truncation bug.

Drives ClaudeBridge directly with a long-output prompt and writes EVERYTHING
to logs/repro-<ts>.jsonl: every SDK event with the FULL payload (text deltas,
result.result body, raw event dicts).

Compares:
  - sum(text_delta lengths)               — what the SDK streamed
  - len(ResultMessage.result)             — what the SDK reports as final text
  - sum(AssistantMessage.TextBlock lens)  — what the AssistantMessage objects carry
  - what would be in the renderer's `buffer` if it followed the same code path

Usage:
  CLAUDED_STREAM_DEBUG=1 PYTHONPATH=src .venv/bin/python scripts/repro_truncation.py [project_path]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Make the repo importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
)
from claude_agent_sdk.types import StreamEvent

from clauded.config import load_config
from clauded.claude_bridge import ClaudeBridge


# A prompt designed to produce a long single-shot text response (no tools,
# pushes Claude to dump prose). The user's truncation case was 6.7k output /
# 471s / $0.25 — let's aim in that ballpark.
PROMPT = (
    "请用纯文字（不要调用任何工具，不要写代码块，全部用中文段落）"
    "为我写一篇详尽的技术分析报告，主题是『将 Claude Code 通过 Discord Bot 暴露给用户'"
    "时，从架构、流式渲染、消息分片、工具调用展示、子 agent thread、权限模型、"
    "限速与 backoff、错误恢复、可观测性、安全这十个维度，每个维度都要写至少 800 个汉字，"
    "总字数务必超过 8000 字。请一次性把全部内容输出完，不要分段问我。"
)


async def main() -> None:
    project = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/dev/AI/claudeD")
    cfg = load_config()

    out_dir = ROOT / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"repro-{int(time.time())}.jsonl"
    log_f = open(log_path, "w")

    def emit(rec: dict) -> None:
        log_f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        log_f.flush()

    print(f"[repro] writing to {log_path}")
    print(f"[repro] project = {project}")

    bridge = ClaudeBridge(project_path=project, config=cfg)
    await bridge.start()
    print(f"[repro] bridge started, model={bridge.model}")

    # The main aggregates we care about
    delta_total = 0          # sum of text_delta lengths from StreamEvent
    textblock_total = 0      # sum of TextBlock.text lengths in AssistantMessage
    delta_pieces: list[str] = []
    textblock_pieces: list[str] = []
    result_text = ""
    last_stop_reason = None
    n_events = 0
    t0 = time.time()
    last_tick = t0

    try:
        async for event in bridge.send_message(PROMPT):
            n_events += 1
            now = time.time()

            # progress heartbeat to console every ~5 seconds
            if now - last_tick > 5:
                print(
                    f"[repro] t+{now-t0:6.1f}s  events={n_events:5d}  "
                    f"delta_chars={delta_total}  textblock_chars={textblock_total}"
                )
                last_tick = now

            base = {"t": now - t0, "n": n_events, "type": type(event).__name__}

            if isinstance(event, StreamEvent):
                ev = event.event
                base["event_type"] = ev.get("type")
                if ev.get("type") == "message_delta":
                    delta = ev.get("delta", {})
                    if "stop_reason" in delta:
                        last_stop_reason = delta["stop_reason"]
                    base["delta"] = delta
                elif ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    base["delta_type"] = delta.get("type")
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        delta_total += len(text)
                        delta_pieces.append(text)
                        base["text_len"] = len(text)
                        # we omit the text body here to keep the log line small
                else:
                    base["raw"] = ev
                emit(base)

            elif isinstance(event, AssistantMessage):
                blocks_summary = []
                for b in event.content:
                    if isinstance(b, TextBlock):
                        blocks_summary.append({"type": "text", "len": len(b.text)})
                        textblock_total += len(b.text)
                        textblock_pieces.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        blocks_summary.append({"type": "tool_use", "name": b.name})
                    elif isinstance(b, ToolResultBlock):
                        blocks_summary.append({"type": "tool_result"})
                    elif isinstance(b, ThinkingBlock):
                        blocks_summary.append({"type": "thinking", "len": len(b.thinking)})
                base["blocks"] = blocks_summary
                emit(base)

            elif isinstance(event, ResultMessage):
                base["subtype"] = event.subtype
                base["is_error"] = event.is_error
                base["num_turns"] = event.num_turns
                base["total_cost_usd"] = event.total_cost_usd
                base["duration_ms"] = event.duration_ms
                base["session_id"] = event.session_id
                base["usage"] = event.usage
                base["result_len"] = len(event.result or "")
                result_text = event.result or ""
                emit(base)
                break
            else:
                base["raw_repr"] = repr(event)[:500]
                emit(base)
    finally:
        try:
            await bridge.stop()
        except Exception:
            pass
        log_f.close()

    elapsed = time.time() - t0

    delta_concat = "".join(delta_pieces)
    textblock_concat = "".join(textblock_pieces)

    summary = {
        "elapsed_sec": elapsed,
        "events": n_events,
        "delta_total": delta_total,
        "textblock_total": textblock_total,
        "result_len": len(result_text),
        "stop_reason": last_stop_reason,
    }
    print("\n=========== SUMMARY ===========")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # Tail compare — does the streamed delta text END the same way the
    # ResultMessage.result text ends?
    def tail(s: str, n: int = 200) -> str:
        return s[-n:].replace("\n", "\\n")

    print("\n--- delta tail (last 200 chars) ---")
    print(tail(delta_concat))
    print("\n--- textblock tail (last 200 chars) ---")
    print(tail(textblock_concat))
    print("\n--- result tail (last 200 chars) ---")
    print(tail(result_text))

    # Save the three full bodies as separate files so we can diff
    for name, body in [
        ("delta_concat", delta_concat),
        ("textblock_concat", textblock_concat),
        ("result", result_text),
    ]:
        p = out_dir / f"repro-{int(t0)}.{name}.txt"
        p.write_text(body)
        print(f"  saved {p} ({len(body)} chars)")

    # Quick verdict
    print("\n--- VERDICT ---")
    if abs(len(textblock_concat) - len(result_text)) > 100:
        print("⚠ textblock_total != result_len — SDK is dropping text between blocks and result")
    if abs(delta_total - len(result_text)) > 100:
        print(f"⚠ delta_total ({delta_total}) != result_len ({len(result_text)}) — stream/result mismatch")
    else:
        print(f"✓ delta_total ≈ result_len (diff = {len(result_text) - delta_total})")


if __name__ == "__main__":
    asyncio.run(main())
