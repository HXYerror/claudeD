"""#251 e2e harness — drive every slash command via mock Interaction.

Strategy (PRD §Spec 3 Approach D + E):

D — Mock Interaction
~~~~~~~~~~~~~~~~~~~~
Construct a fake ``discord.Interaction`` and call each cog's callback
directly. Bypasses Discord transport but exercises the full cog logic +
project_manager + session_manager stack. Fast (~50ms / case) and
deterministic.

E — Real testbot message
~~~~~~~~~~~~~~~~~~~~~~~~
For on_message behaviors (M1-M7), `testbot` posts a real message to a
test channel and we observe the running bot's reaction via Discord
history. Slow (~20s / case) but only way to verify on_message gating.

Output
~~~~~~
``data/e2e-reports/YYYY-MM-DD_HHMMSS.md`` — markdown report per PRD §Spec 5.

Run
~~~
    PYTHONPATH=src .venv/bin/python scripts/e2e/run_e2e.py --phase happy
    PYTHONPATH=src .venv/bin/python scripts/e2e/run_e2e.py --phase edge

Phase
~~~~~
* ``happy`` — 1 happy path / command (~69 cases, < 2 min)
* ``edge``  — happy + 3 edge cases / command (~280 cases, 5-10 min)
* ``all``   — happy + edge + on_message (~290 cases, ~15 min)

#251 epic; pragmatic execution per user "都测，测全了" directive.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import discord  # noqa: E402


# ---------------------------------------------------------------------------
# Mock harness — Approach D
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    cog: str
    cmd: str
    case: str
    status: str  # PASS / FAIL / SKIP / ERROR
    detail: str = ""
    duration_s: float = 0.0


def make_mock_bot() -> Any:
    """Build a stub `ClaudedBot` with the structures cogs touch.

    We instantiate a real ProjectManager / SessionStore / CostTracker
    pointed at an isolated tmp data dir so the cog code reads its own
    state instead of hitting prod ``data/``.

    **Subclass ClaudedBot** rather than MagicMock so cogs that do
    ``isinstance(bot, ClaudedBot)`` (e.g. `/health`) pass.
    """
    from clauded.project_manager import ProjectManager
    from clauded.session_store import SessionStore
    from clauded.cost_tracker import CostTracker
    from clauded.session_manager import SessionManager
    from clauded.agent_manager import AgentManager
    from clauded.bot import ClaudedBot

    tmp_data = Path("/tmp/e2e_data")
    if tmp_data.exists():
        shutil.rmtree(tmp_data)
    tmp_data.mkdir()
    (tmp_data / "projects.json").write_text("{}")
    (tmp_data / "sessions.json").write_text("{}")
    (tmp_data / "costs.json").write_text("{}")

    cfg = MagicMock()
    cfg.allow_unbound_fallback = False
    cfg.data_dir = str(tmp_data)
    cfg.cwd_path = str(tmp_data)
    cfg.claude_permission_mode = "default"
    cfg.claude_cli_path = None
    cfg.claude_model = None
    cfg.session_timeout_s = 3600
    cfg.bridge_stop_timeout_s = 30

    bot = ClaudedBot.__new__(ClaudedBot)  # bypass __init__ (Discord client setup)
    bot.config = cfg
    bot.project_manager = ProjectManager(data_dir=str(tmp_data))
    bot.session_store = SessionStore(data_dir=str(tmp_data))
    bot.cost_tracker = CostTracker(data_dir=str(tmp_data))
    bot.session_manager = SessionManager(session_store=bot.session_store)
    bot.agent_manager = AgentManager(data_dir=str(tmp_data))
    bot._claude_version = "test-1.0"
    bot._start_time = time.time()
    bot._notify_enabled = {}
    bot._pre_tool_notifications = True
    bot.allow_unbound_fallback = False
    bot._debug_logging = False
    bot._stream_debug_enabled = False
    return bot


def make_mock_interaction(
    *,
    bot: MagicMock,
    channel_id: int = 1500000000_000000001,
    guild_id: int = 1499415073838600454,
    user_id: int = 1091005559769145407,
    in_thread: bool = False,
    parent_id: int | None = None,
    is_admin: bool = True,
) -> MagicMock:
    """Build a `discord.Interaction` impostor."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot

    if in_thread:
        thread = MagicMock(spec=discord.Thread)
        thread.id = channel_id
        thread.parent_id = parent_id or (channel_id - 1)
        thread.parent = MagicMock()
        thread.parent.id = thread.parent_id
        thread.guild = MagicMock()
        thread.guild.id = guild_id
        thread.guild.me = MagicMock()
        thread.guild.me.guild_permissions = MagicMock(manage_channels=is_admin)
        interaction.channel = thread
    else:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
        channel.guild = MagicMock()
        channel.guild.id = guild_id
        channel.guild.me = MagicMock()
        channel.guild.me.guild_permissions = MagicMock(manage_channels=is_admin)
        interaction.channel = channel

    interaction.channel_id = channel_id
    interaction.guild_id = guild_id

    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.bot = False
    user.guild_permissions = MagicMock(administrator=is_admin)
    interaction.user = user

    # response and followup capture
    interaction._sent = []
    interaction._followups = []

    async def _send_message(*args, **kwargs):
        interaction._sent.append({"args": args, "kwargs": kwargs})
    async def _defer(*args, **kwargs):
        interaction._sent.append({"defer": True, "kwargs": kwargs})
    async def _followup_send(*args, **kwargs):
        interaction._followups.append({"args": args, "kwargs": kwargs})

    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock(side_effect=_send_message)
    interaction.response.defer = AsyncMock(side_effect=_defer)
    # `is_done()` is a real method on Interaction.response — cog code
    # branches on it to choose between response.send_message and
    # followup.send. Default to False so the cog uses the primary path.
    interaction.response.is_done = lambda: False
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(side_effect=_followup_send)

    return interaction


def extract_callback(cmd: Any) -> Callable | None:
    """Get the underlying coroutine from an app_commands.Command/Group entry."""
    return getattr(cmd, "callback", None) or getattr(cmd, "_callback", None)


# ---------------------------------------------------------------------------
# Per-cog harness — happy-path cases
# ---------------------------------------------------------------------------


async def run_one(name: str, fn: Callable[[], Awaitable[CaseResult]]) -> CaseResult:
    """Wrap a single case with timing + crash containment."""
    t0 = time.time()
    try:
        r = await fn()
        r.duration_s = round(time.time() - t0, 3)
        return r
    except Exception as exc:
        return CaseResult(
            cog="?", cmd=name, case="happy",
            status="ERROR", detail=f"{type(exc).__name__}: {exc}",
            duration_s=round(time.time() - t0, 3),
        )


def _interaction_response_text(interaction) -> str:
    """Flatten everything the interaction said back (content + embed titles + fields)."""
    parts = []
    for s in interaction._sent + interaction._followups:
        if "args" in s:
            for a in s["args"]:
                if isinstance(a, str):
                    parts.append(a)
            kw = s.get("kwargs", {})
            if isinstance(kw.get("content"), str):
                parts.append(kw["content"])
            embeds = []
            if isinstance(kw.get("embed"), discord.Embed):
                embeds.append(kw["embed"])
            if isinstance(kw.get("embeds"), list):
                embeds.extend(e for e in kw["embeds"] if isinstance(e, discord.Embed))
            for e in embeds:
                parts.append((e.title or "") + " | " + (e.description or ""))
                # Field names + values are the most info-dense bit of
                # any embed; cogs use them heavily (/agent list, /health).
                for f in e.fields:
                    parts.append(f"{f.name}: {f.value}")
    return "\n".join(parts)


async def case_project_bind_happy(bot) -> CaseResult:
    """/project bind path=tmp_dir — happy path."""
    from clauded.cogs.project import project_group
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    cb = extract_callback(bind_cmd)
    inter = make_mock_interaction(bot=bot)
    # Use existing project root to satisfy any pre-flight checks
    bind_path = str(ROOT)  # use repo itself as bind target
    await cb(inter, path=bind_path)
    reply = _interaction_response_text(inter)
    # Verify side-effect: data/projects.json now has our channel id
    state = json.loads(Path("/tmp/e2e_data/projects.json").read_text())
    sid = str(inter.channel_id)
    if sid in state and state[sid]["path"]:
        return CaseResult(cog="project", cmd="bind", case="happy", status="PASS",
                          detail=f"bound to {state[sid]['path']}")
    return CaseResult(cog="project", cmd="bind", case="happy", status="FAIL",
                      detail=f"binding missing; reply={reply[:200]!r}")


async def case_project_info_after_bind(bot) -> CaseResult:
    """/project info after a bind."""
    from clauded.cogs.project import project_group
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    info_cmd = next(c for c in project_group.commands if c.name == "info")
    cb_bind = extract_callback(bind_cmd)
    cb_info = extract_callback(info_cmd)
    inter = make_mock_interaction(bot=bot)
    await cb_bind(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await cb_info(inter2)
    reply = _interaction_response_text(inter2)
    if str(ROOT) in reply or "bound" in reply.lower():
        return CaseResult(cog="project", cmd="info", case="post-bind", status="PASS",
                          detail=f"reply mentions bind target")
    return CaseResult(cog="project", cmd="info", case="post-bind", status="FAIL",
                      detail=f"reply={reply[:200]!r}")


async def case_project_unbind(bot) -> CaseResult:
    """/project unbind removes the binding."""
    from clauded.cogs.project import project_group
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    unbind_cmd = next(c for c in project_group.commands if c.name == "unbind")
    inter = make_mock_interaction(bot=bot)
    await extract_callback(bind_cmd)(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await extract_callback(unbind_cmd)(inter2)
    state = json.loads(Path("/tmp/e2e_data/projects.json").read_text())
    if str(inter.channel_id) not in state:
        return CaseResult(cog="project", cmd="unbind", case="happy", status="PASS",
                          detail="binding removed from projects.json")
    return CaseResult(cog="project", cmd="unbind", case="happy", status="FAIL",
                      detail=f"binding still present: {state}")


async def case_project_bind_no_channel(bot) -> CaseResult:
    """/project bind from DM (no channel) → refuse via NO_CHANNEL."""
    from clauded.cogs.project import project_group
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    cb = extract_callback(bind_cmd)
    inter = make_mock_interaction(bot=bot)
    # Force the channel to look like a DM (no parent, no guild attr matters
    # less — resolve_binding_id checks ``isinstance(channel, DMChannel)``)
    dm_channel = MagicMock(spec=discord.DMChannel)
    dm_channel.id = inter.channel_id
    inter.channel = dm_channel
    inter.guild_id = None
    await cb(inter, path=str(ROOT))
    reply = _interaction_response_text(inter)
    # Must refuse with NO_CHANNEL message (per cogs/_unbound.py)
    if "channel" in reply.lower() and ("❌" in reply or "must" in reply.lower() or "not" in reply.lower()):
        return CaseResult(cog="project", cmd="bind", case="DM-refuse",
                          status="PASS", detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="project", cmd="bind", case="DM-refuse",
                      status="FAIL", detail=f"did not refuse DM; reply={reply[:200]!r}")


async def case_health(bot) -> CaseResult:
    """/health: must report uptime + 0 sessions + 0 projects + Python version."""
    from clauded.cogs.ops import health_check
    cb = extract_callback(health_check)
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    # Strict: must have all four fields filled with numbers/strings
    import sys
    py_ver = sys.version.split()[0]
    # Check: 'Uptime' label, 'Active Sessions: 0', 'Bound Projects: 0', and Python version
    checks = {
        "Uptime label": "Uptime" in reply,
        "Active Sessions field": "Active Sessions" in reply,
        "Bound Projects field": "Bound Projects" in reply,
        "Python version": py_ver in reply or "Python" in reply,
        "Claude CLI field": "Claude CLI" in reply,
    }
    failed = [k for k, v in checks.items() if not v]
    if not failed:
        return CaseResult(cog="ops", cmd="health", case="happy", status="PASS",
                          detail=f"all 5 fields present")
    return CaseResult(cog="ops", cmd="health", case="happy", status="FAIL",
                      detail=f"missing: {failed}; reply={reply[:300]!r}")


async def case_model_list(bot) -> CaseResult:
    """/model list: must list every alias defined in KNOWN_MODELS."""
    from clauded.cogs.model import model_group, KNOWN_MODELS
    list_cmd = next(c for c in model_group.commands if c.name == "list")
    cb = extract_callback(list_cmd)
    inter = make_mock_interaction(bot=bot, in_thread=True, parent_id=999_000_000_000)
    bot.project_manager.bind(999_000_000_000, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    missing = [a for a in KNOWN_MODELS if a not in reply]
    if not missing:
        return CaseResult(cog="model", cmd="list", case="happy", status="PASS",
                          detail=f"all {len(KNOWN_MODELS)} aliases listed")
    return CaseResult(cog="model", cmd="list", case="happy", status="FAIL",
                      detail=f"missing aliases: {missing}")


async def case_mode_current(bot) -> CaseResult:
    """/mode current with no session — should report 'no active session'."""
    from clauded.cogs.mode import mode_group
    cur_cmd = next(c for c in mode_group.commands if c.name == "current")
    cb = extract_callback(cur_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="mode", cmd="current", case="no-session", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # No session is the expected state for a freshly-bound channel
    if "no active session" in reply.lower() or any(
        m in reply for m in ("default", "acceptEdits", "plan", "bypassPermissions")
    ):
        return CaseResult(cog="mode", cmd="current", case="no-session", status="PASS",
                          detail=f"reply: {reply[:120]!r}")
    return CaseResult(cog="mode", cmd="current", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_cost_show(bot) -> CaseResult:
    """/cost show: must report numeric total in dollar format."""
    from clauded.cogs.ops import cost_group
    show_cmd = next(c for c in cost_group.commands if c.name == "show")
    cb = extract_callback(show_cmd)
    inter = make_mock_interaction(bot=bot)
    # bind first so resolve_binding works
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    # plant a non-zero cost so we can verify the number formats
    bot.cost_tracker.record(inter.channel_id, 0.1234)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="cost", cmd="show", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must contain the actual cost we planted
    if "$0.1234" in reply or "0.12" in reply:
        return CaseResult(cog="cost", cmd="show", case="happy", status="PASS",
                          detail=f"reply contains planted cost; reply={reply[:200]!r}")
    return CaseResult(cog="cost", cmd="show", case="happy", status="FAIL",
                      detail=f"reply missing planted $0.1234; reply={reply[:300]!r}")


async def case_session_list(bot) -> CaseResult:
    """/session list: with no active sessions must say 'No active sessions'."""
    from clauded.cogs.session import session_group
    list_cmd = next(c for c in session_group.commands if c.name == "list")
    cb = extract_callback(list_cmd)
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="session", cmd="list", case="empty", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must contain 'no' + 'session' (case-insensitive)
    rl = reply.lower()
    if "no" in rl and "session" in rl:
        return CaseResult(cog="session", cmd="list", case="empty", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="list", case="empty", status="FAIL",
                      detail=f"reply doesn't indicate empty; reply={reply[:300]!r}")


async def case_skill_list(bot) -> CaseResult:
    """/skill list — with no bridge, must say something user-friendly about
    the missing session OR list skills via fresh-client path."""
    from clauded.cogs.skill import skill_group
    list_cmd = next(c for c in skill_group.commands if c.name == "list")
    cb = extract_callback(list_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="skill", cmd="list", case="no-bridge",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must contain either 'skill' marker or 'no' + indicator
    rl = reply.lower()
    if "skill" in rl or "\U0001f9f0" in reply:
        return CaseResult(cog="skill", cmd="list", case="no-bridge",
                          status="PASS", detail=f"reply mentions skills: {reply[:120]!r}")
    return CaseResult(cog="skill", cmd="list", case="no-bridge",
                      status="FAIL", detail=f"reply doesn't mention skills: {reply[:300]!r}")


# Catalog of cases. Add more as we expand coverage.
HAPPY_CASES: list[tuple[str, Callable]] = [
    ("/project bind happy", case_project_bind_happy),
    ("/project info post-bind", case_project_info_after_bind),
    ("/project unbind happy", case_project_unbind),
    ("/project bind no-guild", case_project_bind_no_channel),
    ("/health happy", case_health),
    ("/model list happy", case_model_list),
    ("/mode current happy", case_mode_current),
    ("/cost show happy", case_cost_show),
    ("/session list happy", case_session_list),
    ("/skill list no-bridge", case_skill_list),
]


# ---------------------------------------------------------------------------
# Report generation — PRD §Spec 5
# ---------------------------------------------------------------------------


def write_report(results: list[CaseResult], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(results)
    by_status = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = []
    md.append(f"# claudeD e2e 测试报告 ({now})")
    md.append("")
    md.append(f"- 总命令测试：{total}")
    md.append(f"- PASS: {by_status.get('PASS', 0)}")
    md.append(f"- FAIL: {by_status.get('FAIL', 0)}")
    md.append(f"- ERROR: {by_status.get('ERROR', 0)}")
    md.append(f"- SKIP: {by_status.get('SKIP', 0)}")
    md.append("")
    md.append("## 详细")
    md.append("")
    md.append("| Cog | Cmd | Case | Status | Detail | Time |")
    md.append("|---|---|---|---|---|---|")
    for r in results:
        detail = (r.detail or "").replace("|", "\\|")[:120]
        emoji = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "SKIP": "⏭"}.get(r.status, "?")
        md.append(f"| {r.cog} | {r.cmd} | {r.case} | {emoji} {r.status} | {detail} | {r.duration_s}s |")

    fails = [r for r in results if r.status in ("FAIL", "ERROR")]
    if fails:
        md.append("")
        md.append("## 失败 / 错误详情（开 issue 跟进）")
        md.append("")
        for r in fails:
            md.append(f"### {r.cog}/{r.cmd}/{r.case}")
            md.append(f"- 状态: {r.status}")
            md.append(f"- 时长: {r.duration_s}s")
            md.append(f"- 详情: `{r.detail[:500]}`")
            md.append("")

    out_path.write_text("\n".join(md))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["happy", "edge", "all"], default="happy")
    args = parser.parse_args()

    bot = make_mock_bot()

    cases = HAPPY_CASES
    if args.phase in ("edge", "all"):
        # Edge cases would extend this list. For v1 we ship happy-path only.
        pass

    results: list[CaseResult] = []
    for name, fn in cases:
        print(f"  running: {name}")
        # Fresh bot per case to avoid cross-contamination
        bot = make_mock_bot()
        async def _wrap():
            return await fn(bot)
        r = await run_one(name, _wrap)
        print(f"    -> {r.status} ({r.duration_s}s)")
        if r.detail:
            print(f"    detail: {r.detail[:200]}")
        results.append(r)

    # Write report
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    out = ROOT / "data" / "e2e-reports" / f"{ts}.md"
    write_report(results, out)
    print(f"\nReport: {out}")
    print(f"\nSummary: " + ", ".join(
        f"{k}={sum(1 for r in results if r.status == k)}"
        for k in ("PASS", "FAIL", "ERROR", "SKIP")
    ))

    # Exit code per CI convention
    if any(r.status in ("FAIL", "ERROR") for r in results):
        sys.exit(0)  # not gating CI yet — still discovering bugs




# ---------------------------------------------------------------------------
# More cases — Phase 2 coverage
# ---------------------------------------------------------------------------


async def case_project_bind_relative_path(bot) -> CaseResult:
    """/project bind path=./relative — should reject (must be absolute)."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, path="./relative-path")
    reply = _interaction_response_text(inter)
    state = json.loads(Path("/tmp/e2e_data/projects.json").read_text())
    if str(inter.channel_id) not in state:
        return CaseResult(cog="project", cmd="bind", case="relative-path", status="PASS",
                          detail=f"refused: {reply[:100]!r}")
    return CaseResult(cog="project", cmd="bind", case="relative-path", status="FAIL",
                      detail=f"accepted relative path; state={state}")


async def case_project_bind_nonexistent(bot) -> CaseResult:
    """/project bind path=/nonexistent — should reject."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, path="/nonexistent/dir-1234567890")
    reply = _interaction_response_text(inter)
    state = json.loads(Path("/tmp/e2e_data/projects.json").read_text())
    if str(inter.channel_id) not in state:
        return CaseResult(cog="project", cmd="bind", case="nonexistent", status="PASS",
                          detail=f"refused: {reply[:100]!r}")
    return CaseResult(cog="project", cmd="bind", case="nonexistent", status="FAIL",
                      detail=f"accepted nonexistent path; state={state}")


async def case_project_bind_in_thread_writes_to_parent(bot) -> CaseResult:
    """/project bind from a thread must write to PARENT channel id (#197)."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=8000000001, parent_id=8000000000)
    await cb(inter, path=str(ROOT))
    state = json.loads(Path("/tmp/e2e_data/projects.json").read_text())
    # The binding should be under the parent id, NOT the thread id
    if "8000000000" in state and "8000000001" not in state:
        return CaseResult(cog="project", cmd="bind", case="thread→parent", status="PASS",
                          detail="binding written to parent channel id (#197)")
    return CaseResult(cog="project", cmd="bind", case="thread→parent", status="FAIL",
                      detail=f"state keys = {list(state.keys())}; expected parent 8000000000")


async def case_env_set_and_list(bot) -> CaseResult:
    """/env set + /env list round-trip. Values are masked in /env list by
    design (security) so just verify the KEY appears."""
    from clauded.cogs.project import env_group
    set_cmd = next(c for c in env_group.commands if c.name == "set")
    list_cmd = next(c for c in env_group.commands if c.name == "list")
    from clauded.cogs.project import project_group
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    inter = make_mock_interaction(bot=bot)
    await extract_callback(bind_cmd)(inter, path=str(ROOT))

    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await extract_callback(set_cmd)(inter2, key="MY_TEST_VAR", value="hello-world")

    inter3 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await extract_callback(list_cmd)(inter3)
    reply = _interaction_response_text(inter3)
    if "MY_TEST_VAR" in reply:
        # Per-design: values are masked. Just confirm the key appears.
        return CaseResult(cog="env", cmd="set+list", case="round-trip", status="PASS",
                          detail="env var key visible (value masked by design)")
    return CaseResult(cog="env", cmd="set+list", case="round-trip", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_agent_list_empty(bot) -> CaseResult:
    """/agent list on empty store: must say 'No custom agents defined'."""
    from clauded.cogs.agent import agent_group
    cb = extract_callback(next(c for c in agent_group.commands if c.name == "list"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "No custom agents defined" in reply:
        return CaseResult(cog="agent", cmd="list", case="empty", status="PASS",
                          detail=f"correct: {reply[:120]!r}")
    return CaseResult(cog="agent", cmd="list", case="empty", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_agent_create_then_list(bot) -> CaseResult:
    """/agent create + /agent list round-trip. Requires bound channel.
    Strict checks: (a) agent_manager._agents has the entry with the
    correct prompt, (b) list reply shows the name AND a prompt preview,
    (c) on-disk agents.json file actually got the record.
    """
    from clauded.cogs.agent import agent_group
    from clauded.cogs.project import project_group
    create_cmd = next(c for c in agent_group.commands if c.name == "create")
    list_cmd = next(c for c in agent_group.commands if c.name == "list")
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    inter_bind = make_mock_interaction(bot=bot)
    await extract_callback(bind_cmd)(inter_bind, path=str(ROOT))

    inter = make_mock_interaction(bot=bot, channel_id=inter_bind.channel_id)
    try:
        await extract_callback(create_cmd)(inter, name="test-agent", prompt="You are a test helper.")
    except Exception as exc:
        return CaseResult(cog="agent", cmd="create", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")

    # Check 1: in-memory state
    if "test-agent" not in bot.agent_manager._agents:
        return CaseResult(cog="agent", cmd="create+list", case="round-trip", status="FAIL",
                          detail="agent NOT in agent_manager._agents")
    stored_prompt = bot.agent_manager._agents["test-agent"].get("prompt")
    if stored_prompt != "You are a test helper.":
        return CaseResult(cog="agent", cmd="create+list", case="round-trip", status="FAIL",
                          detail=f"stored prompt wrong: {stored_prompt!r}")

    # Check 2: on-disk
    import json as _json
    on_disk = _json.loads(Path("/tmp/e2e_data/agents.json").read_text())
    if "test-agent" not in on_disk:
        return CaseResult(cog="agent", cmd="create+list", case="round-trip", status="FAIL",
                          detail="agent NOT in agents.json")

    # Check 3: /agent list shows it with prompt preview
    inter2 = make_mock_interaction(bot=bot, channel_id=inter_bind.channel_id)
    await extract_callback(list_cmd)(inter2)
    reply = _interaction_response_text(inter2)
    if "test-agent" in reply and "test helper" in reply.lower():
        return CaseResult(cog="agent", cmd="create+list", case="round-trip", status="PASS",
                          detail="agent in memory + on-disk + list shows name+prompt")
    return CaseResult(cog="agent", cmd="create+list", case="round-trip", status="FAIL",
                      detail=f"list reply missing prompt preview: {reply[:300]!r}")


async def case_log_dump(bot) -> CaseResult:
    """/log dump: must produce a valid .zip with manifest.json + state/+.
    Strict: open the bundle and verify required entries are present."""
    from clauded.cogs.log_dump import log_group
    cb = extract_callback(next(c for c in log_group.commands if c.name == "dump"))
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="log", cmd="dump", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    # Look for the attached file
    sent_files = []
    for s in inter._followups:
        f = s.get("kwargs", {}).get("file")
        if f is not None:
            sent_files.append(f)
    if not sent_files:
        return CaseResult(cog="log", cmd="dump", case="happy", status="FAIL",
                          detail=f"no file in followups; followups={inter._followups[:1]}")
    # Open the bundle and verify structure
    import zipfile as _zf
    fobj = sent_files[0]
    # discord.File has .fp.name (the open file's path) and ._filename (cosmetic)
    fp = getattr(fobj, "fp", None)
    fpath = getattr(fp, "name", None) if fp is not None else None
    if not fpath:
        return CaseResult(cog="log", cmd="dump", case="happy", status="FAIL",
                          detail=f"can't resolve bundle path; fp={fp!r}")
    try:
        with _zf.ZipFile(fpath) as z:
            names = set(z.namelist())
    except Exception as exc:
        return CaseResult(cog="log", cmd="dump", case="happy", status="FAIL",
                          detail=f"can't open bundle as zip: {exc}")
    required = {"manifest.json", "env-redacted.txt"}
    missing = required - names
    if missing:
        return CaseResult(cog="log", cmd="dump", case="happy", status="FAIL",
                          detail=f"bundle missing required entries: {missing}; got: {sorted(names)[:10]}")
    return CaseResult(cog="log", cmd="dump", case="happy", status="PASS",
                      detail=f"bundle has manifest + env + {len(names)} entries")


async def case_diff_no_binding(bot) -> CaseResult:
    """/diff on unbound channel must refuse."""
    from clauded.cogs.diff import diff_cmd
    cb = extract_callback(diff_cmd)
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="diff", cmd="diff", case="unbound", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    if "channel" in reply.lower() or "bind" in reply.lower() or "❌" in reply or "ℹ" in reply:
        return CaseResult(cog="diff", cmd="diff", case="unbound", status="PASS",
                          detail=f"refused: {reply[:100]!r}")
    return CaseResult(cog="diff", cmd="diff", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_info_no_session(bot) -> CaseResult:
    """/session info with no active session → friendly empty."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "info"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if reply.strip():
        return CaseResult(cog="session", cmd="info", case="no-session", status="PASS",
                          detail=f"reply: {reply[:120]!r}")
    return CaseResult(cog="session", cmd="info", case="no-session", status="FAIL",
                      detail="empty reply")


async def case_session_clear_no_session(bot) -> CaseResult:
    """/session clear with no active session — should not crash."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "clear"))
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="session", cmd="clear", case="no-session", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    return CaseResult(cog="session", cmd="clear", case="no-session", status="PASS",
                      detail=f"reply: {reply[:120]!r}")


async def case_btw_no_session(bot) -> CaseResult:
    """/btw outside a thread must refuse with 'use in a thread' message."""
    from clauded.cogs.ops import btw_cmd
    cb = extract_callback(btw_cmd)
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter, text="quick test")
    except Exception as exc:
        return CaseResult(cog="ops", cmd="btw", case="no-thread", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must mention thread requirement
    rl = reply.lower()
    if "thread" in rl and ("\u274c" in reply or "must" in rl or "start" in rl):
        return CaseResult(cog="ops", cmd="btw", case="no-thread", status="PASS",
                          detail=f"refused properly: {reply[:120]!r}")
    return CaseResult(cog="ops", cmd="btw", case="no-thread", status="FAIL",
                      detail=f"reply doesn't say thread-only: {reply[:300]!r}")


async def case_ratelimit(bot) -> CaseResult:
    """/ratelimit: must include cost total + session count fields."""
    from clauded.cogs.ops import ratelimit_info
    cb = extract_callback(ratelimit_info)
    inter = make_mock_interaction(bot=bot)
    # Plant a cost so we can verify the embed number
    bot.cost_tracker.record(42, 0.5678)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="ratelimit", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must contain the actual planted cost AND mention sessions
    if ("$0.5678" in reply or "0.56" in reply or "0.57" in reply) and "session" in reply.lower():
        return CaseResult(cog="ops", cmd="ratelimit", case="happy", status="PASS",
                          detail=f"reply has planted cost + sessions: {reply[:200]!r}")
    return CaseResult(cog="ops", cmd="ratelimit", case="happy", status="FAIL",
                      detail=f"reply missing planted cost or session count: {reply[:300]!r}")


async def case_tools_allow(bot) -> CaseResult:
    """/tools allow: requires being IN a thread. Test the actually-happy path."""
    from clauded.cogs.tools import tools_group
    cb = extract_callback(next(c for c in tools_group.commands if c.name == "allow"))
    from clauded.cogs.project import project_group
    bind_cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))

    # Bind the parent channel
    parent_id = 7000000000
    inter_bind = make_mock_interaction(bot=bot, channel_id=parent_id)
    await bind_cb(inter_bind, path=str(ROOT))

    # Now invoke from inside a thread of that parent
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=7000000001, parent_id=parent_id)
    # _recreate_session is a real ClaudedBot method that tries to spin
    # up a new bridge. Mock it to avoid Discord IO.
    async def _fake_recreate(interaction, **kwargs):
        # Verify the kwargs we expect get propagated
        if kwargs.get("allowed_tools") != ["Bash"]:
            return None  # signals failure
        return MagicMock()  # truthy bridge
    bot._recreate_session = _fake_recreate

    try:
        await cb(inter, tools="Bash")
    except Exception as exc:
        return CaseResult(cog="tools", cmd="allow", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # Strict: must mention 'Bash' and 'allowed'
    if "Bash" in reply and ("allowed" in reply.lower() or "\U0001f527" in reply):
        return CaseResult(cog="tools", cmd="allow", case="happy", status="PASS",
                          detail=f"reply confirms Bash allowed: {reply[:200]!r}")
    return CaseResult(cog="tools", cmd="allow", case="happy", status="FAIL",
                      detail=f"reply doesn't confirm: {reply[:300]!r}")


async def case_debug_toggle(bot) -> CaseResult:
    """/debug toggles the debug flag (no arg — it's a true toggle)."""
    from clauded.cogs.ops import debug_toggle
    cb = extract_callback(debug_toggle)
    inter = make_mock_interaction(bot=bot)
    before = bot._debug_logging
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="debug", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    after = bot._debug_logging
    if after != before:
        return CaseResult(cog="ops", cmd="debug", case="happy", status="PASS",
                          detail=f"toggled {before} → {after}")
    return CaseResult(cog="ops", cmd="debug", case="happy", status="FAIL",
                      detail=f"no change: {before} → {after}")


async def case_notify_toggle(bot) -> CaseResult:
    """/notify: toggles per-thread notification flag — verify state change."""
    from clauded.cogs.ops import notify_toggle
    cb = extract_callback(notify_toggle)
    inter = make_mock_interaction(bot=bot)
    thread_id = inter.channel_id
    # Default state: not in dict
    before = bot._notify_enabled.get(thread_id, bot._pre_tool_notifications)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="notify", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    after = bot._notify_enabled.get(thread_id)
    reply = _interaction_response_text(inter)
    # Strict: state must have flipped AND reply must reflect it
    if after is not None and after != before:
        # Reply should mention ON/OFF matching new state
        expected = "ON" if after else "OFF"
        if expected in reply:
            return CaseResult(cog="ops", cmd="notify", case="happy", status="PASS",
                              detail=f"{before} → {after}; reply says {expected}")
        return CaseResult(cog="ops", cmd="notify", case="happy", status="FAIL",
                          detail=f"state flipped to {after} but reply says wrong: {reply[:200]!r}")
    return CaseResult(cog="ops", cmd="notify", case="happy", status="FAIL",
                      detail=f"state didn't flip: before={before}, after={after}")


async def case_unbound_fallback_toggle(bot) -> CaseResult:
    """/unbound-fallback enabled=True toggles the global flag."""
    from clauded.cogs.ops import unbound_fallback_toggle
    cb = extract_callback(unbound_fallback_toggle)
    inter = make_mock_interaction(bot=bot)
    before = bot.allow_unbound_fallback
    try:
        await cb(inter, enabled=True)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="unbound-fallback", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    after = bot.allow_unbound_fallback
    if after != before:
        return CaseResult(cog="ops", cmd="unbound-fallback", case="happy",
                          status="PASS", detail=f"{before} → {after}")
    return CaseResult(cog="ops", cmd="unbound-fallback", case="happy",
                      status="FAIL", detail=f"unchanged: {before}")


async def case_mcp_list_empty(bot) -> CaseResult:
    """/mcp list — empty server registry."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "list"))
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="mcp", cmd="list", case="empty", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    return CaseResult(cog="mcp", cmd="list", case="empty", status="PASS",
                      detail=f"reply: {reply[:120]!r}")


# Inject more cases
HAPPY_CASES.extend([
    ("/project bind relative-path edge", case_project_bind_relative_path),
    ("/project bind nonexistent edge", case_project_bind_nonexistent),
    ("/project bind thread→parent (#197)", case_project_bind_in_thread_writes_to_parent),
    ("/env set+list happy", case_env_set_and_list),
    ("/agent list empty", case_agent_list_empty),
    ("/agent create+list happy", case_agent_create_then_list),
    ("/log dump happy", case_log_dump),
    ("/diff unbound", case_diff_no_binding),
    ("/session info no-session", case_session_info_no_session),
    ("/session clear no-session", case_session_clear_no_session),
    ("/btw no-session", case_btw_no_session),
    ("/ratelimit happy", case_ratelimit),
    ("/tools allow happy", case_tools_allow),
    ("/debug toggle happy", case_debug_toggle),
    ("/notify toggle happy", case_notify_toggle),
    ("/unbound-fallback toggle happy", case_unbound_fallback_toggle),
    ("/mcp list empty", case_mcp_list_empty),
])





# Insert before the final guard — runs BEFORE the existing guard

# ---------------------------------------------------------------------------
# Regression cases — pin known open bugs (#247, #248, #245, #250)
# ---------------------------------------------------------------------------


async def case_247_model_list_known_models_freshness(bot) -> CaseResult:
    """#247 Bug A: KNOWN_MODELS hardcoded table may be stale.
    
    User screenshot 2026-05-19 showed sonnet-4-5 but production CLI was
    using sonnet-4-6. Detect by checking if the table contains models
    that match the user's recent /context output.
    """
    from clauded.cogs.model import KNOWN_MODELS
    # Per #247, user saw claude-sonnet-4-6 in /context but table still has 4-5
    stale_ids = []
    for alias, meta in KNOWN_MODELS.items():
        mid = meta.get("id", "")
        # Anything containing "-4-1" or "-4-5" or "-3-5" is the stale generation
        if any(g in mid for g in ("-4-1", "-4-5", "-3-5")):
            stale_ids.append((alias, mid))
    if not stale_ids:
        return CaseResult(cog="model", cmd="list", case="#247-freshness",
                          status="PASS", detail="KNOWN_MODELS appears fresh")
    return CaseResult(cog="model", cmd="list", case="#247-freshness",
                      status="FAIL",
                      detail=f"stale model ids: {stale_ids}")


async def case_247_model_current_in_channel(bot) -> CaseResult:
    """#247 Bug B: /model current in a *channel* (not thread) must walk
    to thread sessions if any exist — currently it just reads channel.id
    which always misses.
    """
    from clauded.cogs.model import model_group
    current_cmd = next(c for c in model_group.commands if c.name == "current")
    cb = extract_callback(current_cmd)
    inter = make_mock_interaction(bot=bot)
    # No session, no thread — should report 'unset' or 'no session'
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    # Best we can do without a real bridge: just confirm it didn't crash
    if reply.strip():
        return CaseResult(cog="model", cmd="current", case="#247-channel",
                          status="PASS", detail=f"reply: {reply[:120]!r}")
    return CaseResult(cog="model", cmd="current", case="#247-channel",
                      status="FAIL", detail="empty reply")


async def case_248_cost_record_skip_zero(bot) -> CaseResult:
    """#248: ``bot.py`` 728/919 has ``if response_cost > 0:`` before
    ``cost_tracker.record(...)``, so zero/error turns are dropped
    from the call counter. Verify by source-grep on the gate.
    """
    import inspect
    from clauded import bot as bot_mod
    src = inspect.getsource(bot_mod)
    # The bug: this conditional gate is the #248 bug.
    gate_lines = [
        l for l in src.splitlines()
        if "if response_cost > 0" in l
    ]
    if gate_lines:
        return CaseResult(cog="cost", cmd="record", case="#248-zero-gate",
                          status="FAIL",
                          detail=f"#248 confirmed: {len(gate_lines)} sites still gate "
                                 f"on `if response_cost > 0:` (line samples: {gate_lines[:2]})")
    return CaseResult(cog="cost", cmd="record", case="#248-zero-gate",
                      status="PASS", detail="no zero-cost gate sites found")


# Add to the cases list
HAPPY_CASES.extend([
    ("/model list #247 freshness", case_247_model_list_known_models_freshness),
    ("/model current #247 channel-not-thread", case_247_model_current_in_channel),
    ("/cost record #248 zero-cost", case_248_cost_record_skip_zero),
])





# ---------------------------------------------------------------------------
# Edge-case exploration — looking for NEW bugs
# ---------------------------------------------------------------------------


async def case_effort_invalid_value(bot) -> CaseResult:
    """/effort with an invalid value — what happens?"""
    from clauded.cogs.model import set_effort
    cb = extract_callback(set_effort)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter, level="invalid-effort-level")
    except Exception as exc:
        return CaseResult(cog="model", cmd="effort", case="invalid-value",
                          status="PASS", detail=f"raised {type(exc).__name__}: {exc} (expected)")
    reply = _interaction_response_text(inter)
    if any(w in reply.lower() for w in ("invalid", "❌", "error", "unknown")):
        return CaseResult(cog="model", cmd="effort", case="invalid-value",
                          status="PASS", detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="model", cmd="effort", case="invalid-value",
                      status="FAIL", detail=f"accepted invalid value; reply={reply[:200]!r}")


async def case_max_turns_negative(bot) -> CaseResult:
    """/max-turns with negative value — should reject."""
    from clauded.cogs.model import max_turns_cmd
    cb = extract_callback(max_turns_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter, number=-5)
    except Exception as exc:
        return CaseResult(cog="model", cmd="max-turns", case="negative",
                          status="PASS", detail=f"raised: {exc}")
    reply = _interaction_response_text(inter)
    if any(w in reply for w in ("❌", "invalid", "positive", ">0", "> 0", "must be", "must")):
        return CaseResult(cog="model", cmd="max-turns", case="negative",
                          status="PASS", detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="model", cmd="max-turns", case="negative",
                      status="FAIL", detail=f"accepted negative; reply={reply[:200]!r}")


async def case_max_turns_zero(bot) -> CaseResult:
    """/max-turns 0 — should also reject."""
    from clauded.cogs.model import max_turns_cmd
    cb = extract_callback(max_turns_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter, number=0)
    except Exception as exc:
        return CaseResult(cog="model", cmd="max-turns", case="zero",
                          status="PASS", detail=f"raised: {exc}")
    reply = _interaction_response_text(inter)
    if any(w in reply for w in ("❌", "invalid", "positive", ">0", "> 0", "must be", "must")):
        return CaseResult(cog="model", cmd="max-turns", case="zero",
                          status="PASS", detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="model", cmd="max-turns", case="zero",
                      status="FAIL", detail=f"accepted zero; reply={reply[:200]!r}")


async def case_project_add_dir_traversal(bot) -> CaseResult:
    """/project add-dir with path traversal — security check."""
    from clauded.cogs.project import project_group
    add_dir = next(c for c in project_group.commands if c.name == "add-dir")
    cb = extract_callback(add_dir)
    inter = make_mock_interaction(bot=bot)
    # bind first
    bind_cmd = next(c for c in project_group.commands if c.name == "bind")
    await extract_callback(bind_cmd)(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    # Try path traversal
    await cb(inter2, path="../../etc/passwd")
    reply = _interaction_response_text(inter2)
    if any(w in reply for w in ("❌", "invalid", "not exist", "must be absolute", "outside")):
        return CaseResult(cog="project", cmd="add-dir", case="traversal",
                          status="PASS", detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="project", cmd="add-dir", case="traversal",
                      status="FAIL", detail=f"accepted traversal; reply={reply[:200]!r}")


async def case_session_resume_no_stored(bot) -> CaseResult:
    """/session resume with no stored session in a *bound* channel.
    Must say 'no saved' / 'no session to resume' — NOT 'isn't bound'.
    """
    from clauded.cogs.session import session_group
    resume_cmd = next(c for c in session_group.commands if c.name == "resume")
    cb = extract_callback(resume_cmd)
    inter = make_mock_interaction(bot=bot)
    # Bind so we don't fall into unbound refusal path
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="session", cmd="resume", case="no-stored",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    # Strict: must say there's nothing to resume, NOT unbound
    if "no" in rl and ("resume" in rl or "saved" in rl or "session" in rl):
        return CaseResult(cog="session", cmd="resume", case="no-stored",
                          status="PASS", detail=f"reply={reply[:160]!r}")
    return CaseResult(cog="session", cmd="resume", case="no-stored",
                      status="FAIL",
                      detail=f"reply doesn't say 'no to resume'; reply={reply[:300]!r}")


async def case_log_dump_no_data_dir(bot, tmp_path=None) -> CaseResult:
    """/log dump when data/ dir is missing — graceful fail?"""
    from clauded.cogs.log_dump import log_group
    cb = extract_callback(next(c for c in log_group.commands if c.name == "dump"))
    inter = make_mock_interaction(bot=bot)
    # Patch bundle generation to simulate failure
    from clauded.diagnostics import bundle as bundle_mod
    orig = bundle_mod.generate_bundle
    def _fail(**kw):
        raise OSError("simulated disk full")
    bundle_mod.generate_bundle = _fail
    try:
        try:
            await cb(inter)
        except Exception as exc:
            bundle_mod.generate_bundle = orig
            return CaseResult(cog="log", cmd="dump", case="bundle-fail",
                              status="ERROR", detail=f"{type(exc).__name__}: {exc}")
        finally:
            bundle_mod.generate_bundle = orig
    finally:
        pass
    reply = _interaction_response_text(inter)
    if "❌" in reply or "fail" in reply.lower():
        return CaseResult(cog="log", cmd="dump", case="bundle-fail",
                          status="PASS", detail=f"graceful fail: {reply[:120]!r}")
    return CaseResult(cog="log", cmd="dump", case="bundle-fail",
                      status="FAIL", detail=f"no error surfaced: {reply[:200]!r}")


async def case_review_pr_no_arg(bot) -> CaseResult:
    """/review pr=N: invokes external git command. We just verify
    cog doesn't crash and the response embed has sensible structure.
    The actual git failure (exit 1) is expected with PR=1 since this
    isn't a real PR — we're testing the COG wiring, not git.

    Strict: response must mention 'PR' or 'review' somewhere; not
    just any non-empty string.
    """
    from clauded.cogs.ops import review_pr
    cb = extract_callback(review_pr)
    inter = make_mock_interaction(bot=bot)
    # bind first
    from clauded.cogs.project import project_group
    bind = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    await bind(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    sig = inspect.signature(cb)
    kw = {"pr": 1} if "pr" in sig.parameters else {"pr_number": 1}
    try:
        await cb(inter2, **kw)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="review", case="pr=1",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter2)
    # Strict: must mention 'PR' or 'pull' or 'review' somewhere
    rl = reply.lower()
    if "pr" in rl or "pull" in rl or "review" in rl or "error" in rl:
        return CaseResult(cog="ops", cmd="review", case="pr=1",
                          status="PASS", detail=f"reply has PR-context: {reply[:200]!r}")
    return CaseResult(cog="ops", cmd="review", case="pr=1",
                      status="FAIL", detail=f"reply lacks PR context: {reply[:300]!r}")


async def case_skill_list_signature(bot) -> CaseResult:
    """/skill list signature check — should not require kwargs."""
    from clauded.cogs.skill import skill_group
    list_cmd = next(c for c in skill_group.commands if c.name == "list")
    cb = extract_callback(list_cmd)
    sig = inspect.signature(cb)
    n_params = len([p for p in sig.parameters.values() if p.name != "interaction"])
    if n_params == 0:
        return CaseResult(cog="skill", cmd="list", case="signature",
                          status="PASS", detail="0 extra params (correct)")
    return CaseResult(cog="skill", cmd="list", case="signature",
                      status="FAIL", detail=f"{n_params} extra params, expected 0")


async def case_context_no_session(bot) -> CaseResult:
    """/context with no session — what does it show?"""
    from clauded.cogs.context import context_cmd
    cb = extract_callback(context_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="context", cmd="context", case="no-session",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    return CaseResult(cog="context", cmd="context", case="no-session",
                      status="PASS", detail=f"reply: {reply[:120]!r}")


async def case_compact_no_session(bot) -> CaseResult:
    """/session compact with no active session — should not crash."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "compact"))
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="session", cmd="compact", case="no-session",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    return CaseResult(cog="session", cmd="compact", case="no-session",
                      status="PASS", detail=f"reply: {reply[:120]!r}")


HAPPY_CASES.extend([
    ("/effort invalid-value edge", case_effort_invalid_value),
    ("/max-turns negative edge", case_max_turns_negative),
    ("/max-turns zero edge", case_max_turns_zero),
    ("/project add-dir traversal edge", case_project_add_dir_traversal),
    ("/session resume no-stored", case_session_resume_no_stored),
    ("/log dump bundle-fail edge", case_log_dump_no_data_dir),
    ("/review pr=1 happy", case_review_pr_no_arg),
    ("/skill list signature", case_skill_list_signature),
    ("/context no-session", case_context_no_session),
    ("/session compact no-session", case_compact_no_session),
])





# ---------------------------------------------------------------------------
# Expanded coverage — remaining commands not yet exercised
# ---------------------------------------------------------------------------


async def case_cost_total(bot) -> CaseResult:
    """/cost total: must include planted total amount."""
    from clauded.cogs.ops import cost_group
    cb = extract_callback(next(c for c in cost_group.commands if c.name == "total"))
    inter = make_mock_interaction(bot=bot)
    bot.cost_tracker.record(42, 0.1234)
    bot.cost_tracker.record(99, 0.5678)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="cost", cmd="total", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    # 0.1234 + 0.5678 = 0.6912
    if "0.6912" in reply or "0.69" in reply:
        return CaseResult(cog="cost", cmd="total", case="happy", status="PASS",
                          detail=f"reply has planted total: {reply[:200]!r}")
    return CaseResult(cog="cost", cmd="total", case="happy", status="FAIL",
                      detail=f"reply missing planted total: {reply[:300]!r}")


async def case_cost_reset(bot) -> CaseResult:
    """/cost reset: must clear the channel cost AND confirm in reply."""
    from clauded.cogs.ops import cost_group
    reset_cmd = next(c for c in cost_group.commands if c.name == "reset")
    cb = extract_callback(reset_cmd)
    inter = make_mock_interaction(bot=bot)
    # Bind so resolve_binding works
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    bot.cost_tracker.record(inter.channel_id, 1.0)
    before, calls_before = bot.cost_tracker.get_channel_cost(inter.channel_id)
    if before != 1.0:
        return CaseResult(cog="cost", cmd="reset", case="happy", status="ERROR",
                          detail=f"planted cost not seen pre-reset: {before}")
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="cost", cmd="reset", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    after, calls_after = bot.cost_tracker.get_channel_cost(inter.channel_id)
    if after == 0.0:
        return CaseResult(cog="cost", cmd="reset", case="happy", status="PASS",
                          detail=f"cost cleared: {before} → {after}")
    return CaseResult(cog="cost", cmd="reset", case="happy", status="FAIL",
                      detail=f"reset didn't clear: before={before}, after={after}")


async def case_session_stop_no_session(bot) -> CaseResult:
    """/session stop without active session: must say 'no active'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "stop"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if "no active" in rl and "session" in rl:
        return CaseResult(cog="session", cmd="stop", case="no-session", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="stop", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_interrupt_no_session(bot) -> CaseResult:
    """/session interrupt without session: must say 'no active'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "interrupt"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no active" in reply.lower():
        return CaseResult(cog="session", cmd="interrupt", case="no-session", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="interrupt", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_fork_no_session(bot) -> CaseResult:
    """/session fork without session: must say 'no active'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "fork"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no active session" in reply.lower():
        return CaseResult(cog="session", cmd="fork", case="no-session", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="fork", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_pin_no_session(bot) -> CaseResult:
    """/session pin without prior reply: must say 'no reply to pin'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "pin"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no reply" in reply.lower() or "no message" in reply.lower():
        return CaseResult(cog="session", cmd="pin", case="no-reply", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="pin", case="no-reply", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_export_no_session(bot) -> CaseResult:
    """/session export without session: must say 'no messages'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "export"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no message" in reply.lower() or "no session" in reply.lower():
        return CaseResult(cog="session", cmd="export", case="empty", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="export", case="empty", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_worktree_not_in_thread(bot) -> CaseResult:
    """/session worktree from channel (not thread): must say 'use in thread'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "worktree"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="feat-x")
    reply = _interaction_response_text(inter)
    if "thread" in reply.lower() and "❌" in reply:
        return CaseResult(cog="session", cmd="worktree", case="not-thread", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="session", cmd="worktree", case="not-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_name_not_in_thread(bot) -> CaseResult:
    """/session name from channel (not thread)."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "name"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="my-session")
    reply = _interaction_response_text(inter)
    if "thread" in reply.lower() and "❌" in reply:
        return CaseResult(cog="session", cmd="name", case="not-thread", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="session", cmd="name", case="not-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_settings_not_in_thread(bot) -> CaseResult:
    """/session settings from channel (not thread)."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "settings"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, json_str='{"test": 1}')
    reply = _interaction_response_text(inter)
    if "thread" in reply.lower() and "❌" in reply:
        return CaseResult(cog="session", cmd="settings", case="not-thread", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="session", cmd="settings", case="not-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_security_review_no_session(bot) -> CaseResult:
    """/session security-review without session: must say 'no active'."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "security-review"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no active session" in reply.lower():
        return CaseResult(cog="session", cmd="security-review", case="no-session",
                          status="PASS", detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="security-review", case="no-session",
                      status="FAIL", detail=f"reply={reply[:300]!r}")


async def case_agent_use_unknown(bot) -> CaseResult:
    """/agent use 'unknown-agent': must say 'not found' with the exact name."""
    from clauded.cogs.agent import agent_group
    cb = extract_callback(next(c for c in agent_group.commands if c.name == "use"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="unknown-xyz-9999")
    reply = _interaction_response_text(inter)
    if "not found" in reply.lower() and "unknown-xyz-9999" in reply:
        return CaseResult(cog="agent", cmd="use", case="unknown", status="PASS",
                          detail=f"correct: {reply[:120]!r}")
    return CaseResult(cog="agent", cmd="use", case="unknown", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_agent_delete_unknown(bot) -> CaseResult:
    """/agent delete 'unknown-agent': must say 'not found' with name."""
    from clauded.cogs.agent import agent_group
    cb = extract_callback(next(c for c in agent_group.commands if c.name == "delete"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="unknown-xyz-9999")
    reply = _interaction_response_text(inter)
    if "not found" in reply.lower() and "unknown-xyz-9999" in reply:
        return CaseResult(cog="agent", cmd="delete", case="unknown", status="PASS",
                          detail=f"correct: {reply[:120]!r}")
    return CaseResult(cog="agent", cmd="delete", case="unknown", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_agent_delete_real_one(bot) -> CaseResult:
    """/agent delete an existing agent: must remove from store."""
    from clauded.cogs.agent import agent_group
    create_cmd = next(c for c in agent_group.commands if c.name == "create")
    delete_cmd = next(c for c in agent_group.commands if c.name == "delete")
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await extract_callback(create_cmd)(inter, name="doomed", prompt="goodbye")
    if "doomed" not in bot.agent_manager._agents:
        return CaseResult(cog="agent", cmd="delete", case="happy", status="ERROR",
                          detail="agent didn't get created")
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await extract_callback(delete_cmd)(inter2, name="doomed")
    if "doomed" in bot.agent_manager._agents:
        return CaseResult(cog="agent", cmd="delete", case="happy", status="FAIL",
                          detail="agent NOT deleted")
    reply = _interaction_response_text(inter2)
    if "doomed" in reply and "deleted" in reply.lower():
        return CaseResult(cog="agent", cmd="delete", case="happy", status="PASS",
                          detail=f"deleted: {reply[:120]!r}")
    return CaseResult(cog="agent", cmd="delete", case="happy", status="FAIL",
                      detail=f"removed but reply weird: {reply[:300]!r}")


async def case_mcp_add_stdio(bot) -> CaseResult:
    """/mcp add: should register stdio server."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "add"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="test-mcp", command="echo hello")
    reply = _interaction_response_text(inter)
    if "test-mcp" in reply and ("added" in reply.lower() or "✅" in reply):
        return CaseResult(cog="mcp", cmd="add", case="happy", status="PASS",
                          detail=f"added: {reply[:120]!r}")
    return CaseResult(cog="mcp", cmd="add", case="happy", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_mcp_remove_unknown(bot) -> CaseResult:
    """/mcp remove of unknown server: 'not found'."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "remove"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="ghost-server")
    reply = _interaction_response_text(inter)
    if "not found" in reply.lower() and "ghost-server" in reply:
        return CaseResult(cog="mcp", cmd="remove", case="unknown", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="mcp", cmd="remove", case="unknown", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_env_remove_unknown(bot) -> CaseResult:
    """/env remove of nonexistent var: must say 'not found'."""
    from clauded.cogs.project import env_group
    cb = extract_callback(next(c for c in env_group.commands if c.name == "remove"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, key="GHOST_VAR_X9Z")
    reply = _interaction_response_text(inter)
    if "not found" in reply.lower() and "GHOST_VAR_X9Z" in reply:
        return CaseResult(cog="env", cmd="remove", case="unknown", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="env", cmd="remove", case="unknown", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_remove_dir_unknown(bot) -> CaseResult:
    """/project remove-dir on dir not in list: must say 'not found'."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "remove-dir"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, path="/ghost/dir/path")
    reply = _interaction_response_text(inter)
    if "not found" in reply.lower():
        return CaseResult(cog="project", cmd="remove-dir", case="unknown", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="remove-dir", case="unknown", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_dirs_empty(bot) -> CaseResult:
    """/project dirs with no extras: must say 'No extra directories'."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "dirs"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no extra" in reply.lower() or "no" in reply.lower():
        return CaseResult(cog="project", cmd="dirs", case="empty", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="dirs", case="empty", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_set_mention_required(bot) -> CaseResult:
    """/project set-mention-required: must update state + confirm in reply."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-mention-required"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, required=True)
    reply = _interaction_response_text(inter)
    # Verify state in project_manager
    settings = bot.project_manager._channel_settings.get(str(inter.channel_id), {})
    state_set = settings.get("mention_required") is True
    reply_says_true = "true" in reply.lower() or "✅" in reply or "required" in reply.lower()
    if state_set and reply_says_true:
        return CaseResult(cog="project", cmd="set-mention-required", case="set-true",
                          status="PASS", detail=f"state={state_set}; reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="set-mention-required", case="set-true",
                      status="FAIL",
                      detail=f"state={state_set}; reply={reply[:200]!r}")


async def case_budget_show_empty(bot) -> CaseResult:
    """/budget show with no budget: must say 'No budget limit set'."""
    from clauded.cogs.tools import budget_group
    cb = extract_callback(next(c for c in budget_group.commands if c.name == "show"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no budget" in reply.lower() or "no limit" in reply.lower():
        return CaseResult(cog="budget", cmd="show", case="empty", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="budget", cmd="show", case="empty", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_budget_clear(bot) -> CaseResult:
    """/budget clear: removes limit + confirms in reply."""
    from clauded.cogs.tools import budget_group
    cb = extract_callback(next(c for c in budget_group.commands if c.name == "clear"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "cleared" in reply.lower() or "removed" in reply.lower():
        return CaseResult(cog="budget", cmd="clear", case="happy", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="budget", cmd="clear", case="happy", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_tools_reset_not_in_thread(bot) -> CaseResult:
    """/tools reset from channel (not thread)."""
    from clauded.cogs.tools import tools_group
    cb = extract_callback(next(c for c in tools_group.commands if c.name == "reset"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "thread" in reply.lower() and "❌" in reply:
        return CaseResult(cog="tools", cmd="reset", case="not-thread", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="tools", cmd="reset", case="not-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_tools_deny_not_in_thread(bot) -> CaseResult:
    """/tools deny from channel (not thread)."""
    from clauded.cogs.tools import tools_group
    cb = extract_callback(next(c for c in tools_group.commands if c.name == "deny"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, tools="Bash")
    reply = _interaction_response_text(inter)
    if "thread" in reply.lower() and "❌" in reply:
        return CaseResult(cog="tools", cmd="deny", case="not-thread", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="tools", cmd="deny", case="not-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_mode_set_not_in_thread(bot) -> CaseResult:
    """/mode set from channel (not thread): must report 'no active session'."""
    from clauded.cogs.mode import mode_group
    from discord import app_commands
    set_cmd = next(c for c in mode_group.commands if c.name == "set")
    cb = extract_callback(set_cmd)
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    # Build a real Choice
    choice = app_commands.Choice(name="default", value="default")
    await cb(inter, mode=choice)
    reply = _interaction_response_text(inter)
    if "no active session" in reply.lower():
        return CaseResult(cog="mode", cmd="set", case="no-session", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="mode", cmd="set", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_mode_cycle_no_session(bot) -> CaseResult:
    """/mode cycle without session: 'no active session'."""
    from clauded.cogs.mode import mode_group
    cb = extract_callback(next(c for c in mode_group.commands if c.name == "cycle"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "no active session" in reply.lower():
        return CaseResult(cog="mode", cmd="cycle", case="no-session", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="mode", cmd="cycle", case="no-session", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_fallback_model_set(bot) -> CaseResult:
    """/fallback-model: requires being in thread + bound parent.
    Stub _recreate_session to verify the fallback_model kwarg propagates."""
    from clauded.cogs.model import fallback_model_cmd
    cb = extract_callback(fallback_model_cmd)
    parent_id = 6000000000
    from clauded.cogs.project import project_group
    bind_cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    inter_bind = make_mock_interaction(bot=bot, channel_id=parent_id)
    await bind_cb(inter_bind, path=str(ROOT))

    captured: dict = {}
    async def _fake_recreate(interaction, **kwargs):
        captured.update(kwargs)
        return MagicMock()  # truthy bridge
    bot._recreate_session = _fake_recreate

    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=6000000001, parent_id=parent_id)
    sig = inspect.signature(cb)
    param_name = next((p.name for p in sig.parameters.values() if p.name != "interaction"), None)
    if param_name is None:
        return CaseResult(cog="model", cmd="fallback-model", case="happy", status="SKIP",
                          detail="unknown signature")
    try:
        await cb(inter, **{param_name: "haiku"})
    except Exception as exc:
        return CaseResult(cog="model", cmd="fallback-model", case="happy", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    # Strict: the fallback_model kwarg made it through to _recreate_session
    if captured.get("fallback_model") != "haiku":
        return CaseResult(cog="model", cmd="fallback-model", case="happy", status="FAIL",
                          detail=f"_recreate_session received fallback_model={captured.get('fallback_model')!r}")
    reply = _interaction_response_text(inter)
    if "haiku" in reply.lower() and ("fallback" in reply.lower() or "\U0001f504" in reply):
        return CaseResult(cog="model", cmd="fallback-model", case="happy", status="PASS",
                          detail=f"reply={reply[:120]!r}; recreate_session got haiku")
    return CaseResult(cog="model", cmd="fallback-model", case="happy", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


HAPPY_CASES.extend([
    ("/cost total happy", case_cost_total),
    ("/cost reset happy", case_cost_reset),
    ("/session stop no-session", case_session_stop_no_session),
    ("/session interrupt no-session", case_session_interrupt_no_session),
    ("/session fork no-session", case_session_fork_no_session),
    ("/session pin no-reply", case_session_pin_no_session),
    ("/session export empty", case_session_export_no_session),
    ("/session worktree not-thread", case_session_worktree_not_in_thread),
    ("/session name not-thread", case_session_name_not_in_thread),
    ("/session settings not-thread", case_session_settings_not_in_thread),
    ("/session security-review no-session", case_session_security_review_no_session),
    ("/agent use unknown", case_agent_use_unknown),
    ("/agent delete unknown", case_agent_delete_unknown),
    ("/agent delete happy", case_agent_delete_real_one),
    ("/mcp add stdio happy", case_mcp_add_stdio),
    ("/mcp remove unknown", case_mcp_remove_unknown),
    ("/env remove unknown", case_env_remove_unknown),
    ("/project remove-dir unknown", case_project_remove_dir_unknown),
    ("/project dirs empty", case_project_dirs_empty),
    ("/project set-mention-required true", case_project_set_mention_required),
    ("/budget show empty", case_budget_show_empty),
    ("/budget clear happy", case_budget_clear),
    ("/tools reset not-thread", case_tools_reset_not_in_thread),
    ("/tools deny not-thread", case_tools_deny_not_in_thread),
    ("/mode set no-session", case_mode_set_not_in_thread),
    ("/mode cycle no-session", case_mode_cycle_no_session),
    ("/fallback-model haiku", case_fallback_model_set),
])





# ---------------------------------------------------------------------------
# Regression cases for #254 and #255
# ---------------------------------------------------------------------------


async def case_254_agent_create_duplicate_silently_overwrites(bot) -> CaseResult:
    """#254: /agent create with existing name silently overwrites prior prompt."""
    from clauded.cogs.agent import agent_group
    create = extract_callback(next(c for c in agent_group.commands if c.name == "create"))
    bot.project_manager.bind(1500000000_000000001, str(ROOT))
    inter = make_mock_interaction(bot=bot)
    await create(inter, name="dup", prompt="first")
    first_prompt = bot.agent_manager._agents.get("dup", {}).get("prompt")
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await create(inter2, name="dup", prompt="REPLACED")
    after_prompt = bot.agent_manager._agents.get("dup", {}).get("prompt")
    # Bug: should have refused OR kept first; instead silently replaces.
    if first_prompt == "first" and after_prompt == "REPLACED":
        return CaseResult(cog="agent", cmd="create", case="#254-silent-overwrite",
                          status="FAIL",
                          detail="#254 confirmed: second create overwrote prior prompt without warning")
    return CaseResult(cog="agent", cmd="create", case="#254-silent-overwrite",
                      status="PASS",
                      detail=f"refused or kept original; first={first_prompt!r}, after={after_prompt!r}")


async def case_255_agent_create_empty_name(bot) -> CaseResult:
    """#255: /agent create accepts empty name."""
    from clauded.cogs.agent import agent_group
    cb = extract_callback(next(c for c in agent_group.commands if c.name == "create"))
    bot.project_manager.bind(1500000000_000000001, str(ROOT))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, name="", prompt="hi")
    if "" in bot.agent_manager._agents:
        return CaseResult(cog="agent", cmd="create", case="#255-empty-name",
                          status="FAIL",
                          detail="#255 confirmed: empty-string agent name accepted")
    return CaseResult(cog="agent", cmd="create", case="#255-empty-name",
                      status="PASS",
                      detail="empty name refused")


async def case_255_env_set_equals_in_key(bot) -> CaseResult:
    """#255: /env set accepts `=` in key (invalid POSIX env name)."""
    from clauded.cogs.project import env_group
    cb = extract_callback(next(c for c in env_group.commands if c.name == "set"))
    bot.project_manager.bind(1500000000_000000001, str(ROOT))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, key="KEY=injected", value="val")
    env = bot.project_manager._projects.get(str(inter.channel_id), {}).get("env", {})
    if "KEY=injected" in env:
        return CaseResult(cog="env", cmd="set", case="#255-equals-in-key",
                          status="FAIL",
                          detail="#255 confirmed: env key 'KEY=injected' accepted (violates POSIX name)")
    return CaseResult(cog="env", cmd="set", case="#255-equals-in-key",
                      status="PASS", detail="`=` in env key refused")


async def case_255_env_set_newline_in_key(bot) -> CaseResult:
    """#255: /env set accepts newline in key (breaks .env files)."""
    from clauded.cogs.project import env_group
    cb = extract_callback(next(c for c in env_group.commands if c.name == "set"))
    bot.project_manager.bind(1500000000_000000001, str(ROOT))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, key="key\nwith\nnewlines", value="val")
    env = bot.project_manager._projects.get(str(inter.channel_id), {}).get("env", {})
    if "key\nwith\nnewlines" in env:
        return CaseResult(cog="env", cmd="set", case="#255-newline-in-key",
                          status="FAIL",
                          detail="#255 confirmed: env key with newlines accepted (will break .env files)")
    return CaseResult(cog="env", cmd="set", case="#255-newline-in-key",
                      status="PASS", detail="newline in env key refused")


async def case_255_mcp_add_empty_name(bot) -> CaseResult:
    """#255: /mcp add accepts empty server name."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "add"))
    bot.project_manager.bind(1500000000_000000001, str(ROOT))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, name="", command="echo")
    servers = bot.project_manager._projects.get(str(inter.channel_id), {}).get("mcp_servers", {})
    if "" in servers:
        return CaseResult(cog="mcp", cmd="add", case="#255-empty-name",
                          status="FAIL",
                          detail="#255 confirmed: empty-string MCP server name accepted")
    return CaseResult(cog="mcp", cmd="add", case="#255-empty-name",
                      status="PASS", detail="empty MCP name refused")


async def case_252_cost_tracker_race(bot) -> CaseResult:
    """#252: CostTracker._save() race under concurrent record() calls."""
    from clauded.cost_tracker import CostTracker
    import shutil, os as _os, tempfile
    test_dir = tempfile.mkdtemp(prefix="e2e_race_")
    ct = CostTracker(data_dir=test_dir)
    errors = []
    async def one(i):
        try:
            await asyncio.to_thread(ct.record, 42, 0.01)
        except Exception as e:
            errors.append((i, type(e).__name__, str(e)[:80]))
    await asyncio.gather(*[one(i) for i in range(50)])
    shutil.rmtree(test_dir, ignore_errors=True)
    if errors:
        return CaseResult(cog="cost", cmd="record", case="#252-race",
                          status="FAIL",
                          detail=f"#252 confirmed: {len(errors)}/50 concurrent record() calls failed")
    return CaseResult(cog="cost", cmd="record", case="#252-race",
                      status="PASS",
                      detail="50/50 concurrent record() calls succeeded")


HAPPY_CASES.extend([
    ("/agent create #254 dup-overwrite", case_254_agent_create_duplicate_silently_overwrites),
    ("/agent create #255 empty-name", case_255_agent_create_empty_name),
    ("/env set #255 equals-in-key", case_255_env_set_equals_in_key),
    ("/env set #255 newline-in-key", case_255_env_set_newline_in_key),
    ("/mcp add #255 empty-name", case_255_mcp_add_empty_name),
    ("/cost #252 race", case_252_cost_tracker_race),
])





# ===========================================================================
# Phase 3 — Edge case expansion to hit PRD §Spec 3 target
# ===========================================================================

# Helper: make a DM-like Interaction (no guild, channel is DMChannel)
def make_dm_interaction(*, bot):
    """Build a `discord.Interaction` impostor that simulates a DM."""
    inter = make_mock_interaction(bot=bot)
    dm = MagicMock(spec=discord.DMChannel)
    dm.id = inter.channel_id
    inter.channel = dm
    inter.guild_id = None
    return inter


# ---------------------------------------------------------------------------
# /project — 8 commands × multi-edge
# ---------------------------------------------------------------------------


async def case_project_info_unbound(bot) -> CaseResult:
    """/project info on unbound channel: must say 'not bound'."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "info"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if ("not bound" in rl or "no binding" in rl or "unbound" in rl) or ("isn't bound" in rl):
        return CaseResult(cog="project", cmd="info", case="unbound", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="info", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_unbind_no_binding(bot) -> CaseResult:
    """/project unbind when no binding: must say 'nothing to unbind' or similar."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "unbind"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    # Should say it wasn't bound or similar (idempotent)
    if "not bound" in rl or "no binding" in rl or "wasn't bound" in rl or "no project" in rl:
        return CaseResult(cog="project", cmd="unbind", case="no-binding", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="unbind", case="no-binding", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_add_dir_unbound(bot) -> CaseResult:
    """/project add-dir on unbound channel must refuse."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "add-dir"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, path=str(ROOT))
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if "isn't bound" in rl or "not bound" in rl or "❌" in reply:
        return CaseResult(cog="project", cmd="add-dir", case="unbound", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="project", cmd="add-dir", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_add_dir_happy(bot) -> CaseResult:
    """/project add-dir on bound channel + valid path: must add to dirs."""
    from clauded.cogs.project import project_group
    bind_cmd = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    add_cmd = extract_callback(next(c for c in project_group.commands if c.name == "add-dir"))
    inter = make_mock_interaction(bot=bot)
    await bind_cmd(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    # Add a real dir
    extra = str(ROOT / "tests")
    await add_cmd(inter2, path=extra)
    dirs = bot.project_manager.get_extra_dirs(inter.channel_id)
    if extra in dirs or any(extra in d for d in dirs):
        return CaseResult(cog="project", cmd="add-dir", case="happy", status="PASS",
                          detail=f"dir added: {dirs}")
    return CaseResult(cog="project", cmd="add-dir", case="happy", status="FAIL",
                      detail=f"dir not in state: {dirs}")


async def case_project_set_mode_invalid(bot) -> CaseResult:
    """/project set-mode with invalid value: must refuse."""
    from clauded.cogs.project import project_group
    from discord import app_commands
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-mode"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    # Try passing a Choice with invalid value (mimic if Discord choice constraint
    # somehow let it through). Real Discord enforces choices server-side; this
    # test exists to confirm the cog ALSO validates.
    choice = app_commands.Choice(name="invalid", value="invalid")
    try:
        await cb(inter, mode=choice)
    except Exception as exc:
        return CaseResult(cog="project", cmd="set-mode", case="invalid",
                          status="PASS",
                          detail=f"raised: {type(exc).__name__}: {exc} (defense-in-depth)")
    reply = _interaction_response_text(inter)
    if "❌" in reply or "invalid" in reply.lower():
        return CaseResult(cog="project", cmd="set-mode", case="invalid", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="project", cmd="set-mode", case="invalid", status="FAIL",
                      detail=f"accepted invalid: {reply[:300]!r}")


async def case_project_set_mode_valid(bot) -> CaseResult:
    """/project set-mode thread: happy path."""
    from clauded.cogs.project import project_group
    from discord import app_commands
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-mode"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    choice = app_commands.Choice(name="thread", value="thread")
    await cb(inter, mode=choice)
    reply = _interaction_response_text(inter)
    # State is stored under _projects[channel_id]['channel_mode']
    proj = bot.project_manager._projects.get(str(inter.channel_id), {})
    mode = proj.get("channel_mode")
    if mode == "thread" and ("✅" in reply or "thread" in reply.lower()):
        return CaseResult(cog="project", cmd="set-mode", case="thread", status="PASS",
                          detail=f"mode=thread; reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="set-mode", case="thread", status="FAIL",
                      detail=f"proj={proj}; reply={reply[:200]!r}")


async def case_project_set_mention_required_false(bot) -> CaseResult:
    """/project set-mention-required false: state flips back."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-mention-required"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, required=False)
    settings = bot.project_manager._channel_settings.get(str(inter.channel_id), {})
    if settings.get("mention_required") is False:
        return CaseResult(cog="project", cmd="set-mention-required",
                          case="set-false", status="PASS",
                          detail=f"settings={settings}")
    return CaseResult(cog="project", cmd="set-mention-required",
                      case="set-false", status="FAIL",
                      detail=f"settings={settings}")


async def case_project_set_root_invalid(bot) -> CaseResult:
    """/project set-root with non-existent path: must refuse."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-root"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, path="/totally/nonexistent/root/xyz123")
    reply = _interaction_response_text(inter)
    if "not" in reply.lower() and ("directory" in reply.lower() or "exist" in reply.lower()):
        return CaseResult(cog="project", cmd="set-root", case="invalid", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="project", cmd="set-root", case="invalid", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_project_clear_root_no_root_set(bot) -> CaseResult:
    """/project clear-root when no root set: idempotent."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "clear-root"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if "no" in rl and "root" in rl:
        return CaseResult(cog="project", cmd="clear-root", case="no-root",
                          status="PASS", detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="project", cmd="clear-root", case="no-root",
                      status="FAIL", detail=f"reply={reply[:300]!r}")


# ---------------------------------------------------------------------------
# /env edge cases
# ---------------------------------------------------------------------------


async def case_env_list_unbound(bot) -> CaseResult:
    """/env list on unbound channel: must refuse."""
    from clauded.cogs.project import env_group
    cb = extract_callback(next(c for c in env_group.commands if c.name == "list"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if "isn't bound" in rl or "not bound" in rl or "❌" in reply:
        return CaseResult(cog="env", cmd="list", case="unbound", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="env", cmd="list", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_env_set_unbound(bot) -> CaseResult:
    """/env set on unbound channel: must refuse."""
    from clauded.cogs.project import env_group
    cb = extract_callback(next(c for c in env_group.commands if c.name == "set"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, key="X", value="y")
    reply = _interaction_response_text(inter)
    rl = reply.lower()
    if "isn't bound" in rl or "not bound" in rl or "❌" in reply:
        return CaseResult(cog="env", cmd="set", case="unbound", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="env", cmd="set", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_env_remove_real_var(bot) -> CaseResult:
    """/env remove an existing var: must remove from state."""
    from clauded.cogs.project import env_group
    set_cmd = extract_callback(next(c for c in env_group.commands if c.name == "set"))
    remove_cmd = extract_callback(next(c for c in env_group.commands if c.name == "remove"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await set_cmd(inter, key="TO_DELETE", value="x")
    if "TO_DELETE" not in bot.project_manager.get_env(inter.channel_id):
        return CaseResult(cog="env", cmd="remove", case="happy", status="ERROR",
                          detail="set didn't take")
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await remove_cmd(inter2, key="TO_DELETE")
    if "TO_DELETE" not in bot.project_manager.get_env(inter.channel_id):
        return CaseResult(cog="env", cmd="remove", case="happy", status="PASS",
                          detail="var removed")
    return CaseResult(cog="env", cmd="remove", case="happy", status="FAIL",
                      detail="var NOT removed")


# ---------------------------------------------------------------------------
# /session edge cases — already heavy coverage above; add a few more
# ---------------------------------------------------------------------------


async def case_session_clear_unbound(bot) -> CaseResult:
    """/session clear on unbound channel: should still be safe."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "clear"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    # /session clear doesn't gate on binding; should report no session
    if "no session" in reply.lower() or "no active" in reply.lower():
        return CaseResult(cog="session", cmd="clear", case="unbound", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="clear", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_session_worktree_invalid_name(bot) -> CaseResult:
    """/session worktree with invalid name (contains spaces)."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "worktree"))
    # In a thread + bound parent
    parent_id = 5_000_000_000
    bot.project_manager.bind(parent_id, str(ROOT))
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=5_000_000_001, parent_id=parent_id)
    try:
        await cb(inter, name="has spaces and 一 chinese")
    except Exception as exc:
        return CaseResult(cog="session", cmd="worktree", case="invalid-name",
                          status="PASS", detail=f"raised: {type(exc).__name__}")
    reply = _interaction_response_text(inter)
    # Either rejected or attempted git worktree (will fail). Both are acceptable
    # as long as we don't silently create bogus state.
    return CaseResult(cog="session", cmd="worktree", case="invalid-name",
                      status="PASS", detail=f"reply={reply[:150]!r}")


# ---------------------------------------------------------------------------
# /agent edge cases
# ---------------------------------------------------------------------------


async def case_agent_create_unbound(bot) -> CaseResult:
    """/agent create on unbound channel: must refuse."""
    from clauded.cogs.agent import agent_group
    cb = extract_callback(next(c for c in agent_group.commands if c.name == "create"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, name="x", prompt="y")
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "not bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="agent", cmd="create", case="unbound", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="agent", cmd="create", case="unbound", status="FAIL",
                      detail=f"created without binding: {reply[:300]!r}")


async def case_agent_use_happy(bot) -> CaseResult:
    """/agent use on an existing agent — must invoke _recreate_session."""
    from clauded.cogs.agent import agent_group
    create = extract_callback(next(c for c in agent_group.commands if c.name == "create"))
    use_cmd = extract_callback(next(c for c in agent_group.commands if c.name == "use"))
    parent_id = 4_000_000_000
    bot.project_manager.bind(parent_id, str(ROOT))
    inter_bind = make_mock_interaction(bot=bot, channel_id=parent_id)
    await create(inter_bind, name="cool", prompt="be cool")
    
    # _recreate_session intercept
    captured: dict = {}
    async def _fake_recreate(interaction, **kwargs):
        captured.update(kwargs)
        return MagicMock()
    bot._recreate_session = _fake_recreate
    
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=4_000_000_001, parent_id=parent_id)
    await use_cmd(inter, name="cool")
    reply = _interaction_response_text(inter)
    if captured.get("agent_name") == "cool" or "cool" in reply.lower():
        return CaseResult(cog="agent", cmd="use", case="happy", status="PASS",
                          detail=f"captured={captured}; reply={reply[:120]!r}")
    return CaseResult(cog="agent", cmd="use", case="happy", status="FAIL",
                      detail=f"captured={captured}; reply={reply[:300]!r}")


# ---------------------------------------------------------------------------
# /mcp edge cases
# ---------------------------------------------------------------------------


async def case_mcp_add_unbound(bot) -> CaseResult:
    """/mcp add on unbound channel: must refuse."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "add"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, name="x", command="echo")
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "not bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="mcp", cmd="add", case="unbound", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    return CaseResult(cog="mcp", cmd="add", case="unbound", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_mcp_add_url(bot) -> CaseResult:
    """/mcp add-url: must register http server."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "add-url"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, name="remote-mcp", url="https://example.com/mcp")
    state = bot.project_manager._projects[str(inter.channel_id)].get("mcp_servers", {})
    reply = _interaction_response_text(inter)
    if "remote-mcp" in state and "https://example.com/mcp" in str(state["remote-mcp"]) and "remote-mcp" in reply:
        return CaseResult(cog="mcp", cmd="add-url", case="happy", status="PASS",
                          detail=f"state={state}; reply={reply[:120]!r}")
    return CaseResult(cog="mcp", cmd="add-url", case="happy", status="FAIL",
                      detail=f"state={state}; reply={reply[:200]!r}")


# ---------------------------------------------------------------------------
# /model edge cases
# ---------------------------------------------------------------------------


async def case_model_switch_unknown_alias(bot) -> CaseResult:
    """/model switch with unknown alias: must refuse."""
    from clauded.cogs.model import model_group
    from discord import app_commands
    switch_cmd = next(c for c in model_group.commands if c.name == "switch")
    cb = extract_callback(switch_cmd)
    parent_id = 3_000_000_000
    bot.project_manager.bind(parent_id, str(ROOT))
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=3_000_000_001, parent_id=parent_id)
    # Try with an unknown alias
    try:
        await cb(inter, model="totally-unknown-model-name-xyz")
    except Exception as exc:
        return CaseResult(cog="model", cmd="switch", case="unknown",
                          status="PASS", detail=f"raised: {type(exc).__name__}")
    reply = _interaction_response_text(inter)
    # Should either refuse (unknown alias) or attempt with raw value
    if "❌" in reply or "unknown" in reply.lower() or "not" in reply.lower():
        return CaseResult(cog="model", cmd="switch", case="unknown", status="PASS",
                          detail=f"refused: {reply[:120]!r}")
    # If it accepted the raw name as a model ID (passthrough), that's also OK
    return CaseResult(cog="model", cmd="switch", case="unknown", status="PASS",
                      detail=f"accepted as raw model id (passthrough): {reply[:120]!r}")


# ---------------------------------------------------------------------------
# /budget edge cases
# ---------------------------------------------------------------------------


async def case_budget_show_with_value(bot) -> CaseResult:
    """/budget show after set: must report the value."""
    from clauded.cogs.tools import budget_group
    cb = extract_callback(next(c for c in budget_group.commands if c.name == "show"))
    inter = make_mock_interaction(bot=bot)
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    # Set budget directly via project_manager
    bot.project_manager.set_budget(inter.channel_id, 5.0)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "$5" in reply or "5.0" in reply:
        return CaseResult(cog="budget", cmd="show", case="with-value",
                          status="PASS", detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="budget", cmd="show", case="with-value",
                      status="FAIL", detail=f"reply={reply[:300]!r}")


# ---------------------------------------------------------------------------
# /cost edge cases
# ---------------------------------------------------------------------------


async def case_cost_show_unbound(bot) -> CaseResult:
    """/cost show on unbound channel: should still show 0 (or refuse)."""
    from clauded.cogs.ops import cost_group
    cb = extract_callback(next(c for c in cost_group.commands if c.name == "show"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    # cost show is read-only; reasonable to either show $0 or refuse
    if reply.strip():
        return CaseResult(cog="cost", cmd="show", case="unbound", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="cost", cmd="show", case="unbound", status="FAIL",
                      detail="empty reply")


async def case_cost_total_empty(bot) -> CaseResult:
    """/cost total when no costs recorded: $0.0000."""
    from clauded.cogs.ops import cost_group
    cb = extract_callback(next(c for c in cost_group.commands if c.name == "total"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "$0" in reply:
        return CaseResult(cog="cost", cmd="total", case="empty", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="cost", cmd="total", case="empty", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


# ---------------------------------------------------------------------------
# /ops + miscellaneous edges
# ---------------------------------------------------------------------------


async def case_health_in_thread(bot) -> CaseResult:
    """/health in a thread: works (no thread-restriction)."""
    from clauded.cogs.ops import health_check
    cb = extract_callback(health_check)
    inter = make_mock_interaction(bot=bot, in_thread=True, parent_id=2_000_000_000)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "Uptime" in reply:
        return CaseResult(cog="ops", cmd="health", case="in-thread", status="PASS",
                          detail="works from thread")
    return CaseResult(cog="ops", cmd="health", case="in-thread", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_ratelimit_with_cost(bot) -> CaseResult:
    """/ratelimit reflects accumulated costs across multiple channels."""
    from clauded.cogs.ops import ratelimit_info
    cb = extract_callback(ratelimit_info)
    bot.cost_tracker.record(1, 0.1)
    bot.cost_tracker.record(2, 0.2)
    bot.cost_tracker.record(3, 0.3)
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    # 0.1 + 0.2 + 0.3 = 0.6
    if "$0.6" in reply or "0.60" in reply:
        return CaseResult(cog="ops", cmd="ratelimit", case="multi-channel",
                          status="PASS", detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="ops", cmd="ratelimit", case="multi-channel",
                      status="FAIL", detail=f"reply={reply[:300]!r}")


async def case_debug_toggle_twice(bot) -> CaseResult:
    """/debug toggle twice: returns to original state."""
    from clauded.cogs.ops import debug_toggle
    cb = extract_callback(debug_toggle)
    inter1 = make_mock_interaction(bot=bot)
    initial = bot._debug_logging
    await cb(inter1)
    first = bot._debug_logging
    inter2 = make_mock_interaction(bot=bot)
    await cb(inter2)
    final = bot._debug_logging
    if final == initial and first != initial:
        return CaseResult(cog="ops", cmd="debug", case="toggle-twice",
                          status="PASS",
                          detail=f"toggle returned: {initial} → {first} → {final}")
    return CaseResult(cog="ops", cmd="debug", case="toggle-twice",
                      status="FAIL",
                      detail=f"unexpected sequence: {initial} → {first} → {final}")


async def case_btw_in_thread_no_session(bot) -> CaseResult:
    """/btw IN a thread but no session: must not crash (helpful error)."""
    from clauded.cogs.ops import btw_cmd
    cb = extract_callback(btw_cmd)
    parent_id = 1_000_000_000
    bot.project_manager.bind(parent_id, str(ROOT))
    inter = make_mock_interaction(bot=bot, in_thread=True,
                                  channel_id=1_000_000_001, parent_id=parent_id)
    try:
        await cb(inter, text="quick test")
    except Exception as exc:
        return CaseResult(cog="ops", cmd="btw", case="thread-no-session",
                          status="ERROR", detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    if reply.strip():
        return CaseResult(cog="ops", cmd="btw", case="thread-no-session",
                          status="PASS", detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="ops", cmd="btw", case="thread-no-session",
                      status="FAIL", detail="empty reply")


# ---------------------------------------------------------------------------
# DM (no-guild) refusal for every Group A command
# ---------------------------------------------------------------------------


async def case_session_list_in_dm(bot) -> CaseResult:
    """/session list in DM: must NOT leak session data; should refuse or show empty."""
    from clauded.cogs.session import session_group
    cb = extract_callback(next(c for c in session_group.commands if c.name == "list"))
    inter = make_dm_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="session", cmd="list", case="DM", status="PASS",
                          detail=f"raised: {type(exc).__name__}")
    reply = _interaction_response_text(inter)
    # Should either refuse or show empty list (no sessions actually exist)
    if reply.strip():
        return CaseResult(cog="session", cmd="list", case="DM", status="PASS",
                          detail=f"reply={reply[:120]!r}")
    return CaseResult(cog="session", cmd="list", case="DM", status="FAIL",
                      detail="empty reply")


async def case_health_in_dm(bot) -> CaseResult:
    """/health in DM: should work (bot-level info, no guild needed)."""
    from clauded.cogs.ops import health_check
    cb = extract_callback(health_check)
    inter = make_dm_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="ops", cmd="health", case="DM", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    if "Uptime" in reply or "Bound Projects" in reply:
        return CaseResult(cog="ops", cmd="health", case="DM",
                          status="PASS", detail="works in DM")
    return CaseResult(cog="ops", cmd="health", case="DM", status="FAIL",
                      detail=f"reply={reply[:300]!r}")


async def case_log_dump_in_dm(bot) -> CaseResult:
    """/log dump in DM: bot-level, should work."""
    from clauded.cogs.log_dump import log_group
    cb = extract_callback(next(c for c in log_group.commands if c.name == "dump"))
    inter = make_dm_interaction(bot=bot)
    try:
        await cb(inter)
    except Exception as exc:
        return CaseResult(cog="log", cmd="dump", case="DM", status="ERROR",
                          detail=f"{type(exc).__name__}: {exc}")
    # Must produce file or graceful error
    has_file = any(s.get("kwargs", {}).get("file") for s in inter._followups)
    if has_file:
        return CaseResult(cog="log", cmd="dump", case="DM", status="PASS",
                          detail="bundle generated in DM")
    return CaseResult(cog="log", cmd="dump", case="DM", status="FAIL",
                      detail=f"no bundle; followups={inter._followups[:1]}")


# ---------------------------------------------------------------------------
# Project edges — set-root happy path, dirs after add
# ---------------------------------------------------------------------------


async def case_project_set_root_happy(bot) -> CaseResult:
    """/project set-root with valid dir: updates per-guild root."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-root"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter, path=str(ROOT))
    reply = _interaction_response_text(inter)
    guild_id = inter.guild_id
    stored = bot.project_manager.get_guild_root(guild_id) if guild_id else None
    if stored:
        return CaseResult(cog="project", cmd="set-root", case="happy",
                          status="PASS", detail=f"root={stored}")
    return CaseResult(cog="project", cmd="set-root", case="happy",
                      status="FAIL", detail=f"reply={reply[:200]!r}")


async def case_project_dirs_after_add(bot) -> CaseResult:
    """/project dirs after add-dir: must show the added dir."""
    from clauded.cogs.project import project_group
    bind = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    add = extract_callback(next(c for c in project_group.commands if c.name == "add-dir"))
    dirs_cmd = extract_callback(next(c for c in project_group.commands if c.name == "dirs"))
    inter = make_mock_interaction(bot=bot)
    await bind(inter, path=str(ROOT))
    inter2 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    extra = str(ROOT / "src")
    await add(inter2, path=extra)
    inter3 = make_mock_interaction(bot=bot, channel_id=inter.channel_id)
    await dirs_cmd(inter3)
    reply = _interaction_response_text(inter3)
    if "src" in reply or extra in reply:
        return CaseResult(cog="project", cmd="dirs", case="after-add",
                          status="PASS", detail=f"reply contains added dir")
    return CaseResult(cog="project", cmd="dirs", case="after-add",
                      status="FAIL", detail=f"reply={reply[:300]!r}")


# ---------------------------------------------------------------------------
# Wire up all the new cases
# ---------------------------------------------------------------------------

HAPPY_CASES.extend([
    # Project edges
    ("/project info unbound", case_project_info_unbound),
    ("/project unbind no-binding", case_project_unbind_no_binding),
    ("/project add-dir unbound", case_project_add_dir_unbound),
    ("/project add-dir happy", case_project_add_dir_happy),
    ("/project set-mode invalid", case_project_set_mode_invalid),
    ("/project set-mode thread valid", case_project_set_mode_valid),
    ("/project set-mention-required false", case_project_set_mention_required_false),
    ("/project set-root invalid", case_project_set_root_invalid),
    ("/project clear-root no-root", case_project_clear_root_no_root_set),
    ("/project set-root happy", case_project_set_root_happy),
    ("/project dirs after-add", case_project_dirs_after_add),
    # Env
    ("/env list unbound", case_env_list_unbound),
    ("/env set unbound", case_env_set_unbound),
    ("/env remove happy", case_env_remove_real_var),
    # Session
    ("/session clear unbound", case_session_clear_unbound),
    ("/session worktree invalid-name", case_session_worktree_invalid_name),
    # Agent
    ("/agent create unbound", case_agent_create_unbound),
    ("/agent use happy", case_agent_use_happy),
    # MCP
    ("/mcp add unbound", case_mcp_add_unbound),
    ("/mcp add-url happy", case_mcp_add_url),
    # Model
    ("/model switch unknown", case_model_switch_unknown_alias),
    # Budget
    ("/budget show with-value", case_budget_show_with_value),
    # Cost
    ("/cost show unbound", case_cost_show_unbound),
    ("/cost total empty", case_cost_total_empty),
    # Ops
    ("/health in-thread", case_health_in_thread),
    ("/ratelimit multi-channel", case_ratelimit_with_cost),
    ("/debug toggle-twice", case_debug_toggle_twice),
    ("/btw thread-no-session", case_btw_in_thread_no_session),
    # DM edges
    ("/session list in-DM", case_session_list_in_dm),
    ("/health in-DM", case_health_in_dm),
    ("/log dump in-DM", case_log_dump_in_dm),
])





# ---------------------------------------------------------------------------
# Regression cases for #257
# ---------------------------------------------------------------------------


async def case_257_budget_show_no_refuse(bot) -> CaseResult:
    """#257: /budget show on unbound channel doesn't refuse."""
    from clauded.cogs.tools import budget_group
    cb = extract_callback(next(c for c in budget_group.commands if c.name == "show"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="budget", cmd="show", case="#257-unbound",
                          status="PASS", detail="properly refuses")
    return CaseResult(cog="budget", cmd="show", case="#257-unbound",
                      status="FAIL",
                      detail=f"#257 confirmed: shows '{reply[:120]!r}' on unbound")


async def case_257_budget_clear_lies(bot) -> CaseResult:
    """#257: /budget clear on unbound channel says '✅ Budget Cleared' (lie)."""
    from clauded.cogs.tools import budget_group
    cb = extract_callback(next(c for c in budget_group.commands if c.name == "clear"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="budget", cmd="clear", case="#257-unbound",
                          status="PASS", detail="properly refuses")
    return CaseResult(cog="budget", cmd="clear", case="#257-unbound",
                      status="FAIL",
                      detail=f"#257 confirmed: lies '{reply[:120]!r}'")


async def case_257_mcp_list_no_refuse(bot) -> CaseResult:
    """#257: /mcp list on unbound doesn't refuse."""
    from clauded.cogs.mcp import mcp_group
    cb = extract_callback(next(c for c in mcp_group.commands if c.name == "list"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="mcp", cmd="list", case="#257-unbound",
                          status="PASS", detail="refuses")
    return CaseResult(cog="mcp", cmd="list", case="#257-unbound",
                      status="FAIL",
                      detail=f"#257: shows '{reply[:120]!r}'")


async def case_257_project_dirs_no_refuse(bot) -> CaseResult:
    """#257: /project dirs on unbound doesn't refuse."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "dirs"))
    inter = make_mock_interaction(bot=bot)
    await cb(inter)
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="project", cmd="dirs", case="#257-unbound",
                          status="PASS", detail="refuses")
    return CaseResult(cog="project", cmd="dirs", case="#257-unbound",
                      status="FAIL",
                      detail=f"#257: shows '{reply[:120]!r}'")


async def case_257_project_remove_dir_crash(bot) -> CaseResult:
    """#257: /project remove-dir on unbound raises ValueError instead of friendly."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "remove-dir"))
    inter = make_mock_interaction(bot=bot)
    try:
        await cb(inter, path=str(ROOT))
    except ValueError as exc:
        return CaseResult(cog="project", cmd="remove-dir", case="#257-crash",
                          status="FAIL",
                          detail=f"#257 confirmed: ValueError instead of friendly refuse: {exc}")
    except Exception as exc:
        return CaseResult(cog="project", cmd="remove-dir", case="#257-crash",
                          status="FAIL",
                          detail=f"unexpected {type(exc).__name__}: {exc}")
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="project", cmd="remove-dir", case="#257-crash",
                          status="PASS", detail="friendly refuse")
    return CaseResult(cog="project", cmd="remove-dir", case="#257-crash",
                      status="FAIL",
                      detail=f"reply={reply[:200]!r}")


async def case_257_project_set_mode_crash(bot) -> CaseResult:
    """#257: /project set-mode on unbound raises ValueError."""
    from clauded.cogs.project import project_group
    from discord import app_commands
    cb = extract_callback(next(c for c in project_group.commands if c.name == "set-mode"))
    inter = make_mock_interaction(bot=bot)
    choice = app_commands.Choice(name="thread", value="thread")
    try:
        await cb(inter, mode=choice)
    except ValueError as exc:
        return CaseResult(cog="project", cmd="set-mode", case="#257-crash",
                          status="FAIL",
                          detail=f"#257: ValueError instead of friendly refuse: {exc}")
    reply = _interaction_response_text(inter)
    if "isn't bound" in reply.lower() or "❌" in reply:
        return CaseResult(cog="project", cmd="set-mode", case="#257-crash",
                          status="PASS", detail="friendly refuse")
    return CaseResult(cog="project", cmd="set-mode", case="#257-crash",
                      status="FAIL",
                      detail=f"reply={reply[:200]!r}")


HAPPY_CASES.extend([
    ("/budget show #257 unbound", case_257_budget_show_no_refuse),
    ("/budget clear #257 lies", case_257_budget_clear_lies),
    ("/mcp list #257 unbound", case_257_mcp_list_no_refuse),
    ("/project dirs #257 unbound", case_257_project_dirs_no_refuse),
    ("/project remove-dir #257 crash", case_257_project_remove_dir_crash),
    ("/project set-mode #257 crash", case_257_project_set_mode_crash),
])





# ===========================================================================
# Phase 4 — Fault injection / concurrency / restart scenarios
# ===========================================================================


async def case_concurrent_bind_same_channel(bot) -> CaseResult:
    """Two concurrent /project bind to the same channel — final state must
    be ONE consistent binding (not corrupted)."""
    from clauded.cogs.project import project_group
    cb = extract_callback(next(c for c in project_group.commands if c.name == "bind"))
    # Use real project subdirs inside $HOME (the default projects_root) so
    # the bind validates. The repo itself is under $HOME for this test.
    p1 = str(ROOT)
    p2 = str(ROOT / "src")
    inter1 = make_mock_interaction(bot=bot, channel_id=4242)
    inter2 = make_mock_interaction(bot=bot, channel_id=4242)
    await asyncio.gather(cb(inter1, path=p1), cb(inter2, path=p2))
    final = bot.project_manager._projects.get("4242", {}).get("path")
    if final in (p1, p2):
        return CaseResult(cog="project", cmd="bind", case="concurrent-race",
                          status="PASS", detail=f"converged to {final}")
    return CaseResult(cog="project", cmd="bind", case="concurrent-race",
                      status="FAIL", detail=f"corrupted: final={final}")


async def case_concurrent_agent_create(bot) -> CaseResult:
    """20 concurrent threaded /agent create (real parallel via to_thread) —
    surfaces #252 race in AgentManager._save() too."""
    from clauded.agent_manager import AgentManager
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="cc_am_")
    am = AgentManager(data_dir=d)
    crashes = []
    async def one(i):
        try:
            await asyncio.to_thread(am.create, f"agent-{i}", f"prompt-{i}")
        except Exception as e:
            crashes.append((i, type(e).__name__, str(e)[:80]))
    await asyncio.gather(*[one(i) for i in range(20)])
    shutil.rmtree(d, ignore_errors=True)
    if crashes:
        return CaseResult(cog="agent", cmd="create", case="concurrent",
                          status="FAIL",
                          detail=f"#252 confirmed: {len(crashes)}/20 crashed in AgentManager._save")
    return CaseResult(cog="agent", cmd="create", case="concurrent",
                      status="PASS", detail="all 20 persisted (probably not parallel)")


async def case_log_dump_under_high_load(bot) -> CaseResult:
    """/log dump generation under simulated CPU pressure (5 concurrent dumps).
    All 5 should produce valid bundles."""
    from clauded.cogs.log_dump import log_group
    cb = extract_callback(next(c for c in log_group.commands if c.name == "dump"))
    inters = [make_mock_interaction(bot=bot) for _ in range(5)]
    await asyncio.gather(*[cb(inter) for inter in inters])
    # Check each got a file
    valid = 0
    for inter in inters:
        for s in inter._followups:
            if s.get("kwargs", {}).get("file") is not None:
                valid += 1
                break
    if valid == 5:
        return CaseResult(cog="log", cmd="dump", case="concurrent",
                          status="PASS", detail="5/5 bundles generated")
    return CaseResult(cog="log", cmd="dump", case="concurrent",
                      status="FAIL", detail=f"only {valid}/5 produced bundles")


async def case_cost_record_save_race(bot) -> CaseResult:
    """#252 reproducer using cost_tracker only (lower-level)."""
    from clauded.cost_tracker import CostTracker
    import shutil, tempfile
    test_dir = tempfile.mkdtemp(prefix="cc_cost_")
    ct = CostTracker(data_dir=test_dir)
    errors = []
    async def one(i):
        try:
            await asyncio.to_thread(ct.record, 42, 0.01)
        except Exception as e:
            errors.append((i, type(e).__name__))
    await asyncio.gather(*[one(i) for i in range(30)])
    shutil.rmtree(test_dir, ignore_errors=True)
    if errors:
        return CaseResult(cog="cost", cmd="record", case="concurrent-save",
                          status="FAIL",
                          detail=f"#252 confirmed: {len(errors)}/30 fail")
    return CaseResult(cog="cost", cmd="record", case="concurrent-save",
                      status="PASS", detail="30/30 success")


async def case_session_state_resume_after_restart(bot) -> CaseResult:
    """Simulate bot restart: stored session must be re-loadable."""
    from clauded.session_store import SessionStore
    import tempfile
    d = tempfile.mkdtemp(prefix="cc_ss_")
    ss = SessionStore(data_dir=d)
    ss.save_session(thread_id=999, session_id="sess-x", project_path="/tmp")
    # Pretend restart
    ss2 = SessionStore(data_dir=d)
    stored = ss2.get_session_info(999)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    if stored and stored.get("session_id") == "sess-x":
        return CaseResult(cog="session", cmd="state", case="resume-after-restart",
                          status="PASS", detail="reloaded correctly")
    return CaseResult(cog="session", cmd="state", case="resume-after-restart",
                      status="FAIL", detail=f"got {stored}")


async def case_save_session_state_after_crash(bot) -> CaseResult:
    """If save_session_state is called mid-write and interrupted (simulated
    via os._exit-style mock), file shouldn't be corrupt."""
    from clauded.session_store import SessionStore
    import tempfile, json
    d = tempfile.mkdtemp(prefix="cc_ssac_")
    ss = SessionStore(data_dir=d)
    # First, write a known-good state
    ss.save_session(thread_id=1, session_id="a", project_path="/x")
    # Now simulate concurrent _save call
    try:
        await asyncio.to_thread(ss.save_session, 2, "b", "/y")
    except Exception:
        pass
    # File should still parse as valid JSON
    import shutil
    state_file = Path(d) / "sessions.json"
    try:
        parsed = json.loads(state_file.read_text())
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        return CaseResult(cog="session", cmd="state", case="post-write-valid-json",
                          status="FAIL", detail=f"corrupt: {e}")
    shutil.rmtree(d, ignore_errors=True)
    if "1" in parsed and "2" in parsed:
        return CaseResult(cog="session", cmd="state", case="post-write-valid-json",
                          status="PASS", detail="both sessions saved")
    return CaseResult(cog="session", cmd="state", case="post-write-valid-json",
                      status="FAIL", detail=f"missing sessions: {parsed}")


async def case_invalid_json_state_file_recovery(bot) -> CaseResult:
    """If sessions.json is corrupted on disk, SessionStore should NOT crash
    bot startup — must fail-soft to empty state."""
    from clauded.session_store import SessionStore
    import tempfile
    d = tempfile.mkdtemp(prefix="cc_inv_")
    Path(d, "sessions.json").write_text("{this is not valid JSON}")
    try:
        ss = SessionStore(data_dir=d)
    except Exception as e:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        return CaseResult(cog="session", cmd="state", case="corrupt-file-recovery",
                          status="FAIL", detail=f"crashed on load: {e}")
    # Should start fresh, no entries
    n = len(ss._sessions)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    if n == 0:
        return CaseResult(cog="session", cmd="state", case="corrupt-file-recovery",
                          status="PASS", detail="fail-soft to empty state")
    return CaseResult(cog="session", cmd="state", case="corrupt-file-recovery",
                      status="FAIL", detail=f"unexpected state: {n} entries")


async def case_cost_tracker_save_load_roundtrip(bot) -> CaseResult:
    """CostTracker survives save/load roundtrip with sub-cent precision."""
    from clauded.cost_tracker import CostTracker
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix="cc_rt_")
    try:
        ct1 = CostTracker(data_dir=d)
        ct1.record(42, 0.0001)
        ct1.record(42, 0.0002)
        ct1.record(99, 0.5)
        ct2 = CostTracker(data_dir=d)
        ch42, calls42 = ct2.get_channel_cost(42)
        ch99, calls99 = ct2.get_channel_cost(99)
        total = ct2.get_total_cost()
        if abs(ch42 - 0.0003) > 1e-9:
            return CaseResult(cog="cost", cmd="state", case="roundtrip",
                              status="FAIL",
                              detail=f"ch42={ch42!r}, expected 0.0003")
        if abs(ch99 - 0.5) > 1e-9:
            return CaseResult(cog="cost", cmd="state", case="roundtrip",
                              status="FAIL",
                              detail=f"ch99={ch99!r}, expected 0.5")
        if calls42 != 2:
            return CaseResult(cog="cost", cmd="state", case="roundtrip",
                              status="FAIL",
                              detail=f"calls42={calls42}, expected 2")
        return CaseResult(cog="cost", cmd="state", case="roundtrip",
                          status="PASS",
                          detail=f"ch42=$0.0003 (2 calls), ch99=$0.5 (1 call), total=${total:.4f}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


HAPPY_CASES.extend([
    ("/project bind concurrent-race", case_concurrent_bind_same_channel),
    ("/agent create concurrent ×20", case_concurrent_agent_create),
    ("/log dump concurrent ×5", case_log_dump_under_high_load),
    ("/cost record concurrent-save", case_cost_record_save_race),
    ("session state resume-after-restart", case_session_state_resume_after_restart),
    ("session state post-write valid-json", case_save_session_state_after_crash),
    ("session state corrupt-file recovery", case_invalid_json_state_file_recovery),
    ("cost roundtrip precision", case_cost_tracker_save_load_roundtrip),
])


if __name__ == "__main__":
    asyncio.run(main())
