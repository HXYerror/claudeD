"""Replay v3: same as v2, but FakeTarget can simulate intermittent send failures.

Demonstrates the truncation bug: when _typewriter_tick splits a buffer into
multiple chunks and any chunk's _safe_send fails, that chunk is silently
dropped — content never makes it to Discord.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/replay_with_failures.py logs/repro2-XXXXXX.jsonl

Run with FAIL_RATE env var to control failure injection (default 0.10 = 10%).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import discord
from claude_code_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from clauded import discord_renderer as _renderer_mod

_VIRTUAL_NOW = [time.time()]


def _virtual_time() -> float:
    return _VIRTUAL_NOW[0]


_renderer_mod.time.time = _virtual_time

from clauded.discord_renderer import DiscordRenderer

FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.10"))
SEED = int(os.environ.get("SEED", "42"))
random.seed(SEED)

_msg_id = 0


def _next_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


class FakeMessage:
    def __init__(self, content: str = "", embed=None, file=None) -> None:
        self.id = _next_id()
        self.content = content or ""
        self.embed = embed
        self.file = file

    async def edit(self, *, content=None, embed=None, **kw):
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed
        return self

    async def add_reaction(self, *a, **k):
        pass


class FlakyTarget:
    """Like FakeTarget but raises HTTPException on a fraction of sends."""

    def __init__(self, fail_rate: float) -> None:
        self.messages: list[FakeMessage] = []
        self.fail_rate = fail_rate
        self.guild = None
        self.parent = None
        self.id = 1
        self.name = "flaky"
        self.failed_send_chars = 0
        self.fail_count = 0
        self.success_count = 0

    async def send(self, content=None, embed=None, file=None, **kw) -> FakeMessage:
        # Fail a fraction of text sends. Always succeed for embeds-only or
        # file uploads (for cleaner stats).
        if content and random.random() < self.fail_rate:
            self.fail_count += 1
            self.failed_send_chars += len(content)
            raise discord.HTTPException(
                _FakeResp(500), "simulated transient failure"
            )
        self.success_count += 1
        m = FakeMessage(content=content or "", embed=embed, file=file)
        self.messages.append(m)
        return m

    async def create_thread(self, *, name: str, **kw):
        return FlakyTarget(self.fail_rate)


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Bad Gateway"


# ---------------------------------------------------------------------------
# Reuse v2's reconstruction
# ---------------------------------------------------------------------------

def load_bodies(path: Path):
    deltas = defaultdict(list)
    textblocks = defaultdict(list)
    with path.open() as f:
        for line in f:
            d = json.loads(line)
            n = d["n"]
            if d["kind"] == "delta":
                deltas[n].append(d["text"])
            elif d["kind"] == "textblock":
                textblocks[n].append(d["text"])
    return deltas, textblocks


def reconstruct(events_path: Path, bodies_path: Path | None):
    with events_path.open() as f:
        events = [json.loads(l) for l in f if l.strip()]
    deltas = defaultdict(list)
    textblocks = defaultdict(list)
    if bodies_path and bodies_path.exists():
        deltas, textblocks = load_bodies(bodies_path)
    else:
        # repro1 didn't capture per-event bodies; rebuild deltas from delta_concat.txt
        # by slicing using the recorded text_len in order
        delta_text_path = events_path.parent / (events_path.stem + ".delta_concat.txt")
        # try other patterns
        if not delta_text_path.exists():
            cand = sorted(events_path.parent.glob("repro-*.delta_concat.txt"))
            delta_text_path = cand[-1] if cand else None
        full_delta = delta_text_path.read_text() if delta_text_path else ""
        cursor = 0
        for rec in events:
            if (
                rec.get("type") == "StreamEvent"
                and rec.get("event_type") == "content_block_delta"
                and rec.get("delta_type") == "text_delta"
            ):
                ln = rec.get("text_len", 0)
                deltas[rec["n"]].append(full_delta[cursor : cursor + ln])
                cursor += ln
    out = []
    for rec in events:
        kind = rec["type"]
        n = rec["n"]
        t = rec["t"]
        if kind == "StreamEvent":
            ev = dict(rec.get("raw") or {})
            if ev.get("type") == "content_block_delta":
                delta = dict(ev.get("delta") or {})
                if delta.get("type") == "text_delta" and n in deltas:
                    delta["text"] = "".join(deltas[n])
                # repro1 events have only 'event_type' + 'delta_type', no full 'raw'
                # rebuild minimal raw
                if not ev:
                    ev = {"type": "content_block_delta", "delta": delta}
                else:
                    ev["delta"] = delta
            out.append((t, StreamEvent(uuid=f"u{n}", session_id="r", event=ev, parent_tool_use_id=None)))
        elif kind == "AssistantMessage":
            blocks = []
            tb_texts = textblocks.get(n, [])
            tb_idx = 0
            for b in rec.get("blocks", []):
                if b.get("type") == "text":
                    text = tb_texts[tb_idx] if tb_idx < len(tb_texts) else ""
                    tb_idx += 1
                    blocks.append(TextBlock(text=text))
                elif b.get("type") == "tool_use":
                    blocks.append(ToolUseBlock(id=b.get("id") or f"t{n}", name=b.get("name") or "tool", input=b.get("input") or {}))
            out.append((t, AssistantMessage(content=blocks, model="r", parent_tool_use_id=None)))
        elif kind == "ResultMessage":
            out.append((t, ResultMessage(
                subtype=rec.get("subtype", "success"),
                duration_ms=rec.get("duration_ms", 0) or 0,
                duration_api_ms=rec.get("duration_ms", 0) or 0,
                is_error=rec.get("is_error", False),
                num_turns=rec.get("num_turns", 0) or 0,
                session_id="r", total_cost_usd=0, usage={}, result="",
            )))
    return out


class FakeBridge:
    def __init__(self, events) -> None:
        self.events = events

    async def send_message(self, _user_text: str):
        prev_t = None
        for t, ev in self.events:
            if prev_t is not None:
                _VIRTUAL_NOW[0] += t - prev_t
                await asyncio.sleep(0)
            prev_t = t
            yield ev


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: replay_with_failures.py logs/repro2-XXXXXX.jsonl")
        sys.exit(2)
    events_path = Path(sys.argv[1])
    bodies_path_arg = events_path.parent / (events_path.stem + ".bodies.jsonl")
    if not bodies_path_arg.exists():
        bodies_path_arg = None  # falls back to delta_concat.txt slicing

    events = reconstruct(events_path, bodies_path_arg)
    print(f"reconstructed {len(events)} events  fail_rate={FAIL_RATE}  seed={SEED}")

    target = FlakyTarget(fail_rate=FAIL_RATE)
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)

    async def go():
        await renderer.render_response(bridge, "test")

    asyncio.run(go())

    CURSOR = "▌"
    rendered_chars = 0
    for m in target.messages:
        c = m.content or ""
        if "\n\n-# 💰" in c:
            c = c.split("\n\n-# 💰", 1)[0]
        c = c.rstrip(CURSOR)
        rendered_chars += len(c) if c else 0

    expected_path = events_path.parent / (events_path.stem + ".textblock_concat.txt")
    if not expected_path.exists():
        # repro1 layout: timestamp differs by 1
        cand = sorted(events_path.parent.glob("repro-*.textblock_concat.txt"))
        if not cand:
            cand = sorted(events_path.parent.glob("repro-*.delta_concat.txt"))
        expected_path = cand[-1] if cand else None
    expected_total = len(expected_path.read_text()) if (expected_path and expected_path.exists()) else 0

    print(f"\nflaky stats: success={target.success_count}  fail={target.fail_count}  failed_send_chars={target.failed_send_chars}")
    print(f"final rendered text chars:  {rendered_chars}")
    print(f"expected:                   {expected_total}")
    print(f"diff: {rendered_chars - expected_total}  (lost: {expected_total - rendered_chars})")
    if rendered_chars < expected_total:
        print(f"\n⚠ TRUNCATION: {expected_total - rendered_chars} chars lost due to send failures")
    else:
        print("\n✓ no truncation")


if __name__ == "__main__":
    main()
