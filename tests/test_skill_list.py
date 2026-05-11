"""Tests for ``/skill list`` (PRD docs/prd/v1.13-skill-list.md, issue #109).

Covers the PRD §Tests checklist:

* ``_classify`` table-driven over (user), (project), (plugin:foo), no
  suffix, and the edge case ``"foo (plugin:bar) more text"`` (must NOT
  be classified as a plugin because the suffix is not at end-of-string).
* Path A — active bridge piggyback.
* Path B — temp ``ClaudeSDKClient`` for bound + unbound channels.
* ``CLIConnectionError`` → red error embed.
* ``get_server_info()`` returns ``None`` → red error embed.
* Empty/built-in-only commands list → empty-state body.
* 50 user skills with long descriptions → no field >1024 chars and the
  truncation footer is appended when we have to drop rows.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import discord

from clauded.bot import ClaudedBot
from clauded.cogs.skill import _classify, skill_list


# ---------------------------------------------------------------------------
# _classify — table-driven
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cmd, expected",
    [
        # User skill
        (
            {"name": "crew", "description": "Multi-agent workflow (user)"},
            ("user", "crew", "Multi-agent workflow"),
        ),
        # Project skill
        (
            {"name": "testproj", "description": "Project-specific tooling (project)"},
            ("project", "testproj", "Project-specific tooling"),
        ),
        # Plugin skill
        (
            {"name": "myplug", "description": "From a plugin (plugin:myplug)"},
            ("plugin:myplug", "myplug", "From a plugin"),
        ),
        # Built-in (no suffix)
        (
            {"name": "clear", "description": "Clear the chat"},
            ("", "", ""),
        ),
        # Empty description — built-in / unrecognized
        (
            {"name": "noop", "description": ""},
            ("", "", ""),
        ),
        # None description — handled by "or ''"
        (
            {"name": "noop2", "description": None},
            ("", "", ""),
        ),
        # Edge: " (plugin:bar)" appears mid-string, description does NOT end
        # with ")". Must NOT classify as plugin.
        (
            {"name": "foo", "description": "foo (plugin:bar) more text"},
            ("", "", ""),
        ),
        # Edge: description ends with ")" but not the plugin marker.
        (
            {"name": "bar", "description": "does math (rounded)"},
            ("", "", ""),
        ),
        # Edge: trailing whitespace before the suffix is rstripped.
        (
            {"name": "tidy", "description": "Trim trailing spaces   (user)"},
            ("user", "tidy", "Trim trailing spaces"),
        ),
        # Edge: missing description key.
        (
            {"name": "anon"},
            ("", "", ""),
        ),
    ],
)
def test_classify(cmd, expected):
    assert _classify(cmd) == expected


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
    interaction.response.is_done = MagicMock(return_value=False)

    async def _defer(*_a, **_kw):
        interaction.response.is_done.configure_mock(return_value=True)

    interaction.response.defer = AsyncMock(side_effect=_defer)
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
async def test_path_a_uses_bridge_client_and_does_not_spin_temp():
    bridge_client = MagicMock()
    bridge_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)
    bridge = MagicMock()
    bridge.is_active = True
    bridge._client = bridge_client

    bot = _make_bot(is_bound=True, bridge=bridge)
    interaction = _make_interaction()
    interaction.client = bot

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        await skill_list.callback(interaction)

    bridge_client.get_server_info.assert_awaited_once()
    fake_ctor.assert_not_called()  # no Path B spin-up

    # The embed shape: 4 skills total (2 user + 1 project + 1 plugin),
    # built-ins filtered.
    sent = interaction.followup.send.await_args
    embed = sent.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "🧰 Skills (4)"
    field_names = [f.name for f in embed.fields]
    assert "Project" in field_names
    assert "User (Global)" in field_names
    assert "Plugin: myplug" in field_names
    # Bound: no unbound-footer.
    assert embed.footer.text in (None, discord.Embed.Empty if hasattr(discord.Embed, "Empty") else None)


# ---------------------------------------------------------------------------
# Path B — bound channel uses ["user", "project", "local"]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_b_bound_uses_full_setting_sources():
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
async def test_path_b_unbound_uses_user_only_and_adds_footer():
    bot = _make_bot(is_bound=False, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    # In an unbound channel the SDK would only return user + built-ins.
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
    # Footer presence is the key acceptance.
    assert embed.footer.text is not None
    assert "Unbound channel" in embed.footer.text
    assert "/project bind" in embed.footer.text


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cli_connection_error_returns_red_embed():
    from claude_agent_sdk import CLIConnectionError

    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    class _Boom:
        async def __aenter__(self):
            raise CLIConnectionError("boom")

        async def __aexit__(self, *a):
            return False

    with patch("clauded.cogs.skill.ClaudeSDKClient", return_value=_Boom()):
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "❌ Skills unavailable"
    assert "CLIConnectionError" in (embed.description or "")


@pytest.mark.asyncio
async def test_cli_not_found_error_returns_red_embed():
    from claude_agent_sdk import CLINotFoundError

    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    class _Boom:
        async def __aenter__(self):
            raise CLINotFoundError("no claude")

        async def __aexit__(self, *a):
            return False

    with patch("clauded.cogs.skill.ClaudeSDKClient", return_value=_Boom()):
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "❌ Skills unavailable"
    assert "CLINotFoundError" in (embed.description or "")


@pytest.mark.asyncio
async def test_get_server_info_returns_none_yields_red_embed():
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
async def test_empty_commands_yields_empty_state_line():
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
# Pagination guard — 50 long user skills must not overflow Discord limits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_many_skills_truncated_within_embed_budget():
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction()
    interaction.client = bot

    # 50 user skills, each with a ~200-char description so we definitely
    # blow past the per-field 1024-char cap if untruncated.
    long_desc = "x" * 200
    commands = [
        {"name": f"skill{i:02d}", "description": f"{long_desc} (user)"}
        for i in range(50)
    ]
    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value={"commands": commands})

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]

    # All fields stay within Discord's 1024-char value limit.
    for field in embed.fields:
        assert field.value is not None
        assert len(field.value) <= 1024, f"field {field.name!r} too long: {len(field.value)}"

    # Serialized embed stays within Discord's 6000-char total ceiling
    # (and we aim much lower per the PRD's 4000-char budget).
    assert len(embed) <= 6000

    # If anything was dropped, the truncation footer is present.
    truncation_present = any(
        "more skills" in (f.value or "") for f in embed.fields
    )
    # With 50×~200-char rows in a single field we definitely drop rows.
    assert truncation_present, "expected truncation notice with 50 long skills"


# ---------------------------------------------------------------------------
# Half-broken SKILL.md — empty description shows "_(no description)_"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_description_renders_no_description_placeholder():
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
    # Empty stripped desc → "_(no description)_"
    user_field = next(f for f in embed.fields if f.name == "User (Global)")
    assert "_(no description)_" in user_field.value


# ---------------------------------------------------------------------------
# Thread channel resolves to parent_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thread_channel_resolves_to_parent_id():
    bot = _make_bot(is_bound=True, bridge=None)
    interaction = _make_interaction(channel_id=4242, is_thread=True)
    interaction.client = bot

    fake_client = MagicMock()
    fake_client.get_server_info = AsyncMock(return_value=FIXTURE_INFO)

    with patch("clauded.cogs.skill.ClaudeSDKClient") as fake_ctor:
        fake_ctor.return_value = _FakeAsyncCtx(fake_client)
        await skill_list.callback(interaction)

    # parent_id is 4242 — that's what should be passed to project_manager.
    bot.project_manager.get_path_or_default.assert_called_with(4242)
