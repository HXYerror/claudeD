"""Context window is CLI-authoritative (dynamic), not a hardcoded table.

Originally #280 cached each model's window from a hardcoded ``KNOWN_MODELS``
table because ``get_context_usage()`` was believed to report the *previous*
model's ``maxTokens`` until the next turn round-tripped. That staleness is gone
(verified on CLI 2.1.212: ``set_model`` → ``get_context_usage`` reports the new
window immediately), and the hardcoded table is a drift trap — it can't know
``claude-opus-5`` = 200k vs ``claude-opus-5[1m]`` = 1M. So ``set_model`` no
longer populates ``_context_window_override``; the footer/``/context`` trust the
SDK's live ``maxTokens``.

This file covers:
  - the pure ``compute_global_context_pct`` override capability (kept as a
    dormant manual escape hatch — the function still honors an explicit
    override when one is passed);
  - ``set_model`` NOT hardcoding a window anymore;
  - the renderer footer honoring a manually-set override end-to-end.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# compute_global_context_pct — the override capability (dormant but intact).
# ---------------------------------------------------------------------------


def test_compute_uses_override_when_passed():
    """When an explicit override is passed, it wins over the SDK's maxTokens."""
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 200_000,
        "rawMaxTokens": 200_000,
        "totalTokens": 189_400,
        "percentage": 95,
        "model": "claude-sonnet-4-6",
        "categories": [
            {"name": "Messages", "tokens": 180_000},
            {"name": "System", "tokens": 9_400},
            {"name": "Free space", "tokens": 10_600},
        ],
    }
    result = compute_global_context_pct(cu, max_tokens_override=1_000_000)
    assert result is not None
    used, max_t, pct = result
    assert max_t == 1_000_000
    assert used == pytest.approx(189_400, abs=1.0)
    assert pct == pytest.approx(18.94, abs=0.05)


def test_compute_ignores_override_when_matches_sdk():
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 1_000_000,
        "totalTokens": 100_000,
        "categories": [
            {"name": "Messages", "tokens": 100_000},
            {"name": "Free space", "tokens": 900_000},
        ],
    }
    with_override = compute_global_context_pct(cu, max_tokens_override=1_000_000)
    no_override = compute_global_context_pct(cu)
    assert with_override == no_override


def test_compute_ignores_invalid_override():
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 200_000,
        "categories": [
            {"name": "Messages", "tokens": 50_000},
            {"name": "Free space", "tokens": 150_000},
        ],
    }
    for bad in (None, 0, -1, "1000000"):
        result = compute_global_context_pct(cu, max_tokens_override=bad)  # type: ignore[arg-type]
        assert result is not None
        assert result[1] == 200_000.0, f"bad override {bad!r} leaked into denominator"


def test_compute_default_uses_sdk_maxtokens():
    """No override → the SDK's (now always-correct) maxTokens is the denominator."""
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 1_000_000,  # e.g. opus-5[1m]
        "totalTokens": 568_000,
        "categories": [
            {"name": "Messages", "tokens": 568_000},
            {"name": "Free space", "tokens": 432_000},
        ],
    }
    result = compute_global_context_pct(cu)
    assert result is not None
    _used, max_t, pct = result
    assert max_t == 1_000_000
    assert pct == pytest.approx(56.8, abs=0.5)


# ---------------------------------------------------------------------------
# Bridge.set_model no longer hardcodes a window (dynamic-from-CLI).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_model_does_not_hardcode_context_window():
    """set_model must NOT cache a hardcoded window. The SDK reports the new
    model's maxTokens immediately, so a KNOWN_MODELS window is unnecessary AND a
    staleness trap (opus-5=200k vs opus-5[1m]=1M). The override stays None."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(discord_bot_token="tok", claude_model="sonnet",
                 claude_permission_mode="default", projects_root="/tmp")
    bridge = ClaudeBridge(project_path="/tmp", config=cfg)
    bridge._client = AsyncMock()
    bridge._client.set_model = AsyncMock()
    bridge._active = True

    for m in ("opus", "claude-opus-4-7", "claude-opus-5",
              "claude-opus-5[1m]", "some-future-model"):
        await bridge.set_model(m)
        assert bridge._context_window_override is None, (
            f"set_model({m!r}) must not hardcode a window; got "
            f"{bridge._context_window_override}"
        )
        assert bridge._model_override == m


# ---------------------------------------------------------------------------
# End-to-end: footer honors a manually-set override (dormant capability).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_honors_manual_context_window_override():
    """The renderer still honors an explicitly-set _context_window_override
    (the escape hatch), computing % against it instead of the SDK's value."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer
    from tests.conftest import FakeBridge, FakeTarget

    events = [
        AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-5[1m]",
                         parent_tool_use_id=None),
        ResultMessage(subtype="result", duration_ms=50, duration_api_ms=30,
                      is_error=False, num_turns=1, session_id="sess-ctx",
                      total_cost_usd=0.01, usage={"input_tokens": 50, "output_tokens": 20}),
    ]
    ctx_response = {
        "percentage": 95, "totalTokens": 189_400,
        "maxTokens": 200_000, "rawMaxTokens": 200_000, "model": "claude-sonnet-4-6",
        "categories": [
            {"name": "Messages", "tokens": 180_000},
            {"name": "Free space", "tokens": 10_600},
        ],
    }
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events, get_context_usage_returns=ctx_response)
    bridge._context_window_override = 1_000_000  # manual escape hatch

    await renderer.render_response(bridge, "hello")
    all_content = " ".join(m.content for m in target._sent if m.content)
    assert "🔥" not in all_content, f"stale 200k leaked: {all_content!r}"
    assert "🧠" in all_content, f"missing 🧠 segment: {all_content!r}"
    assert ("🧠 18%" in all_content) or ("🧠 19%" in all_content), (
        f"expected ~19% (189.4k / 1M): {all_content!r}"
    )
