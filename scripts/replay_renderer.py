"""Offline renderer replay harness.

Reads a repro-*.jsonl produced by scripts/repro_truncation.py, rebuilds the
SDK event objects, and feeds them into DiscordRenderer.render_response with
a fake Discord target that just records every send / edit.

We then sum the final content of every "message" the renderer produced and
compare against the known-good total (17048 chars in our captured run). If
the totals don't match, the renderer is dropping text; the recorded log
tells us exactly which event was processed when the loss happened.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/replay_renderer.py logs/repro-XXXXXX.jsonl
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_code_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from clauded import discord_renderer as _renderer_mod
from clauded.discord_renderer import DiscordRenderer

# ---------------------------------------------------------------------------
# Virtual clock — advance time without sleeping. The renderer reads time.time()
# to decide when to enter typewriter mode and when to tick. We override it.
# ---------------------------------------------------------------------------
_VIRTUAL_NOW = [time.time()]


def _virtual_time() -> float:
    return _VIRTUAL_NOW[0]


_real_sleep = asyncio.sleep


async def _instant_sleep(_seconds: float) -> None:
    # Yield control once so other tasks run, but no wallclock delay.
    await _real_sleep(0)


# Patch the renderer's time and asyncio.sleep modules
_renderer_mod.time.time = _virtual_time  # type: ignore[assignment]
_renderer_mod.asyncio.sleep = _instant_sleep  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fake Discord target — records every send / edit
# ---------------------------------------------------------------------------

_msg_id_counter = 0


def _next_id() -> int:
    global _msg_id_counter
    _msg_id_counter += 1
    return _msg_id_counter


class FakeMessage:
    """Minimal stand-in for discord.Message."""

    def __init__(self, content: str = "", embed=None, file=None) -> None:
        self.id = _next_id()
        self.content = content or ""
        self.embed = embed  # we record but don't compare embed text
        self.file = file
        self.history: list[tuple[str, str]] = [("send", self.content)]

    async def edit(self, *, content=None, embed=None, **kw) -> "FakeMessage":
        if content is not None:
            self.content = content
            self.history.append(("edit", content))
        if embed is not None:
            self.embed = embed
            self.history.append(("edit-embed", str(embed.to_dict()) if hasattr(embed, "to_dict") else "<embed>"))
        return self

    async def create_thread(self, *, name: str, **kw):
        # Sub-agent path — not exercised in pure-text repro
        raise NotImplementedError("create_thread not supported in fake target")

    async def add_reaction(self, *args, **kw):
        return None

    def __repr__(self) -> str:
        return f"<FakeMessage id={self.id} len={len(self.content)}>"


class FakeTarget:
    """Stand-in for discord.abc.Messageable."""

    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        # Renderer touches these via getattr — return None / fake to avoid attr errors
        self.guild = None  # disables _process_markers' permission checks gracefully
        self.parent = None
        self.id = 999999
        self.name = "fake-thread"

    async def send(self, content=None, embed=None, file=None, **kw) -> FakeMessage:
        msg = FakeMessage(content=content or "", embed=embed, file=file)
        self.messages.append(msg)
        return msg

    async def create_thread(self, *, name: str, **kw):
        raise NotImplementedError("create_thread not supported")


# ---------------------------------------------------------------------------
# Fake ClaudeBridge — yields reconstructed events
# ---------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, events: list, preserve_timing: bool = True) -> None:
        self._events = events
        self._preserve_timing = preserve_timing

    async def send_message(self, _user_text: str):
        prev_t = None
        for rec in self._events:
            t = rec["t"]
            if prev_t is not None:
                gap = t - prev_t
                # Always advance virtual clock by the recorded gap, so the
                # renderer's FAST_PATH/EDIT_INTERVAL thresholds fire as in
                # the real session. Yield once to let edits run.
                _VIRTUAL_NOW[0] += gap
                await asyncio.sleep(0)
            prev_t = t

            kind = rec["type"]
            if kind == "StreamEvent":
                ev = {
                    "type": rec.get("event_type"),
                }
                if rec.get("event_type") == "content_block_delta" and rec.get("delta_type") == "text_delta":
                    # We don't have the text body in the log — but the repro
                    # jsonl was lossy. Reconstruct from delta_concat.txt below.
                    ev["delta"] = {"type": "text_delta", "text": rec["__text"]}
                elif rec.get("event_type") == "message_delta":
                    ev["delta"] = rec.get("delta", {})
                elif rec.get("event_type") == "content_block_delta":
                    ev["delta"] = {"type": rec.get("delta_type"), **rec.get("delta", {})}
                else:
                    ev = rec.get("raw") or {"type": rec.get("event_type")}
                yield StreamEvent(
                    uuid="repro-uuid",
                    session_id="repro-session",
                    event=ev,
                    parent_tool_use_id=None,
                )
            elif kind == "AssistantMessage":
                blocks = []
                for b in rec.get("blocks", []):
                    if b.get("type") == "text":
                        # Skip — TextBlock content is duplicate of stream deltas
                        # and the repro logger didn't capture the text body.
                        # We give an empty text so the structure matches.
                        blocks.append(TextBlock(text=""))
                if blocks:
                    yield AssistantMessage(content=blocks, model="repro", parent_tool_use_id=None)
            elif kind == "ResultMessage":
                yield ResultMessage(
                    subtype=rec.get("subtype", "success"),
                    duration_ms=rec.get("duration_ms", 0) or 0,
                    duration_api_ms=rec.get("duration_ms", 0) or 0,
                    is_error=rec.get("is_error", False),
                    num_turns=rec.get("num_turns", 0) or 0,
                    session_id=rec.get("session_id", "repro"),
                    total_cost_usd=rec.get("total_cost_usd", 0) or 0,
                    usage=rec.get("usage") or {},
                    result="",
                )


def reattach_text_to_stream_events(events: list, full_text: str) -> int:
    """The repro jsonl recorded only text_len, not the body. Slice the
    captured delta_concat back into each text_delta event in order so the
    renderer sees the actual text."""
    cursor = 0
    n = 0
    for rec in events:
        if (
            rec.get("type") == "StreamEvent"
            and rec.get("event_type") == "content_block_delta"
            and rec.get("delta_type") == "text_delta"
        ):
            ln = rec.get("text_len", 0)
            rec["__text"] = full_text[cursor : cursor + ln]
            cursor += ln
            n += 1
    return n  # also returns count


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: replay_renderer.py logs/repro-XXXXXX.jsonl")
        sys.exit(2)
    log_path = Path(sys.argv[1])
    events: list = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))

    # Find sibling delta_concat.txt and result.txt
    base = log_path.stem.split("-", 1)[1].split(".", 1)[0]
    # repro-1778309631.jsonl  -> base=1778309631
    # We want repro-1778309632.delta_concat.txt — note the timestamp differs by 1!
    # Just glob for any delta_concat.txt in same dir
    candidates = sorted(log_path.parent.glob(f"repro-*.delta_concat.txt"))
    if not candidates:
        print("no delta_concat.txt sibling found")
        sys.exit(2)
    delta_text = candidates[-1].read_text()
    result_text_candidates = sorted(log_path.parent.glob("repro-*.result.txt"))
    result_text = result_text_candidates[-1].read_text()

    expected_total = len(result_text)
    print(f"events:           {len(events)}")
    print(f"delta_concat len: {len(delta_text)}")
    print(f"result len:       {expected_total}")

    n_attached = reattach_text_to_stream_events(events, delta_text)
    print(f"attached text to {n_attached} stream events")

    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events, preserve_timing=False)  # virtual clock advances time

    async def go():
        await renderer.render_response(bridge, "test prompt")

    t0 = time.time()
    asyncio.run(go())
    elapsed = time.time() - t0

    # Sum total final content across all messages, ignoring cost footer & cursor
    CURSOR = "▌"
    total_chars = 0
    breakdown = []
    for m in target.messages:
        c = m.content or ""
        # Strip cost footer if present
        if "\n\n-# 💰" in c:
            c = c.split("\n\n-# 💰", 1)[0]
        c_stripped = c.rstrip(CURSOR)
        total_chars += len(c_stripped)
        breakdown.append((m.id, len(c_stripped), repr(c_stripped[-80:])))

    print(f"\nreplay elapsed: {elapsed:.2f}s")
    print(f"messages produced: {len(target.messages)}")
    print(f"final total chars: {total_chars}")
    print(f"expected (sum):    {expected_total}")
    print(f"diff:              {total_chars - expected_total}")

    print("\n--- per-message breakdown ---")
    for mid, n, tail in breakdown:
        print(f"  msg#{mid}: {n} chars  tail={tail}")

    if total_chars < expected_total:
        gap = expected_total - total_chars
        print(f"\n⚠ RENDERER DROPPED {gap} chars")
    elif total_chars > expected_total:
        print(f"\n⚠ RENDERER PRODUCED {total_chars - expected_total} EXTRA chars (duplication?)")
    else:
        print(f"\n✓ no characters dropped or duplicated")


if __name__ == "__main__":
    main()
