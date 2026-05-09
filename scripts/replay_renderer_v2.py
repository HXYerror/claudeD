"""Replay v2: handles repro2-*.jsonl + bodies.jsonl which preserve full event payloads.

Reads the captured event stream, reconstructs StreamEvent / AssistantMessage
(with TextBlocks and ToolUseBlocks) / ResultMessage, feeds them into the
real DiscordRenderer with a virtual clock so typewriter mode triggers
exactly as in the live session.

Then sums every send/edit final content and compares against the recorded
result text.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_code_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

# Patch renderer's clock BEFORE importing
from clauded import discord_renderer as _renderer_mod

_VIRTUAL_NOW = [time.time()]


def _virtual_time() -> float:
    return _VIRTUAL_NOW[0]


_renderer_mod.time.time = _virtual_time

from clauded.discord_renderer import DiscordRenderer


# ---------------------------------------------------------------------------
# Fake target
# ---------------------------------------------------------------------------

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
        self.history = [("send", self.content, embed)]

    async def edit(self, *, content=None, embed=None, **kw):
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed
        self.history.append(("edit", self.content, self.embed))
        return self

    async def add_reaction(self, *a, **k):
        pass


class FakeTarget:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.guild = None
        self.parent = None
        self.id = 1
        self.name = "fake"

    async def send(self, content=None, embed=None, file=None, **kw) -> FakeMessage:
        m = FakeMessage(content=content or "", embed=embed, file=file)
        self.messages.append(m)
        return m

    async def create_thread(self, *, name: str, **kw):
        # subagent path — return another FakeTarget masquerading
        return FakeTarget()


# ---------------------------------------------------------------------------
# Event reconstruction
# ---------------------------------------------------------------------------

def load_bodies(path: Path) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Map event-n -> list of delta texts and textblock texts."""
    deltas: dict[int, list[str]] = defaultdict(list)
    textblocks: dict[int, list[str]] = defaultdict(list)
    with path.open() as f:
        for line in f:
            d = json.loads(line)
            n = d["n"]
            if d["kind"] == "delta":
                deltas[n].append(d["text"])
            elif d["kind"] == "textblock":
                textblocks[n].append(d["text"])
    return deltas, textblocks


def reconstruct(events_path: Path, bodies_path: Path):
    with events_path.open() as f:
        events = [json.loads(l) for l in f if l.strip()]
    deltas, textblocks = load_bodies(bodies_path)

    # delta order: text_delta events appear in order; map to event n
    # but bodies file may have multiple delta entries per event n only if
    # event is content_block_delta with text_delta — exactly one per event
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
                ev["delta"] = delta
            se = StreamEvent(
                uuid=f"u{n}", session_id="repro2", event=ev, parent_tool_use_id=None
            )
            out.append((t, se))
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
                    blocks.append(
                        ToolUseBlock(
                            id=b.get("id") or f"tool-{n}",
                            name=b.get("name") or "tool",
                            input=b.get("input") or {},
                        )
                    )
            am = AssistantMessage(
                content=blocks, model="repro2", parent_tool_use_id=None
            )
            out.append((t, am))
        elif kind == "ResultMessage":
            rm = ResultMessage(
                subtype=rec.get("subtype", "success"),
                duration_ms=rec.get("duration_ms", 0) or 0,
                duration_api_ms=rec.get("duration_ms", 0) or 0,
                is_error=rec.get("is_error", False),
                num_turns=rec.get("num_turns", 0) or 0,
                session_id=rec.get("session_id", "repro2"),
                total_cost_usd=rec.get("total_cost_usd", 0) or 0,
                usage=rec.get("usage") or {},
                result="",
            )
            out.append((t, rm))
    return out


class FakeBridge:
    def __init__(self, events) -> None:
        self.events = events

    async def send_message(self, _user_text: str):
        prev_t = None
        for t, ev in self.events:
            if prev_t is not None:
                gap = t - prev_t
                _VIRTUAL_NOW[0] += gap
                await asyncio.sleep(0)
            prev_t = t
            yield ev


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: replay_renderer_v2.py logs/repro2-XXXXXX.jsonl")
        sys.exit(2)
    events_path = Path(sys.argv[1])
    bodies_path = events_path.with_suffix("").with_suffix(".bodies.jsonl")
    if not bodies_path.exists():
        # fall back: same stem, .bodies.jsonl
        bodies_path = events_path.parent / (events_path.stem + ".bodies.jsonl")
    if not bodies_path.exists():
        # try the pattern repro2-TS.bodies.jsonl
        cand = list(events_path.parent.glob(events_path.stem + ".bodies.jsonl"))
        if cand:
            bodies_path = cand[0]
        else:
            print(f"no bodies sibling for {events_path}")
            sys.exit(2)
    print(f"events: {events_path}")
    print(f"bodies: {bodies_path}")

    events = reconstruct(events_path, bodies_path)
    print(f"reconstructed {len(events)} events")

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events)

    async def go():
        await renderer.render_response(bridge, "test")

    t0 = time.time()
    asyncio.run(go())
    elapsed = time.time() - t0

    CURSOR = "▌"
    total_text_chars = 0
    msg_breakdown = []
    for m in target.messages:
        c = m.content or ""
        # strip cost footer
        if "\n\n-# 💰" in c:
            c = c.split("\n\n-# 💰", 1)[0]
        c = c.rstrip(CURSOR)
        # only count "text" messages, not embeds-only
        if c:
            total_text_chars += len(c)
            msg_breakdown.append((m.id, len(c), c[-100:]))

    # expected text = textblock_concat (or delta_concat — they're equal in good runs)
    expected_path = events_path.parent / (events_path.stem.replace("repro2-", "repro2-") + ".textblock_concat.txt")
    if not expected_path.exists():
        # fallback to delta
        expected_path = events_path.parent / (events_path.stem + ".delta_concat.txt")
    expected = expected_path.read_text() if expected_path.exists() else ""
    expected_total = len(expected)

    print(f"\nelapsed: {elapsed:.2f}s (virtual)")
    print(f"messages with text content: {len(msg_breakdown)}")
    print(f"total embeds: {sum(1 for m in target.messages if m.embed and not m.content)}")
    print(f"total text chars in output: {total_text_chars}")
    print(f"expected (textblock total):  {expected_total}")
    print(f"diff: {total_text_chars - expected_total}")

    print("\n--- per-text-message breakdown ---")
    for mid, n, tail in msg_breakdown:
        print(f"  msg#{mid}: {n} chars  tail=...{tail!r}")

    if total_text_chars < expected_total:
        print(f"\n⚠ RENDERER LOST {expected_total - total_text_chars} chars")
    elif total_text_chars > expected_total:
        print(f"\n⚠ RENDERER PRODUCED {total_text_chars - expected_total} EXTRA chars")
    else:
        print(f"\n✓ exact match")

    # Save the recorded final content for diff
    out_path = events_path.parent / (events_path.stem + ".rendered.txt")
    with out_path.open("w") as f:
        for mid, n, _ in msg_breakdown:
            msg = next(m for m in target.messages if m.id == mid)
            c = msg.content or ""
            if "\n\n-# 💰" in c:
                c = c.split("\n\n-# 💰", 1)[0]
            f.write(c.rstrip(CURSOR))
            f.write("\n========\n")
    print(f"\nsaved rendered output to {out_path}")


if __name__ == "__main__":
    main()
