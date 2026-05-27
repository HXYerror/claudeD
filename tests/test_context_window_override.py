"""#280 — context-window override prevents stale maxTokens after /model switch.

After ``/model switch opus``, the SDK keeps returning the *previous*
model's ``maxTokens`` (e.g. sonnet's 200k) from ``get_context_usage()``
until the next turn round-trips. The bridge caches the new model's
window in ``_context_window_override`` so ``/context`` + the footer 🧠
segment can display the correct denominator immediately.

This test file covers:
  - the pure ``compute_global_context_pct`` override path
    (denominator switches to 1M while ``used`` stays truthful);
  - ``set_model`` populating ``_context_window_override`` from
    ``KNOWN_MODELS`` (by alias and by full id);
  - the renderer footer using the override end-to-end.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# Primary unit test — the one called out in the implementation plan.
# ---------------------------------------------------------------------------


def test_compute_uses_override_when_sdk_max_is_stale():
    """Mock bridge with _context_window_override=1_000_000, SDK returns
    maxTokens=200_000 — compute must use 1M for the denominator."""
    from clauded._context_usage import compute_global_context_pct

    # Simulated post-switch stale state: SDK still reports sonnet's 200k
    # window plus a Free space supplement consistent with that 200k frame.
    cu = {
        "maxTokens": 200_000,         # stale (sonnet)
        "rawMaxTokens": 200_000,
        "totalTokens": 189_400,
        "percentage": 95,
        "model": "claude-sonnet-4-6",
        "categories": [
            {"name": "Messages", "tokens": 180_000},
            {"name": "System", "tokens": 9_400},
            {"name": "Free space", "tokens": 10_600},  # 200k - 189.4k
        ],
    }

    # Mock bridge with the override field populated as it would be by
    # ``set_model("opus")``.
    bridge = MagicMock()
    bridge._context_window_override = 1_000_000

    result = compute_global_context_pct(
        cu,
        max_tokens_override=bridge._context_window_override,
    )
    assert result is not None
    used, max_t, pct = result
    # Denominator must be the override (1M), not SDK's stale 200k.
    assert max_t == 1_000_000, f"expected 1M denominator, got {max_t}"
    # Used = sdk_max - free_space = 200k - 10.6k = 189.4k (truthful).
    assert used == pytest.approx(189_400, abs=1.0)
    # Percentage = 189.4k / 1M ≈ 18.94% (not the misleading 94.7%).
    assert pct == pytest.approx(18.94, abs=0.05)


# ---------------------------------------------------------------------------
# Override edge cases on compute_global_context_pct
# ---------------------------------------------------------------------------


def test_compute_ignores_override_when_matches_sdk():
    """If override == SDK's maxTokens (SDK already caught up), behave as
    if no override was passed."""
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 1_000_000,
        "totalTokens": 100_000,
        "categories": [
            {"name": "Messages", "tokens": 100_000},
            {"name": "Free space", "tokens": 900_000},
        ],
    }
    # Override matches → use SDK's value
    with_override = compute_global_context_pct(cu, max_tokens_override=1_000_000)
    no_override = compute_global_context_pct(cu)
    assert with_override == no_override


def test_compute_ignores_invalid_override():
    """Negative / zero / non-numeric overrides are silently ignored."""
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
        # All map to: use SDK's 200k.
        assert result[1] == 200_000.0, f"bad override {bad!r} leaked into denominator"


def test_compute_override_falls_back_to_totalTokens_when_no_free_space():
    """Override still applies when free_space is absent (totalTokens path)."""
    from clauded._context_usage import compute_global_context_pct

    cu = {
        "maxTokens": 200_000,    # stale
        "totalTokens": 50_000,
    }
    result = compute_global_context_pct(cu, max_tokens_override=1_000_000)
    assert result is not None
    used, max_t, pct = result
    assert used == 50_000.0
    assert max_t == 1_000_000.0
    # 50k / 1M = 5%
    assert pct == pytest.approx(5.0, abs=0.01)


def test_compute_override_no_op_when_sdk_max_missing():
    """If SDK gives no maxTokens, override doesn't synthesize one — the
    SDK-percentage fallback path is preserved."""
    from clauded._context_usage import compute_global_context_pct

    cu = {"percentage": 42}
    result = compute_global_context_pct(cu, max_tokens_override=1_000_000)
    assert result is not None
    used, max_t, pct = result
    assert max_t == 0.0  # legacy contract — no denominator known
    assert pct == 42.0


# ---------------------------------------------------------------------------
# Bridge.set_model populates the override.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_model_caches_context_window_for_alias():
    """``set_model("opus")`` caches 1M from KNOWN_MODELS."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    bridge = ClaudeBridge(project_path="/tmp", config=cfg)
    bridge._client = AsyncMock()
    bridge._client.set_model = AsyncMock()
    bridge._active = True

    assert bridge._context_window_override is None
    await bridge.set_model("opus")
    assert bridge._context_window_override == 1_000_000


@pytest.mark.asyncio
async def test_set_model_caches_context_window_for_full_id():
    """``set_model("claude-opus-4-7")`` resolves through KNOWN_MODELS' id field."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    bridge = ClaudeBridge(project_path="/tmp", config=cfg)
    bridge._client = AsyncMock()
    bridge._client.set_model = AsyncMock()
    bridge._active = True

    await bridge.set_model("claude-opus-4-7")
    assert bridge._context_window_override == 1_000_000


@pytest.mark.asyncio
async def test_set_model_unknown_model_leaves_override_unset():
    """Unknown model names don't populate the override (fall back to SDK)."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    bridge = ClaudeBridge(project_path="/tmp", config=cfg)
    bridge._client = AsyncMock()
    bridge._client.set_model = AsyncMock()
    bridge._active = True

    await bridge.set_model("some-future-model-not-in-table")
    assert bridge._context_window_override is None


@pytest.mark.asyncio
async def test_set_model_resets_override_between_switches():
    """Switching opus → unknown clears the prior 1M cache so we don't
    keep showing opus's window for the wrong model."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    bridge = ClaudeBridge(project_path="/tmp", config=cfg)
    bridge._client = AsyncMock()
    bridge._client.set_model = AsyncMock()
    bridge._active = True

    await bridge.set_model("opus")
    assert bridge._context_window_override == 1_000_000
    await bridge.set_model("totally-unknown")
    assert bridge._context_window_override is None


# ---------------------------------------------------------------------------
# End-to-end: footer renders against override after model switch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_uses_context_window_override_after_switch():
    """End-to-end: bridge has _context_window_override=1M, SDK still
    returns 200k. Footer 🧠 % must be computed against 1M (≈19%), not
    200k (≈95%)."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ResultMessage
    from clauded.discord_renderer import DiscordRenderer

    from tests.conftest import FakeBridge, FakeTarget

    events = [
        AssistantMessage(
            content=[TextBlock(text="ok")],
            model="claude-opus-4-7",
            parent_tool_use_id=None,
        ),
        ResultMessage(
            subtype="result", duration_ms=50, duration_api_ms=30,
            is_error=False, num_turns=1, session_id="sess-280",
            total_cost_usd=0.01,
            usage={"input_tokens": 50, "output_tokens": 20},
        ),
    ]
    # Stale SDK data (sonnet's 200k frame).
    ctx_response = {
        "percentage": 95,
        "totalTokens": 189_400,
        "maxTokens": 200_000,
        "rawMaxTokens": 200_000,
        "model": "claude-sonnet-4-6",
        "categories": [
            {"name": "Messages", "tokens": 180_000},
            {"name": "Free space", "tokens": 10_600},
        ],
    }
    target = FakeTarget()
    renderer = DiscordRenderer(target)
    bridge = FakeBridge(events, get_context_usage_returns=ctx_response)
    # Simulate post-/model-switch state.
    bridge._context_window_override = 1_000_000

    await renderer.render_response(bridge, "hello")

    all_content = " ".join(m.content for m in target._sent if m.content)
    # 189.4k / 1M ≈ 18.94% → footer integer tier renders "🧠 19%" (or
    # `18%` if truncated). Crucially: must NOT show the misleading
    # 95% / ⚠️ / 🔥 that the stale 200k would have produced.
    assert "🔥" not in all_content, (
        f"expected calm 🧠 segment with override; got 🔥 (stale 200k leaked): {all_content!r}"
    )
    assert "⚠️ " not in all_content or "🧠" in all_content, (
        f"expected 🧠 segment, not ⚠️ from stale-window threshold: {all_content!r}"
    )
    assert "🧠" in all_content, f"missing 🧠 segment entirely: {all_content!r}"
    # Sanity-check the actual rounded percentage neighborhood.
    assert ("🧠 18%" in all_content) or ("🧠 19%" in all_content), (
        f"expected ~19% (189.4k / 1M), got: {all_content!r}"
    )
