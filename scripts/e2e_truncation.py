"""End-to-end truncation verification.

Drives a real Discord channel + real Claude SDK + the post-fix renderer to
verify that long Claude responses are delivered to Discord without truncation.

Modes:
  --mode A : happy path. No injection. Validates that on a clean network the
             channel content matches what Claude streamed.
  --mode B : failure injection. Wraps channel.send / message.edit to raise
             discord.HTTPException(503) at a configurable rate. Validates
             that the retry/fallback logic recovers content.

Both modes:
  - Use a TeeBridge to capture SDK ground truth (text_delta sum and
    ResultMessage.result).
  - Read channel.history between two marker messages to capture the
    rendered ground truth.
  - Compare to detect truncation.

Output:
  logs/e2e_<MODE>_<ts>.log  — full run log
  logs/e2e_<MODE>_<ts>.json — structured summary

Usage:
  PYTHONPATH=src .venv/bin/python scripts/e2e_truncation.py --mode A
  PYTHONPATH=src .venv/bin/python scripts/e2e_truncation.py --mode B --inject-rate 0.3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

load_dotenv()

# ---------------------------------------------------------------------------
# CLI / logging setup (must come before importing clauded modules so logging
# is fully configured by the time the renderer's logger is touched).
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["A", "B"], required=True)
parser.add_argument(
    "--inject-rate", type=float, default=0.3,
    help="B-mode: per-send/edit 503 injection probability",
)
parser.add_argument(
    "--prompt-min-chars", type=int, default=1500,
    help="Min Claude output chars for the test to be considered valid",
)
parser.add_argument(
    "--happy-render-diff", type=int, default=100,
    help="Mode-A: max acceptable |result - channel| chars",
)
parser.add_argument(
    "--inject-render-diff", type=int, default=200,
    help="Mode-B: max acceptable |result - channel| chars",
)
args = parser.parse_args()

TS = int(time.time())
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"e2e_{args.mode}_{TS}.log"
JSON_PATH = LOG_DIR / f"e2e_{args.mode}_{TS}.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
    force=True,
)
# discord.py is chatty about gateway events; we don't care for this test.
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("e2e")

# Capture renderer log records (warning+) so we can assert on retry/giveup
# behavior without scraping the log file.
_renderer_records: list[logging.LogRecord] = []


class _RecordCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        _renderer_records.append(record)


_capture = _RecordCapture(level=logging.WARNING)
logging.getLogger("clauded.discord_renderer").addHandler(_capture)

# Stream debug log for SDK forensics (per repo convention)
os.environ.setdefault("CLAUDED_STREAM_DEBUG", "1")

from claude_code_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_code_sdk.types import StreamEvent  # noqa: E402

from clauded.config import load_config  # noqa: E402
from clauded.claude_bridge import ClaudeBridge  # noqa: E402
from clauded.discord_renderer import DiscordRenderer, CURSOR  # noqa: E402

# Test channel (matches scripts/selftest.py)
GUILD_ID = 1499415073838600454
CHANNEL_ID = 1499415074614280234

# Same prompt as scripts/repro_truncation.py — designed to push Claude past
# the 6k-8k char band where the user originally observed truncation.
PROMPT = (
    "请用纯文字（不要调用任何工具，不要写代码块，全部用中文段落）"
    "为我写一篇详尽的技术分析报告，主题是『将 Claude Code 通过 Discord Bot 暴露给用户』"
    "时，从架构、流式渲染、消息分片、工具调用展示、子 agent thread、权限模型、"
    "限速与 backoff、错误恢复、可观测性、安全这十个维度，每个维度都要写至少 800 个汉字，"
    "总字数务必超过 8000 字。请一次性把全部内容输出完，不要分段问我。"
)


# ---------------------------------------------------------------------------
# Tee bridge: captures SDK ground truth without changing what the renderer sees
# ---------------------------------------------------------------------------


class TeeBridge:
    """Wraps ClaudeBridge, teeing events to record SDK-side ground truth.

    The renderer accesses `bridge.send_message(...)`. We yield exactly the
    same events the real bridge would, while accumulating:
      - `delta_chars`  : sum of len(text_delta) across all StreamEvents
      - `result_text`  : ResultMessage.result (final assembled text)

    These two should be equal (modulo a handful of chars) — when they aren't,
    the SDK itself is dropping content, not the renderer.
    """

    def __init__(self, real_bridge: ClaudeBridge) -> None:
        self._real = real_bridge
        self.delta_chars: int = 0
        self.delta_concat: list[str] = []
        self.result_text: str = ""
        self.events_seen: int = 0

    async def start(self) -> None:
        await self._real.start()

    async def stop(self) -> None:
        await self._real.stop()

    @property
    def total_cost(self) -> float:
        return self._real.total_cost

    @property
    def num_turns(self) -> int:
        return self._real.num_turns

    @property
    def session_id(self):  # type: ignore[no-untyped-def]
        return self._real.session_id

    @property
    def model(self) -> str:
        return self._real.model

    @property
    def is_active(self) -> bool:
        return self._real.is_active

    async def send_message(self, text: str):  # type: ignore[no-untyped-def]
        async for ev in self._real.send_message(text):
            self.events_seen += 1
            if isinstance(ev, StreamEvent):
                e = ev.event
                if e.get("type") == "content_block_delta":
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta":
                        t = d.get("text", "")
                        self.delta_chars += len(t)
                        self.delta_concat.append(t)
            elif isinstance(ev, ResultMessage):
                self.result_text = ev.result or ""
            yield ev


# ---------------------------------------------------------------------------
# Failure injection (mode B only)
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal aiohttp-response stand-in for discord.HTTPException()."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "injected"


def _install_renderer_injection(
    renderer: DiscordRenderer, inject_rate: float,
) -> tuple[dict, "callable"]:
    """Wrap the renderer's ``_retry_http`` so each attempt of ``op()`` has
    ``inject_rate`` probability of raising ``discord.HTTPException(503)`` —
    *before* it delegates to ``target.send`` / ``msg.edit``.

    This is the boundary the spec asks for: the retry/backoff loop in
    ``_retry_http`` stays exactly the same; we just make the inner call
    flaky. ``channel.send`` is read-only on slotted ``TextChannel``, so this
    is also the most direct injection point that actually works.

    Returns (counters, restore).
    """
    real_retry = renderer._retry_http
    counters = {
        "send_attempts": 0, "send_injects": 0,
        "edit_attempts": 0, "edit_injects": 0,
    }

    async def flaky_retry(op, *, label, content_len, between_attempts=None):
        akey = f"{label}_attempts"
        ikey = f"{label}_injects"

        async def flaky_op():
            counters[akey] = counters.get(akey, 0) + 1
            if random.random() < inject_rate:
                counters[ikey] = counters.get(ikey, 0) + 1
                raise discord.HTTPException(
                    _FakeResp(503), f"injected on {label}",
                )
            return await op()

        return await real_retry(
            flaky_op,
            label=label,
            content_len=content_len,
            between_attempts=between_attempts,
        )

    renderer._retry_http = flaky_retry  # type: ignore[method-assign]

    def restore() -> None:
        try:
            del renderer._retry_http
        except AttributeError:
            pass

    return counters, restore


# ---------------------------------------------------------------------------
# Channel content read-back
# ---------------------------------------------------------------------------

# Cost/stats footer the renderer appends to its last message:
#   "\n\n-# 💰 $X │ 📥 N │ 📤 N │ ⏱️ T(s)" + optional " │ ⚠️ stop_reason"
_FOOTER_RE = re.compile(r"\n\n-# 💰\s+\$[0-9.]+\s+│.*$", flags=re.DOTALL)


def _strip_renderer_artifacts(content: str, *, is_last: bool) -> str:
    """Remove cursor + (on last msg) cost footer."""
    content = content.replace(CURSOR, "")
    if is_last:
        content = _FOOTER_RE.sub("", content)
    return content


async def _collect_channel_text(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    after: discord.Message,
    before: discord.Message,
) -> tuple[str, int, list[str]]:
    """Concatenate message contents authored by the bot between markers."""
    msgs: list[discord.Message] = []
    async for m in channel.history(
        after=after, before=before, limit=500, oldest_first=True,
    ):
        if m.author.id != bot_user.id:
            continue
        # Embed-only messages (e.g. tool activity logs) carry no .content.
        # The truncation question is about prose chars; ignore embeds.
        if not m.content:
            continue
        msgs.append(m)

    cleaned: list[str] = []
    for i, m in enumerate(msgs):
        is_last = (i == len(msgs) - 1)
        cleaned.append(_strip_renderer_artifacts(m.content, is_last=is_last))

    samples: list[str] = []
    if cleaned:
        samples.append("FIRST200=" + cleaned[0][:200])
        samples.append("LAST200=" + cleaned[-1][-200:])

    joined = "".join(cleaned)
    return joined, len(msgs), samples


# ---------------------------------------------------------------------------
# Test orchestration
# ---------------------------------------------------------------------------


class TestState:
    verdict: str = "?"
    error: str | None = None
    sdk_diff: int = -1
    render_diff: int = -1
    delta_chars: int = -1
    result_chars: int = -1
    channel_chars: int = -1
    channel_msgs: int = -1
    elapsed_s: float = -1.0
    samples: list[str]
    saw_retry_log: bool = False
    saw_dropped_log: bool = False
    inject_counters: dict | None = None
    sdk_stats: dict
    delta_tail: str = ""
    result_tail: str = ""
    channel_tail: str = ""

    def __init__(self) -> None:
        self.samples = []
        self.sdk_stats = {}


async def _run_test(
    channel: discord.TextChannel,
    bot_user: discord.ClientUser,
    state: TestState,
) -> None:
    cfg = load_config()
    log.info("Mode %s start ts=%d project=%s", args.mode, TS, ROOT)

    # 1) Marker
    start_marker = await channel.send(
        f"🟢 E2E {args.mode} start ts={TS}",
    )
    log.info("start_marker id=%s", start_marker.id)

    # 2) Real bridge + tee
    real = ClaudeBridge(project_path=str(ROOT), config=cfg)
    await real.start()
    tee = TeeBridge(real)
    log.info("bridge ready model=%s", real.model)

    # 3) Build renderer (injection installs on it, after construction)
    renderer = DiscordRenderer(target=channel)
    restore_inject = None
    if args.mode == "B":
        state.inject_counters, restore_inject = _install_renderer_injection(
            renderer, args.inject_rate,
        )
        log.info("Injection enabled at rate=%.2f", args.inject_rate)

    # 4) Render
    t0 = time.time()
    try:
        await renderer.render_response(tee, PROMPT)  # type: ignore[arg-type]
    finally:
        try:
            await real.stop()
        except Exception:
            log.exception("bridge.stop failed (ignored)")
        if restore_inject is not None:
            restore_inject()
    state.elapsed_s = time.time() - t0
    log.info("render_response done in %.1fs", state.elapsed_s)

    # 5) Discord propagation
    await asyncio.sleep(2.0)

    # 6) End marker
    end_marker = await channel.send(f"🔴 E2E {args.mode} end")

    # 7) Read history
    joined, n_msgs, samples = await _collect_channel_text(
        channel, bot_user, after=start_marker, before=end_marker,
    )
    state.channel_chars = len(joined)
    state.channel_msgs = n_msgs
    state.samples = samples
    state.delta_chars = tee.delta_chars
    state.result_chars = len(tee.result_text)
    state.sdk_diff = abs(state.delta_chars - state.result_chars)
    state.render_diff = abs(state.result_chars - state.channel_chars)
    state.sdk_stats = {
        "events_seen": tee.events_seen,
        "session_id": real.session_id,
        "total_cost": real.total_cost,
        "num_turns": real.num_turns,
        "model": real.model,
    }

    # tails for the JSON artifact
    def _tail(s: str, n: int = 500) -> str:
        return s[-n:] if len(s) > n else s

    state.delta_tail = _tail("".join(tee.delta_concat))
    state.result_tail = _tail(tee.result_text)
    state.channel_tail = _tail(joined)

    # Persist full bodies for diff (logs/ is gitignored).
    body_dir = LOG_DIR / f"e2e_{args.mode}_{TS}_bodies"
    body_dir.mkdir(parents=True, exist_ok=True)
    (body_dir / "delta.txt").write_text("".join(tee.delta_concat))
    (body_dir / "result.txt").write_text(tee.result_text)
    (body_dir / "channel.txt").write_text(joined)
    log.info("Wrote full bodies to %s", body_dir)

    log.info(
        "lengths: delta=%d  result=%d  channel=%d  msgs=%d  events=%d",
        state.delta_chars, state.result_chars, state.channel_chars,
        state.channel_msgs, tee.events_seen,
    )
    log.info(
        "sdk_diff=%d  render_diff=%d  elapsed=%.1fs",
        state.sdk_diff, state.render_diff, state.elapsed_s,
    )

    # 8) Inspect captured renderer logs for retry / drop signatures
    for r in _renderer_records:
        msg = r.getMessage()
        if "transient failure" in msg or "rate-limited" in msg:
            state.saw_retry_log = True
        if "DROPPED" in msg or "UNDELIVERED" in msg:
            state.saw_dropped_log = True

    # 9) Verdict
    failures: list[str] = []
    warnings: list[str] = []

    if state.result_chars < args.prompt_min_chars:
        state.verdict = "PRECONDITION_FAILED"
        state.error = (
            f"Claude only returned {state.result_chars} chars "
            f"(<{args.prompt_min_chars}); test is meaningless"
        )
        return

    if state.sdk_diff > 5:
        failures.append(f"sdk_diff {state.sdk_diff} > 5 (SDK self-inconsistent)")

    if args.mode == "A":
        # Per spec: <=100 clean, 100<x<=500 suspicious (warn), >500 fail
        if state.render_diff > 500:
            failures.append(
                f"render_diff {state.render_diff} > 500 (FAIL threshold)",
            )
        elif state.render_diff > args.happy_render_diff:
            warnings.append(
                f"render_diff {state.render_diff} > {args.happy_render_diff} "
                f"(suspicious but below FAIL=500)",
            )
        if state.channel_chars < 6000:
            failures.append(
                f"channel_chars {state.channel_chars} < 6000 — "
                f"prompt didn't push past truncation regime",
            )
    else:  # mode B
        if state.render_diff > args.inject_render_diff:
            warnings.append(
                f"render_diff {state.render_diff} > {args.inject_render_diff}",
            )
        if state.channel_chars < 0.95 * state.result_chars:
            failures.append(
                f"channel_chars {state.channel_chars} < 95% of "
                f"result {state.result_chars} ({state.channel_chars/state.result_chars:.2%})",
            )
        if not state.saw_retry_log:
            failures.append(
                "expected at least one transient/rate-limited warning "
                "but renderer log shows none — injection may not have fired "
                "or _retry_http isn't logging",
            )
        if state.saw_dropped_log:
            failures.append(
                "renderer logged DROPPED/UNDELIVERED — content was permanently lost",
            )

    state.verdict = "PASS" if not failures else "FAIL"
    parts = []
    if failures:
        parts.append("FAILURES: " + "; ".join(failures))
    if warnings:
        parts.append("WARNINGS: " + "; ".join(warnings))
    state.error = " | ".join(parts) if parts else None


def _dump_json(state: TestState) -> None:
    payload = {
        "mode": args.mode,
        "ts": TS,
        "verdict": state.verdict,
        "error": state.error,
        "elapsed_s": state.elapsed_s,
        "sdk_diff": state.sdk_diff,
        "render_diff": state.render_diff,
        "delta_chars": state.delta_chars,
        "result_chars": state.result_chars,
        "channel_chars": state.channel_chars,
        "channel_msgs": state.channel_msgs,
        "saw_retry_log": state.saw_retry_log,
        "saw_dropped_log": state.saw_dropped_log,
        "inject_rate": args.inject_rate if args.mode == "B" else None,
        "inject_counters": state.inject_counters,
        "sdk_stats": state.sdk_stats,
        "samples": state.samples,
        "delta_tail": state.delta_tail,
        "result_tail": state.result_tail,
        "channel_tail": state.channel_tail,
    }
    JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    )
    log.info("Wrote summary JSON to %s", JSON_PATH)


async def _amain(state: TestState) -> None:
    cfg = load_config()
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():  # type: ignore[no-untyped-def]
        try:
            log.info("Connected as %s (id=%s)", client.user, client.user.id)
            guild = client.get_guild(GUILD_ID)
            if guild is None:
                state.verdict = "BLOCKED"
                state.error = f"Guild {GUILD_ID} not visible to bot"
                log.error(state.error)
                return
            channel = guild.get_channel(CHANNEL_ID)
            if channel is None:
                state.verdict = "BLOCKED"
                state.error = f"Channel {CHANNEL_ID} not visible"
                log.error(state.error)
                return
            await _run_test(channel, client.user, state)
        except Exception as e:
            log.exception("Test crashed")
            state.verdict = "CRASH"
            state.error = repr(e)
        finally:
            await client.close()

    await client.start(cfg.discord_bot_token)


def main() -> int:
    state = TestState()
    try:
        asyncio.run(_amain(state))
    except KeyboardInterrupt:
        state.verdict = "INTERRUPTED"
        state.error = "user interrupted"

    _dump_json(state)

    print()
    print("=" * 70)
    print(f"E2E truncation test — mode {args.mode}")
    print("=" * 70)
    print(f"VERDICT       : {state.verdict}")
    if state.error:
        print(f"  reason      : {state.error}")
    print(f"  delta_chars : {state.delta_chars}")
    print(f"  result_chars: {state.result_chars}")
    print(f"  chan_chars  : {state.channel_chars}  (over {state.channel_msgs} msgs)")
    print(f"  sdk_diff    : {state.sdk_diff}")
    print(f"  render_diff : {state.render_diff}")
    print(f"  elapsed     : {state.elapsed_s:.1f}s")
    print(f"  retry_log   : {state.saw_retry_log}")
    print(f"  drop_log    : {state.saw_dropped_log}")
    if state.inject_counters:
        print(f"  inject_cnts : {state.inject_counters}")
    print(f"  log file    : {LOG_PATH}")
    print(f"  json file   : {JSON_PATH}")
    print("=" * 70)

    return 0 if state.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
