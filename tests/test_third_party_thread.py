"""#195 — bot must NOT auto-engage in 3rd-party threads (owner_id != bot).

Pre-fix: bot would silently respond to every message in any thread
opened by a third party in a bound channel — leaking cwd to random
users, burning tokens.

Fix: thread.owner_id != bot.id → require explicit @mention or
matching role-mention to engage. Bot-created thread behavior
unchanged (v1.1 PRD F1).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
import discord


class _StubBot:
    """Minimal stand-in for ClaudedBot; bypasses full __init__."""
    def __init__(self, *, bot_id=1499415416701980704, bot_name="ClaudeBot",
                 allow_unbound_fallback=False, bound_parents=None):
        # MagicMock interprets ``name=`` as the mock's own debug name; set
        # the ``.name`` attribute explicitly so production reads work.
        self._user = MagicMock(id=bot_id)
        self._user.name = bot_name
        self.allow_unbound_fallback = allow_unbound_fallback
        bound = set(bound_parents or [])
        self.project_manager = MagicMock()
        self.project_manager.is_bound = MagicMock(side_effect=lambda pid: pid in bound)
        self.project_manager.should_refuse_unbound = MagicMock(return_value=False)
        # get_path_or_default — hit only after both gates pass
        from pathlib import Path
        self.project_manager.get_path_or_default = MagicMock(
            return_value=(Path("/tmp"), True)
        )
        self.session_manager = MagicMock()
        self.session_manager.get_session = MagicMock(return_value=None)
        self.session_manager.create_session = AsyncMock()
        self.session_manager.get_lock = MagicMock(return_value=MagicMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock()
        ))
        self._logged_third_party_thread: set[int] = set()
        self.create_session_calls = []

    @property
    def user(self):
        return self._user


def _make_message(*, thread_owner_id, parent_id, bot_id=1499415416701980704,
                   mentions=None, role_mentions=None, content="hello"):
    """Build a Message-like mock with a discord.Thread channel."""
    msg = MagicMock()
    # Use a real Thread-like — duck-typed via isinstance check;
    # we have to use a MagicMock that PASSES isinstance(thread, discord.Thread).
    # discord.Thread's isinstance check works with any subclass; build a
    # thin subclass via type() so isinstance works.
    class _FakeThread(discord.Thread):
        def __init__(self):
            # Bypass parent __init__ which requires discord state
            pass
    thread = _FakeThread()
    # Manually set attrs the bot reads
    object.__setattr__(thread, "id", 9999)
    object.__setattr__(thread, "owner_id", thread_owner_id)
    object.__setattr__(thread, "parent_id", parent_id)
    msg.channel = thread
    msg.id = 8888
    msg.content = content
    msg.author = MagicMock(id=1, bot=False)
    msg.author.id = 1
    msg.mentions = mentions or []
    msg.role_mentions = role_mentions or []
    msg.reply = AsyncMock()
    return msg, thread


@pytest.mark.asyncio
async def test_third_party_thread_no_mention_silently_ignored():
    """Bound parent + 3rd-party thread (owner != bot) + no @mention →
    silent return; no session created."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bound_parents=[1234])
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234)
    # Patch _handle_thread_message helper deps: get_lock
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    # Patch the _recreate path too
    bot._recreate_session = AsyncMock()
    # Call the handler directly
    await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    # MUST NOT create session
    assert not bot.session_manager.create_session.await_args_list, (
        "Bot must not call create_session in 3rd-party thread without @"
    )
    # And session_manager.get_session never queried (entry gate returns first)
    bot.session_manager.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_third_party_thread_with_at_mention_engages():
    """3rd-party thread + @bot in mentions → bot engages (existing
    behavior post-fix)."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bound_parents=[1234])
    mention_user = MagicMock(id=1499415416701980704)  # bot's own id
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234,
                            mentions=[mention_user])
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    # We just need to verify the handler doesn't return early — it
    # tries to acquire a lock + look up the session. So the test
    # checks get_session WAS called (which means we passed the gate).
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass  # Downstream session creation likely fails on bare mock, fine
    bot.project_manager.get_path_or_default.assert_called(), (
        "Bot must pass the entry gate when @-mentioned in 3rd-party thread"
    )


@pytest.mark.asyncio
async def test_third_party_thread_with_role_mention_engages():
    """3rd-party thread + role mention matching bot name → engages."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bot_name="ClaudeBot", bound_parents=[1234])
    role = MagicMock()
    role.name = "claudebot"  # case-insensitive match
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234,
                            role_mentions=[role])
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass
    bot.project_manager.get_path_or_default.assert_called(), (
        "Bot must pass the entry gate when role-mentioned matching its name"
    )


@pytest.mark.asyncio
async def test_bot_owned_thread_no_mention_still_engages():
    """v1.1 PRD F1 preserved: bot-created thread → @ not required."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bound_parents=[1234])
    msg, _ = _make_message(thread_owner_id=1499415416701980704,  # bot's own id
                            parent_id=1234)
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass
    bot.project_manager.get_path_or_default.assert_called(), (
        "Bot-owned thread must continue working without @mention (PRD F1)"
    )


@pytest.mark.asyncio
async def test_third_party_thread_with_fallback_true_still_ignored():
    """Critical negative invariant: even with allow_unbound_fallback=True,
    ownership check fires first → 3rd-party threads stay silent."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bound_parents=[1234], allow_unbound_fallback=True)
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234)
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    bot.session_manager.get_session.assert_not_called(), (
        "Ownership check must run BEFORE the unbound-fallback gate; "
        "3rd-party thread must stay silent regardless of fallback flag"
    )


@pytest.mark.asyncio
async def test_third_party_thread_logs_once_per_thread():
    """Repeat messages in the same 3rd-party thread log INFO once,
    then stay silent on subsequent messages from the same thread."""
    from clauded.bot import ClaudedBot
    import logging
    bot = _StubBot(bound_parents=[1234])
    msg1, _ = _make_message(thread_owner_id=99999, parent_id=1234)
    msg2, _ = _make_message(thread_owner_id=99999, parent_id=1234)
    # Same thread.id on both
    msg2.channel = msg1.channel
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    await ClaudedBot._handle_thread_message(bot, msg1, parent_id=1234)
    await ClaudedBot._handle_thread_message(bot, msg2, parent_id=1234)
    # Thread id logged once
    assert 9999 in bot._logged_third_party_thread


@pytest.mark.asyncio
async def test_role_mention_case_insensitive():
    """Role mention match is case-insensitive."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bot_name="ClaudeBot", bound_parents=[1234])
    role = MagicMock()
    role.name = "CLAUDEBOT"  # all caps
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234,
                            role_mentions=[role])
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    try:
        await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    except Exception:
        pass
    bot.project_manager.get_path_or_default.assert_called()


@pytest.mark.asyncio
async def test_role_mention_no_name_does_not_engage():
    """Edge: role with empty name doesn't false-trigger."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bot_name="ClaudeBot", bound_parents=[1234])
    role = MagicMock()
    role.name = ""
    msg, _ = _make_message(thread_owner_id=99999, parent_id=1234,
                            role_mentions=[role])
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    await ClaudedBot._handle_thread_message(bot, msg, parent_id=1234)
    bot.session_manager.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_unbound_parent_third_party_thread_also_silent():
    """Edge: unbound parent + 3rd-party thread + no @ → still silent
    (the existing bound gate kicks in but ownership runs first)."""
    from clauded.bot import ClaudedBot
    bot = _StubBot(bound_parents=[])  # NOT bound
    msg, _ = _make_message(thread_owner_id=99999, parent_id=5555)
    bot.session_manager.get_lock = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock()
    ))
    await ClaudedBot._handle_thread_message(bot, msg, parent_id=5555)
    bot.session_manager.get_session.assert_not_called()
    # No reply attempted either
    msg.reply.assert_not_called()
