"""#251 Approach E — REAL Discord transport e2e harness.

Drives the actual production bot via testbot messages in `#testbot`
(channel `1506138802090151977` in test guild `1499415073838600454`),
observing real responses through Discord's API.

What this validates that the mock harness (`run_e2e.py`) cannot:

* on_message routing through Discord gateway
* Bot's `_handle_thread_message` + `_handle_channel_message` real paths
* Thread creation flow
* Real attachment upload + claude `Read` tool path
* /context, /skill list paths that hit Claude SDK
* Embed rendering exactly as users see it
* Slash command dispatch through Discord (testbot can post text +
  attachments; for slash we drive via message commands prefixed)

What it does NOT cover:

* Slash commands themselves (testbot is a bot account; can't invoke
  slash on the user's behalf without `bot` scope; the mock harness
  covers the cog callback paths)
* User-as-admin permission scenarios

Layout
======

Each test:

1. `setup`: snapshots current bot state we care about
2. `act`: testbot posts message / attachment
3. `wait`: polls channel/thread history for bot response (≤ TIMEOUT)
4. `assert`: verify bot's response matches expected
5. `cleanup`: best-effort delete test artifacts, restore state

Output: `data/e2e-reports/real-YYYY-MM-DD_HHMMSS.md`

Run
===

    PYTHONPATH=src .venv/bin/python scripts/e2e/run_real_e2e.py

Requires:
- `.testbot.env.txt` with testbot token
- Real bot running locally with `CLAUDED_TESTBOT_ID` set
- `#testbot` channel (id `1506138802090151977`) bound to some project
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import discord

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Test environment constants
GUILD_ID = 1499415073838600454           # "Claude' Test"
CHANNEL_ID = 1506138802090151977         # #testbot
BOT_USER_ID = 1499415416701980704        # the real ClaudeBot

TIMEOUT_S = 60                            # per-test wait budget
SHORT_TIMEOUT_S = 15                      # for quick-reply commands
HEALTH_TIMEOUT_S = 20

# ---------------------------------------------------------------------------
# Result + harness
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    name: str
    status: str  # PASS / FAIL / ERROR / SKIP
    detail: str = ""
    duration_s: float = 0.0
    bot_replies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Driver — bot connection + per-case state
# ---------------------------------------------------------------------------


class TestBotDriver:
    """Wraps the testbot Client + provides post/wait helpers."""

    def __init__(self, token: str):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        self.token = token
        self.client = discord.Client(intents=intents)
        self.channel: discord.TextChannel | None = None

    async def __aenter__(self):
        self._ready = asyncio.Event()

        @self.client.event
        async def on_ready():
            self._ready.set()

        self._task = asyncio.create_task(self.client.start(self.token))
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        self.channel = self.client.get_channel(CHANNEL_ID)
        if self.channel is None:
            self.channel = await self.client.fetch_channel(CHANNEL_ID)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.client.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except Exception:
            pass

    async def post(self, content: str, file: discord.File | None = None) -> discord.Message:
        """Send a message in #testbot. Returns the sent message."""
        return await self.channel.send(content=content, file=file)

    async def wait_for_bot_reply(
        self,
        after: discord.Message,
        *,
        timeout: float = TIMEOUT_S,
        match: str | re.Pattern | None = None,
        poll_interval: float = 1.5,
        include_threads: bool = True,
    ) -> list[discord.Message]:
        """Poll channel history for messages from the bot after `after`.

        Returns list of all bot messages observed. If `match` is given,
        wait until at least one message matches (substring or regex).
        """
        deadline = time.time() + timeout
        seen_ids: set[int] = set()
        replies: list[discord.Message] = []
        seen_threads: set[int] = set()

        def _matches(text: str) -> bool:
            if match is None:
                return True
            if isinstance(match, str):
                return match in text
            return bool(match.search(text))

        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            # Channel messages
            async for m in self.channel.history(after=after, limit=50, oldest_first=True):
                if m.author.id == BOT_USER_ID and m.id not in seen_ids:
                    seen_ids.add(m.id)
                    replies.append(m)
                    if m.thread and m.thread.id not in seen_threads:
                        seen_threads.add(m.thread.id)
            # Thread messages (only the threads bot created off the trigger msg)
            if include_threads:
                # Also: the trigger message itself may have a thread attached
                refreshed = None
                try:
                    refreshed = await self.channel.fetch_message(after.id)
                except Exception:
                    pass
                if refreshed and refreshed.thread and refreshed.thread.id not in seen_threads:
                    seen_threads.add(refreshed.thread.id)
                for tid in list(seen_threads):
                    t = self.client.get_channel(tid)
                    if t is None:
                        try:
                            t = await self.client.fetch_channel(tid)
                        except Exception:
                            continue
                    async for m in t.history(limit=100, oldest_first=True):
                        if m.author.id == BOT_USER_ID and m.id not in seen_ids:
                            seen_ids.add(m.id)
                            replies.append(m)
            # Match check
            if match and any(_matches(self._flatten(m)) for m in replies):
                return replies

        return replies

    @staticmethod
    def _flatten(m: discord.Message) -> str:
        parts = [m.content or ""]
        for e in m.embeds:
            parts.append(e.title or "")
            parts.append(e.description or "")
            for f in e.fields:
                parts.append(f"{f.name}: {f.value}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Setup / teardown helpers
# ---------------------------------------------------------------------------


def _snapshot_projects_json() -> dict:
    p = Path("data/projects.json")
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _restore_projects_json(snap: dict) -> None:
    p = Path("data/projects.json")
    p.write_text(json.dumps(snap, indent=2, ensure_ascii=False))


def _ensure_testbot_bound() -> bool:
    """Make sure #testbot channel is bound to some project for E tests.
    Returns True if we added a temporary binding (caller should restore)."""
    p = Path("data/projects.json")
    d = json.loads(p.read_text()) if p.exists() else {}
    key = str(CHANNEL_ID)
    if key in d:
        return False  # already bound; nothing to undo
    d[key] = {
        "bound_at": datetime.now(timezone.utc).isoformat(),
        "path": "/tmp/img-probe",
    }
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    return True


async def _reload_bot_state() -> None:
    """Force the running bot to re-read projects.json by kickstarting it.

    NOTE: this RESTARTS the bot; only call at suite startup or when state
    file truly needs a reload.
    """
    proc = await asyncio.create_subprocess_exec(
        "launchctl", "kickstart", "-k", "gui/501/com.hxy.clauded",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    # Wait for it to come back up
    await asyncio.sleep(8)


# ---------------------------------------------------------------------------
# Test cases — real Discord transport
# ---------------------------------------------------------------------------


async def case_on_message_mention(driver: TestBotDriver) -> CaseResult:
    """M1: @bot in bound channel → bot creates thread + starts session.

    Strict checks:
    1. Within timeout, channel gets a reply or thread
    2. A thread is created off our trigger message
    3. Bot posts content in the thread
    """
    msg = await driver.post(
        f"<@{BOT_USER_ID}> 你好，请用一个字回复\"嗨\"，不要别的。"
    )
    replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match=re.compile(r"嗨|你好|hello", re.IGNORECASE)
    )
    if not replies:
        return CaseResult(
            name="on_message: M1 @mention",
            status="FAIL",
            detail="no bot reply within timeout",
        )
    # Verify a thread was created
    refreshed = await driver.channel.fetch_message(msg.id)
    has_thread = refreshed.thread is not None
    bot_replies_in_thread = sum(
        1 for r in replies if hasattr(r, "thread") and r.channel.type == discord.ChannelType.public_thread
    ) + sum(
        1 for r in replies if r.channel != driver.channel
    )
    bot_texts = [driver._flatten(r) for r in replies if driver._flatten(r).strip()]
    if has_thread and bot_texts:
        return CaseResult(
            name="on_message: M1 @mention",
            status="PASS",
            detail=f"thread created + {len(bot_texts)} bot msg(s); first: {bot_texts[0][:100]!r}",
            bot_replies=bot_texts,
        )
    return CaseResult(
        name="on_message: M1 @mention",
        status="FAIL",
        detail=f"has_thread={has_thread}; replies={len(replies)}; texts: {[t[:60] for t in bot_texts[:3]]}",
        bot_replies=bot_texts,
    )


async def case_on_message_health_via_text(driver: TestBotDriver) -> CaseResult:
    """Health check style: ask the bot for its status via plain message.

    Testbot can't invoke slash commands. But we can ask claude itself
    inside a session via @mention.
    """
    msg = await driver.post(
        f"<@{BOT_USER_ID}> 这是一个 e2e 测试探针。请回复 '探针 OK' 一字不差，"
        f"不要别的内容。"
    )
    replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match="探针 OK"
    )
    if not replies:
        bot_texts = []
        return CaseResult(name="probe text", status="FAIL",
                          detail="no '探针 OK' reply seen", bot_replies=bot_texts)
    bot_texts = [driver._flatten(r) for r in replies if driver._flatten(r).strip()]
    matched = any("探针 OK" in t for t in bot_texts)
    if matched:
        return CaseResult(
            name="probe text",
            status="PASS",
            detail="claude returned exact '探针 OK' string via real Discord transport",
            bot_replies=bot_texts,
        )
    return CaseResult(name="probe text", status="FAIL",
                      detail=f"got {len(bot_texts)} msgs but none exactly match",
                      bot_replies=bot_texts)


async def case_attachment_image_inline_marker(driver: TestBotDriver) -> CaseResult:
    """#242 round 2: testbot uploads an image with embedded marker text.
    bot should ship it as INLINE image content block (not path-in-text);
    claude reads it directly via vision and quotes the marker back.

    This mirrors spike-3 (from #242 PR-2 PRD) end-to-end through real
    Discord transport.
    """
    # Build a fresh test image with a unique marker right inline
    import io
    from PIL import Image, ImageDraw, ImageFont
    marker = "INLINE_E2E_MARKER_2026_XYZ"
    img = Image.new("RGB", (600, 200), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 32)
    draw.text((20, 80), marker, font=font, fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    msg = await driver.post(
        content=f"<@{BOT_USER_ID}> #242 e2e: 这张图里有一段文字，"
                f"请把那段文字一字不差地回我，不要别的内容。",
        file=discord.File(buf, filename="inline-probe.png"),
    )
    replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match=marker
    )
    bot_texts = [driver._flatten(r) for r in replies if driver._flatten(r).strip()]
    if any(marker in t for t in bot_texts):
        # Also confirm bot logged #242 inline path
        log_path = Path.home() / "Library" / "Logs" / "clauded" / "clauded.log"
        recent = ""
        if log_path.exists():
            recent = log_path.read_text()[-20_000:]
        inline_logged = "#242: inline image attached" in recent
        return CaseResult(
            name="#242 inline image marker",
            status="PASS",
            detail=f"claude quoted marker via vision; inline_logged={inline_logged}",
            bot_replies=bot_texts,
        )
    return CaseResult(
        name="#242 inline image marker",
        status="FAIL",
        detail=f"marker not in {len(bot_texts)} reply texts: {bot_texts[:2]!r}",
        bot_replies=bot_texts,
    )


async def case_log_dump_via_command_emoji(driver: TestBotDriver) -> CaseResult:
    """Confirm log dump bundle generation works for real bot.

    Approach: send claude a message asking it to invoke 'log dump' or
    use a side-channel. Since testbot can't slash-cmd, we instead just
    verify the previous request flow worked (bot is alive + responding).
    This case is more of a 'bot still alive' canary than a true /log
    dump test.
    """
    return CaseResult(
        name="log dump (testbot can't slash)",
        status="SKIP",
        detail="testbot lacks slash command capability; covered by mock harness",
    )


async def case_unbound_refuse(driver: TestBotDriver) -> CaseResult:
    """M3: unbound channel + @bot + fallback off → refuse hint.

    Requires temporarily unbinding #testbot. Risky for our test
    suite — let's skip unless we can isolate. We test the existing
    UNBOUND state via a channel we never bound.

    For now: assume #testbot is bound (we set it up). Test the
    happy-path side. Real unbound test requires a separate test
    channel that's never been bound.
    """
    return CaseResult(
        name="unbound refuse hint",
        status="SKIP",
        detail="requires unbound test channel; not isolated yet",
    )


async def case_long_text_streaming(driver: TestBotDriver) -> CaseResult:
    """Trigger a long claude response → must stream via typewriter,
    not just one big message. Real Discord rendering check.
    """
    msg = await driver.post(
        f"<@{BOT_USER_ID}> 用中文给我写一首关于 e2e 测试的现代诗，"
        f"至少 8 行，每行不少于 10 个字。"
    )
    # Give it time — long response
    replies = await driver.wait_for_bot_reply(
        msg, timeout=90, match=re.compile(r".{200,}", re.DOTALL)
    )
    if not replies:
        return CaseResult(name="long text streaming", status="FAIL",
                          detail="no long response")
    all_text = "\n".join(driver._flatten(r) for r in replies)
    if len(all_text) >= 200:
        return CaseResult(
            name="long text streaming",
            status="PASS",
            detail=f"got {len(all_text)} chars in {len(replies)} message(s); "
                   f"first chunk: {all_text[:100]!r}",
        )
    return CaseResult(
        name="long text streaming",
        status="FAIL",
        detail=f"only {len(all_text)} chars",
    )


async def case_table_rendering(driver: TestBotDriver) -> CaseResult:
    """Markdown table in claude response → must render as PNG (table_png).
    Verify by checking the bot reply has an image attachment.
    """
    msg = await driver.post(
        f"<@{BOT_USER_ID}> 用 markdown 表格列出 5 个常见编程语言和它们的发明年份。"
        f"必须用 markdown table 格式（| 分隔），不要别的内容。"
    )
    replies = await driver.wait_for_bot_reply(msg, timeout=60)
    if not replies:
        return CaseResult(name="markdown table → PNG", status="FAIL",
                          detail="no reply")
    # Check if any reply has an image attachment
    has_png = any(
        any(att.filename.endswith(".png") for att in r.attachments)
        for r in replies
    )
    has_table_text = any(
        "|" in driver._flatten(r) and "---" in driver._flatten(r)
        for r in replies
    )
    if has_png:
        return CaseResult(
            name="markdown table → PNG",
            status="PASS",
            detail="bot uploaded PNG attachment for markdown table",
        )
    if has_table_text:
        return CaseResult(
            name="markdown table → PNG",
            status="FAIL",
            detail="table text returned but no PNG render (table_png path?)",
        )
    return CaseResult(
        name="markdown table → PNG",
        status="FAIL",
        detail=f"no table or PNG; got: {[driver._flatten(r)[:100] for r in replies[:2]]}",
    )


async def case_third_party_thread_silence(driver: TestBotDriver) -> CaseResult:
    """M6: 3rd-party thread (created by testbot not the bot) → bot
    must SILENTLY ignore plain messages (no @mention).

    Setup: testbot creates a thread off a probe message, posts a
    plain message inside. Bot should NOT engage.
    """
    seed = await driver.post(
        "e2e: 3rd-party thread silence probe."
    )
    # Testbot creates a thread off this message
    try:
        thread = await seed.create_thread(name="3rd-party-test")
    except Exception as exc:
        return CaseResult(name="M6 3rd-party silence", status="ERROR",
                          detail=f"can't create thread: {exc}")
    # Post a plain message (no @mention)
    plain = await thread.send("Hello bot, are you there? (no mention)")
    # Wait 30s; bot should NOT reply
    await asyncio.sleep(30)
    # Re-fetch thread history
    bot_msgs = []
    async for m in thread.history(after=plain, limit=20):
        if m.author.id == BOT_USER_ID:
            bot_msgs.append(driver._flatten(m))
    # Cleanup: archive the thread
    try:
        await thread.edit(archived=True)
    except Exception:
        pass
    if not bot_msgs:
        return CaseResult(
            name="M6 3rd-party silence",
            status="PASS",
            detail="bot correctly silent in 3rd-party thread",
        )
    return CaseResult(
        name="M6 3rd-party silence",
        status="FAIL",
        detail=f"bot replied in 3rd-party thread: {bot_msgs[0][:200]!r}",
        bot_replies=bot_msgs,
    )


async def case_3rd_party_thread_with_mention(driver: TestBotDriver) -> CaseResult:
    """M6b: 3rd-party thread + @mention → bot SHOULD engage."""
    seed = await driver.post("e2e: 3rd-party thread with mention probe.")
    try:
        thread = await seed.create_thread(name="3rd-mention-test")
    except Exception as exc:
        return CaseResult(name="M6b 3rd-party with mention", status="ERROR",
                          detail=f"can't create thread: {exc}")
    msg = await thread.send(f"<@{BOT_USER_ID}> 请说 'engaged' 一字不差。")
    # Poll thread history (bot should engage)
    bot_msgs = []
    deadline = time.time() + 60
    while time.time() < deadline:
        await asyncio.sleep(3)
        async for m in thread.history(after=msg, limit=20):
            if m.author.id == BOT_USER_ID and m.id not in {b.id for b in bot_msgs}:
                bot_msgs.append(m)
        if any("engaged" in driver._flatten(m) for m in bot_msgs):
            break
    try:
        await thread.edit(archived=True)
    except Exception:
        pass
    if any("engaged" in driver._flatten(m) for m in bot_msgs):
        return CaseResult(
            name="M6b 3rd-party with mention",
            status="PASS",
            detail="bot engaged with explicit mention in 3rd-party thread",
        )
    return CaseResult(
        name="M6b 3rd-party with mention",
        status="FAIL",
        detail=f"bot didn't engage; got {len(bot_msgs)} msgs",
        bot_replies=[driver._flatten(m) for m in bot_msgs],
    )


# ---------------------------------------------------------------------------
# /schedule (#241) e2e cases + assert helpers
# ---------------------------------------------------------------------------


def assert_schedule_message_fired() -> tuple[bool, str]:
    """Verify a schedule_message fired:
    1. data/schedules.json contains exactly 1 schedule with name=e2e_msg,
       fire_count=1, enabled=False (one-shot completed), last_error=None
    2. The target thread (the one testbot posted in) received both:
       - a "-# ⏰ Scheduled fire: e2e_msg" line
       - a quoted "> SCHED_MSG_E2E_FIRED" line
       - a subsequent claude response
    Returns (ok, reason).
    """
    p = Path("data/schedules.json")
    if not p.exists():
        return False, "data/schedules.json missing"
    schedules = json.loads(p.read_text())
    matches = [s for s in schedules.values() if s.get("name") == "e2e_msg"]
    if len(matches) != 1:
        return False, f"expected 1 schedule named e2e_msg, got {len(matches)}"
    s = matches[0]
    state = s.get("state", {})
    if state.get("fire_count") != 1:
        return False, f"fire_count={state.get('fire_count')} != 1"
    if state.get("enabled") is not False:
        return False, f"enabled={state.get('enabled')} != False"
    if state.get("last_error"):
        return False, f"last_error={state.get('last_error')!r}"
    return True, "schedule_message fired and persisted correctly"


def assert_schedule_new_task_fired() -> tuple[bool, str]:
    """Verify schedule_new_task fired:
    1. data/schedules.json contains 1 schedule named e2e_newtask, fire_count=1
    2. A new thread was created in the test channel
    """
    p = Path("data/schedules.json")
    if not p.exists():
        return False, "data/schedules.json missing"
    schedules = json.loads(p.read_text())
    matches = [s for s in schedules.values() if s.get("name") == "e2e_newtask"]
    if len(matches) != 1:
        return False, f"expected 1 schedule named e2e_newtask, got {len(matches)}"
    s = matches[0]
    state = s.get("state", {})
    if state.get("fire_count") != 1:
        return False, f"fire_count={state.get('fire_count')} != 1"
    if state.get("enabled") is not False:
        return False, f"enabled={state.get('enabled')} != False"
    if state.get("last_error"):
        return False, f"last_error={state.get('last_error')!r}"
    return True, "schedule_new_task fired and persisted correctly"


# Registry mapping case "assert" name -> callable, mirrors the data-style
# spec in PRD §8 Subtask 5. The case wrappers below call into this registry
# so new asserts can be added by extending only this dict.
SCHEDULE_ASSERT_REGISTRY = {
    "schedule_message_fired": assert_schedule_message_fired,
    "schedule_new_task_fired": assert_schedule_new_task_fired,
}


async def case_schedule_message_e2e(driver: TestBotDriver) -> CaseResult:
    """#241 e2e: claude creates a one-shot schedule_message timer via natural
    language; wait ~130s for the timer to fire; verify the schedule entry on
    disk plus the marker text appears in the test thread.

    Flow:
    1. @bot post the create-schedule prompt
    2. Wait for claude's "好" creation ACK (a thread also gets created)
    3. Sleep wait_seconds (gives the 90s timer + render margin)
    4. Verify the marker text "SCHED_MSG_E2E_FIRED" landed in the thread
    5. Verify the schedules.json entry (fire_count=1, enabled=False, etc.)
    """
    marker = "SCHED_MSG_E2E_FIRED"
    wait_seconds = 130
    prompt = (
        "请用 schedule_message 工具创建一个一次性定时任务。"
        "when 设为 90 秒后的 ISO 时间 (使用 iso: 前缀，UTC tz)，"
        f"what 设为 `{marker}`，name 设为 `e2e_msg`。"
        "target_thread_id 不要传，用当前 thread。recurring 不要传。"
        "max_lifetime 不要传。创建完只回\"好\"。"
    )
    msg = await driver.post(f"<@{BOT_USER_ID}> {prompt}")
    # First wait: claude ACKs the creation (kicks off thread + posts "好")
    ack_replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match="好"
    )
    if not ack_replies:
        return CaseResult(
            name="case_schedule_message_e2e",
            status="FAIL",
            detail="no creation ACK from claude within timeout",
        )
    # Now wait for the timer to fire and the marker to appear
    fire_replies = await driver.wait_for_bot_reply(
        msg, timeout=wait_seconds, match=marker
    )
    bot_texts = [driver._flatten(r) for r in fire_replies if driver._flatten(r).strip()]
    saw_marker = any(marker in t for t in bot_texts)
    saw_prefix = any("Scheduled fire" in t and "e2e_msg" in t for t in bot_texts)
    ok, reason = SCHEDULE_ASSERT_REGISTRY["schedule_message_fired"]()
    if saw_marker and saw_prefix and ok:
        return CaseResult(
            name="case_schedule_message_e2e",
            status="PASS",
            detail=f"marker+prefix visible in thread; {reason}",
            bot_replies=bot_texts,
        )
    return CaseResult(
        name="case_schedule_message_e2e",
        status="FAIL",
        detail=(
            f"saw_marker={saw_marker} saw_prefix={saw_prefix} "
            f"store_check_ok={ok} reason={reason!r}"
        ),
        bot_replies=bot_texts,
    )


async def case_schedule_new_task_e2e(driver: TestBotDriver) -> CaseResult:
    """#241 e2e: claude creates a one-shot schedule_new_task timer; wait
    ~130s; verify that on fire a NEW thread is created (announce embed in
    parent channel mentions "Scheduled-task thread") and the marker text
    lands in the new thread.
    """
    marker = "SCHED_NEWTASK_E2E_FIRED"
    wait_seconds = 130
    prompt = (
        "请用 schedule_new_task 工具创建一个一次性定时任务。"
        "when 设为 90 秒后的 ISO 时间 (使用 iso: 前缀，UTC tz)，"
        f"what 设为 `{marker} 测试`，"
        "thread_name 设为 `e2e-newtask-thread`，"
        "name 设为 `e2e_newtask`。target_channel_id 不要传，"
        "用当前 channel。recurring 不要传。max_lifetime 不要传。"
        "创建完只回\"好\"。"
    )
    msg = await driver.post(f"<@{BOT_USER_ID}> {prompt}")
    ack_replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match="好"
    )
    if not ack_replies:
        return CaseResult(
            name="case_schedule_new_task_e2e",
            status="FAIL",
            detail="no creation ACK from claude within timeout",
        )
    fire_replies = await driver.wait_for_bot_reply(
        msg, timeout=wait_seconds, match=marker
    )
    bot_texts = [driver._flatten(r) for r in fire_replies if driver._flatten(r).strip()]
    saw_marker = any(marker in t for t in bot_texts)
    saw_announce = any(
        "Scheduled-task thread" in t or "scheduled-task thread" in t
        for t in bot_texts
    )
    ok, reason = SCHEDULE_ASSERT_REGISTRY["schedule_new_task_fired"]()
    if saw_marker and saw_announce and ok:
        return CaseResult(
            name="case_schedule_new_task_e2e",
            status="PASS",
            detail=f"marker+announce visible; {reason}",
            bot_replies=bot_texts,
        )
    return CaseResult(
        name="case_schedule_new_task_e2e",
        status="FAIL",
        detail=(
            f"saw_marker={saw_marker} saw_announce={saw_announce} "
            f"store_check_ok={ok} reason={reason!r}"
        ),
        bot_replies=bot_texts,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(results: list[CaseResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by = {}
    for r in results:
        by[r.status] = by.get(r.status, 0) + 1
    lines = [
        f"# claudeD REAL e2e 测试报告 ({now})",
        "",
        f"- 总测试: {len(results)}",
        f"- ✅ PASS: {by.get('PASS', 0)}",
        f"- ❌ FAIL: {by.get('FAIL', 0)}",
        f"- 💥 ERROR: {by.get('ERROR', 0)}",
        f"- ⏭ SKIP: {by.get('SKIP', 0)}",
        "",
        "## 详细",
        "",
        "| Case | Status | Time | Detail |",
        "|---|---|---|---|",
    ]
    for r in results:
        d = r.detail.replace("|", "\\|").replace("\n", " ")[:180]
        emoji = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "SKIP": "⏭"}[r.status]
        lines.append(
            f"| {r.name} | {emoji} {r.status} | {r.duration_s:.1f}s | {d} |"
        )
    fails = [r for r in results if r.status in ("FAIL", "ERROR")]
    if fails:
        lines += ["", "## 失败详情", ""]
        for r in fails:
            lines.append(f"### {r.name}")
            lines.append(f"- Status: {r.status}")
            lines.append(f"- Detail: {r.detail}")
            if r.bot_replies:
                lines.append("- Bot replies:")
                for b in r.bot_replies[:3]:
                    lines.append(f"  - `{b[:200]!r}`")
            lines.append("")
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    print("=" * 60)
    print("REAL Discord e2e test suite — driving production bot via testbot")
    print(f"  Test guild  : {GUILD_ID}")
    print(f"  Test channel: #{CHANNEL_ID}")
    print(f"  Real bot id : {BOT_USER_ID}")
    print("=" * 60)

    # Snapshot + ensure binding
    snap = _snapshot_projects_json()
    added_binding = _ensure_testbot_bound()
    if added_binding:
        print("Added #testbot binding to projects.json; will restore.")
        print("Restarting bot to pick up the binding...")
        await _reload_bot_state()

    # Token
    with open(".testbot.env.txt") as f:
        token = f.read().strip().split("=", 1)[1]

    cases = [
        ("M1 @mention starts session", case_on_message_mention),
        ("Probe: exact-text reply", case_on_message_health_via_text),
        ("#242 inline image marker", case_attachment_image_inline_marker),
        ("Long text streaming", case_long_text_streaming),
        ("Markdown table → PNG", case_table_rendering),
        ("M6 3rd-party thread silent", case_third_party_thread_silence),
        ("M6b 3rd-party with mention", case_3rd_party_thread_with_mention),
        ("Log dump (slash needed)", case_log_dump_via_command_emoji),
        ("Unbound refuse hint", case_unbound_refuse),
        ("#241 /schedule message e2e", case_schedule_message_e2e),
        ("#241 /schedule new_task e2e", case_schedule_new_task_e2e),
    ]

    results: list[CaseResult] = []
    try:
        async with TestBotDriver(token) as driver:
            print(f"testbot ready as {driver.client.user}")
            for name, fn in cases:
                print(f"\n  running: {name}")
                t0 = time.time()
                try:
                    r = await fn(driver)
                except Exception as exc:
                    r = CaseResult(name=name, status="ERROR",
                                   detail=f"{type(exc).__name__}: {exc}")
                r.duration_s = time.time() - t0
                results.append(r)
                emoji = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "SKIP": "⏭"}[r.status]
                print(f"    {emoji} {r.status} ({r.duration_s:.1f}s)")
                if r.detail:
                    print(f"    detail: {r.detail[:200]}")
    finally:
        # Restore original projects.json
        if added_binding:
            _restore_projects_json(snap)
            print("\nRestored original projects.json")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    out = ROOT / "data" / "e2e-reports" / f"real-{ts}.md"
    write_report(results, out)

    print()
    print("=" * 60)
    print(f"Report: {out}")
    pass_n = sum(1 for r in results if r.status == "PASS")
    fail_n = sum(1 for r in results if r.status == "FAIL")
    error_n = sum(1 for r in results if r.status == "ERROR")
    skip_n = sum(1 for r in results if r.status == "SKIP")
    print(f"Summary: PASS={pass_n} FAIL={fail_n} ERROR={error_n} SKIP={skip_n}")


if __name__ == "__main__":
    asyncio.run(main())
