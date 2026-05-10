"""Repro #2: long text WITH interleaved tool calls.

Asks Claude to:
  1. write 600+ chars of intro text
  2. run a Bash command
  3. write 600+ chars of analysis based on the bash output
  4. read a file
  5. write 800+ chars of conclusion

This forces the renderer to take the ToolUseBlock branch — which resets
buffer/typewriter/start_time. If subsequent StreamEvent text isn't
correctly resumed into the new typewriter, the tail text is lost.

Captures full event stream + ALL text bodies (delta text, textblock text,
result text) so we can replay against the renderer offline.

Usage:
  CLAUDED_STREAM_DEBUG=1 PYTHONPATH=src .venv/bin/python scripts/repro_with_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

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


PROMPT = (
    "请按下面的步骤完成一个真实的小任务，注意每段说明文字必须用中文写不少于 600 字，"
    "总文字量要超过 4000 字：\n"
    "1) 先用 Bash 工具执行 `ls /Users/xuzhang/dev/AI/claudeD/src/clauded/` 看看项目结构\n"
    "2) 然后写一段不少于 800 字的中文，分析这个目录里每个文件的职责\n"
    "3) 再用 Read 工具读取 /Users/xuzhang/dev/AI/claudeD/pyproject.toml\n"
    "4) 接着写一段不少于 800 字的中文，详细说明这个项目的依赖、构建系统和入口点\n"
    "5) 再用 Bash 工具执行 `wc -l /Users/xuzhang/dev/AI/claudeD/src/clauded/*.py` 数行数\n"
    "6) 最后写一段不少于 1000 字的中文总结，结合前面的发现讨论代码组织、复杂度分布、"
    "潜在的重构方向。\n"
    "每段中文说明都必须完整写出来不要省略。"
)


async def main() -> None:
    cfg = load_config()
    out_dir = ROOT / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    log_path = out_dir / f"repro2-{ts}.jsonl"
    log_f = open(log_path, "w")

    def emit(rec: dict) -> None:
        log_f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        log_f.flush()

    print(f"[repro2] writing to {log_path}")

    bridge = ClaudeBridge(project_path=str(ROOT), config=cfg)
    await bridge.start()
    print(f"[repro2] bridge started, model={bridge.model}")

    delta_pieces: list[str] = []
    textblock_pieces: list[str] = []
    delta_total = 0
    textblock_total = 0
    n_events = 0
    n_tools = 0
    n_results = 0
    last_stop_reason = None
    t0 = time.time()
    last_tick = t0

    # also save full body for replay
    bodies_log = open(out_dir / f"repro2-{ts}.bodies.jsonl", "w")

    try:
        async for event in bridge.send_message(PROMPT):
            n_events += 1
            now = time.time()
            if now - last_tick > 5:
                print(
                    f"[repro2] t+{now-t0:6.1f}s  events={n_events:5d}  "
                    f"deltas={delta_total}  blocks={textblock_total}  "
                    f"tools={n_tools}  results={n_results}"
                )
                last_tick = now

            base = {"t": now - t0, "n": n_events, "type": type(event).__name__}

            if isinstance(event, StreamEvent):
                ev = event.event
                base["event_type"] = ev.get("type")
                base["raw"] = ev  # keep full raw for replay accuracy
                if ev.get("type") == "message_delta":
                    delta = ev.get("delta", {})
                    if "stop_reason" in delta:
                        last_stop_reason = delta["stop_reason"]
                elif ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        delta_total += len(text)
                        delta_pieces.append(text)
                        bodies_log.write(json.dumps({
                            "n": n_events, "kind": "delta", "text": text
                        }, ensure_ascii=False) + "\n")
                emit(base)

            elif isinstance(event, AssistantMessage):
                blocks_summary = []
                for b in event.content:
                    if isinstance(b, TextBlock):
                        blocks_summary.append({"type": "text", "len": len(b.text)})
                        textblock_total += len(b.text)
                        textblock_pieces.append(b.text)
                        bodies_log.write(json.dumps({
                            "n": n_events, "kind": "textblock", "text": b.text
                        }, ensure_ascii=False) + "\n")
                    elif isinstance(b, ToolUseBlock):
                        n_tools += 1
                        blocks_summary.append({
                            "type": "tool_use",
                            "name": b.name,
                            "id": b.id,
                            "input": b.input,
                        })
                    elif isinstance(b, ToolResultBlock):
                        blocks_summary.append({"type": "tool_result"})
                    elif isinstance(b, ThinkingBlock):
                        blocks_summary.append({"type": "thinking", "len": len(b.thinking)})
                base["blocks"] = blocks_summary
                emit(base)

            elif isinstance(event, ResultMessage):
                n_results += 1
                base["subtype"] = event.subtype
                base["is_error"] = event.is_error
                base["num_turns"] = event.num_turns
                base["total_cost_usd"] = event.total_cost_usd
                base["duration_ms"] = event.duration_ms
                base["session_id"] = event.session_id
                base["usage"] = event.usage
                base["result_len"] = len(event.result or "")
                bodies_log.write(json.dumps({
                    "n": n_events, "kind": "result", "text": event.result or ""
                }, ensure_ascii=False) + "\n")
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
        bodies_log.close()

    elapsed = time.time() - t0
    delta_concat = "".join(delta_pieces)
    textblock_concat = "".join(textblock_pieces)

    # Save the bodies as plain text
    (out_dir / f"repro2-{ts}.delta_concat.txt").write_text(delta_concat)
    (out_dir / f"repro2-{ts}.textblock_concat.txt").write_text(textblock_concat)

    print(f"\n=========== SUMMARY ===========")
    print(f"  elapsed:        {elapsed:.1f}s")
    print(f"  events:         {n_events}")
    print(f"  tool_uses:      {n_tools}")
    print(f"  delta_total:    {delta_total}")
    print(f"  textblock_total:{textblock_total}")
    print(f"  stop_reason:    {last_stop_reason}")


if __name__ == "__main__":
    asyncio.run(main())
