"""Parametrized tests verifying Group A commands call ``reject_if_unbound``.

PRD: ``docs/prd/v1.11-unbound-fallback.md`` Acceptance Criterion C.
Issue: #127.

The contract under test:
    On an unbound channel, a Group A command MUST send the unified ephemeral
    refusal message AND skip its underlying side-effect (no agent created, no
    MCP server registered, no session spawned, ...).

We pick one representative command per cog file. The helper itself is covered
by ``tests/test_unbound_handler.py`` (#126); here we just verify each cog
correctly applies the guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from clauded.bot import ClaudedBot
from clauded.cogs._unbound import UNBOUND_REFUSE_MESSAGE
from clauded.cogs.agent import agent_create
from clauded.cogs.mcp import mcp_add
from clauded.cogs.ops import review_pr
from clauded.cogs.project import env_set
from clauded.cogs.session import session_resume


def _make_interaction(*, channel_id: int = 1234) -> MagicMock:
    """Build a discord.Interaction stub whose channel is a bound TextChannel
    by default. Tests mutate ``bot.project_manager.is_bound`` to flip state.
    """
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = channel_id
    interaction.guild_id = 7777
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock(
        side_effect=lambda *a, **kw: interaction.response.is_done.configure_mock(
            return_value=True
        )
    )
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot(*, is_bound: bool) -> MagicMock:
    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = MagicMock()
    bot.project_manager.is_bound = MagicMock(return_value=is_bound)
    bot.project_manager.add_mcp_server = MagicMock()
    bot.project_manager.set_env = MagicMock()
    bot.project_manager.get_path = MagicMock(return_value=None)
    bot.project_manager.get_stored_session = MagicMock(return_value=None)
    bot.agent_manager = MagicMock()
    bot.agent_manager.create = MagicMock()
    return bot


# (cog_name, callback, kwargs_for_call, sentinel_check)
# sentinel_check: callable(bot) → asserts the side-effect helper was NOT invoked.
GROUP_A_CASES = [
    (
        "agent_create",
        agent_create.callback,
        {"name": "tester", "prompt": "hi", "description": ""},
        lambda bot: bot.agent_manager.create.assert_not_called(),
    ),
    (
        "mcp_add",
        mcp_add.callback,
        {"name": "srv", "command": "/bin/true", "args": ""},
        lambda bot: bot.project_manager.add_mcp_server.assert_not_called(),
    ),
    (
        "review_pr",
        review_pr.callback,
        {"pr": "42"},
        # /review never calls add_mcp_server; the meaningful no-side-effect
        # check is "we didn't call get_path" because reject_if_unbound returns
        # before that line.
        lambda bot: bot.project_manager.get_path.assert_not_called(),
    ),
    (
        "session_resume",
        session_resume.callback,
        {},
        lambda bot: bot.project_manager.get_stored_session.assert_not_called(),
    ),
    (
        "env_set",
        env_set.callback,
        {"key": "FOO", "value": "bar"},
        lambda bot: bot.project_manager.set_env.assert_not_called(),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,callback,kwargs,no_side_effect", GROUP_A_CASES, ids=[c[0] for c in GROUP_A_CASES]
)
async def test_group_a_refuses_on_unbound(
    name: str, callback, kwargs: dict, no_side_effect
) -> None:
    """Each Group A command, when run on an unbound channel, replies with the
    unified ephemeral refusal and DOES NOT invoke its side-effect helper.
    """
    interaction = _make_interaction()
    bot = _make_bot(is_bound=False)
    interaction.client = bot

    await callback(interaction, **kwargs)

    no_side_effect(bot)

    # Refusal message must have gone out — either via response or followup
    # depending on whether the command deferred first. Helper unit tests cover
    # the routing logic; here we just check ONE of them carries the message.
    response_calls = [
        ca.args + tuple(ca.kwargs.values())
        for ca in interaction.response.send_message.await_args_list
    ]
    followup_calls = [
        ca.args + tuple(ca.kwargs.values())
        for ca in interaction.followup.send.await_args_list
    ]
    refusal_seen = any(
        UNBOUND_REFUSE_MESSAGE in str(c) for c in response_calls + followup_calls
    )
    assert refusal_seen, (
        f"{name} did not send UNBOUND_REFUSE_MESSAGE. "
        f"response={response_calls} followup={followup_calls}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,callback,kwargs,no_side_effect", GROUP_A_CASES, ids=[c[0] for c in GROUP_A_CASES]
)
async def test_group_a_passes_through_on_bound(
    name: str, callback, kwargs: dict, no_side_effect
) -> None:
    """Each Group A command, when run on a bound channel, does NOT send the
    unified refusal. (The command may still bail for other reasons — e.g.,
    ``/session resume`` returning ``No saved session to resume.`` — but the
    bound check itself must pass through.)
    """
    interaction = _make_interaction()
    bot = _make_bot(is_bound=True)
    interaction.client = bot

    # Stub out anything that would otherwise blow up in the body. We only
    # care that the FIRST gate (reject_if_unbound) didn't fire.
    bot._recreate_session = AsyncMock(return_value=None)
    bot.session_manager = MagicMock()
    bot.session_manager.get_lock = MagicMock(return_value=AsyncMock())
    bot.session_manager.create_session = AsyncMock(return_value=None)

    try:
        await callback(interaction, **kwargs)
    except Exception:
        # Some commands fail downstream because of incomplete mocks; that's
        # fine — we only assert the helper didn't reject.
        pass

    response_calls = [
        ca.args + tuple(ca.kwargs.values())
        for ca in interaction.response.send_message.await_args_list
    ]
    followup_calls = [
        ca.args + tuple(ca.kwargs.values())
        for ca in interaction.followup.send.await_args_list
    ]
    refusal_seen = any(
        UNBOUND_REFUSE_MESSAGE in str(c) for c in response_calls + followup_calls
    )
    assert not refusal_seen, (
        f"{name} sent UNBOUND_REFUSE_MESSAGE on a BOUND channel. "
        f"response={response_calls} followup={followup_calls}"
    )
