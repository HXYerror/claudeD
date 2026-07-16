"""review A3/A4/A5/A6 — subagent-completion notification chain (bot.py).

Pins the fixed behavior of the per-subagent pending tracking + the
transcript-result surfacing, at the callback level (the same layer the
SubagentStart / SubagentStop hooks in claude_bridge.py invoke).

Bugs these tests lock down:
  A4/A5: pending tracking used to key a session→thread map with ONE entry
         per session, so `_warn_pending_subagents` false-warned "1 subagent
         still running" on EVERY normal turn, and the map got del'd after
         the FIRST subagent stopped (destroying the count + routing for
         later parallel subagents). Now keyed per agent_id in
         `_pending_subagents[thread_id]`.
  A3/A6: `_on_subagent_stop` used to read stop_reason / summary /
         duration_ms — none of which SubagentStopHookInput provides — so
         the embed was always the contentless "Subagent finished." Now it
         reads agent_id / agent_type / agent_transcript_path and surfaces
         the subagent's real final assistant text.

We bind the real (unbound) ClaudedBot methods onto a lightweight fake so
we exercise the production code paths without the full constructor cost
(mirrors tests/test_bot_fire_callbacks.py's approach).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from clauded.bot import ClaudedBot


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeMsg:
    content: str = ""
    embeds: list = field(default_factory=list)


class _FakeChannel:
    """Minimal discord channel/thread that records every send()."""

    def __init__(self, cid: int) -> None:
        self.id = cid
        self.sent: list[_FakeMsg] = []

    async def send(self, content=None, **kwargs):
        msg = _FakeMsg(content=content or "")
        if "embed" in kwargs and kwargs["embed"] is not None:
            msg.embeds = [kwargs["embed"]]
        self.sent.append(msg)
        return msg


class _FakeBot:
    """Binds the real subagent methods from ClaudedBot onto a bare object.

    Provides just the collaborators those methods touch: ``_pending_subagents``
    and ``get_channel`` (so ``safe_send_message`` routes to a recorder). The
    methods under test are otherwise self-contained.
    """

    def __init__(self) -> None:
        self._pending_subagents: dict[int, dict[str, str]] = {}
        self._channels: dict[int, _FakeChannel] = {}

    def get_channel(self, cid: int):
        return self._channels.get(cid)

    async def fetch_channel(self, cid: int):  # pragma: no cover - not hit in tests
        return self._channels.get(cid)

    def channel_for(self, cid: int) -> _FakeChannel:
        ch = self._channels.get(cid)
        if ch is None:
            ch = _FakeChannel(cid)
            self._channels[cid] = ch
        return ch

    # Bind the real implementations.
    _make_subagent_start_cb = ClaudedBot._make_subagent_start_cb
    _make_subagent_stop_cb = ClaudedBot._make_subagent_stop_cb
    _warn_pending_subagents = ClaudedBot._warn_pending_subagents
    # Already a plain function on the class (it's a @staticmethod), so
    # referencing it here rebinds it as a staticmethod on _FakeBot too.
    _read_subagent_result = staticmethod(ClaudedBot._read_subagent_result)


def _all_embeds(ch: _FakeChannel) -> list:
    return [e for m in ch.sent for e in m.embeds]


# ---------------------------------------------------------------------------
# A4/A5 — pending tracking + no false warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_turn_no_subagents_warns_nothing():
    """review A4/A5: a normal turn (no SubagentStart fired) → pending count
    is 0 → `_warn_pending_subagents` sends NOTHING. This was the guaranteed
    false positive in the old code (stale session→thread entry counted as 1)."""
    bot = _FakeBot()
    thread_id = 111
    ch = bot.channel_for(thread_id)

    await bot._warn_pending_subagents(thread_id)

    assert _all_embeds(ch) == [], "expected no warning embed on a no-subagent turn"


@pytest.mark.asyncio
async def test_two_parallel_starts_then_stops_decrement_count():
    """review A4/A5: two SubagentStart (parallel) → pending == 2. One
    SubagentStop → 1. Second SubagentStop → 0. The first stop must NOT wipe
    the whole map (the old `del _subagent_threads[session_id]` bug)."""
    bot = _FakeBot()
    thread_id = 222
    bot.channel_for(thread_id)

    start_cb = bot._make_subagent_start_cb(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)

    await start_cb({"agent_id": "a1", "agent_type": "general-purpose"})
    await start_cb({"agent_id": "a2", "agent_type": "Explore"})
    assert len(bot._pending_subagents[thread_id]) == 2

    # First stop removes only a1.
    await stop_cb({"agent_id": "a1", "agent_type": "general-purpose"})
    assert len(bot._pending_subagents.get(thread_id, {})) == 1
    assert "a2" in bot._pending_subagents[thread_id]

    # Second stop removes a2 → bucket empties (and is pruned).
    await stop_cb({"agent_id": "a2", "agent_type": "Explore"})
    assert bot._pending_subagents.get(thread_id, {}) == {}


@pytest.mark.asyncio
async def test_warn_after_one_of_two_stops_reports_one():
    """review A4/A5: with one of two subagents still pending, the warning
    fires and reports exactly 1 (real count, not the old constant 1/0)."""
    bot = _FakeBot()
    thread_id = 333
    ch = bot.channel_for(thread_id)

    start_cb = bot._make_subagent_start_cb(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)
    await start_cb({"agent_id": "a1", "agent_type": "t"})
    await start_cb({"agent_id": "a2", "agent_type": "t"})
    ch.sent.clear()  # drop the stop embed noise below

    await stop_cb({"agent_id": "a1", "agent_type": "t"})
    ch.sent.clear()  # only look at the warning, not the completion embed

    await bot._warn_pending_subagents(thread_id)
    embeds = _all_embeds(ch)
    assert len(embeds) == 1
    assert "1 subagent" in (embeds[0].description or "")


@pytest.mark.asyncio
async def test_start_without_agent_id_is_ignored():
    """Defensive: a SubagentStart missing agent_id must not create a bogus
    None-keyed pending entry (which would false-warn forever)."""
    bot = _FakeBot()
    thread_id = 444
    start_cb = bot._make_subagent_start_cb(thread_id)
    await start_cb({"agent_type": "t"})  # no agent_id
    assert bot._pending_subagents.get(thread_id, {}) == {}


@pytest.mark.asyncio
async def test_stop_without_prior_start_does_not_crash():
    """Graceful degradation: if SubagentStart never fired (CLI variance),
    SubagentStop still routes a notification and raises nothing."""
    bot = _FakeBot()
    thread_id = 555
    ch = bot.channel_for(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)

    # No KeyError even though _pending_subagents has no bucket for this thread.
    await stop_cb({"agent_id": "z9", "agent_type": "loner"})
    embeds = _all_embeds(ch)
    assert len(embeds) == 1
    assert "loner" in (embeds[0].title or "")


# ---------------------------------------------------------------------------
# A3/A6 — surface the real transcript result (no stop_reason/summary reads)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_surfaces_transcript_result(tmp_path):
    """review A6: a real agent_transcript_path JSONL with a final assistant
    message → the embed description contains that result text (NOT the old
    contentless 'Subagent finished.')."""
    transcript = tmp_path / "agent.jsonl"
    lines = [
        {"type": "user", "message": {"content": "do the thing"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "Intermediate step done."},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "FINAL RESULT: 42 files patched."},
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")

    bot = _FakeBot()
    thread_id = 666
    ch = bot.channel_for(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)

    await stop_cb(
        {
            "agent_id": "a1",
            "agent_type": "general-purpose",
            "agent_transcript_path": str(transcript),
        }
    )

    embeds = _all_embeds(ch)
    assert len(embeds) == 1
    embed = embeds[0]
    # A6: the LAST assistant text is surfaced, not an earlier one.
    assert "FINAL RESULT: 42 files patched." in (embed.description or "")
    assert "Intermediate step done." not in (embed.description or "")
    # A3: title carries the agent_type.
    assert "general-purpose" in (embed.title or "")


@pytest.mark.asyncio
async def test_stop_missing_transcript_falls_back_gracefully(tmp_path):
    """review A6: a missing/unreadable agent_transcript_path must NOT crash
    and must fall back to a concise completed message naming the agent_type."""
    bot = _FakeBot()
    thread_id = 777
    ch = bot.channel_for(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)

    missing = tmp_path / "does_not_exist.jsonl"
    await stop_cb(
        {
            "agent_id": "a1",
            "agent_type": "code-reviewer",
            "agent_transcript_path": str(missing),
        }
    )

    embeds = _all_embeds(ch)
    assert len(embeds) == 1
    desc = embeds[0].description or ""
    assert "code-reviewer" in desc
    assert "completed" in desc.lower()


@pytest.mark.asyncio
async def test_stop_with_no_transcript_path_key():
    """review A6: agent_transcript_path entirely absent → fallback, no crash."""
    bot = _FakeBot()
    thread_id = 888
    ch = bot.channel_for(thread_id)
    stop_cb = bot._make_subagent_stop_cb(thread_id)

    await stop_cb({"agent_id": "a1", "agent_type": "Plan"})
    embeds = _all_embeds(ch)
    assert len(embeds) == 1
    assert "Plan" in (embeds[0].title or "")


def test_read_subagent_result_truncates_to_800(tmp_path):
    """review A6: an over-long final result is truncated to ~800 chars so a
    verbose subagent can't blow the Discord embed limit."""
    transcript = tmp_path / "big.jsonl"
    big = "X" * 5000
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": big}]}}
        ),
        encoding="utf-8",
    )
    out = ClaudedBot._read_subagent_result(str(transcript))
    assert len(out) == 800
    assert set(out) == {"X"}


def test_read_subagent_result_none_and_missing():
    """review A6: None path and a nonexistent file both return '' (no raise)."""
    assert ClaudedBot._read_subagent_result(None) == ""
    assert ClaudedBot._read_subagent_result("/no/such/file/here.jsonl") == ""


def test_read_subagent_result_accepts_string_content(tmp_path):
    """review A6: some transcript rows carry content as a plain string, not a
    list of blocks — handle both shapes."""
    transcript = tmp_path / "strcontent.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": "plain answer"}}),
        encoding="utf-8",
    )
    assert ClaudedBot._read_subagent_result(str(transcript)) == "plain answer"


def test_read_subagent_result_malformed_lines_dont_crash(tmp_path):
    """review A6: partial/garbage JSONL lines (e.g. a truncated tail slice)
    are skipped; a valid final assistant line still wins."""
    transcript = tmp_path / "mixed.jsonl"
    content = "\n".join(
        [
            "{not valid json at all",
            "",
            json.dumps({"type": "system", "foo": "bar"}),
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
            ),
            "}{ trailing garbage",
        ]
    )
    transcript.write_text(content, encoding="utf-8")
    assert ClaudedBot._read_subagent_result(str(transcript)) == "ok"
