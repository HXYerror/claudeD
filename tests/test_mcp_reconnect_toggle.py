"""#audit(#14): /mcp reconnect + /mcp toggle — in-place MCP server control via
the SDK, no session restart. Covers the ClaudeBridge wrappers and the cog
commands (live-bridge dispatch + no-session refusal)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


def _cfg() -> Config:
    return Config(
        discord_bot_token="x",
        claude_model=None,
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _active_bridge() -> ClaudeBridge:
    b = ClaudeBridge("/tmp", _cfg(), SessionConfig())
    b._client = MagicMock()
    b._client.reconnect_mcp_server = AsyncMock()
    b._client.toggle_mcp_server = AsyncMock()
    b._active = True
    return b


# ---- bridge wrappers ----


@pytest.mark.asyncio
async def test_bridge_reconnect_delegates_to_client():
    b = _active_bridge()
    await b.reconnect_mcp_server("srv")
    b._client.reconnect_mcp_server.assert_awaited_once_with("srv")


@pytest.mark.asyncio
async def test_bridge_toggle_delegates_to_client():
    b = _active_bridge()
    await b.toggle_mcp_server("srv", False)
    b._client.toggle_mcp_server.assert_awaited_once_with("srv", False)


@pytest.mark.asyncio
async def test_bridge_mcp_ops_raise_when_inactive():
    b = ClaudeBridge("/tmp", _cfg(), SessionConfig())  # never started
    with pytest.raises(RuntimeError):
        await b.reconnect_mcp_server("srv")
    with pytest.raises(RuntimeError):
        await b.toggle_mcp_server("srv", True)


# ---- cog commands ----


def _interaction(channel_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.channel_id = channel_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _bot_with_bridge(bridge) -> MagicMock:
    from clauded.bot import ClaudedBot
    bot = MagicMock(spec=ClaudedBot)
    bot.session_manager = MagicMock()
    bot.session_manager.get_session = MagicMock(return_value=bridge)
    return bot


@pytest.mark.asyncio
async def test_mcp_reconnect_calls_bridge_on_live_session():
    from clauded.cogs.mcp import mcp_reconnect
    bridge = MagicMock()
    bridge.is_active = True
    bridge.reconnect_mcp_server = AsyncMock()
    interaction = _interaction()
    interaction.client = _bot_with_bridge(bridge)
    await mcp_reconnect.callback(interaction, "srv")
    bridge.reconnect_mcp_server.assert_awaited_once_with("srv")


@pytest.mark.asyncio
async def test_mcp_toggle_calls_bridge_on_live_session():
    from clauded.cogs.mcp import mcp_toggle
    bridge = MagicMock()
    bridge.is_active = True
    bridge.toggle_mcp_server = AsyncMock()
    interaction = _interaction()
    interaction.client = _bot_with_bridge(bridge)
    await mcp_toggle.callback(interaction, "srv", False)
    bridge.toggle_mcp_server.assert_awaited_once_with("srv", False)


@pytest.mark.asyncio
async def test_mcp_reconnect_refuses_without_session():
    from clauded.cogs.mcp import mcp_reconnect
    interaction = _interaction()
    interaction.client = _bot_with_bridge(None)  # no active session
    await mcp_reconnect.callback(interaction, "srv")
    # Refuses via response.send_message, never defers into a control-plane call.
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    assert "No active session" in msg
