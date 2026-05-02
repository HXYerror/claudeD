"""Tests for :class:`SessionConfig` dataclass."""

from __future__ import annotations

import pytest

from clauded.session_config import SessionConfig


def test_default_values():
    """SessionConfig defaults are all None/empty/False."""
    sc = SessionConfig()
    assert sc.system_prompt is None
    assert sc.model_override is None
    assert sc.resume_session_id is None
    assert sc.effort is None
    assert sc.allowed_tools == []
    assert sc.disallowed_tools == []
    assert sc.max_budget_usd is None
    assert sc.fork_session is False
    assert sc.add_dirs is None
    assert sc.from_pr is None
    assert sc.worktree is None
    assert sc.agent_name is None
    assert sc.custom_agents is None
    assert sc.mcp_servers is None
    assert sc.max_turns is None
    assert sc.fallback_model is None
    assert sc.plugin_dirs is None
    assert sc.settings is None
    assert sc.env is None
    assert sc.user is None
    assert sc.on_ask_user is None
    assert sc.on_pre_tool_use is None
    assert sc.on_post_tool_use is None
    assert sc.on_stop is None


def test_with_values():
    """SessionConfig stores provided values."""
    sc = SessionConfig(
        system_prompt="Be helpful",
        model_override="opus",
        effort="high",
        allowed_tools=["Bash", "Read"],
        max_budget_usd=5.0,
        fork_session=True,
        user="testuser#1234",
    )
    assert sc.system_prompt == "Be helpful"
    assert sc.model_override == "opus"
    assert sc.effort == "high"
    assert sc.allowed_tools == ["Bash", "Read"]
    assert sc.max_budget_usd == 5.0
    assert sc.fork_session is True
    assert sc.user == "testuser#1234"


def test_allowed_tools_independent_instances():
    """Each SessionConfig gets its own list for allowed_tools."""
    sc1 = SessionConfig()
    sc2 = SessionConfig()
    sc1.allowed_tools.append("Bash")
    assert sc2.allowed_tools == []  # not contaminated


def test_kwargs_construction():
    """SessionConfig can be created with **kwargs."""
    overrides = {
        "model_override": "haiku",
        "effort": "low",
        "max_turns": 10,
    }
    sc = SessionConfig(**overrides)
    assert sc.model_override == "haiku"
    assert sc.effort == "low"
    assert sc.max_turns == 10


def test_env_value_masking():
    """Test the env value masking logic used in /env list."""
    def _mask(v: str) -> str:
        return v[:2] + "****" + v[-2:] if len(v) > 6 else "****"

    assert _mask("supersecretkey") == "su****ey"
    assert _mask("short") == "****"
    assert _mask("1234567") == "12****67"
    assert _mask("ab") == "****"
    assert _mask("") == "****"
