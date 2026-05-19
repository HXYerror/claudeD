"""Tests for the v1.18 ``/schedule`` cog + bot wiring (issue #241, Subtask 4).

Covers (per PRD ``docs/prd/v1.18-scheduler.md`` §8 Subtask 4):

* The 5 subcommands on :data:`clauded.cogs.schedule.schedule_group`
* Bot-side scheduler wiring (``_register_scheduler_ctx`` /
  ``_scheduler_ctx_provider``) and the
  ``claude_bridge.ClaudeBridge._build_mcp_servers`` merge.

We deliberately drive the cog ``.callback`` functions directly with mocked
``Interaction`` / ``Bot`` objects rather than spinning up a real Discord
gateway connection. This matches the pattern in the existing
``test_scheduler_mcp.py`` (call tool handlers directly via ``.handler``).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from clauded.cogs.schedule import (
    _REMINDER_MESSAGE,
    _REMINDER_NEW_TASK,
    _resolve_full_schedule_id,
    schedule_delete_cmd,
    schedule_group,
    schedule_list_cmd,
    schedule_message_cmd,
    schedule_new_task_cmd,
    schedule_toggle_cmd,
)
from clauded.scheduler import SchedulerManager
from clauded.scheduler_store import SchedulerStore


# ---------------------------------------------------------------- Helpers


def _future_iso(seconds: int = 3600) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return "iso: " + dt.isoformat()


async def _noop_cb(_sched):
    return None


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return SchedulerStore(data_dir=str(d))


@pytest.fixture
def mgr(store):
    return SchedulerManager(
        store,
        fire_message_callback=_noop_cb,
        fire_new_task_callback=_noop_cb,
        expire_notify_callback=_noop_cb,
    )


@pytest.fixture
def fake_bot(mgr):
    """A MagicMock standing in for :class:`ClaudedBot`.

    Wired up with the bits the cog actually touches: scheduler, store,
    project_manager (``is_bound``, ``get_path``, …), session_manager
    (``get_session`` returns active-by-default bridge), and the
    ``_register_scheduler_ctx`` callable that the cog invokes before
    handing off to the renderer.
    """
    bot = MagicMock()
    bot.scheduler = mgr
    bot.scheduler.store = mgr.store  # alias for cog convenience
    bot.scheduler_store = mgr.store
    # project_manager: pretend channel 100 is bound to /tmp/proj
    bot.project_manager.is_bound = MagicMock(return_value=True)
    bot.project_manager.get_path = MagicMock(return_value="/tmp/proj")
    bot.project_manager.get_system_prompt = MagicMock(return_value=None)
    bot.project_manager.get_extra_dirs = MagicMock(return_value=None)
    bot.project_manager.get_mcp_servers = MagicMock(return_value=None)
    bot.project_manager.get_env = MagicMock(return_value=None)
    # session_manager: live, active bridge
    bridge = MagicMock()
    bridge.is_active = True
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    bot.session_manager.get_stored_session = MagicMock(return_value=None)
    bot.session_manager.create_session = AsyncMock(return_value=bridge)
    # context registration
    bot._scheduler_current_ctx = {}

    def _register(*, thread_id, channel_id, guild_id, tz_name="Asia/Shanghai"):
        bot._scheduler_current_ctx = {
            "thread_id": thread_id,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "tz_name": tz_name,
        }
    bot._register_scheduler_ctx = MagicMock(side_effect=_register)
    return bot


def _make_thread_interaction(bot, thread_id=42, parent_id=100, user_id=999,
                             is_admin=False):
    """Build an ``Interaction``-like mock anchored in a Thread."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    thread.guild = MagicMock()
    thread.guild.id = 7
    thread.mention = f"<#{thread_id}>"
    thread.send = AsyncMock()

    user = MagicMock()
    user.id = user_id
    user.name = f"user{user_id}"
    # When testing admin override we set spec=discord.Member; otherwise
    # leave plain so the ``isinstance(..., Member)`` short-circuit fails.
    if is_admin:
        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.name = f"admin{user_id}"
        member.guild_permissions = MagicMock()
        member.guild_permissions.manage_guild = True
        user = member

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel = thread
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_channel_interaction(bot, channel_id=100, user_id=999):
    """Build an Interaction anchored in a plain TextChannel (no thread)."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.guild = MagicMock()
    ch.guild.id = 7

    user = MagicMock()
    user.id = user_id
    user.name = f"user{user_id}"

    interaction = MagicMock()
    interaction.client = bot
    interaction.channel = ch
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ============================================================
# /schedule message — reminder + ctx + render path
# ============================================================


@pytest.mark.asyncio
async def test_schedule_message_outside_thread_rejects(fake_bot):
    """PRD: /schedule message requires thread context."""
    interaction = _make_channel_interaction(fake_bot)
    await schedule_message_cmd.callback(interaction, "remind me at 9")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "thread" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_schedule_message_unbound_channel_rejects(fake_bot):
    """When parent channel is unbound, refuse with an ephemeral message."""
    fake_bot.project_manager.is_bound = MagicMock(return_value=False)
    interaction = _make_thread_interaction(fake_bot)
    await schedule_message_cmd.callback(interaction, "remind me at 9")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "bind" in args[0].lower() or "not bound" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_schedule_message_happy_path_renders_reminder(fake_bot):
    """Happy path: the cog invokes the renderer with the PRD §6.1 reminder
    (system-reminder + user-text) and registers the per-turn ctx."""
    interaction = _make_thread_interaction(fake_bot)
    user_text = "明天 9 点提醒我开会"
    # Patch the renderer at the module that USES it, not where it's defined.
    with patch("clauded.cogs.schedule.DiscordRenderer") as RendererCls:
        renderer_inst = MagicMock()
        renderer_inst.render_response = AsyncMock()
        RendererCls.return_value = renderer_inst
        await schedule_message_cmd.callback(interaction, user_text)

    # ack
    interaction.response.send_message.assert_awaited_once()
    # ctx
    fake_bot._register_scheduler_ctx.assert_called_once_with(
        thread_id=42, channel_id=100, guild_id=7,
    )
    # render
    renderer_inst.render_response.assert_awaited_once()
    rendered_text = renderer_inst.render_response.await_args.args[1]
    assert "<system-reminder>" in rendered_text
    assert "schedule_message" in rendered_text
    assert f"<user-text>{user_text}</user-text>" in rendered_text


# ============================================================
# /schedule new_task — reminder + ctx + render
# ============================================================


@pytest.mark.asyncio
async def test_schedule_new_task_outside_thread_rejects(fake_bot):
    interaction = _make_channel_interaction(fake_bot)
    await schedule_new_task_cmd.callback(interaction, "weekly report")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "thread" in args[0].lower()
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_schedule_new_task_happy_renders_reminder(fake_bot):
    interaction = _make_thread_interaction(fake_bot)
    user_text = "每周一 9am 整理周报"
    with patch("clauded.cogs.schedule.DiscordRenderer") as RendererCls:
        renderer_inst = MagicMock()
        renderer_inst.render_response = AsyncMock()
        RendererCls.return_value = renderer_inst
        await schedule_new_task_cmd.callback(interaction, user_text)
    interaction.response.send_message.assert_awaited_once()
    renderer_inst.render_response.assert_awaited_once()
    rendered_text = renderer_inst.render_response.await_args.args[1]
    assert "<system-reminder>" in rendered_text
    assert "schedule_new_task" in rendered_text
    assert f"<user-text>{user_text}</user-text>" in rendered_text


# ============================================================
# /schedule list — empty + populated + markers
# ============================================================


@pytest.mark.asyncio
async def test_schedule_list_empty(fake_bot):
    """No schedules → ephemeral '(no schedules)' message."""
    interaction = _make_thread_interaction(fake_bot)
    await schedule_list_cmd.callback(interaction, "thread")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "(no schedules)" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_schedule_list_renders_markers(fake_bot):
    """List emits embed with 📨 (message), 🧵 (new_task), 🤖/👤 markers."""
    # Insert one of each kind / creator combination.
    mgr = fake_bot.scheduler
    mgr.create(
        kind="message",
        when=_future_iso(7200),
        what="hi from claude",
        target_thread_id=42,
        name="claude-msg",
        created_by="claude",
        is_claude_created=True,
        channel_id=100,
        guild_id=7,
        tz_name="UTC",
    )
    mgr.create(
        kind="new_task",
        when=_future_iso(7200),
        what="hi from user",
        target_channel_id=100,
        name="user-task",
        created_by="999",
        channel_id=100,
        guild_id=7,
        tz_name="UTC",
    )
    interaction = _make_thread_interaction(fake_bot)
    await schedule_list_cmd.callback(interaction, "all")
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    embed = kwargs.get("embed")
    assert embed is not None
    desc = embed.description
    assert "📨" in desc        # kind=message marker
    assert "🧵" in desc        # kind=new_task marker
    assert "🤖" in desc        # claude creator marker
    assert "👤" in desc        # user creator marker


# ============================================================
# /schedule delete — permission matrix
# ============================================================


@pytest.mark.asyncio
async def test_schedule_delete_ambiguous_prefix(fake_bot):
    """Two ids share the same first 4 chars → cog refuses delete."""
    mgr = fake_bot.scheduler
    # Manually seed two schedules whose ids share a prefix.
    base = {
        "kind": "message",
        "name": "x",
        "created_by": "999",
        "channel_id": 100,
        "guild_id": 7,
        "target_thread_id": 42,
        "trigger": {
            "kind": "once", "iso": _future_iso(3600).split(": ", 1)[1],
            "cron": None, "tz_when_created": "UTC", "recurring": False,
        },
        "payload": {"what": "x"},
        "max_lifetime_seconds": None,
        "state": {
            "enabled": True,
            "next_fire_at": _future_iso(3600).split(": ", 1)[1],
            "first_fired_at": None, "last_fired_at": None,
            "last_error": None, "fire_count": 0, "missed_count": 0,
        },
    }
    s1 = dict(base, schedule_id="abcd0000aaaa1111")
    s2 = dict(base, schedule_id="abcd0000bbbb2222")
    mgr.store.add(s1)
    mgr.store.add(s2)

    interaction = _make_thread_interaction(fake_bot, user_id=999)
    await schedule_delete_cmd.callback(interaction, "abcd")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "Ambiguous" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_schedule_delete_non_creator_non_admin_denied(fake_bot):
    """A non-creator without admin can't delete a schedule (PRD §4.4)."""
    mgr = fake_bot.scheduler
    s = mgr.create(
        kind="message",
        when=_future_iso(3600),
        what="hi",
        target_thread_id=42,
        created_by="111",     # someone else
        channel_id=100,
        guild_id=7,
        tz_name="UTC",
    )
    interaction = _make_thread_interaction(fake_bot, user_id=999)
    await schedule_delete_cmd.callback(interaction, s["schedule_id"])
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "permission denied" in args[0]
    assert kwargs.get("ephemeral") is True
    # Schedule should still exist.
    assert mgr.store.get(s["schedule_id"]) is not None


@pytest.mark.asyncio
async def test_schedule_delete_by_creator_succeeds(fake_bot):
    mgr = fake_bot.scheduler
    s = mgr.create(
        kind="message",
        when=_future_iso(3600),
        what="hi",
        target_thread_id=42,
        created_by="999",
        channel_id=100,
        guild_id=7,
        tz_name="UTC",
    )
    interaction = _make_thread_interaction(fake_bot, user_id=999)
    await schedule_delete_cmd.callback(interaction, s["schedule_id"])
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "Deleted" in args[0]
    assert mgr.store.get(s["schedule_id"]) is None


# ============================================================
# /schedule toggle — admin override
# ============================================================


@pytest.mark.asyncio
async def test_schedule_toggle_admin_can_disable_others(fake_bot):
    mgr = fake_bot.scheduler
    s = mgr.create(
        kind="message",
        when=_future_iso(3600),
        what="hi",
        target_thread_id=42,
        created_by="111",   # someone else
        channel_id=100,
        guild_id=7,
        tz_name="UTC",
    )
    # interaction.user is a Member with manage_guild=True
    interaction = _make_thread_interaction(
        fake_bot, user_id=999, is_admin=True,
    )
    await schedule_toggle_cmd.callback(interaction, s["schedule_id"], False)
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert "enabled=False" in args[0]
    assert mgr.store.get(s["schedule_id"])["state"]["enabled"] is False


# ============================================================
# Sanity: the slash group exposes exactly 5 subcommands
# ============================================================


def test_schedule_group_has_five_subcommands():
    """Spec: 5 subcommands (message, new_task, list, delete, toggle)."""
    names = sorted(c.name for c in schedule_group.commands)
    assert names == ["delete", "list", "message", "new_task", "toggle"]


# ============================================================
# Bot wiring: ctx provider + bridge MCP merge
# ============================================================


@pytest.mark.asyncio
async def test_bot_ctx_provider_round_trip(tmp_path, monkeypatch):
    """``_register_scheduler_ctx`` + ``_scheduler_ctx_provider`` round-trip."""
    monkeypatch.chdir(tmp_path)
    from clauded.bot import ClaudedBot
    from clauded.config import Config
    cfg = Config(
        discord_bot_token="x",
        claude_model=None,
        claude_permission_mode="default",
        projects_root=str(tmp_path),
        allow_unbound_fallback=False,
    )
    bot = ClaudedBot(cfg)
    assert bot._scheduler_ctx_provider() == {}
    bot._register_scheduler_ctx(thread_id=42, channel_id=100, guild_id=7)
    ctx = bot._scheduler_ctx_provider()
    assert ctx == {
        "thread_id": 42,
        "channel_id": 100,
        "guild_id": 7,
        "tz_name": "Asia/Shanghai",
    }
    # Re-register with explicit tz override
    bot._register_scheduler_ctx(
        thread_id=1, channel_id=2, guild_id=3, tz_name="UTC",
    )
    assert bot._scheduler_ctx_provider()["tz_name"] == "UTC"


def test_claude_bridge_build_mcp_servers_merges_scheduler():
    """``_build_mcp_servers`` always includes the ``clauded-scheduler`` key."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config
    from clauded.session_config import SessionConfig

    cfg = Config(
        discord_bot_token="x",
        claude_model=None,
        claude_permission_mode="default",
        projects_root="/tmp",
        allow_unbound_fallback=False,
    )
    # Pre-populate a user-configured mcp dict so we can assert the merge
    # preserves both keys.
    sc = SessionConfig(mcp_servers={"user-mcp": {"command": "true"}})
    bridge = ClaudeBridge("/tmp", cfg, sc)
    merged = bridge._build_mcp_servers()
    assert "clauded-scheduler" in merged
    assert "user-mcp" in merged
    # Order: scheduler shouldn't overwrite user-mcp.
    assert merged["user-mcp"] == {"command": "true"}


def test_claude_bridge_build_mcp_servers_with_none():
    """``_mcp_servers=None`` still produces a dict with the scheduler key."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config
    from clauded.session_config import SessionConfig

    cfg = Config(
        discord_bot_token="x",
        claude_model=None,
        claude_permission_mode="default",
        projects_root="/tmp",
        allow_unbound_fallback=False,
    )
    sc = SessionConfig(mcp_servers=None)
    bridge = ClaudeBridge("/tmp", cfg, sc)
    merged = bridge._build_mcp_servers()
    assert list(merged.keys()) == ["clauded-scheduler"]


# ============================================================
# Helper: prefix resolution
# ============================================================


def test_resolve_full_schedule_id_unknown(fake_bot):
    full, err = _resolve_full_schedule_id(fake_bot, "deadbeef")
    assert full is None
    assert "Unknown" in err
