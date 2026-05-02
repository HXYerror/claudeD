"""Unit tests for :class:`ClaudeBridge`.

We can't actually start a Claude session in tests, so we monkeypatch
``ClaudeSDKClient`` with an :class:`AsyncMock` and drive the bridge
through its public methods. The goal is to cover three behaviors:

1. An exception in the SDK stream flips ``is_active`` to False.
2. That same exception triggers a best-effort ``client.disconnect()``.
3. ``ResultMessage`` events update the cumulative ``total_cost`` /
   ``num_turns`` / ``model`` attributes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_code_sdk import ResultMessage

from clauded.claude_bridge import ClaudeBridge
from clauded.config import Config
from clauded.session_config import SessionConfig


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> Config:
    return Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )


def _make_client(receive_response_factory: Any) -> AsyncMock:
    """Build an AsyncMock standing in for ``ClaudeSDKClient``.

    ``receive_response`` is a *sync* method that returns an *async*
    iterator, so we wire it as a regular MagicMock side-effect rather
    than an AsyncMock.
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.receive_response = receive_response_factory
    return client


async def _async_iter(items: list[Any]):
    for item in items:
        yield item


async def _async_iter_then_raise(items: list[Any], exc: BaseException):
    for item in items:
        yield item
    raise exc


# ---------------------------------------------------------------------------
# send_message: exception path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_exception_marks_inactive(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """When the SDK raises, the bridge must flip ``is_active`` to False."""
    boom = RuntimeError("sdk crashed mid-stream")
    client = _make_client(lambda: _async_iter_then_raise([], boom))

    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()
    assert bridge.is_active is True

    with pytest.raises(RuntimeError, match="sdk crashed"):
        async for _ in bridge.send_message("hi"):
            pass

    assert bridge.is_active is False


@pytest.mark.asyncio
async def test_send_message_exception_disconnects_client(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """A stream failure must call ``disconnect`` on the underlying client."""
    boom = RuntimeError("bang")
    client = _make_client(lambda: _async_iter_then_raise([], boom))

    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()

    with pytest.raises(RuntimeError):
        async for _ in bridge.send_message("hello"):
            pass

    client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# ResultMessage stats propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_message_updates_stats(
    monkeypatch: pytest.MonkeyPatch, cfg: Config
) -> None:
    """``total_cost`` / ``num_turns`` / ``model`` are updated from ResultMessage."""
    rm = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=4,
        session_id="sess-1",
        total_cost_usd=0.1234,
    )
    # The bridge reads ``model`` off the ResultMessage via getattr; the SDK
    # type doesn't carry one, so we attach it directly.
    rm.model = "claude-sonnet-4-5"  # type: ignore[attr-defined]

    client = _make_client(lambda: _async_iter([rm]))
    monkeypatch.setattr(
        "clauded.claude_bridge.ClaudeSDKClient", lambda options=None: client
    )

    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    await bridge.start()
    assert bridge.total_cost == 0.0
    assert bridge.num_turns == 0
    assert bridge.model == "sonnet"  # defaults to config claude_model

    received: list[Any] = []
    async for event in bridge.send_message("ping"):
        received.append(event)

    assert received == [rm]
    assert bridge.total_cost == pytest.approx(0.1234)
    assert bridge.num_turns == 4
    assert bridge.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# SessionConfig integration
# ---------------------------------------------------------------------------


def test_bridge_accepts_session_config(cfg: Config) -> None:
    """ClaudeBridge can be constructed with a SessionConfig."""
    sc = SessionConfig(
        system_prompt="Be concise",
        model_override="opus",
        effort="high",
        max_budget_usd=10.0,
        user="testuser",
    )
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    assert bridge.system_prompt == "Be concise"
    assert bridge.model == "opus"
    assert bridge._effort == "high"
    assert bridge._max_budget_usd == 10.0
    assert bridge._user == "testuser"


def test_bridge_default_session_config(cfg: Config) -> None:
    """ClaudeBridge uses default SessionConfig when none is provided."""
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg)
    assert bridge.system_prompt is None
    assert bridge._effort is None
    assert bridge._user is None
