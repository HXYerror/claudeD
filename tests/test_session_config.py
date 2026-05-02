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


def test_bare_default():
    """SessionConfig.bare defaults to False."""
    sc = SessionConfig()
    assert sc.bare is False


def test_session_name_default():
    """SessionConfig.session_name defaults to None."""
    sc = SessionConfig()
    assert sc.session_name is None


def test_bare_and_session_name():
    """SessionConfig stores bare and session_name values."""
    sc = SessionConfig(bare=True, session_name="my-session")
    assert sc.bare is True
    assert sc.session_name == "my-session"


def test_retry_preserves_full_config():
    """Simulate retry config preservation — all fields carry over."""
    original = SessionConfig(
        system_prompt="test",
        model_override="opus",
        effort="high",
        allowed_tools=["Bash"],
        disallowed_tools=["Write"],
        max_budget_usd=10.0,
        fork_session=True,
        add_dirs=["/extra"],
        from_pr="123",
        worktree="feat-branch",
        agent_name="reviewer",
        custom_agents={"reviewer": {"prompt": "review"}},
        mcp_servers={"srv": {"type": "stdio"}},
        max_turns=5,
        fallback_model="haiku",
        plugin_dirs=["/plugins"],
        settings='{"k": "v"}',
        env={"KEY": "VAL"},
        user="alice#1234",
        bare=True,
        session_name="my-session",
    )
    # Simulate what _on_retry does
    retry_sc = SessionConfig(
        system_prompt=original.system_prompt,
        model_override=original.model_override,
        effort=original.effort,
        allowed_tools=list(original.allowed_tools) if original.allowed_tools else [],
        disallowed_tools=list(original.disallowed_tools) if original.disallowed_tools else [],
        max_budget_usd=original.max_budget_usd,
        fork_session=original.fork_session,
        add_dirs=original.add_dirs,
        from_pr=original.from_pr,
        worktree=original.worktree,
        agent_name=original.agent_name,
        custom_agents=original.custom_agents,
        mcp_servers=original.mcp_servers,
        max_turns=original.max_turns,
        fallback_model=original.fallback_model,
        plugin_dirs=list(original.plugin_dirs) if original.plugin_dirs else None,
        settings=original.settings,
        env=original.env,
        user=original.user,
        bare=original.bare,
        session_name=original.session_name,
        on_ask_user=None,  # fresh handler
    )
    assert retry_sc.system_prompt == "test"
    assert retry_sc.model_override == "opus"
    assert retry_sc.effort == "high"
    assert retry_sc.allowed_tools == ["Bash"]
    assert retry_sc.disallowed_tools == ["Write"]
    assert retry_sc.max_budget_usd == 10.0
    assert retry_sc.fork_session is True
    assert retry_sc.add_dirs == ["/extra"]
    assert retry_sc.from_pr == "123"
    assert retry_sc.worktree == "feat-branch"
    assert retry_sc.agent_name == "reviewer"
    assert retry_sc.custom_agents == {"reviewer": {"prompt": "review"}}
    assert retry_sc.mcp_servers == {"srv": {"type": "stdio"}}
    assert retry_sc.max_turns == 5
    assert retry_sc.fallback_model == "haiku"
    assert retry_sc.plugin_dirs == ["/plugins"]
    assert retry_sc.settings == '{"k": "v"}'
    assert retry_sc.env == {"KEY": "VAL"}
    assert retry_sc.user == "alice#1234"
    assert retry_sc.bare is True
    assert retry_sc.session_name == "my-session"


def test_env_value_masking_bot_style():
    """Test the exact masking logic used in bot.py /env list."""
    def _mask(v: str) -> str:
        return v[:2] + "****" if len(v) > 4 else "****"

    assert _mask("supersecretkey") == "su****"
    assert _mask("short") == "sh****"
    assert _mask("12345") == "12****"
    assert _mask("abcd") == "****"
    assert _mask("ab") == "****"
    assert _mask("") == "****"


def test_bridge_passes_user_to_options():
    """Verify ClaudeBridge stores user for passing to ClaudeCodeOptions."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    sc = SessionConfig(user="bob#5678")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    assert bridge._user == "bob#5678"


def test_bridge_stores_bare_and_session_name():
    """Verify ClaudeBridge stores bare and session_name from SessionConfig."""
    from clauded.claude_bridge import ClaudeBridge
    from clauded.config import Config

    cfg = Config(
        discord_bot_token="tok",
        claude_model="sonnet",
        claude_permission_mode="default",
        projects_root="/tmp",
    )
    sc = SessionConfig(bare=True, session_name="test-session")
    bridge = ClaudeBridge(project_path="/tmp/p", config=cfg, session_config=sc)
    assert bridge._bare is True
    assert bridge._session_name == "test-session"
