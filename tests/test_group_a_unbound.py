"""Parametrized tests verifying Group A commands call ``reject_if_unbound``.

Each Group A command MUST, on an unbound channel, send the unified
ephemeral refusal AND skip its underlying side-effect (no agent created,
no MCP server registered, no session spawned, ...).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import discord

from clauded.bot import ClaudedBot
from clauded.cogs._unbound import UNBOUND_REFUSE_MESSAGE
from clauded.cogs.agent import agent_create, agent_delete
from clauded.cogs.mcp import mcp_add, mcp_add_url, mcp_remove
from clauded.cogs.ops import plugin_add, review_pr
from clauded.cogs.project import env_remove, env_set, project_add_dir
from clauded.cogs.session import (
    session_fork,
    session_resume,
    session_security_review,
    session_settings,
    session_worktree,
)


def _make_interaction(*, channel_id: int = 1234) -> MagicMock:
    """Build a discord.Interaction stub. Tests mutate ``bot.project_manager.is_bound``
    to flip channel state.
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
    bot.project_manager.remove_mcp_server = MagicMock(return_value=True)
    bot.project_manager.set_env = MagicMock()
    bot.project_manager.remove_env = MagicMock(return_value=True)
    bot.project_manager.add_extra_dir = MagicMock(return_value="/tmp/x")
    bot.project_manager.get_path = MagicMock(return_value=None)
    bot.project_manager.get_stored_session = MagicMock(return_value=None)
    bot.agent_manager = MagicMock()
    bot.agent_manager.create = MagicMock()
    bot.agent_manager.delete = MagicMock(return_value=True)
    bot._recreate_session = AsyncMock(return_value=None)
    return bot


# (cog_name, callback, kwargs_for_call, sentinel_check)
# sentinel_check: callable(bot) → asserts the side-effect helper was NOT
# invoked. Each entry covers a distinct (cog × subcommand) site so we
# catch a future copy-paste site that forgets the guard.
GROUP_A_CASES = [
    # cogs/agent.py
    (
        "agent_create",
        agent_create.callback,
        {"name": "tester", "prompt": "hi", "description": ""},
        lambda bot: bot.agent_manager.create.assert_not_called(),
    ),
    (
        "agent_delete",
        agent_delete.callback,
        {"name": "tester"},
        lambda bot: bot.agent_manager.delete.assert_not_called(),
    ),
    # cogs/mcp.py
    (
        "mcp_add",
        mcp_add.callback,
        {"name": "srv", "command": "/bin/true", "args": ""},
        lambda bot: bot.project_manager.add_mcp_server.assert_not_called(),
    ),
    (
        "mcp_add_url",
        mcp_add_url.callback,
        {"name": "srv", "url": "https://example.com/mcp"},
        lambda bot: bot.project_manager.add_mcp_server.assert_not_called(),
    ),
    (
        "mcp_remove",
        mcp_remove.callback,
        {"name": "srv"},
        lambda bot: bot.project_manager.remove_mcp_server.assert_not_called(),
    ),
    # cogs/ops.py
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
        "plugin_add",
        plugin_add.callback,
        {"path": "/tmp"},
        lambda bot: bot._recreate_session.assert_not_called(),
    ),
    # cogs/session.py
    (
        "session_resume",
        session_resume.callback,
        {},
        lambda bot: bot.project_manager.get_stored_session.assert_not_called(),
    ),
    (
        "session_fork",
        session_fork.callback,
        {},
        lambda bot: bot._recreate_session.assert_not_called(),
    ),
    (
        "session_worktree",
        session_worktree.callback,
        {"name": "feat"},
        lambda bot: bot._recreate_session.assert_not_called(),
    ),
    (
        "session_security_review",
        session_security_review.callback,
        {},
        lambda bot: bot._recreate_session.assert_not_called(),
    ),
    (
        "session_settings",
        session_settings.callback,
        {"json_str": "{}"},
        lambda bot: bot._recreate_session.assert_not_called(),
    ),
    # cogs/project.py
    (
        "env_set",
        env_set.callback,
        {"key": "FOO", "value": "bar"},
        lambda bot: bot.project_manager.set_env.assert_not_called(),
    ),
    (
        "env_remove",
        env_remove.callback,
        {"key": "FOO"},
        lambda bot: bot.project_manager.remove_env.assert_not_called(),
    ),
    (
        "project_add_dir",
        project_add_dir.callback,
        {"path": "/tmp"},
        lambda bot: bot.project_manager.add_extra_dir.assert_not_called(),
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
    unified refusal. We also positively assert that ``is_bound`` was
    consulted, which proves the gate executed (and didn't reject) instead
    of being silently bypassed by a future refactor.
    """
    interaction = _make_interaction()
    bot = _make_bot(is_bound=True)
    interaction.client = bot

    bot.session_manager = MagicMock()
    bot.session_manager.get_lock = MagicMock(return_value=AsyncMock())
    bot.session_manager.create_session = AsyncMock(return_value=None)
    bot.session_manager.stop_session = AsyncMock(return_value=True)
    bot.session_manager.get_session = MagicMock(return_value=None)

    try:
        await callback(interaction, **kwargs)
    except Exception:
        # Some commands fail downstream because of incomplete mocks; the
        # gate's outcome was already captured above.
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

    bot.project_manager.is_bound.assert_called()
