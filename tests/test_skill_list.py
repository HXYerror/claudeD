"""Tests for ``/skill list`` (PRD docs/prd/v1.13-skill-list.md, issue #109).

Classify tests live in ``tests/test_skill_parser.py`` (R1 #C5 — parser
moved to ``clauded.skill_parser``). This file covers the cog wiring:
Path A piggyback, Path A → Path B fallthrough, Path B bound/unbound,
errors, empty state, truncation, half-broken descriptions, and channel
resolution (thread + DM).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

from clauded.bot import ClaudedBot
from clauded.cogs.skill import skill_list


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Mirrors the live-probe shape: 2 user + 1 project + 1 plugin + 3 built-ins.
FIXTURE_COMMANDS = [
    {"name": "crew", "description": "Multi-agent dev workflow (user)"},
    {"name": "opc", "description": "One-person company workflow (user)"},
    {"name": "testproj", "description": "Project helper (project)"},
    {"name": "myplug", "description": "Plugin command (plugin:myplug)"},
    {"name": "clear", "description": "Clear chat"},
    {"name": "compact", "description": "Compact context"},
    {"name": "init", "description": "Initialize project"},
]
FIXTURE_INFO = {"commands": FIXTURE_COMMANDS}


def _make_interaction(*, channel_id: int = 4242, is_thread: bool = False) -> MagicMock:
    if is_thread:
        channel = MagicMock(spec=discord.Thread)
        channel.id = 9999
        channel.parent_id = channel_id
    else:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
    interaction = MagicMock(spec=discord.Interaction)
    interaction.channel = channel
    interaction.channel_id = channel_id
    interaction.client = None  # set per-test
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_bot(*, is_bound: bool, bridge=None) -> MagicMock:
    bot = MagicMock(spec=ClaudedBot)
    bot.project_manager = MagicMock()
    bot.project_manager.get_path_or_default = MagicMock(
        return_value=(Path("/tmp/fake-cwd"), is_bound)
    )
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    return bot


class _FakeAsyncCtx:
    """Async-context-manager fake returning ``client`` from ``__aenter__``."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
# Path A — active bridge piggyback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_a_uses_bridge_and_does_not_spin_temp() -> None:
    bridge = MagicMock()
    bridge.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    bot = _make_bot(is_bound=True, bridge=bridge)
    interaction = _make_interaction()
    interaction.client = bot

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        await skill_list.callback(interaction)

    bridge.get_server_info.assert_awaited_once()
    fake_ctor.assert_not_called()  # no Path B spin-up

    sent = interaction.followup.send.await_args
    embed = sent.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "🧰 Skills (4)"
    field_names = [f.name for f in embed.fields]
    # Order pinned: Project → User (Global) → Plugin: <name> alphabetized.
    assert field_names == ["Project", "User (Global)", "Plugin: myplug"]
    # Bound: no unbound-footer.
    assert not embed.footer.text


# ---------------------------------------------------------------------------
# C1 — Path A → Path B fallthrough coverage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_a_get_server_info_raises_falls_through_to_path_b() -> None:
    """If ``bridge.get_server_info()`` raises, Path B runs and renders."""
    bridge = MagicMock()
    bridge.get_server_info = AsyncMock(side_effect=RuntimeError("boom"))

    bot = _make_bot(is_bound=True, bridge=bridge)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    bridge.get_server_info.assert_awaited_once()
    fake_ctor.assert_called_once()  # Path B ran
    fake_client.get_server_info.assert_awaited_once()

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (4)"


@pytest.mark.asyncio
async def test_path_a_inactive_bridge_skips_to_b() -> None:
    """Bridge present but wrapper returns None (inactive) → Path B runs."""
    bridge = MagicMock()
    bridge.get_server_info = AsyncMock(return_value=None)

    bot = _make_bot(is_bound=True, bridge=bridge)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    bridge.get_server_info.assert_awaited_once()
    fake_ctor.assert_called_once()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (4)"


@pytest.mark.asyncio
async def test_path_a_no_bridge_skips_to_b() -> None:
    """No bridge in session_manager → Path B runs directly."""
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    fake_ctor.assert_called_once()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (4)"


# ---------------------------------------------------------------------------
# Path B — bound channel uses ["user", "project", "local"]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_b_bound_uses_full_setting_sources() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor, patch(
        "clauded.cogs.skill.ClaudeAgentOptions"
    ) as fake_opts:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    fake_opts.assert_called_once()
    kwargs = fake_opts.call_args.kwargs
    assert kwargs["setting_sources"] == ["user", "project", "local"]
    assert kwargs["cwd"] == "/tmp/fake-cwd"
    fake_ctor.assert_called_once()
    fake_client.get_server_info.assert_awaited_once()

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (4)"


# ---------------------------------------------------------------------------
# Path B — unbound channel uses ["user"] only + footer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_b_unbound_uses_user_only_and_adds_footer() -> None:
    bot = _make_bot(is_bound=False, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    unbound_info = {
        "commands": [
            {"name": "crew", "description": "Multi-agent dev workflow (user)"},
            {"name": "opc", "description": "One-person company workflow (user)"},
            {"name": "clear", "description": "Clear chat"},
        ]
    }
    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=unbound_info)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor, patch(
        "clauded.cogs.skill.ClaudeAgentOptions"
    ) as fake_opts:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    kwargs = fake_opts.call_args.kwargs
    assert kwargs["setting_sources"] == ["user"]

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (2)"
    assert embed.footer.text is not None
    assert "Unbound channel" in embed.footer.text
    assert "/project bind" in embed.footer.text


# ---------------------------------------------------------------------------
# C3 + I2 — Single parametrized error-path test (no message-body leak per I1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        # Imported lazily to avoid coupling collection to SDK module shape.
        pytest.param("CLIConnectionError", id="cli-connection"),
        pytest.param("CLINotFoundError", id="cli-not-found"),
        pytest.param("RuntimeError", id="generic"),
    ],
)
async def test_path_b_exception_yields_red_embed(exc: str) -> None:
    if exc == "CLIConnectionError":
        from claude_agent_sdk import CLIConnectionError as ExcCls
        raised = ExcCls("/Users/operator/.claude/bin/claude exploded")
    elif exc == "CLINotFoundError":
        from claude_agent_sdk import CLINotFoundError as ExcCls
        raised = ExcCls("no claude at /Users/operator/.claude/bin")
    else:
        ExcCls = RuntimeError  # type: ignore[assignment]
        raised = RuntimeError("/Users/operator/secret-path")

    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    class _Boom:
        async def __aenter__(self):
            raise raised

        async def __aexit__(self, *a):
            return False

    with patch("clauded.cogs.skill.ClaudeSDKClient", return_value=_Boom()):
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "❌ Skills unavailable"
    # I1 — class name is shown for diagnostics.
    assert ExcCls.__name__ in (embed.description or "")
    # I1 — exception message body must NOT leak into the embed
    # (it can contain cli_path / home dirs / env from SDK strings).
    assert "/Users/operator" not in (embed.description or "")


@pytest.mark.asyncio
async def test_get_server_info_returns_none_yields_red_embed() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=None)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "❌ Skills unavailable"


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_commands_yields_empty_state_line() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(
        return_value={"commands": [{"name": "clear", "description": "Clear"}]}
    )

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🧰 Skills (0)"
    assert embed.description == "No user or project skills installed."


# ---------------------------------------------------------------------------
# C2 — Truncation: every field ≤1024; if budget blown, truncation notice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_truncates_when_many_skills() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    # 8 plugin groups × 10 skills each, each row ~120 chars after the
    # _DESC_TRUNCATE_AT slice. Per-group value ~1.2KB → trips per-field
    # cap and across 8 groups blows the 5500-char total budget, so the
    # drops notice MUST fire.
    long_desc = "x" * 200
    commands = []
    for plugin in "abcdefgh":
        for i in range(10):
            commands.append(
                {
                    "name": f"{plugin}{i:02d}",
                    "description": f"{long_desc} (plugin:{plugin})",
                }
            )
    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value={"commands": commands})

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]

    # No field exceeds Discord's per-field 1024-char ceiling.
    for field in embed.fields:
        assert field.value is not None
        assert len(field.value) <= 1024, f"field {field.name!r} too long: {len(field.value)}"

    # Serialized embed comfortably under Discord's 6000-char ceiling.
    assert len(embed) <= 6000

    # Truncation notice present as the final field.
    assert embed.fields, "expected at least one field"
    last = embed.fields[-1]
    assert "more skills" in (last.value or ""), (
        f"expected truncation notice in last field, got {last.name!r}={last.value!r}"
    )


# ---------------------------------------------------------------------------
# Half-broken SKILL.md — empty description shows "_(no description)_"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_description_renders_no_description_placeholder() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(
        return_value={
            "commands": [
                {"name": "halfbroken", "description": " (user)"},
            ]
        }
    )
    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    user_field = next(f for f in embed.fields if f.name == "User (Global)")
    assert "_(no description)_" in user_field.value


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thread_channel_resolves_to_parent_id() -> None:
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction(channel_id=4242, is_thread=True)
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    bot.project_manager.get_path_or_default.assert_called_with(4242)


@pytest.mark.asyncio
async def test_dm_channel_yields_must_be_in_channel() -> None:
    """C6: DM channel resolves to None → friendly error, no Path A/B."""
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot
    interaction.channel = MagicMock(spec=discord.DMChannel)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        await skill_list.callback(interaction)

    fake_ctor.assert_not_called()
    bot.project_manager.get_path_or_default.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    sent_msg = interaction.followup.send.await_args.args[0]
    assert "must be run in a channel" in sent_msg
