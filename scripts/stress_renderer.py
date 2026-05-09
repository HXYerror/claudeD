"""Stress-test the truncation fix in pure unit form.

Drives _typewriter_tick + _finalize_typewriter directly with a long buffer
that MUST split, while injecting Discord HTTP failures, and checks that
no characters are lost.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import discord

from clauded.discord_renderer import DiscordRenderer

FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.30"))
SEED = int(os.environ.get("SEED", "0"))
random.seed(SEED)


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "test"


_id = 0


def _next_id():
    global _id
    _id += 1
    return _id


class FakeMessage:
    def __init__(self, content=""):
        self.id = _next_id()
        self.content = content or ""

    async def edit(self, *, content=None, **kw):
        if content is not None:
            self.content = content
        return self


class FlakyMessage(FakeMessage):
    """Like FakeMessage but ``edit`` rolls a probability and raises 503."""

    def __init__(self, content="", fr: float = 0.0):
        super().__init__(content)
        self._fr = fr

    async def edit(self, *, content=None, **kw):
        if random.random() < self._fr:
            raise discord.HTTPException(_Resp(503), "flaky edit")
        return await super().edit(content=content, **kw)


class Flaky:
    def __init__(self, fr):
        self.fr = fr
        self.messages = []
        self.guild = None
        self.parent = None
        self.id = 1
        self.name = "flaky"
        self.send_fails = 0

    async def send(self, content=None, **kw):
        if random.random() < self.fr:
            self.send_fails += 1
            raise discord.HTTPException(_Resp(503), "flaky send")
        m = FlakyMessage(content, fr=self.fr)
        self.messages.append(m)
        return m


async def main():
    target = Flaky(FAIL_RATE)
    r = DiscordRenderer(target)

    # Build a 6000-char buffer that will require multiple splits at 1900-char chunks.
    chunk = "这是一段中文文字用于测试截断bug是否已经修复。" * 30  # ~640 chars
    buffer = (chunk + "\n\n") * 9  # ~5800 chars
    print(f"buffer len: {len(buffer)}")

    live = None
    # Simulate growing buffer over many ticks (typewriter mode)
    accumulated = ""
    for i in range(50):
        # Add ~120 chars per tick, like Claude streaming
        accumulated += chunk[:120]
        live, accumulated = await r._typewriter_tick(live, accumulated)
        if len(accumulated) > 5000:
            break

    # Finalize
    if live is not None:
        await r._finalize_typewriter(live, accumulated)

    # Sum up
    total = 0
    for m in target.messages:
        c = m.content or ""
        total += len(c.rstrip("▌"))

    print(f"send_fails={target.send_fails}")
    print(f"messages produced: {len(target.messages)}")
    print(f"total chars in output: {total}")
    last = target.messages[-1].content if target.messages else ""
    last_tail = accumulated[-200:]
    print(f"last_msg_contains_tail = {last_tail in last}")

    # Assemble all messages in order and check accumulated text appears
    joined = "".join(m.content.rstrip("▌") for m in target.messages)
    missing = 0
    for piece_start in range(0, len(accumulated), 200):
        piece = accumulated[piece_start:piece_start + 200]
        if piece not in joined:
            missing += len(piece)
    print(f"missing chars vs joined output: {missing}")

    if missing == 0:
        print("✓ no characters lost")
    else:
        print(f"⚠ LOST {missing} chars")


if __name__ == "__main__":
    asyncio.run(main())
