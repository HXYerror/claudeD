"""Session configuration dataclass — replaces 25+ kwargs threading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionConfig:
    """All optional session parameters bundled into one object."""

    system_prompt: str | None = None
    model_override: str | None = None
    permission_mode_override: str | None = None
    resume_session_id: str | None = None
    effort: str | None = field(default_factory=lambda: os.environ.get("CLAUDED_DEFAULT_EFFORT", "max"))
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None
    fork_session: bool = False
    add_dirs: list[str] | None = None
    from_pr: str | None = None
    worktree: str | None = None
    agent_name: str | None = None
    custom_agents: dict | None = None
    mcp_servers: dict | None = None
    max_turns: int | None = None
    fallback_model: str | None = None
    plugin_dirs: list[str] | None = None
    settings: str | None = None
    env: dict[str, str] | None = None
    user: str | None = None
    bare: bool = False
    session_name: str | None = None
    on_ask_user: Any = None  # callback
    on_pre_tool_use: Any = None  # callback
    on_post_tool_use: Any = None  # callback
    on_stop: Any = None  # callback
    on_subagent_stop: Any = None  # callback: fired when a subagent stops
    on_subagent_start: Any = None  # callback: fired when a subagent starts (#310 R2: per-subagent pending tracking)


__all__ = ["SessionConfig"]
