"""Tests for CLAUDED_TESTBOT_ID env-gated bot-author allowlist (v1.18 smoke utility)."""
from unittest.mock import MagicMock
import pytest


class _Stub:
    """Bare minimal stand-in that ClaudedBot.on_message can dispatch on.
    We bypass ClaudedBot's full __init__ (needs discord client, intents,
    etc.) and only assert on the early-return short-circuits."""
    user_id = 99
    def __init__(self):
        self._user_obj = MagicMock(id=self.user_id)
    @property
    def user(self):
        return self._user_obj


def _make_msg(author_id: int, author_is_bot: bool, content: str = "x"):
    msg = MagicMock()
    msg.author.id = author_id
    msg.author.bot = author_is_bot
    msg.content = content
    msg.channel.id = 1
    return msg


@pytest.mark.asyncio
async def test_on_message_self_always_skipped(monkeypatch):
    """Self-bot messages MUST always be skipped even if testbot allowlist
    is set to bot's own id (misconfig guard against infinite loop)."""
    from clauded.bot import ClaudedBot
    monkeypatch.setenv("CLAUDED_TESTBOT_ID", "99")  # misconfig
    msg = _make_msg(author_id=99, author_is_bot=True)
    # If self-skip doesn't fire, the channel lookup will run and break on
    # the bare MagicMock. We want early-return BEFORE that.
    result = await ClaudedBot.on_message(_Stub(), msg)
    assert result is None  # early return


@pytest.mark.asyncio
async def test_on_message_other_bot_skipped_when_env_unset(monkeypatch):
    """Production: env unset → all other bots skipped."""
    from clauded.bot import ClaudedBot
    monkeypatch.delenv("CLAUDED_TESTBOT_ID", raising=False)
    msg = _make_msg(author_id=12345, author_is_bot=True)
    result = await ClaudedBot.on_message(_Stub(), msg)
    assert result is None


@pytest.mark.asyncio
async def test_on_message_other_bot_still_skipped_with_different_allowlist(monkeypatch):
    """Smoke mode set to bot A, message from bot B → still skipped."""
    from clauded.bot import ClaudedBot
    monkeypatch.setenv("CLAUDED_TESTBOT_ID", "555")
    msg = _make_msg(author_id=12345, author_is_bot=True)
    result = await ClaudedBot.on_message(_Stub(), msg)
    assert result is None


@pytest.mark.asyncio
async def test_on_message_allowlisted_bot_passes_early_filter(monkeypatch, caplog):
    """Smoke mode: env=555, message from bot 555 → passes the early bot
    filter and reaches the log.info call before downstream parts fail on
    the bare MagicMock. Use caplog to detect the on_message INFO log
    that runs RIGHT AFTER the early-return block."""
    import logging
    from clauded.bot import ClaudedBot
    monkeypatch.setenv("CLAUDED_TESTBOT_ID", "555")
    msg = _make_msg(author_id=555, author_is_bot=True)
    caplog.set_level(logging.INFO, logger="clauded.bot")
    # Will likely raise downstream when project_manager / bot.session_manager
    # are touched. We only care that the on_message INFO log fires (proving
    # we passed the early return).
    try:
        await ClaudedBot.on_message(_Stub(), msg)
    except Exception:
        pass
    info_records = [r for r in caplog.records if r.name == "clauded.bot" and "on_message" in r.message]
    assert info_records, "Allowlisted testbot was wrongly short-circuited before the on_message log"
