"""#audit(live-log): the CommandTree error handler must swallow expired
interactions (discord.NotFound 10062) at WARNING instead of letting them
escalate to full CommandInvokeError tracebacks, and surface other errors as a
friendly ephemeral message."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from clauded.bot import ClaudedBot


def _interaction(*, is_done: bool, cmd: str = "project bind") -> MagicMock:
    interaction = MagicMock()
    interaction.command = MagicMock()
    interaction.command.qualified_name = cmd
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=is_done)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _expired_10062() -> discord.NotFound:
    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    err = discord.NotFound(resp, {"code": 10062, "message": "Unknown interaction"})
    assert err.code == 10062  # sanity: discord parsed the code
    return err


@pytest.mark.asyncio
async def test_tree_error_swallows_expired_interaction_10062():
    interaction = _interaction(is_done=True)
    # Must NOT raise, and must NOT try to send a user-facing message.
    await ClaudedBot._on_app_command_error(MagicMock(), interaction, _expired_10062())
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tree_error_reports_other_errors_friendly():
    interaction = _interaction(is_done=False, cmd="session info")
    await ClaudedBot._on_app_command_error(
        MagicMock(), interaction, RuntimeError("boom")
    )
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    assert "failed" in msg.lower()


@pytest.mark.asyncio
async def test_tree_error_uses_followup_when_response_done():
    interaction = _interaction(is_done=True, cmd="session info")
    await ClaudedBot._on_app_command_error(
        MagicMock(), interaction, RuntimeError("boom")
    )
    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()
